"""astrbot_plugin_seer_dsh — 把赛尔号问答外包给 DSH agent

工作方式：
    群消息 → 判断「是不是 @ 了 bot + 赛尔问题」
      → 是：拦下来，转给 DSH（它会自己跑 seer.py 查库、算数、验算）
      → 否：什么都不做，交给 AstrBot 原本的流程

    另有 /赛尔 <问题> 强制走 DSH（关键词漏判时的兜底）。

为什么这么做：
    AstrBot 的工具是「给了模型，但模型不一定会调」；
    DSH 的 agent loop 会自己查、自己发现错了再改。

注意：
    · DSH 实例用独立的 DSH_HOME（默认 G:\\dsh-seer），不带任何 prompt 注入插件
    · 每个 chat 一个 sessionId，跨消息保留上下文
    · DSH 挂了/超时 → 回一句可读的提示，不阻塞 AstrBot
"""
from __future__ import annotations

import asyncio
import os
import time
import re
import sqlite3
from typing import Any, Optional

from astrbot.api import AstrBotConfig, llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .dsh_client import DshClient

PLUGIN_NAME = "astrbot_plugin_seer_dsh"

# 通用赛尔词（不含精灵名，精灵名从库动态加载）
GENERIC_KEYWORDS = (
    "赛尔", "魂印", "技能", "属性", "克制", "伤害", "连击", "先制",
    "本系", "专属", "特性", "宝石", "刻印", "第五", "巅峰", "对战",
    "精灵王", "种族值", "威力", "进化", "配招", "刷法", "魂印",
)

# 精灵名里常见后缀，剥掉后剩下的片段也当关键词（「灵巢之主」→「灵巢」）
NAME_SUFFIXES = (
    "之主", "之王", "之神", "之魂", "之灵", "之影", "之刃", "之翼", "之瞳",
    "之印", "之息", "之愿", "大帝", "帝君", "星君", "皇帝", "女王", "王子",
    "公主", "守护者",
)


def _find_data_dir() -> str:
    """找 plugin_data（seerapi.sqlite / kb.sqlite 所在）。"""
    cands = []
    try:
        cands.append(get_astrbot_plugin_data_path())
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    cands += [
        os.path.join(here, "..", "..", "plugin_data"),
        os.path.join(here, "..", "..", "..", "plugin_data"),
        r"G:\temp\instances\eb74352a-db17-4359-aade-ba98505d2e47\core\data\plugin_data",
    ]
    for c in cands:
        if c and os.path.isdir(os.path.join(c, "astrbot_plugin_seer_info")):
            return os.path.abspath(c)
    return os.path.abspath(cands[0]) if cands else ""


REPHRASE_PROMPT = """你是欧鲸鲸，在 QQ 群里回答赛尔号问题。下面是一段已经查库得到的技术结论，把它用你自己的话说出来。

硬要求：
1. 数值、精灵名、刻印名、结论——一律照抄，一个字都不许改，不许四舍五入，不许加「大约」「左右」。
2. 不要提「上面」「以下」「根据查询」「据我了解」「参考资料」。
3. 不要复述用户在问什么，直接给答案。
4. 长度跟原文相当，不要扩写，不要加客套，不要加「希望对你有帮助」。
5. 不要说自己是 AI 或任何模型，你就是欧鲸鲸。
6. 不要用 Markdown 表格和标题，QQ 不渲染；用「·」或「-」做列表。
7. 如果原文里有你判断不了的东西，就原样保留，不要自己补。

技术结论：
"""


@register(
    PLUGIN_NAME,
    "大肥鱼",
    "赛尔号问答外包给 DSH agent：@bot 问赛尔问题 → DSH 自主查库推理",
    "0.1.0",
)


class SeerDshPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.cfg = config or {}
        self.client: Optional[DshClient] = None
        self._init_lock = asyncio.Lock()
        self._pool: set[str] = set()
        # 追问窗口：chat_key → 上次 DSH 答完的时间戳。
        # 窗口内的 @ 消息即使不含精灵名/关键词也交给 DSH，
        # 因为 DSH 那边是同一个 session，能接着上文回答「那速度呢」。
        self._last_answer: dict[str, float] = {}
        self._seer_db: str = ""
        self._build_pool()

        # 预热：插件加载时就把 DSH 进程拉起来，别让第一条消息承担 2~4 秒冷启动
        if bool(self._c("prewarm", True)):
            try:
                asyncio.get_running_loop().create_task(self._prewarm())
            except Exception:
                pass

    # ── 配置 ────────────────────────────────────────────────────
    def _c(self, key: str, default: Any = None) -> Any:
        try:
            return self.cfg.get(key, default)
        except Exception:
            return default

    # ── 词表构建（插件加载时一次） ──────────────────────────────
    def _build_pool(self) -> None:
        data_dir = self.cfg.get("data_dir") if hasattr(self.cfg, "get") else None
        data_dir = str(data_dir or "") or _find_data_dir()
        seer = os.path.join(data_dir, "astrbot_plugin_seer_info", "seerapi.sqlite")
        kb = os.path.join(data_dir, "astrbot_plugin_seer_expert", "kb.sqlite")
        self._kb_path = kb if os.path.isfile(kb) else ""
        self._seer_db = seer

        pool: set[str] = set(GENERIC_KEYWORDS)
        try:
            if os.path.isfile(seer):
                con = sqlite3.connect(f"file:{seer}?mode=ro", uri=True)
                try:
                    pool |= {r[0] for r in con.execute(
                        "select name from element_type where length(name)>=2")}
                    for (name,) in con.execute("select name from pet where length(name)>=2"):
                        pool.add(name)
                        # 按 · 切，再剥后缀
                        for part in re.split(r"[·・]", name):
                            part = part.strip()
                            if len(part) >= 2:
                                pool.add(part)
                            for suf in NAME_SUFFIXES:
                                if part.endswith(suf) and len(part) - len(suf) >= 2:
                                    pool.add(part[: -len(suf)])
                finally:
                    con.close()
            if os.path.isfile(kb):
                con = sqlite3.connect(f"file:{kb}?mode=ro", uri=True)
                try:
                    pool |= {r[0] for r in con.execute(
                        "select alias from pet_alias where length(alias)>=2")}
                finally:
                    con.close()
        except Exception as e:
            logger.warning("[seer_dsh] 词表构建部分失败: %s", e)

        # 用户自定义关键词追加
        raw = str(self._c("keywords", "") or "").strip()
        if raw:
            pool |= {x.strip() for x in re.split(r"[,，\n]", raw) if x.strip()}

        self._pool = pool
        logger.info("[seer_dsh] 词表就绪: %d 条（seerapi=%s）", len(pool), os.path.isfile(seer))

    def _recall_facts(self, text: str, limit: int = 5) -> list[tuple[str, str]]:
        """按群友原话召回已确认规则，附在问题里一起交给 DSH。

        为什么需要：DSH 是独立进程、没有注入插件，只能自己调 seer.py facts。
        而「自己查」有两个毛病——查不全、或者查到了只读第一条
        （2026-11-03 冰王漏傲视×2 就是这么来的）。
        bot 侧靠 facts_for_chat 把 5 条规则直接推进上下文，DSH 缺这一层。

        打分口径对齐 db.py 的 facts_for_chat：
          · topic 按「·、（）-／空格」切段，段命中问题 → +3
          · 问题里出现的精灵名/俗称，若 topic 里也有 → +4（身份命中优先于词面）
        实测：冰王/战刀掉命斩/帝录打星皇 三题都召回到正确条目。
        """
        kb = getattr(self, "_kb_path", "")
        if not kb or not os.path.isfile(kb):
            return []
        text = str(text or "")[:400]
        if not text:
            return []
        try:
            con = sqlite3.connect(f"file:{kb}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "select topic, content from facts order by updated_at desc"
                ).fetchall()
            finally:
                con.close()
        except Exception:
            return []
        if not rows:
            return []

        # 问题里出现的精灵名/俗称（用已有词表，只遍历一遍）
        pool = getattr(self, "_pool", None) or set()
        pets_in = {w for w in pool if len(w) >= 2 and w in text}

        scored: list[tuple[int, str, str]] = []
        for topic, content in rows:
            t = str(topic or "")
            segs = [x.strip() for x in re.split(r"[·、与和（）()：:\-—\/ ]+", t) if x.strip()]
            hit = 0
            for seg in segs:
                if len(seg) >= 2 and seg in text:
                    hit += 3
            for p in pets_in:
                if p in t:
                    hit += 4
            if hit:
                scored.append((hit, t, content))
        scored.sort(key=lambda x: -x[0])
        return [(t, c) for _, t, c in scored[:limit]]

    def _load_system_prompt(self) -> str:
        """读 system-prompt.md。sdk-minimal 只认 DSH_SYSTEM_PROMPT 环境变量，
        不读任何 md 文件，所以必须由这里读出来再注入。"""
        path = str(self._c("system_prompt_file", "") or "")
        if not path:
            path = os.path.join(str(self._c("dsh_home", r"G:\dsh-seer")), "system-prompt.md")
        try:
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    text = f.read().strip()
                logger.info("[seer_dsh] system prompt 已加载: %d 字 (%s)", len(text), path)
                return text
            logger.warning("[seer_dsh] system prompt 文件不存在: %s", path)
        except Exception as e:
            logger.warning("[seer_dsh] 读 system prompt 失败: %s", e)
        return ""

    # ── 客户端懒启动 ────────────────────────────────────────────
    async def _ensure_client(self) -> DshClient:
        if self.client is not None:
            return self.client
        async with self._init_lock:
            if self.client is not None:
                return self.client
            self.client = DshClient(
                system_prompt=self._load_system_prompt(),
                home=str(self._c("dsh_home", r"G:\dsh-seer")),
                cwd=str(self._c("dsh_cwd", r"G:\dsh workspace")),
                api_key=str(self._c("api_key", "") or "").strip(),
                provider=str(self._c("provider", "deepseek-official")),
                model=str(self._c("model", "deepseek-flash")),
                reasoning_effort=str(self._c("reasoning_effort", "high")),
                profile=str(self._c("dsh_profile", "sdk-minimal")),
                max_tokens=int(self._c("max_tokens", 0) or 0),
            )
            return self.client

    # ── 判定 ────────────────────────────────────────────────────
    def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            self_id = ""
        if not self_id:
            return False
        # 段数组
        try:
            for seg in (event.message_obj.message if event.message_obj else []) or []:
                if getattr(seg, "type", "") == "At":
                    if str(getattr(seg, "qq", "") or "") == self_id:
                        return True
        except Exception:
            pass
        # 原始 CQ 码兜底
        raw = ""
        try:
            rm = event.message_obj.raw_message if event.message_obj else None
            raw = rm if isinstance(rm, str) else ""
        except Exception:
            raw = ""
        if raw and f"[CQ:at,qq={self_id}]" in raw:
            return True
        return False

    def _looks_like_seer(self, text: str) -> tuple[bool, str]:
        """四层判定：词表 → 精灵名反查 → 俗称反查 → 技能名反查。"""
        if not text:
            return False, ""
        hits = [k for k in self._pool if k in text]
        if hits:
            return True, f"pool:{hits[0]}"
        seer = self._seer_db
        if not seer or not os.path.isfile(seer):
            return False, ""
        try:
            con = sqlite3.connect(f"file:{seer}?mode=ro", uri=True)
            try:
                r = con.execute(
                    "select name from skill where length(name)>=3 and instr(?, name) > 0 limit 1",
                    (text,),
                ).fetchone()
                if r:
                    return True, f"skill:{r[0]}"
            finally:
                con.close()
        except Exception:
            pass
        return False, ""

    # ── 主入口：自动接管 ────────────────────────────────────────
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_message(self, event: AstrMessageEvent):
        if not bool(self._c("enable", True)):
            return

        # 工具模式（默认）：不抢答，正常聊天交给聊天模型自己，
        # 它判断需要数据时会主动调 seer_query 工具。
        # 只有把 auto_takeover 打开，才回到「插件直接接管、抢答」的老模式。
        if not bool(self._c("auto_takeover", False)):
            return

        is_group = False
        try:
            is_group = bool(event.get_group_id())
        except Exception:
            pass
        if is_group and not bool(self._c("enable_group", True)):
            return
        if not is_group and not bool(self._c("enable_private", False)):
            return

        if bool(self._c("require_at", True)) and not self._is_at_bot(event):
            return

        text = (event.message_str or "").strip()
        text = re.sub(r"\[CQ:at,[^\]]*\]", "", text).strip()
        if not text:
            return

        if bool(self._c("require_keyword", True)):
            ok, why = self._looks_like_seer(text)
            if not ok:
                # 追问窗口：刚答过、同一个人/同一个群接着问，就放行
                ck = ""
                try:
                    ck = str(event.unified_msg_origin or "")
                except Exception:
                    pass
                win = float(self._c("follow_up_seconds", 180) or 0)
                last = self._last_answer.get(ck, 0.0) if ck else 0.0
                if win > 0 and last and (time.monotonic() - last) <= win:
                    why = f"追问（距上次 {int(time.monotonic() - last)}s）"
                else:
                    return
            logger.info("[seer_dsh] 命中 %s", why)

        if not self._group_allowed(event):
            return

        # 关键：先停掉事件传播，再慢慢算。
        # 如果等到算完才 stop，AstrBot 会认为本 handler 还没交活儿，
        # 照样把消息丢给 LLM（旧插件就是这时候抢答的）。
        try:
            event.stop_event()
        except Exception:
            pass

        # 提示必须直接发，不能 yield：
        # 上面已经 stop_event()，框架在收到第一个 yield（发完消息）后会看到 is_stopped=True，
        # 就不再把这个 generator 驱动第二遍 —— 于是 DSH 永远没人调用（20:13 那次就死在这）。
        if bool(self._c("notify_waiting", True)):
            hint = str(self._c("waiting_hint", "") or "（查库中，稍等）")
            try:
                await event.send(MessageChain().message(hint))
            except Exception as e:
                logger.warning("[seer_dsh] 发提示失败（忽略）: %s", e)

        # generator 只 yield 一次：最终答案
        async for r in self._answer(event, text):
            yield r

    # ── 手动兜底：/赛尔 <问题> ─────────────────────────────────
    # 2026-11-03：去掉 seer 这个别名。它太容易误触（群友随手打一句带 seer 的话
    # 就把整段丢给 DSH），当初改名叫「强制dsh」就是为了防这个。
    @filter.command(
        "强制dsh",
        alias={"强制走dsh", "问赛尔", "问dsh"},
    )
    async def cmd_seer(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        # 命令前缀可能带斜杠也可能不带（AstrBot 两种都能触发），都要剥掉，
        # 否则「强制dsh xxx」会把前缀原样传给 DSH（2026-11-03 在会话记录里看到的）
        for _c in ("强制dsh", "强制走dsh", "问赛尔", "问dsh"):
            for _pre in (_c, "/" + _c, "／" + _c):
                if text.startswith(_pre):
                    text = text[len(_pre):].lstrip()
                    break
            else:
                continue
            break
        text = re.sub(r"\[CQ:at,[^\]]*\]", "", text).strip()
        if not text:
            yield event.plain_result(
                "用法：/强制dsh <问题>\n"
                "别名：/seer、/问赛尔、/问dsh\n"
                "例：/强制dsh 次元打自然超能几倍"
            )
            return
        if not self._group_allowed(event):
            yield event.plain_result("本群不在允许列表里。")
            return

        # 走 DSH 之前先吱一声（提示语在配置 waiting_hint 里改）。
        # 之前只在 on_message（自动接管）路径发，而 /强制dsh 走的是这里，所以一直没提示；
        # 且 auto_takeover=False 时 on_message 直接 return，那条也永远不会响。
        if bool(self._c("notify_waiting", True)):
            hint = str(self._c("waiting_hint", "") or "（查库中，稍等）")
            try:
                await event.send(MessageChain().message(hint))
            except Exception as e:
                logger.warning("[seer_dsh] 发提示失败（忽略）: %s", e)

        async for r in self._answer(event, text):
            yield r

    def _group_allowed(self, event: AstrMessageEvent) -> bool:
        allow = str(self._c("allow_groups", "") or "").strip()
        if not allow:
            return True
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            gid = ""
        if not gid:
            return True
        ok = gid in {x.strip() for x in re.split(r"[,，\s]+", allow) if x.strip()}
        return ok

    # ── 实际执行 ────────────────────────────────────────────────
    async def _prewarm(self) -> None:
        """后台把 DSH 进程拉起来并握手，省掉首条消息的冷启动。"""
        t0 = time.monotonic()
        try:
            client = await self._ensure_client()
            await client.ensure()
            logger.info("[seer_dsh] 预热完成 %.1fs", time.monotonic() - t0)
        except Exception as e:
            logger.warning("[seer_dsh] 预热失败（不影响使用）: %s", e)

    # ── 工具模式：让聊天模型像用工具一样查 DSH ──────────────────

    def _session_of(self, event: AstrMessageEvent) -> str:
        chat_key = str(event.unified_msg_origin or "")
        return "seer-" + re.sub(r"[^A-Za-z0-9_-]", "_", chat_key)[-60:]

    # ★ 2026-11-03 降级：seer_query 不再是 @llm_tool，聊天模型看不到它，
    #   自然不会主动转 DSH 了。实测 DSH 在群内答题上不如 bot 的专用工具 + 注入器
    #   （冰王漏傲视×2、耗时 20s vs 几秒）。
    #   唯一入口是 cmd_seer（@filter.command "强制dsh"，在 L384），
    #   它才是真正把群友原话转给 DSH 的地方；这里保留成内部方法，供调试复用。
    async def seer_query(self, event: AstrMessageEvent, question: str) -> str:
        """赛尔号深度查询：多步推理、配装计算、能力值面板、机制判定。

        本工具会跑一条完整的查证链（查库 → 计算 → 复核），耗时十几秒，别用来问能一眼查到的东西。

        【什么时候先用别的工具】
        下面这些单点事实，优先用对应的专用工具，它们直接查库、几十毫秒就回来：
        · 精灵属性/种族/魂印/技能 → seer_pet
        · 技能详情 → seer_skill
        · 配招 → seer_moveset
        · 魂印原文 → seer_soulmark
        · 刻印数值 → seer_mintmark
        · 属性克制 → seer_type_chart
        · 一次伤害计算 → seer_damage
        · 官方词条 → seer_glossary
        · 巅峰限制池 → seer_pool
        · 机制专栏原文（时点/免死/伤害流程/抗性概率的成文讲解）→ seer_doc
        · 玩家已确认过的规则条目 → seer_rules
        · 术语、异常状态的单条定义 → seer_term / seer_status

        【什么时候改用本工具】
        · 要算完整面板：学习力怎么刷、套装称号目镜叠完是多少、魂印派生之后多少
        · 要配装：该给什么刻印、几角、同系列几个、有没有更划算的替代
        · **对线/对局讨论**：谁先手、谁能秒谁、换血亏不亏、这只能不能扛住那只、
          某个配置能不能稳压某个配置、怎么针对。对线要拿双方完整面板（速度、双防、体力、
          魂印触发后数值）做对比，专用工具查不到这种综合结论，必须走本工具。
        · **细节判定**：结算先后、时点冲突、效果谁先生效、两个效果撞在一起算谁的、
          异常状态覆盖与顶替、免死/护盾/减伤同时存在时怎么算、连击里每段各自触发什么、
          「这个效果算不算触发那个条件」。这类问题没有现成答案，要拿原文逐条对撞才能判。
          注意：查**某条机制的文字说明**用 seer_doc；判断**两条机制撞一起谁赢**用本工具。
        · 要跨多张表综合判断，或者要自己推一遍数（比如按双攻差值算英雄之耀几层加多少）
        · 专用工具答不上来、答案需要现算的时候
        · 群友追问细节，需要接着上文继续算

        【对线问题怎么问】
        对线结论对配置极度敏感，配置猜错结论就反了。所以：

        如果群友只说「A 打 B」没说配置，**先反问一句再算**，别自己假设。
        话可以这么说：「你俩都什么套装？性格和刻印按顶配算吗」。
        问一次就够，对方答了或者明确说「按默认」再算，不要反复确认。

        带全配置的理想问法：
        例：「古龙打玄武，双方晨曦+神话，都固执顶配」
            「南霜能不能先手秒掉无极，都按晨曦神话顶配算」
            「卫岳对线修罗，我是晨曦，对面裁决」

        【要看群友上传的配置图】
        这类需求不是本工具的范围——那是另一个插件的指令：
        让群友发「配置 精灵名」或「配置图 精灵名」，会直接回群里收集的配置截图。
        你只说这一句，不用自己查。

        【判定问题怎么问】
        把撞在一起的两个效果都点名，说清顺序或条件：
        例：「对手先手打过来，我方免死和护盾同时触发，先扣哪个」
            「连击技能每段都触发魂印的减伤吗」
            「被控制了还能触发登场效果吗」

        【怎么用】
        question 写完整，别写「那速度呢」这种只有你知道指代的句子——把精灵名和意图都带上，
        例如「古龙速度学习力刷多少、0浪费刷多少」。同一个群连续追问会接上文。

        Args:
            question(string): 完整的问题。例：「南霜晨曦神话攻体顶配面板多少」「无极圣武英雄之耀五层后多少」「古龙攻击学习力刷多少才0浪费」
        """
        q = (question or "").strip()
        if not q:
            return "（问题为空，没有可查的内容）"

        # ★ 只用「群友原话」，聊天模型的加工版一律丢弃（2026-11-03 第二次踩这个坑）。
        #   第一次：LLM 把问题写成「晨曦套装的奥斯卡（圣灵系，1020体力）…」，
        #          而奥斯卡最新形态是 圣灵/次元（#3425），「圣灵系」是它自己编的，
        #          DSH 只能照算，克制从 ×2.25 变 ×0.5。
        #   第二次：改成「原话 + LLM补充说明（标注仅供参考）」后，DSH 仍然被补充说明带偏。
        #   → 结论：只要让 LLM 参与转述，它就会把自己的假设写进去。彻底不用它。
        #   追问场景也不受影响：同一个群共用同一个 session，DSH 自己有上文。
        raw_text = (event.message_str or "").strip() if event is not None else ""
        raw_text = re.sub(r"\[CQ:at,[^\]]*\]", "", raw_text).strip()
        for _c in ("强制dsh", "强制走dsh", "问赛尔", "问dsh"):
            for _pre in (_c, "/" + _c, "／" + _c):
                if raw_text.startswith(_pre):
                    raw_text = raw_text[len(_pre):].lstrip()
                    break
            else:
                continue
            break

        if raw_text:
            q = raw_text
            # LLM 那份只进日志供事后审计，绝不传给 DSH
            if question and (question or "").strip() != raw_text:
                logger.info("[seer_dsh] 忽略聊天模型加工版（仅审计）：%s", (question or "")[:120])
        # 原话为空（纯 @ 无正文）才退回聊天模型的加工版

        try:
            client = await self._ensure_client()
        except Exception as e:
            logger.warning("[seer_dsh] 工具模式启动 DSH 失败: %s", e)
            return f"（赛尔号数据库暂时不可用：{e}）"

        # ★ 把召回到的规则附在问题后面（等价于 bot 侧的「赛尔注入」）。
        #   DSH 拿到的是「这句话相关的 5 条规则已确认结论」，不用自己再去碰运气检索。
        try:
            recalled = self._recall_facts(q, limit=int(self._c("inject_facts", 5) or 5))
        except Exception:
            recalled = []
        if recalled:
            _budget = int(self._c("inject_chars", 4000) or 4000)
            _used = 0
            _lines = []
            for _t, _c in recalled:
                _room = _budget - _used
                if _room <= 80:
                    break
                _cap = min(len(_c), _room - 20)
                _body = _c[:_cap] + ("…" if len(_c) > _cap else "")
                _lines.append(f"- 【{_t}】{_body}")
                _used += len(_body) + len(_t) + 6
            if _lines:
                q = (
                    q
                    + chr(10) + chr(10)
                    + "【已召回的玩家确认规则（这些都是实测结论，直接引用，不必再查）】"
                    + chr(10)
                    + chr(10).join(_lines)
                    + chr(10)
                    + "注：若某条规则与本题无关，忽略即可；若与本题相关，必须按它算，不要另立算法。"
                )
                logger.info(
                    "[seer_dsh] 注入 %d 条规则（%d 字）命中=%s",
                    len(_lines), _used,
                    "｜".join(t[:14] for t, _ in recalled[:3]),
                )

        timeout = float(self._c("timeout_seconds", 150) or 150)
        sid = self._session_of(event)
        t0 = time.monotonic()
        answer = await client.ask(sid, q, timeout=timeout)
        logger.info("[seer_dsh] 工具查询 %.1fs ｜ 实际传给DSH=%s", time.monotonic() - t0, q[:150])
        if answer.startswith("\u26a0"):
            return f"（查库失败：{answer.lstrip(chr(0x26a0) + ' ')}）"
        # 交给聊天模型总结，这里只保证数据完整
        return answer

    async def _rephrase(self, event: AstrMessageEvent, raw: str) -> str:
        """让聊天模型把 DSH 的技术结论复述成群聊口吻。

        为什么要这一层：DSH 是独立进程，它说的话在 AstrBot 的历史里是「别人说的」，
        而且 DSH 容易自称模型名。让本机聊天模型复述一遍，身份和口吻就都统一了。
        """
        if not bool(self._c("rephrase", True)):
            return raw
        if not raw or len(raw) < 4:
            return raw
        # 快路径：短答案、且不含任何 Markdown 痕迹 —— 原样发即可，省一次 LLM 调用
        if len(raw) <= 80 and not any(ch in raw for ch in ("**", "#", "|", "```")):
            return raw
        try:
            umo = ""
            try:
                umo = event.unified_msg_origin
            except Exception:
                pass
            prov = self.context.get_using_provider(umo) if umo else self.context.get_using_provider()
            if not prov:
                logger.warning("[seer_dsh] 没有可用 provider，跳过复述")
                return raw
            pid = prov.meta().id
            tpl = str(self._c("rephrase_prompt", "") or REPHRASE_PROMPT)
            resp = await self.context.llm_generate(
                chat_provider_id=pid,
                prompt=raw,
                system_prompt=tpl,
                max_tokens=max(256, min(1200, len(raw) * 2)),
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            if not text:
                return raw
            logger.info("[seer_dsh] 复述完成 %d → %d 字", len(raw), len(text))
            return text
        except Exception as e:
            logger.warning("[seer_dsh] 复述失败，回落原文: %s", e)
            return raw

    async def _answer(self, event: AstrMessageEvent, text: str):
        client = await self._ensure_client()
        timeout = float(self._c("timeout_seconds", 150) or 150)

        chat_key = str(event.unified_msg_origin or "")
        session_id = "seer-" + re.sub(r"[^A-Za-z0-9_-]", "_", chat_key)[-60:]

        logger.info("[seer_dsh] 接管 session=%s len=%d", session_id, len(text))
        t0 = time.monotonic()
        answer = await client.ask(session_id, text, timeout=timeout)
        t1 = time.monotonic()
        raw = self._format(answer)

        # DSH 报错就直接发，别再花时间复述
        if raw.startswith("\u26a0"):
            logger.info("[seer_dsh] 耗时 DSH=%.1fs（出错，跳过复述）", t1 - t0)
            try:
                self._last_answer[str(event.unified_msg_origin or "")] = time.monotonic()
            except Exception:
                pass
            yield event.plain_result(raw)
            return

        # 让聊天模型复述一遍：身份和口吻统一，历史里也认得出是「自己」说的
        final = await self._rephrase(event, raw)
        t2 = time.monotonic()
        logger.info("[seer_dsh] 耗时 DSH=%.1fs 复述=%.1fs 合计=%.1fs",
                    t1 - t0, t2 - t1, t2 - t0)
        # 记下答完的时刻，供追问窗口判断
        try:
            self._last_answer[str(event.unified_msg_origin or "")] = time.monotonic()
        except Exception:
            pass
        yield event.plain_result(self._format(final))

    def _format(self, text: str) -> str:
        """QQ 不吃 Markdown：去掉最常见的几种标记。"""
        t = text.replace("**", "").replace("__", "").replace("`", "")
        t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
        t = re.sub(r"^\s*[-*]\s+", "· ", t, flags=re.M)
        return t.strip()

    # ── 生命周期 ────────────────────────────────────────────────
    async def terminate(self):
        if self.client is not None:
            try:
                await self.client.close()
            except Exception as e:
                logger.warning("[seer_dsh] 关闭客户端失败: %s", e)
            self.client = None
