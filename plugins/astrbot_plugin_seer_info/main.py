import asyncio
import re
from pathlib import Path
from functools import partial

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.custom_filter import CustomFilter

from .config import PluginConfig
from .core import (
    AliasHandler,
    MilkDragonHandler,
    UploaderStore,
    WhitelistHandler,
    AttributeHandler,
    EffectHandler,
    EquipHandler,
    MintmarkHandler,
    MiscHandler,
    PetConfigHandler,
    PetHandler,
    TitleHandler,
    close_renderer,
    configure_render_cache,
    get_renderer,
    render_cache_stats,
)
from .core.ai_fallback import (
    DEFAULT_DENIAL_HINT,
    build_capability_prompt,
    build_messages,
    has_seer_keyword,
    looks_like_capability_denial,
    mentions_other_game,
    parse_intent_json,
    should_ask_ai,
)
from .core.command_parser import (
    INTENT_BY_KEY,
    bare_name_candidates,
    is_known_command,
    looks_like_bare_name,
    parse_query,
)
from .data import (
    BattleEffectDataGetter,
    EquipDataGetter,
    GemDataGetter,
    MintmarkDataGetter,
    PetDataGetter,
    PetSkinDataGetter,
    SuitDataGetter,
    TitleDataGetter,
    cancel_sync_tasks,
    db_manager,
    register_custom_aliases,
    register_database,
    register_local_database,
)
from .core.custom_alias import parse_alias_config
from .service import close_shared_session

# 裸名自动识别：拿名字去这些表里碰，命中就推断出指令。
# 顺序即优先级（精确同名优先，其次按这个顺序取第一个有结果的）。
_AUTO_GETTERS = (
    ("suit", SuitDataGetter),
    ("equip", EquipDataGetter),
    ("pet_image", PetSkinDataGetter),
    ("pet_info", PetDataGetter),
    ("mintmark", MintmarkDataGetter),
    ("gem", GemDataGetter),
    ("title", TitleDataGetter),
    ("battle_effect", BattleEffectDataGetter),
)

_NAME_SEP_RE = re.compile(r"[\s·・•‧∙⋅。—–\-_/]")


def _cfg_flag(value, default: bool) -> bool:
    """旧配置文件里没有新字段时回退到默认值。"""
    return default if value is None else bool(value)


DISABLE_PRIVATE_QUERY = True   # 私聊里禁用老插件的查询类命令（深挖交给大模型）


def _is_private_chat(event: AstrMessageEvent) -> bool:
    try:
        return not (event.get_group_id() or "")
    except Exception:
        return False


def _private_query_blocked(event: AstrMessageEvent) -> bool:
    return DISABLE_PRIVATE_QUERY and _is_private_chat(event)


def _dive_focus_active(event: AstrMessageEvent) -> bool:
    """深挖专注模式期间，本插件主动让路，把问题交给大模型。"""
    try:
        import json
        import os
        import time
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        path = os.path.join(
            get_astrbot_plugin_data_path(), "astrbot_plugin_seer_expert", "focus.json"
        )
        if not os.path.exists(path):
            return False
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        return float(data.get(umo) or 0) > time.time()
    except Exception:
        return False


def _event_text(event: AstrMessageEvent) -> str:
    """取消息正文，兼容不同 AstrBot 版本的消息串接口。"""
    getter = getattr(event, "get_message_str", None)
    if callable(getter):
        return getter() or ""
    return getattr(event, "message_str", "") or ""


def _event_is_woken(event: AstrMessageEvent) -> bool:
    flag = getattr(event, "is_at_or_wake_command", None)
    if flag is None:
        flag = getattr(event, "is_wake", False)
    return bool(flag)


def _platform_says_mentioned_me(event: AstrMessageEvent) -> bool | None:
    """平台原始消息里有没有「@ 的是机器人」。

    QQ 官方适配器会把第一个 mention 当成机器人（``self_id = mention.id``），
    所以「@ 群友」时 ``At.qq`` 恰好等于 ``self_id``，靠 id 比较分辨不出来。
    这里直接看原始消息里的 ``is_you`` 标记，返回 None 表示平台没给这个信息。
    """
    try:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        mentions = getattr(raw, "mentions", None)
        if mentions is None:
            return None
        return any(bool(getattr(item, "is_you", False)) for item in mentions)
    except Exception:
        return None


_MENTION_MARKUP_RE = re.compile(r"<@!?[0-9A-Za-z_-]{4,}>")
_PLACEHOLDER_SELF_IDS = {"", "qq_official", "unknown_selfid"}


def _mentions_other(event: AstrMessageEvent) -> bool:
    """消息是不是 @ 了别人（不含 @机器人 / @全体）。

    只拦「真的 @ 了别人」这一种：

    * QQ 官方消息里 @ 别人会留下 ``<@OPENID>`` 文本标记；
    * 适配器在「没真的 @ 机器人」时会塞一个占位 At（qq=``qq_official``），
      这种**不能**当成 @ 别人，否则普通闲聊都会被忽略，机器人就成死鱼了。

    判断顺序：私聊不管 → 平台明确说 @ 了我就不算 → 文本里有 @ 标记就算 → 再看 At 组件的 id。
    """
    try:
        if event.is_private_chat():
            return False
    except Exception:
        pass

    text = _event_text(event)
    has_markup = bool(_MENTION_MARKUP_RE.search(text))

    mentioned_me = _platform_says_mentioned_me(event)
    if mentioned_me is True:
        return False
    if has_markup:
        return True

    try:
        messages = event.get_messages() or []
    except Exception:
        return False
    ats = []
    for comp in messages:
        qq = getattr(comp, "qq", None)
        if qq is None or str(qq) == "all":
            continue
        ats.append(str(qq))
    if not ats:
        return False

    try:
        self_id = str(event.get_self_id())
    except Exception:
        self_id = ""
    others = [
        qq for qq in ats if qq != self_id and qq not in _PLACEHOLDER_SELF_IDS
    ]
    if others:
        return True
    return False


def _completion_text(resp) -> str | None:
    """从 LLMResponse 里取纯文本。"""
    if resp is None:
        return None
    text = getattr(resp, "completion_text", None)
    if not isinstance(text, str) or not text.strip():
        text = getattr(resp, "text", None)
    if not isinstance(text, str) or not text.strip():
        return None
    return text.strip()


_seen_hook = None


def _record_seen_from_filter(event) -> None:
    if _seen_hook is not None:
        try:
            _seen_hook(event)
        except Exception:
            pass


class TolerantCommandFilter(CustomFilter):
    """宽松指令过滤器。

    AstrBot 的 CommandFilter 要求消息严格以「指令名 + 空格」开头，
    这条过滤器只接管它匹配不到的消息：

        被唤醒(@ / 唤醒前缀 / 私聊) 且
        不是标准指令 且
        宽松解析能识别出指令

    三个条件同时成立才放行，所以不会抢走原指令，也不会在闲聊里乱回复。
    """

    # 由插件初始化时按配置写入
    enabled: bool = True
    allow_bare: bool = False
    auto_dispatch: bool = False

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        # 顺手记录发送者（用于白名单候选），不影响放行判断
        _record_seen_from_filter(event)
        if not self.enabled:
            return False
        if _mentions_other(event):
            # @ 了别人（且没 @ 机器人）：这大概率是在跟群友说话
            return False
        try:
            if not _event_is_woken(event):
                return False
            return _tolerant_accepts(_event_text(event))
        except Exception as e:  # 解析异常不应该以报错形式打扰用户
            logger.debug(f"宽松指令解析失败: {e}")
            return False


class AiFallbackFilter(CustomFilter):
    """AI 兜底过滤器。

    为什么不能只挂 ``@filter.event_message_type(ALL)``：
    ``EventMessageTypeFilter(ALL)`` 对任何消息都返回 True，而 waking_check 里
    「只要有一个过滤器通过，就把事件标记为已唤醒并交给插件」。
    单独挂 ALL 会让机器人在群里对每一条消息都醒过来、并且消费掉事件。
    这里再叠一层自定义过滤器，只有

        被唤醒(@ / 唤醒前缀 / 私聊) 且
        标准指令、宽松指令都识别不了 且
        消息像在问赛尔号数据（或配置成 always）

    才真正放行，其余消息原样交给后续的聊天流程。
    """

    # 由插件初始化时按配置写入
    mode: str = "keyword"
    allow_bare: bool = False
    max_length: int = 120

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        try:
            if _dive_focus_active(event):
                return False      # 深挖专注中，让大模型完整处理
            if not _event_is_woken(event):
                return False
            if _mentions_other(event):
                return False
            text = _event_text(event)
            # 宽松层/裸名层能接的，让给它们，避免同一条消息被两个 handler 处理
            if _tolerant_accepts(text):
                return False
            return should_ask_ai(
                text,
                mode=self.mode,
                allow_bare=self.allow_bare,
                max_length=self.max_length,
            )
        except Exception as e:
            logger.debug(f"AI 兜底判断异常: {e}")
            return False


def _name_key(text: str) -> str:
    """名字归一化：去掉空格和常用分隔符，用于「精确同名」判断。"""
    return _NAME_SEP_RE.sub("", (text or "").lower())


def _tolerant_accepts(text: str) -> bool:
    """宽松层是否要接管这条消息。

    两种情况：规则能解析（`精灵雷伊`），或者整条消息看着像个名字且
    允许裸名自动识别（`影翼裁决`）——后者只是放行，真正确认交给数据库。
    """
    if not text or not text.strip():
        return False
    if is_known_command(text):
        return False
    if mentions_other_game(text):
        return False
    if parse_query(text, allow_bare=TolerantCommandFilter.allow_bare) is not None:
        return True
    return TolerantCommandFilter.auto_dispatch and looks_like_bare_name(text)


class SeerInfoPlugin(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self._ctx = context
        self.cfg = PluginConfig(config, context)
        self._is_local_render = self.cfg.render_mode == "local"
        self._setup_command_layers()
        self._setup_databases()
        self._setup_custom_aliases()
        self._setup_render_cache()
        self._init_handlers()
        if self._is_local_render:
            asyncio.create_task(self._prewarm_renderer())

    def _setup_command_layers(self):
        """按配置初始化三层指令识别，以及聊天时的能力说明。"""
        TolerantCommandFilter.enabled = _cfg_flag(self.cfg.fuzzy_command, True)
        TolerantCommandFilter.allow_bare = _cfg_flag(self.cfg.bare_name_search, False)
        TolerantCommandFilter.auto_dispatch = _cfg_flag(
            self.cfg.auto_dispatch, True
        )
        if TolerantCommandFilter.enabled:
            logger.info("已开启宽松指令识别（精灵雷伊 / 雷伊精灵 / 查一下精灵 雷伊 等）")
        if TolerantCommandFilter.auto_dispatch:
            logger.info("已开启裸名自动识别（直接发「影翼裁决」「漫游者-AH」也能查）")

        mode = str(self.cfg.ai_fallback or "keyword").strip().lower()
        if mode not in ("off", "keyword", "always"):
            logger.warning(f"未知的 ai_fallback 配置值 {mode!r}，已回退为 keyword")
            mode = "keyword"
        max_length = self.cfg.ai_fallback_max_length
        AiFallbackFilter.mode = mode
        AiFallbackFilter.allow_bare = TolerantCommandFilter.allow_bare
        AiFallbackFilter.max_length = (
            max_length if isinstance(max_length, int) and max_length > 0 else 120
        )
        self._ai_provider_id = str(self.cfg.ai_fallback_provider_id or "").strip()
        if mode != "off":
            logger.info(f"已开启 AI 兜底识别（模式: {mode}）")

        self._llm_awareness = _cfg_flag(self.cfg.llm_awareness, True)
        custom_prompt = str(self.cfg.llm_capability_prompt or "").strip()
        self._capability_prompt = custom_prompt or build_capability_prompt()
        self._inject_count = 0
        self._last_inject_len = 0
        denial_hint = str(self.cfg.llm_denial_hint or "").strip()
        self._denial_hint = denial_hint or DEFAULT_DENIAL_HINT
        self._denial_patch_count = 0
        self._auto_hit_count = 0
        if self._llm_awareness:
            logger.info("已开启能力说明注入（聊天时模型知道自己能查赛尔号数据）")

    def _status_line(self) -> str:
        """给 /帮助 用的一行自检信息，方便确认各层是否真的生效。"""
        fuzzy = "开" if TolerantCommandFilter.enabled else "关"
        auto = "开" if TolerantCommandFilter.auto_dispatch else "关"
        ai_mode = AiFallbackFilter.mode
        aware = "开" if self._llm_awareness else "关"
        hits, misses, count, total = render_cache_stats()
        return (
            f"🔧 自检：宽松指令 {fuzzy} / 裸名识别 {auto} / AI 兜底 {ai_mode} / 能力注入 {aware}"
            f"（已注入 {self._inject_count} 次，最近长度 {self._last_inject_len}"
            f"，兜底修正 {self._denial_patch_count} 次，裸名命中 {self._auto_hit_count} 次）"
            f"\n🎨 渲染缓存：命中 {hits} / 未命中 {misses}（{count} 个文件，"
            f"{total / 1024 / 1024:.1f} MB）"
        )

    def _setup_custom_aliases(self):
        """把配置里的自定义外号写进内存别名库（同步覆盖后会自动补回）。"""
        mapping = parse_alias_config(str(self.cfg.pet_aliases or ""))
        if not mapping:
            return
        applied = register_custom_aliases(mapping)
        logger.info(f"自定义外号：配置 {len(mapping)} 条，当前生效 {applied} 条")

    def _setup_render_cache(self):
        """渲染结果缓存（同一张图重复查询直接复用）。"""
        enabled = self.cfg.render_cache
        if enabled is None:
            enabled = True
        max_mb = self.cfg.render_cache_max_size_mb
        if max_mb is None:
            max_mb = 300
        configure_render_cache(bool(enabled), int(max_mb))

    def _auto_lookup(self, text: str) -> tuple[str, str] | None:
        """裸名自动识别：把名字丢到各数据表里查，命中就推断出指令。

        依次尝试「原样」和「去掉修饰词」两种候选：这样 `查一下漫游者`
        能查到 `漫游者`，而本身以查/看开头的名字（如 `查尔顿`）也不会被吃坏。
        查不到返回 None（这条消息就不管，交给后续聊天流程）。
        """
        if not TolerantCommandFilter.auto_dispatch:
            return None
        if not db_manager.is_database_loaded("seerapi"):
            return None
        for candidate in bare_name_candidates(text):
            hit = self._lookup_name(candidate)
            if hit is not None:
                return hit[0], candidate
        return None

    def _lookup_name(self, name: str) -> tuple[str, str] | None:
        """在 `_AUTO_GETTERS` 里找这个名字——**只认精确同名**。

        以前「有结果就算命中」，而查名会走拼音索引：群里有人说「肥鱼」，
        拼音 feiyu 能命中「费约克 / 绯云 / 绯玉」一串精灵，于是随机闲聊也被
        当成裸名查询，还会弹出选择菜单把后面所有人的发言都吃掉。
        所以这里改成：名字（或去修饰词后的名字）必须和库里某项完全一致才认。
        想按拼音/模糊查，用显式指令「精灵 肥鱼」即可。
        """
        sessions = db_manager.get_all_sessions()
        key_name = _name_key(name)
        if not key_name:
            return None
        for key, getter in _AUTO_GETTERS:
            try:
                rows = getter(sessions, name)
            except Exception as e:
                logger.debug(f"裸名查询 {key} 失败: {e}")
                continue
            for row in rows:
                row_key = _name_key(getattr(row, "name", ""))
                if row_key == key_name:
                    return key, name
                # 「漫游者」→「漫游者-AH」这种：只允许后面接一小段英文/数字后缀，
                # 拼音模糊（肥鱼 → 费约克）不会满足前缀关系，仍然被挡住
                if row_key.startswith(key_name):
                    rest = row_key[len(key_name) :]
                    if rest and len(rest) <= 8 and rest.isascii():
                        return key, name
        return None

    async def _ask_llm(
        self,
        event: AstrMessageEvent,
        prompt: str,
        system_prompt: str,
    ) -> str | None:
        """调用一次大模型，返回纯文本；任何失败都返回 None。"""
        ctx = self._ctx
        umo = getattr(event, "unified_msg_origin", None) or ""
        generate = getattr(ctx, "llm_generate", None)

        provider_id = self._ai_provider_id
        if not provider_id and callable(generate):
            resolver = getattr(ctx, "get_current_chat_provider_id", None)
            if callable(resolver):
                try:
                    provider_id = await resolver(umo)
                except Exception as e:
                    logger.debug(f"AI 兜底获取当前模型失败: {e}")
                    provider_id = ""

        if provider_id and callable(generate):
            try:
                resp = await generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    contexts=[],
                )
            except Exception as e:
                logger.warning(f"AI 兜底调用失败: {e}")
                return None
            return _completion_text(resp)

        # 兼容旧版本 AstrBot：直接使用当前会话的 Provider
        getter = getattr(ctx, "get_using_provider", None)
        if not callable(getter):
            return None
        try:
            provider = getter(umo) if umo else getter()
        except Exception:
            provider = None
        if provider is None:
            logger.debug("AI 兜底：没有可用的对话模型")
            return None
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=system_prompt,
                contexts=[],
            )
        except Exception as e:
            logger.warning(f"AI 兜底调用失败: {e}")
            return None
        return _completion_text(resp)

    async def _classify_intent(self, event: AstrMessageEvent, message: str):
        """让模型把消息解析成 (intent, keyword)；不是查询时返回 None。"""
        system_prompt, prompt = build_messages(message)
        raw = await self._ask_llm(event, prompt, system_prompt)
        if raw is None:
            return None
        result = parse_intent_json(raw)
        if result is not None:
            logger.debug(
                f"AI 兜底识别: {message!r} -> {result.key}/{result.keyword!r}"
            )
        return result


    def _setup_databases(self):
        async def get_fingerprint(url: str, session: aiohttp.ClientSession) -> str:
            async with session.get(url) as resp:
                resp.raise_for_status()
                return (await resp.read()).decode().strip()

        def _register(name: str, sync_url_key: str, fp_url_key: str, interval_key: str):
            sync_url = getattr(self.cfg, sync_url_key, "")
            if sync_url:
                fp_url = getattr(self.cfg, fp_url_key, "")
                fp = partial(get_fingerprint, fp_url) if fp_url else None
                interval = getattr(self.cfg, interval_key, 60)
                register_database(
                    name,
                    sync_url=sync_url,
                    sync_interval_minutes=interval,
                    get_fingerprint=fp,
                )
            else:
                register_local_database(name)

        _register(
            "seerapi",
            "seerapi_sync_url",
            "seerapi_fingerprint_url",
            "seerapi_sync_interval_minutes",
        )
        _register(
            "aliases",
            "alias_sync_url",
            "alias_fingerprint_url",
            "alias_sync_interval_minutes",
        )

    def _init_handlers(self):
        html_render = None if self._is_local_render else self.html_render
        image_format = self.cfg.image_format
        jpeg_quality = self.cfg.jpeg_quality
        self._pet_handler = PetHandler(
            html_render=html_render,
            image_format=image_format,
            jpeg_quality=jpeg_quality,
        )
        self._attr_handler = AttributeHandler(
            html_render=html_render,
            image_format=image_format,
            jpeg_quality=jpeg_quality,
        )
        self._effect_handler = EffectHandler()
        self._mintmark_handler = MintmarkHandler()
        self._equip_handler = EquipHandler()
        self._title_handler = TitleHandler()
        self._misc_handler = MiscHandler()
        self._alias_handler = AliasHandler()
        data_dir = getattr(self.cfg, "data_dir", None)
        uploader_path = Path(data_dir) / "uploaders.json" if data_dir else None
        self._uploader_store = UploaderStore(path=uploader_path)
        self._uploader_store.load()
        self._uploader_store.load_config(str(self.cfg.pet_config_uploaders or ""))
        self._uploader_seen = 0
        self._whitelist_handler = WhitelistHandler(self._uploader_store)
        self._pet_config_handler = PetConfigHandler(
            self.cfg.data_dir, store=self._uploader_store
        )
        self._milk_dragon = MilkDragonHandler(
            data_dir or Path("."), can_upload=self._pet_config_handler.can_upload
        )
        global _seen_hook
        _seen_hook = self._record_seen
        # 宽松指令 -> 具体 handler
        self._command_targets = {
            "pet_info": self._pet_handler.pet_info,
            "pet_image": self._pet_handler.pet_image,
            "pet_config": self._pet_config_handler.query,
            "type_matchup": self._attr_handler.type_matchup,
            "battle_effect": self._effect_handler.battle_effect,
            "mintmark": self._mintmark_handler.mintmark,
            "gem": self._mintmark_handler.gem,
            "suit": self._equip_handler.suit,
            "equip": self._equip_handler.equip,
            "title": self._title_handler.title_info,
            "preview": self._misc_handler.preview_cmd,
            "server": self._misc_handler.server_info_cmd,
            "help": self._misc_handler.help_cmd,
            "alias": self._alias_handler.query,
        }

    async def _prewarm_renderer(self):
        try:
            renderer = await get_renderer()
            await renderer.prewarm()
        except Exception as e:
            logger.warning(f"Playwright 浏览器预热失败: {e}")

    async def terminate(self):
        await cancel_sync_tasks()
        await close_shared_session()
        db_manager.dispose_all()
        if self._is_local_render:
            await close_renderer()
        logger.info("SeerInfo 插件已卸载")

    @filter.command(
        "精灵",
        alias={"查询精灵信息"},
        desc="查询精灵基础信息",
        ignore_prefix=True,
    )
    async def pet_info(self, event: AstrMessageEvent, arg: str = ""):
        if _private_query_blocked(event):
            yield event.plain_result("私聊里已关闭卡片查询，改用 /深挖 精灵名（会给出结构化的属性/魂印/技能）。")
            return
        if _dive_focus_active(event):
            return
        async for result in self._pet_handler.pet_info(event, arg):
            yield result

    @filter.command(
        "立绘",
        alias={"皮肤", "查询立绘"},
        desc="查询精灵或皮肤立绘",
        ignore_prefix=True,
    )
    async def pet_image(self, event: AstrMessageEvent, arg: str = ""):
        if _private_query_blocked(event):
            return
        if _dive_focus_active(event):
            return
        async for result in self._pet_handler.pet_image(event, arg):
            yield result

    # ★ 2026-11-03：加关键词分流。
    #   command_parser 是**后缀匹配**，所以「看看顶配无极的配置」会被这条命令吃掉，
    #   结果是去查群友上传的截图，而用户其实想要「按数据算一张新图」。
    #   含下面这些词时直接 return，把消息让给 LLM 走 seer_config_card（生成图）。
    # ★ 分流：靠「话题词 + 动作词」组合判断，而不是单个关键词。
    #   单靠「配置」会把「看看顶配无极的配置」当成查截图（command_parser 是后缀匹配）；
    #   单靠「生成」又会和 gemini 生图插件抢消息（它也认「生成」「改」「画」）。
    #   组合判断：必须同时出现「配置类话题」和「生成类动作」才让给 LLM。
    _CFG_TOPIC = ("配置", "配装", "面板", "刻印搭配")
    _CFG_ACT = ("顶配", "生成", "算", "计算", "看看", "出一张", "做一张",
                "给我", "改成", "改一下", "拉满", "换一下", "配一下", "新")
    # 明显是「画图/生图」的，一律让给 gemini 那条路，别抢
    _DRAW_WORDS = ("生图", "画一张", "画个", "改图", "切图", "画风", "手办化", "插画")

    @filter.command("配置图", alias={"配置"}, desc="查询精灵配置图", ignore_prefix=True)
    async def pet_config(self, event: AstrMessageEvent, arg: str = ""):
        if _private_query_blocked(event):
            return
        if _dive_focus_active(event):
            return
        _raw = str(getattr(event, "message_str", "") or "")
        if any(w in _raw for w in self._DRAW_WORDS):
            logger.info("[配置图] 含绘画词，让给生图插件：%s", _raw[:40])
            return
        # 注：这里原本还有一段 `if any(w in _raw for w in _PURE_ACT)` 的「纯动作词」
        # 检查，但 _PURE_ACT 从来没定义过 → NameError，导致 /配置图 每次直接 500。
        # 而且下面的「话题+动作」组合判断已经覆盖了这个场景（命令名里就带「配置」），
        # 所以直接删掉，不要补定义。
        _has_topic = any(t in _raw for t in self._CFG_TOPIC)
        _has_act = any(a in _raw for a in self._CFG_ACT)
        if _has_topic and _has_act:
            logger.info("[配置图] 话题+动作命中，让给 LLM 生成：%s", _raw[:40])
            return
        async for result in self._pet_config_handler.query(event, arg):
            yield result

    @filter.command(
        "收录配置图",
        desc="收录精灵配置图（可带 作者:某某 备注:文字）",
        ignore_prefix=True,
    )
    async def pet_config_save(self, event: AstrMessageEvent, arg: str = ""):
        async for result in self._pet_config_handler.save(event, arg):
            yield result

    @filter.command(
        "配置备注",
        desc="修改配置图的作者/备注（管理员/白名单）",
        ignore_prefix=True,
    )
    async def pet_config_remark(self, event: AstrMessageEvent, arg: str = ""):
        async for result in self._pet_config_handler.remark(event, arg):
            yield result

    def _record_seen(self, event: AstrMessageEvent):
        """被动记录群里说过话的人，做白名单候选（不唤醒、不回复）。"""
        try:
            getter = getattr(event, "get_sender_id", None)
            uid = str(getter() or "") if callable(getter) else ""
        except Exception:
            return
        if not uid:
            return
        try:
            name_getter = getattr(event, "get_sender_name", None)
            name = str(name_getter() or "").strip() if callable(name_getter) else ""
        except Exception:
            name = ""
        self._uploader_store.touch(uid, name)
        self._uploader_seen += 1
        if self._uploader_seen % 20 == 0:
            self._uploader_store.save()

    @filter.command(
        "奶龙精灵查询",
        alias={"奶龙查询"},
        desc="查恶搞奶龙图（按序号看图）",
        ignore_prefix=True,
    )
    async def milk_dragon_query(self, event: AstrMessageEvent, arg: str = ""):
        if _private_query_blocked(event):
            return
        async for result in self._milk_dragon.query(event, arg):
            yield result

    @filter.command(
        "上传奶龙图",
        desc="上传奶龙图：图片 + 奶龙名 + 精灵名或ID（管理员/白名单）",
        ignore_prefix=True,
    )
    async def milk_dragon_upload(self, event: AstrMessageEvent, arg: str = ""):
        async for result in self._milk_dragon.upload(event, arg):
            yield result

    @filter.command(
        "白名单",
        alias={"上传白名单"},
        desc="管理配置图收录白名单（按昵称/序号/UID 添加）",
        ignore_prefix=True,
    )
    async def whitelist_cmd(self, event: AstrMessageEvent, arg: str = ""):
        async for result in self._whitelist_handler.command(event, arg):
            yield result

    @filter.command(
        "外号",
        alias={"别名"},
        desc="查询精灵的外号/别称（不带参数看别名库概况）",
        ignore_prefix=True,
    )
    async def alias_query(self, event: AstrMessageEvent, arg: str = ""):
        if _private_query_blocked(event):
            return
        async for result in self._alias_handler.query(event, arg):
            yield result

    @filter.command("属性", alias={"属性表"}, desc="查询属性克制表", ignore_prefix=True)
    async def type_matchup(self, event: AstrMessageEvent, arg: str = ""):
        if _private_query_blocked(event):
            return
        async for result in self._attr_handler.type_matchup(event, arg):
            yield result

    @filter.command(
        "异常",
        alias={"查询异常", "异常状态", "查询异常状态"},
        desc="查询异常状态信息",
        ignore_prefix=True,
    )
    async def battle_effect(self, event: AstrMessageEvent, arg: str = ""):
        if _private_query_blocked(event):
            return
        async for result in self._effect_handler.battle_effect(event, arg):
            yield result

    @filter.command("刻印", desc="查询刻印信息及数值", ignore_prefix=True)
    async def mintmark(self, event: AstrMessageEvent, arg: str = ""):
        if _private_query_blocked(event):
            return
        async for result in self._mintmark_handler.mintmark(event, arg):
            yield result

    @filter.command("宝石", alias={"刻印宝石"}, desc="查询刻印宝石信息", ignore_prefix=True)
    async def gem(self, event: AstrMessageEvent, arg: str = ""):
        if _private_query_blocked(event):
            return
        async for result in self._mintmark_handler.gem(event, arg):
            yield result

    @filter.command("套装", alias={"查询套装信息"}, desc="查询套装信息", ignore_prefix=True)
    async def suit(self, event: AstrMessageEvent, arg: str = ""):
        if _private_query_blocked(event):
            return
        async for result in self._equip_handler.suit(event, arg):
            yield result

    @filter.command("部件", alias={"查询部件信息"}, desc="查询装备部件信息", ignore_prefix=True)
    async def equip(self, event: AstrMessageEvent, arg: str = ""):
        if _private_query_blocked(event):
            return
        async for result in self._equip_handler.equip(event, arg):
            yield result

    @filter.command("称号", alias={"查询称号信息"}, desc="查询称号信息", ignore_prefix=True)
    async def title_info(self, event: AstrMessageEvent, arg: str = ""):
        if _private_query_blocked(event):
            return
        async for result in self._title_handler.title_info(event, arg):
            yield result

    @filter.command("下周预告", alias={"预告"}, desc="获取下周预告图", ignore_prefix=True)
    async def preview_cmd(self, event: AstrMessageEvent):
        if _private_query_blocked(event):
            return
        async for result in self._misc_handler.preview_cmd(event):
            yield result

    @filter.command("开服查询", alias={"开服了吗"}, desc="查询服务器是否已开服", ignore_prefix=True)
    async def server_info_cmd(self, event: AstrMessageEvent):
        if _private_query_blocked(event):
            return
        async for result in self._misc_handler.server_info_cmd(event):
            yield result

    @filter.command("帮助", desc="显示帮助信息", ignore_prefix=True)
    async def help_cmd(self, event: AstrMessageEvent):
        async for result in self._misc_handler.help_cmd(
            event, status=self._status_line()
        ):
            yield result

    @filter.custom_filter(TolerantCommandFilter)
    async def tolerant_query(self, event: AstrMessageEvent):
        if _private_query_blocked(event):
            return
        """宽松指令入口：精灵雷伊 / 雷伊精灵 / 影翼裁决（裸名自动识别）……"""

        message = _event_text(event)
        query = parse_query(
            message,
            allow_bare=TolerantCommandFilter.allow_bare,
        )
        if query is not None:
            key, arg, takes_arg = query.key, query.arg, query.takes_arg
        else:
            auto = self._auto_lookup(message)
            if auto is None:
                return
            key, arg, takes_arg = auto[0], auto[1], True
            self._auto_hit_count += 1
            logger.debug(f"裸名自动识别: {message!r} -> {key}/{arg}")

        target = self._command_targets.get(key)
        if target is None:
            logger.debug(f"宽松指令没有对应 handler: {key}")
            return

        stream = target(event, arg) if takes_arg else target(event)
        async for result in stream:
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.custom_filter(AiFallbackFilter)
    async def ai_fallback(self, event: AstrMessageEvent):
        if _private_query_blocked(event):
            return
        """通用消息监听：规则识别不了时，让 AI 判断意图，再转给对应查询。

        AI 判定「不是赛尔号查询」时直接返回、不产生任何回复，
        事件会继续走 AstrBot 的正常聊天流程，不影响普通对话。
        """

        message = _event_text(event)
        result = await self._classify_intent(event, message)
        if result is None:
            return

        intent = INTENT_BY_KEY.get(result.key)
        target = self._command_targets.get(result.key)
        if intent is None or target is None:
            logger.debug(f"AI 兜底没有对应 handler: {result.key}")
            return

        stream = (
            target(event, result.keyword) if intent.takes_arg else target(event)
        )
        async for item in stream:
            yield item

    @filter.on_llm_request()
    async def inject_capability_prompt(self, event: AstrMessageEvent, req):
        if _mentions_other(event):
            return
        """把插件能力说明拼进系统提示词。

        模型默认不知道插件存在，聊天时会说「我不知道」；注入这段说明后，
        它会引导用户发指令（或直接用自然语言问），也不会编造具体数值。
        """

        if not self._llm_awareness:
            return
        try:
            current = (getattr(req, "system_prompt", "") or "").strip()
            if self._capability_prompt in current:
                return
            req.system_prompt = (
                f"{current}\n\n{self._capability_prompt}"
                if current
                else self._capability_prompt
            )
            self._inject_count += 1
            self._last_inject_len = len(req.system_prompt)
        except Exception as e:  # 注入失败不应该影响正常对话
            logger.debug(f"能力说明注入失败: {e}")

    @filter.on_decorating_result()
    async def patch_capability_denial(self, event: AstrMessageEvent):
        """兜底：模型嘴硬说「查不了」时，在消息发出前补一句引导。

        deepseek-flash 这类小模型面对长人格时经常忽略系统提示里的能力说明，
        所以这里不依赖模型，直接改最终消息。
        """

        # @ 了别人（且没 @ 机器人）的消息，别把模型的话发到群里
        if _mentions_other(event):
            result = event.get_result()
            if result is not None and getattr(result, "chain", None):
                result.chain = []
            return

        if not self._llm_awareness:
            return
        try:
            message = _event_text(event)
            # 「查一下漫游者」这种没关键词、但明显是在查名字的消息也要兜住
            if not has_seer_keyword(message) and not (
                TolerantCommandFilter.auto_dispatch and looks_like_bare_name(message)
            ):
                return
            # 指令已经被插件自己处理过，那些回复不该动
            if is_known_command(message) or parse_query(message) is not None:
                return
            result = event.get_result()
            if result is None or not getattr(result, "chain", None):
                return
            text = "".join(c.text for c in result.chain if isinstance(c, Comp.Plain))
            if not looks_like_capability_denial(text):
                return
            result.chain.append(Comp.Plain(self._denial_hint))
            self._denial_patch_count += 1
            logger.debug(f"已兜底修正否认能力的回复: {text[:40]}")
        except Exception as e:
            logger.debug(f"能力兜底失败: {e}")
