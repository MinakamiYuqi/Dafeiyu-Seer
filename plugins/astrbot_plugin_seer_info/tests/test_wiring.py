"""接线测试：用桩模块把 main.py 真正导入，验证三层识别与分发。

AstrBot 本体不可用，这里用最小的桩替代 astrbot.* 和插件内部的重模块，
只验证「过滤器判定 -> handler 分发 -> 参数传递」这条链路。

运行：python tests/test_wiring.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CALLS: list[tuple[str, str, str]] = []


# --------------------------------------------------------------------------- #
# astrbot 桩
# --------------------------------------------------------------------------- #


class _Logger:
    def debug(self, *a, **k):
        pass

    info = warning = error = debug


class CustomFilter:
    def __init__(self, raise_error: bool = True, **kwargs):
        self.raise_error = raise_error

    def filter(self, event, cfg) -> bool:  # pragma: no cover - 由子类覆盖
        raise NotImplementedError


class EventMessageType:
    GROUP_MESSAGE = 1
    PRIVATE_MESSAGE = 2
    OTHER_MESSAGE = 4
    ALL = GROUP_MESSAGE | PRIVATE_MESSAGE | OTHER_MESSAGE


class _FilterApi:
    EventMessageType = EventMessageType

    class PermissionType:
        ADMIN = "admin"
        MEMBER = "member"

    @staticmethod
    def permission_type(*args, **kwargs):
        return lambda func: func

    @staticmethod
    def command(*args, **kwargs):
        return lambda func: func

    @staticmethod
    def custom_filter(*args, **kwargs):
        return lambda func: func

    @staticmethod
    def event_message_type(*args, **kwargs):
        return lambda func: func

    @staticmethod
    def on_llm_request(*args, **kwargs):
        return lambda func: func

    @staticmethod
    def on_decorating_result(*args, **kwargs):
        return lambda func: func


def _module(name: str, **attrs):
    m = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(m, key, value)
    sys.modules[name] = m
    return m


class _Star:
    def __init__(self, context):
        self.context = context
        self.html_render = None


class _AstrMessageEvent:
    pass


class _Plain:
    def __init__(self, text: str = ""):
        self.text = text


_module("astrbot")
_module("astrbot.api", logger=_Logger())
_module("astrbot.api.event", AstrMessageEvent=_AstrMessageEvent, filter=_FilterApi())
_module("astrbot.api.star", Context=object, Star=_Star)
_module("astrbot.api.message_components", Plain=_Plain)
setattr(
    sys.modules["astrbot.api"],
    "message_components",
    sys.modules["astrbot.api.message_components"],
)
_module("astrbot.core")
_module("astrbot.core.star")
_module("astrbot.core.star.filter")
_module(
    "astrbot.core.star.filter.custom_filter",
    CustomFilter=CustomFilter,
)
# main.py 只在类型注解和数据库同步里用到 aiohttp
_module("aiohttp", ClientSession=object)


# --------------------------------------------------------------------------- #
# 插件内部重模块的桩（保留 core/command_parser.py 和 ai_fallback.py 为真实现）
# --------------------------------------------------------------------------- #


def _handler(kind: str, method: str):
    async def gen(self, event, arg: str = "", **kwargs):
        CALLS.append((kind, method, arg))
        yield ("result", kind, method, arg)

    return gen


class _PetHandler:
    def __init__(self, **kwargs):
        pass

    pet_info = _handler("pet", "pet_info")
    pet_image = _handler("pet", "pet_image")


class _PetConfigHandler:
    def __init__(self, data_dir=None, uploaders=None, store=None):
        self.root = data_dir
        self.uploaders = uploaders
        self.store = store

    def can_upload(self, event):
        return True

    query = _handler("pet_config", "query")
    save = _handler("pet_config", "save")


class _AttributeHandler:
    def __init__(self, **kwargs):
        pass

    type_matchup = _handler("attr", "type_matchup")


class _EffectHandler:
    battle_effect = _handler("effect", "battle_effect")


class _MintmarkHandler:
    mintmark = _handler("mintmark", "mintmark")
    gem = _handler("mintmark", "gem")


class _EquipHandler:
    suit = _handler("equip", "suit")
    equip = _handler("equip", "equip")


class _TitleHandler:
    title_info = _handler("title", "title_info")


class _MiscHandler:
    preview_cmd = _handler("misc", "preview_cmd")
    server_info_cmd = _handler("misc", "server_info_cmd")
    help_cmd = _handler("misc", "help_cmd")


class _AliasHandler:
    query = _handler("alias", "query")


class _WhitelistHandler:
    def __init__(self, store):
        self.store = store

    async def command(self, event, arg=""):
        yield ("plain", "stub")


class _MilkDragonHandler:
    def __init__(self, data_dir=None, can_upload=None):
        self.data_dir = data_dir
        self.can_upload = can_upload

    async def query(self, event, arg=""):
        yield ("plain", "stub")

    async def upload(self, event, arg=""):
        yield ("plain", "stub")


class _UploaderStore:
    def __init__(self, path=None):
        self.path = path
        self.members = {"tester": "测试"}

    def load(self):
        return None

    def load_config(self, text):
        return None

    def contains(self, uid):
        return True

    def touch(self, uid, name=""):
        return None

    def save(self):
        return None


class _PluginConfig:
    def __init__(self, data, context):
        object.__setattr__(self, "_data", dict(data or {}))
        object.__setattr__(self, "context", context)

    def __getattr__(self, key):
        if key.startswith("_"):
            raise AttributeError(key)
        return self._data.get(key)


class _DBManager:
    def is_database_loaded(self, name):
        return True

    def get_all_sessions(self):
        return {"seerapi": object()}

    def dispose_all(self):
        pass


async def _noop(*args, **kwargs):
    return None


class _Row:
    def __init__(self, name: str):
        self.name = name


# 裸名自动识别用的假数据表：{名字: {getter_key: [行]}}
FAKE_ROWS: dict[str, dict[str, list]] = {}


class _Getter:
    def __init__(self, key: str):
        self.key = key

    def __call__(self, sessions, arg):
        return tuple(FAKE_ROWS.get(arg, {}).get(self.key, []))


pkg = types.ModuleType("seer_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules["seer_plugin"] = pkg

_module("seer_plugin.config", PluginConfig=_PluginConfig)
_module(
    "seer_plugin.data",
    db_manager=_DBManager(),
    register_database=lambda *a, **k: None,
    register_local_database=lambda *a, **k: None,
    cancel_sync_tasks=_noop,
    register_custom_aliases=lambda *a, **k: 0,
    PetDataGetter=_Getter("pet_info"),
    PetSkinDataGetter=_Getter("pet_image"),
    MintmarkDataGetter=_Getter("mintmark"),
    GemDataGetter=_Getter("gem"),
    SuitDataGetter=_Getter("suit"),
    EquipDataGetter=_Getter("equip"),
    TitleDataGetter=_Getter("title"),
    BattleEffectDataGetter=_Getter("battle_effect"),
)
_module("seer_plugin.service", close_shared_session=_noop)

core_stub = types.ModuleType("seer_plugin.core")
core_stub.__path__ = [str(ROOT / "core")]
for _name, _value in {
    "PetHandler": _PetHandler,
    "PetConfigHandler": _PetConfigHandler,
    "AttributeHandler": _AttributeHandler,
    "EffectHandler": _EffectHandler,
    "MintmarkHandler": _MintmarkHandler,
    "EquipHandler": _EquipHandler,
    "TitleHandler": _TitleHandler,
    "MiscHandler": _MiscHandler,
    "AliasHandler": _AliasHandler,
    "WhitelistHandler": _WhitelistHandler,
    "UploaderStore": _UploaderStore,
    "MilkDragonHandler": _MilkDragonHandler,
    "close_renderer": _noop,
    "get_renderer": _noop,
    "configure_render_cache": lambda *a, **k: None,
    "render_cache_stats": lambda: (0, 0, 0, 0),
}.items():
    setattr(core_stub, _name, _value)
sys.modules["seer_plugin.core"] = core_stub

spec = importlib.util.spec_from_file_location("seer_plugin.main", ROOT / "main.py")
main = importlib.util.module_from_spec(spec)
sys.modules["seer_plugin.main"] = main
spec.loader.exec_module(main)


# --------------------------------------------------------------------------- #
# 事件 / 上下文桩
# --------------------------------------------------------------------------- #


class _At:
    def __init__(self, qq: str):
        self.qq = qq


class _Mention:
    def __init__(self, is_you: bool):
        self.is_you = is_you


class _RawMessage:
    def __init__(self, mentions):
        self.mentions = [_Mention(flag) for flag in mentions]


class _MessageObj:
    def __init__(self, mentions):
        self.raw_message = _RawMessage(mentions) if mentions is not None else object()


class Event:
    def __init__(
        self,
        text: str,
        woken: bool = True,
        at=None,
        mentions=None,
        private: bool = False,
        text_markup: str = "",
    ):
        self.message_str = text
        self.is_at_or_wake_command = woken
        self.unified_msg_origin = "test:GroupMessage:1"
        self.result = None
        self._messages = [_At(qq) for qq in (at or [])]
        self.message_obj = _MessageObj(mentions)
        self.private = private
        if text_markup:
            self.message_str = f"{text_markup} {text}"

    def get_message_str(self):
        return self.message_str

    def get_messages(self):
        return list(self._messages)

    def get_self_id(self):
        return "BOT123"

    def is_private_chat(self):
        return getattr(self, "private", False)

    def get_result(self):
        return self.result


class Context:
    def __init__(self, reply: str | None = None, fail: bool = False):
        self.reply = reply
        self.fail = fail
        self.calls = 0

    async def get_current_chat_provider_id(self, umo):
        return "provider-1"

    async def llm_generate(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")

        class Resp:
            completion_text = self.reply

        return Resp()


CFG = {
    "render_mode": "remote",
    "image_format": "jpeg",
    "jpeg_quality": 85,
    "fuzzy_command": True,
    "bare_name_search": False,
    "ai_fallback": "keyword",
    "ai_fallback_provider_id": "",
    "ai_fallback_max_length": 120,
}


def make_plugin(ctx: Context):
    CALLS.clear()
    return main.SeerInfoPlugin(ctx, dict(CFG))


async def collect(agen):
    return [item async for item in agen]


failures: list[str] = []


def check(cond: bool, msg: str):
    if not cond:
        failures.append(msg)


# --------------------------------------------------------------------------- #
# 1. 严格指令：宽松层与 AI 层都必须让路
# --------------------------------------------------------------------------- #

plugin = make_plugin(Context())
check(
    main.TolerantCommandFilter().filter(Event("精灵 雷伊"), None) is False,
    "严格指令不应被宽松层接管",
)
check(
    main.AiFallbackFilter().filter(Event("精灵 雷伊"), None) is False,
    "严格指令不应被 AI 层接管",
)

# --------------------------------------------------------------------------- #
# 2. 宽松层
# --------------------------------------------------------------------------- #

plugin = make_plugin(Context())
asyncio.run(collect(plugin.tolerant_query(Event("精灵雷伊"))))
check(CALLS == [("pet", "pet_info", "雷伊")], f"宽松层分发错误: {CALLS}")
check(
    main.TolerantCommandFilter().filter(Event("精灵雷伊"), None) is True,
    "「精灵雷伊」应被宽松层接管",
)

# 未被唤醒时谁都不接管
check(
    main.TolerantCommandFilter().filter(Event("精灵雷伊", woken=False), None) is False,
    "未唤醒时宽松层不应接管",
)
check(
    main.AiFallbackFilter().filter(Event("雷伊的技能都有啥", woken=False), None)
    is False,
    "未唤醒时 AI 层不应接管",
)

# 别的游戏不能被套成赛尔号查询
check(
    main.TolerantCommandFilter().filter(Event("原神的立绘"), None) is False,
    "别的游戏的「立绘」不被宽松层接管",
)

# @ 了别人（且没 @ 机器人）时，宽松层和 AI 层都不该插话
check(
    main.TolerantCommandFilter().filter(
        Event("精灵雷伊", woken=True, at=["U_OTHER"]), None
    )
    is False,
    "@别人时宽松层不接管",
)
check(
    main.AiFallbackFilter().filter(
        Event("雷伊的技能都有啥", woken=True, at=["U_OTHER"]), None
    )
    is False,
    "@别人时 AI 层不接管",
)
# QQ 官方适配器把「第一个 mention」当成机器人：@群友 时 At.qq 恰好等于 self_id，
# 只能靠原始消息里的 is_you 分辨
check(
    main._mentions_other(
        Event(
            "在干嘛",
            woken=True,
            at=["BOT123"],
            mentions=[False],
            text_markup="<@494EC5E95CD4F4AFBDAA3322947F9A71>",
        )
    )
    is True,
    "平台说没 @ 我 + 文本里有 @ 标记 → 算 @ 别人",
)
check(
    main._mentions_other(Event("在干嘛", woken=True, at=["BOT123"], mentions=[True]))
    is False,
    "平台说 @ 了我时不算 @ 别人",
)
check(
    main.TolerantCommandFilter().filter(
        Event(
            "精灵雷伊",
            woken=True,
            at=["BOT123"],
            mentions=[False],
            text_markup="<@494EC5E95CD4F4AFBDAA3322947F9A71>",
        ),
        None,
    )
    is False,
    "误判唤醒的 @群友 消息，宽松层不接管",
)

# 适配器没真的 @ 机器人时会填占位 At —— 这种是普通闲聊，不能忽略
check(
    main._mentions_other(Event("精灵雷伊", woken=True, at=["qq_official"])) is False,
    "占位 At 的普通闲聊不受影响",
)
check(
    main._mentions_other(
        Event("在干嘛", woken=True, at=["qq_official"], text_markup="<@494EC5E95CD4F4AFBDAA3322947F9A71>")
    )
    is True,
    "@ 别人的消息要拦下",
)
check(
    main._mentions_other(Event("精灵雷伊", woken=True, at=["qq_official"], private=True))
    is False,
    "私聊不抑制",
)

# @ 机器人的消息照常处理
check(
    main.TolerantCommandFilter().filter(
        Event("精灵雷伊", woken=True, at=["BOT123"]), None
    )
    is True,
    "@机器人时宽松层正常接管",
)

check(    main.AiFallbackFilter().filter(Event("宝可梦精灵有哪些"), None) is False,
    "别的游戏不被 AI 层接管",
)

# ---- 配置图走宽松层 ----
plugin = make_plugin(Context())
asyncio.run(collect(plugin.tolerant_query(Event("配置图 雷伊"))))
check(CALLS == [("pet_config", "query", "雷伊")], f"配置图分发错误: {CALLS}")

# ---- 裸名自动识别：直接发套装名 / 部件名 ----
FAKE_ROWS["影翼裁决"] = {"suit": [_Row("影翼裁决")]}
plugin = make_plugin(Context())
asyncio.run(collect(plugin.tolerant_query(Event("影翼裁决"))))
check(CALLS == [("equip", "suit", "影翼裁决")], f"裸名套装分发错误: {CALLS}")

FAKE_ROWS["漫游者-AH"] = {"equip": [_Row("漫游者-AH")]}
plugin = make_plugin(Context())
asyncio.run(collect(plugin.tolerant_query(Event("漫游者-AH"))))
check(CALLS == [("equip", "equip", "漫游者-AH")], f"裸名部件分发错误: {CALLS}")

# 查不到的名字不产生任何回复、也不调 handler
plugin = make_plugin(Context())
result = asyncio.run(collect(plugin.tolerant_query(Event("这个真不错"))))
check(result == [] and CALLS == [], "查不到的裸名不触发任何 handler")

# 裸名消息归宽松层，AI 层不重复处理
check(
    main.TolerantCommandFilter().filter(Event("影翼裁决"), None) is True,
    "裸名被宽松层放行",
)
check(
    main.AiFallbackFilter().filter(Event("影翼裁决"), None) is False,
    "裸名不会被 AI 层重复处理",
)

# 带修饰词的裸名：查一下漫游者 -> 漫游者
FAKE_ROWS["漫游者"] = {"equip": [_Row("漫游者-AH")]}
plugin = make_plugin(Context())
hit = plugin._auto_lookup("查一下漫游者")
check(hit == ("equip", "漫游者"), f"带修饰词的裸名识别错误: {hit}")

# 本身以「查」开头的名字不能被剥坏
FAKE_ROWS["查尔顿"] = {"pet_info": [_Row("查尔顿")]}
plugin = make_plugin(Context())
hit = plugin._auto_lookup("查尔顿")
check(hit == ("pet_info", "查尔顿"), f"查尔顿识别错误: {hit}")

# --------------------------------------------------------------------------- #
# 3. AI 层
# --------------------------------------------------------------------------- #

plugin = make_plugin(Context(reply='{"intent": "pet_info", "keyword": "雷伊"}'))
asyncio.run(collect(plugin.ai_fallback(Event("雷伊的技能都有啥"))))
check(CALLS == [("pet", "pet_info", "雷伊")], f"AI 层分发错误: {CALLS}")
check(plugin.context.calls == 1, f"AI 应只调用一次模型，实际 {plugin.context.calls} 次")

# 无参指令
plugin = make_plugin(Context(reply='{"intent": "preview"}'))
asyncio.run(collect(plugin.ai_fallback(Event("这周有啥预告吗"))))
check(CALLS == [("misc", "preview_cmd", "")], f"无参指令分发错误: {CALLS}")

# 模型判定不是查询 -> 什么都不发，交给正常聊天
plugin = make_plugin(Context(reply='{"intent": "none", "keyword": ""}'))
result = asyncio.run(collect(plugin.ai_fallback(Event("这个刻印值钱吗"))))
check(result == [], f"AI 判定 none 时不应有输出: {result}")
check(CALLS == [], f"AI 判定 none 时不应调用 handler: {CALLS}")

# 模型输出乱码 -> 同样静默
plugin = make_plugin(Context(reply="我觉得你在问精灵"))
result = asyncio.run(collect(plugin.ai_fallback(Event("雷伊的技能都有啥"))))
check(result == [], f"模型输出无法解析时不应有输出: {result}")

# 模型调用失败 -> 静默，不报错
plugin = make_plugin(Context(fail=True))
result = asyncio.run(collect(plugin.ai_fallback(Event("雷伊的技能都有啥"))))
check(result == [], f"模型调用失败时不应有输出: {result}")

# 中文 intent 也能认
plugin = make_plugin(Context(reply='{"intent": "属性表", "keyword": "火"}'))
asyncio.run(collect(plugin.ai_fallback(Event("火属性克制啥"))))
check(CALLS == [("attr", "type_matchup", "火")], f"中文 intent 分发错误: {CALLS}")

# --------------------------------------------------------------------------- #
# 4. 配置开关
# --------------------------------------------------------------------------- #

plugin = make_plugin(Context(reply='{"intent": "pet_info", "keyword": "雷伊"}'))
main.AiFallbackFilter.mode = "off"
check(
    main.AiFallbackFilter().filter(Event("雷伊的技能都有啥"), None) is False,
    "ai_fallback=off 时不应接管",
)
main.AiFallbackFilter.mode = "keyword"

# always 模式下，不含关键词的句子也会问 AI
main.AiFallbackFilter.mode = "always"
check(
    main.AiFallbackFilter().filter(Event("今天天气不错"), None) is True,
    "always 模式应接管不含关键词的句子",
)
main.AiFallbackFilter.mode = "keyword"
check(
    main.AiFallbackFilter().filter(Event("今天天气不错"), None) is False,
    "keyword 模式下不含关键词的句子不应接管",
)

# 「雷伊长什么样」现在归裸名层（剥掉疑问尾巴能查到雷伊），不该再走 AI
main.AiFallbackFilter.mode = "always"
check(
    main.AiFallbackFilter().filter(Event("雷伊长什么样"), None) is False,
    "裸名层能接的消息不再走 AI",
)
main.AiFallbackFilter.mode = "keyword"

# 长度超限不送 AI
check(
    main.AiFallbackFilter().filter(Event("刻印" + "很" * 300), None) is False,
    "超长消息不应送 AI",
)

# 三层互斥：宽松层能解析的消息，AI 层必须让路
for text in ("精灵雷伊", "雷伊精灵", "刻印宝石火"):
    tol = main.TolerantCommandFilter().filter(Event(text), None)
    ai = main.AiFallbackFilter().filter(Event(text), None)
    check(tol is True and ai is False, f"{text!r}: 宽松层={tol}, AI 层={ai}，期望 True/False")

# --------------------------------------------------------------------------- #
# 5. 聊天时的能力说明注入
# --------------------------------------------------------------------------- #


class Req:
    def __init__(self, system_prompt: str = ""):
        self.system_prompt = system_prompt


plugin = make_plugin(Context())
req = Req("你是一个赛尔号玩家助手。")
asyncio.run(plugin.inject_capability_prompt(Event("随便聊聊"), req))
check(
    "【你会的】" in req.system_prompt,
    "能力说明已拼进 system_prompt",
)
check(
    "你是一个赛尔号玩家助手。" in req.system_prompt,
    "原有 system_prompt 保留",
)
check("别编具体数值" in req.system_prompt, "能力说明包含防编造约束")
check("你会的" in req.system_prompt, "能力说明用第一人称措辞（贴合群聊人格）")
check("插件" not in req.system_prompt, "能力说明不提「插件」这类元信息")

before = req.system_prompt
asyncio.run(plugin.inject_capability_prompt(Event("随便聊聊"), req))
check(req.system_prompt == before, "重复注入不会叠加")

empty = Req("")
asyncio.run(plugin.inject_capability_prompt(Event("随便聊聊"), empty))
check(
    empty.system_prompt.startswith("【你会的】"),
    "空 system_prompt 时直接写入说明",
)

plugin._llm_awareness = False
req_off = Req("你是助手。")
asyncio.run(plugin.inject_capability_prompt(Event("随便聊聊"), req_off))
check(req_off.system_prompt == "你是助手。", "llm_awareness=False 时不注入")

# --------------------------------------------------------------------------- #
# 6. 模型否认能力时的兜底修正
# --------------------------------------------------------------------------- #


class FakeResult:
    def __init__(self, text: str):
        self.chain = [_Plain(text)]


plugin = make_plugin(Context())
ev = Event("你可以查赛尔号的数据吗")
ev.result = FakeResult("查不了，我这儿没接图鉴数据")
asyncio.run(plugin.patch_capability_denial(ev))
check(len(ev.result.chain) == 2, "否认能力时追加了引导")
check("精灵 雷伊" in ev.result.chain[-1].text, "引导语包含指令示例")

ev2 = Event("你可以查赛尔号的数据吗")
ev2.result = FakeResult("能啊，发「精灵 雷伊」就行")
asyncio.run(plugin.patch_capability_denial(ev2))
check(len(ev2.result.chain) == 1, "正常回答不追加")

ev3 = Event("精灵 雷伊")
ev3.result = FakeResult("查不了")
asyncio.run(plugin.patch_capability_denial(ev3))
check(len(ev3.result.chain) == 1, "指令消息的回复不追加")

ev4 = Event("你可以查赛尔号的数据吗")
ev4.result = FakeResult("❌数据库未加载，请稍后再试")
asyncio.run(plugin.patch_capability_denial(ev4))
check(len(ev4.result.chain) == 1, "插件自己的报错不追加")

ev5 = Event("今天天气不错")
ev5.result = FakeResult("查不了")
asyncio.run(plugin.patch_capability_denial(ev5))
check(len(ev5.result.chain) == 1, "和赛尔号无关的对话不追加")

# 没有关键词、但明显在查名字时也要兜底（「查一下漫游者」）
ev6 = Event("查一下漫游者")
ev6.result = FakeResult("漫游者我查不了，没这数据")
asyncio.run(plugin.patch_capability_denial(ev6))
check(len(ev6.result.chain) == 2, "像在查名字但没关键词时也追加引导")

# --------------------------------------------------------------------------- #

if failures:
    print(f"FAILED ({len(failures)})")
    for item in failures:
        print("  -", item)
    sys.exit(1)

print("OK: 接线测试通过（严格/宽松/AI 三层互斥、分发与容错均正常）")
