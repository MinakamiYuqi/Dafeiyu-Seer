"""真实 AstrBot 环境下的集成测试（需要 AstrBot 的 venv + 插件数据库）。

和另外三个离线测试不同，这个脚本**会真的导入 astrbot**，用真实的
CommandFilter / EventMessageTypeFilter / 渲染器 / 数据库跑一遍插件。

运行方式（在 AstrBot 的 venv 里，工作目录任意）：

    set ASTRBOT_ROOT=G:\\temp\\instances\\<id>\\core
    <实例>\\venv\\Scripts\\python.exe tests\\test_real_astrbot.py

前置条件：
* `import astrbot` 成功；
* `$ASTRBOT_ROOT/data/plugin_data/astrbot_plugin_seer_info/seerapi.sqlite` 存在
  （直接用线上实例的数据即可，脚本只读不写）；
* 本脚本所在目录必须是插件根目录（即 main.py 的上一级）。

任何前置条件不满足时会打印 SKIP 并返回 0，不会让 CI 挂掉。

注意：脚本末尾用 `os._exit()` 结束，避免 Playwright 的 Node driver 在解释器
finalize 阶段刷一堆 ResourceWarning。即便如此，最后可能仍会看到一小段
`node:events` / `EPIPE` 输出——那是被杀掉的子进程在收尾，**以退出码和
末尾的 `OK:` 行为准**，不是插件报错。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import time
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get("ASTRBOT_ROOT") or os.getcwd()).resolve()
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "plugin_data" / "astrbot_plugin_seer_info" / "seerapi.sqlite"

failures: list[str] = []
notes: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)
    print(("  ok   " if cond else "  FAIL ") + msg)


def skip(reason: str) -> None:
    print(f"SKIP: {reason}")
    sys.exit(0)


# --------------------------------------------------------------------------- #
# 前置检查
# --------------------------------------------------------------------------- #

try:
    import astrbot  # noqa: F401
except Exception as exc:  # pragma: no cover - 只在本机没有 AstrBot 时触发
    skip(f"当前解释器没有 astrbot（{exc}）；请用 AstrBot 的 venv python 运行")

if not DB_PATH.exists():
    skip(f"找不到插件数据库 {DB_PATH}；请确认 ASTRBOT_ROOT 指向 AstrBot 根目录")

print(f"astrbot : {Path(astrbot.__file__).parent}")
print(f"root    : {ROOT}")
print(f"plugin  : {PLUGIN_DIR}")

# --------------------------------------------------------------------------- #
# 带真实 astrbot 的导入
# --------------------------------------------------------------------------- #

from astrbot.core.star.filter.command import CommandFilter  # noqa: E402
from astrbot.core.star.filter.custom_filter import CustomFilter  # noqa: E402
from astrbot.core.star.filter.event_message_type import (  # noqa: E402
    EventMessageType,
    EventMessageTypeFilter,
)

_pkg = types.ModuleType("astrbot_plugin_seer_info")
_pkg.__path__ = [str(PLUGIN_DIR)]
sys.modules[_pkg.__name__] = _pkg

_spec = importlib.util.spec_from_file_location(
    "astrbot_plugin_seer_info.main", PLUGIN_DIR / "main.py"
)
plugin_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = plugin_mod
_spec.loader.exec_module(plugin_mod)

from astrbot_plugin_seer_info.core import command_parser as parser  # noqa: E402
from astrbot_plugin_seer_info.core import ai_fallback  # noqa: E402


# --------------------------------------------------------------------------- #
# 事件 / 上下文桩（只替代平台层，其余都是真实现）
# --------------------------------------------------------------------------- #


class Event:
    """最小事件桩：只提供 handler 真正会用到的方法。"""

    def __init__(self, text: str, woken: bool = True):
        self.message_str = text
        self.is_at_or_wake_command = woken
        self.unified_msg_origin = "test:FriendMessage:1"
        self.extras: dict = {}
        self.sent: list = []
        self.plugins_name = None
        self._stopped = False
        self.trace = types.SimpleNamespace(record=lambda *a, **k: None)

    def is_stopped(self) -> bool:
        return self._stopped

    def stop_event(self) -> None:
        self._stopped = True

    def get_result(self):
        return getattr(self, "result", None)

    def get_message_str(self) -> str:
        return self.message_str

    def set_extra(self, key, value) -> None:
        self.extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self.extras
        return self.extras.get(key, default)

    def get_message_type(self):
        from astrbot.core.platform.message_type import MessageType

        return MessageType.FRIEND_MESSAGE

    def plain_result(self, text):
        return ("plain", text)

    def image_result(self, image, url=None):
        return ("image", image)

    def chain_result(self, chain):
        return ("chain", tuple(chain))

    async def send(self, result):
        self.sent.append(result)


class LLM:
    def __init__(self, reply: str | None):
        self.reply = reply
        self.calls = 0

    async def get_current_chat_provider_id(self, umo):
        return "test-provider"

    async def llm_generate(self, **kwargs):
        self.calls += 1
        if self.reply is None:
            raise RuntimeError("provider exploded")

        class Resp:
            completion_text = self.reply

        return Resp()


class Context:
    def __init__(self, reply: str | None = None):
        self.llm = LLM(reply)

    async def get_current_chat_provider_id(self, umo):
        return await self.llm.get_current_chat_provider_id(umo)

    async def llm_generate(self, **kwargs):
        return await self.llm.llm_generate(**kwargs)


CONFIG = {
    "seerapi_sync_url": "",
    "seerapi_fingerprint_url": "",
    "seerapi_sync_interval_minutes": 60,
    "alias_sync_url": "",
    "alias_fingerprint_url": "",
    "alias_sync_interval_minutes": 60,
    "render_mode": "local",
    "image_format": "jpeg",
    "jpeg_quality": 85,
    "fuzzy_command": True,
    "bare_name_search": False,
    "ai_fallback": "keyword",
    "ai_fallback_provider_id": "",
    "ai_fallback_max_length": 120,
    # 白名单候选池写到临时目录，别污染真实数据
    "data_dir": str(Path(tempfile.gettempdir()) / "seer_info_test_data"),
}


async def collect(agen) -> list:
    return [item async for item in agen]


# --------------------------------------------------------------------------- #
# 1. 真实 CommandFilter 语义 vs 我们的副本
# --------------------------------------------------------------------------- #

print("\n[1] 真实 CommandFilter 语义对照")
real_filter = CommandFilter("精灵", alias={"查询精灵信息", "魂印", "技能"})
real_filter.handler_params = {"arg": ""}

CORPUS = [
    "精灵 雷伊",
    "精灵",
    "精灵  雷伊",
    "精灵　　雷伊",
    "魂印 雷伊",
    "精灵雷伊",
    "雷伊精灵",
    "精灵：雷伊",
    "精灵雷伊技能",
    "查询精灵信息 雷伊",
    "查一下精灵 雷伊",
    "帮助下",
    "精灵 雷伊 技能",
]
mismatch = []
for text in CORPUS:
    expected = matches = parser.matches_strict_command(text)
    try:
        actual = real_filter.filter(Event(text), {})
    except Exception as exc:  # pragma: no cover
        actual = f"error: {exc}"
    if expected != actual:
        mismatch.append((text, expected, actual))
check(not mismatch, f"matches_strict_command 与真实 CommandFilter 一致（{len(CORPUS)} 条）")
for item in mismatch:
    notes.append(f"  不一致: {item!r}")

# --------------------------------------------------------------------------- #
# 2. EventMessageTypeFilter(ALL) 确实对所有消息返回 True（所以必须再叠过滤器）
# --------------------------------------------------------------------------- #

print("\n[2] ALL 过滤器的语义")
emt = EventMessageTypeFilter(EventMessageType.ALL)
check(emt.filter(Event("随便说点什么"), {}) is True, "event_message_type(ALL) 对普通消息返回 True")

# --------------------------------------------------------------------------- #
# 3. 过滤器是真正的 CustomFilter 子类，且判定符合预期
# --------------------------------------------------------------------------- #

print("\n[3] 两个自定义过滤器")
check(issubclass(plugin_mod.TolerantCommandFilter, CustomFilter), "TolerantCommandFilter 继承真实 CustomFilter")
check(issubclass(plugin_mod.AiFallbackFilter, CustomFilter), "AiFallbackFilter 继承真实 CustomFilter")

tol = plugin_mod.TolerantCommandFilter()
ai = plugin_mod.AiFallbackFilter()
check(tol.filter(Event("精灵雷伊"), {}) is True, "宽松层接管「精灵雷伊」")
check(tol.filter(Event("精灵 雷伊"), {}) is False, "宽松层让路给严格指令")
check(tol.filter(Event("精灵雷伊", woken=False), {}) is False, "未唤醒时宽松层不接管")
check(ai.filter(Event("雷伊的技能都有啥"), {}) is True, "AI 层接管「雷伊的技能都有啥」")
check(ai.filter(Event("精灵雷伊"), {}) is False, "AI 层让路给宽松层")
check(ai.filter(Event("今天天气不错"), {}) is False, "AI 层不接闲聊")


# --------------------------------------------------------------------------- #
# 主流程：真正实例化插件，跑查询与渲染
# --------------------------------------------------------------------------- #


async def main() -> None:
    print("\n[4] 实例化插件（真实 astrbot + 真实数据库）")
    ctx = Context(reply='{"intent": "pet_info", "keyword": "70"}')
    plugin = plugin_mod.SeerInfoPlugin(ctx, dict(CONFIG))

    from astrbot_plugin_seer_info.data import db_manager

    check(db_manager.is_database_loaded("seerapi"), "seerapi 数据库已加载")

    # 4.1 严格指令：按 ID 查询并渲染
    print("\n[5] 严格指令 pet_info(id=70) 渲染")
    results = await collect(plugin.pet_info(Event("精灵 70"), "70"))
    check(bool(results), "pet_info 产生了结果")
    kind, payload = results[0] if results else (None, None)
    check(kind == "image", f"结果是图片（实际 {kind}）")
    if kind == "image":
        path = Path(str(payload))
        ok = path.exists()
        check(ok, f"渲染文件存在: {path}")
        if ok:
            raw = path.read_bytes()
            check(len(raw) > 5000, f"图片大小 {len(raw) / 1024:.0f} KB")
            check(raw[:2] == b"\xff\xd8", "内容是 JPEG")

    # 4.2 宽松层：精灵70
    print("\n[6] 宽松层 tolerant_query(精灵70)")
    results = await collect(plugin.tolerant_query(Event("精灵70")))
    check(results and results[0][0] == "image", "宽松层走到了 pet_info 并出图")

    # 4.3 AI 层
    print("\n[7] AI 层 ai_fallback（模型返回 pet_info/70）")
    results = await collect(plugin.ai_fallback(Event("雷伊的技能都有啥")))
    check(ctx.llm.calls == 1, f"AI 只调用一次模型（实际 {ctx.llm.calls}）")
    check(results and results[0][0] == "image", "AI 层走到了 pet_info 并出图")

    print("\n[13] 裸名自动识别（真实数据库）")
    hit = plugin._auto_lookup("影翼裁决")
    check(hit is not None and hit[0] == "suit", f"「影翼裁决」识别为套装（实际 {hit}）")
    hit = plugin._auto_lookup("漫游者-AH")
    check(hit is not None and hit[0] == "equip", f"「漫游者-AH」识别为部件（实际 {hit}）")
    hit = plugin._auto_lookup("查一下漫游者")
    check(
        hit is not None and hit[0] == "equip" and hit[1] == "漫游者",
        f"「查一下漫游者」清洗后识别为部件（实际 {hit}）",
    )
    check(plugin._auto_lookup("这个真不错") is None, "查不到的名字不触发")
    check(plugin._auto_lookup("今天天气不错") is None, "闲聊不触发")

    print("\n[8] AI 层判定不是查询")
    ctx2 = Context(reply='{"intent": "none", "keyword": ""}')
    plugin2 = plugin_mod.SeerInfoPlugin(ctx2, dict(CONFIG))
    results = await collect(plugin2.ai_fallback(Event("今天天气不错")))
    check(results == [], "AI 返回 none 时零输出")
    await plugin2.terminate()

    print("\n[9] 模型不可用")
    ctx3 = Context(reply=None)  # llm_generate 抛异常
    plugin3 = plugin_mod.SeerInfoPlugin(ctx3, dict(CONFIG))
    results = await collect(plugin3.ai_fallback(Event("雷伊的技能都有啥")))
    check(results == [], "模型报错时静默")
    await plugin3.terminate()

    print("\n[10] 能力说明注入（真实 ProviderRequest）")
    from astrbot.core.provider.entities import ProviderRequest

    req = ProviderRequest(prompt="hi")
    await plugin.inject_capability_prompt(Event("随便聊聊"), req)
    check("【你会的】" in req.system_prompt, "真实 system_prompt 里注入了能力说明")
    check("别编具体数值" in req.system_prompt, "能力说明包含防编造约束")
    snapshot = req.system_prompt
    await plugin.inject_capability_prompt(Event("随便聊聊"), req)
    check(req.system_prompt == snapshot, "重复注入不会叠加")

    print("\n[11] 钩子注册与真实调度路径")
    import functools

    from astrbot.core.pipeline.context_utils import call_event_hook
    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    hook_handlers = [
        h
        for h in star_handlers_registry.get_handlers_by_event_type(
            EventType.OnLLMRequestEvent, plugins_name=None
        )
        if h.handler_name == "inject_capability_prompt"
    ]
    check(bool(hook_handlers), "inject_capability_prompt 已注册为 OnLLMRequestEvent handler")

    # 生产环境里 PluginManager 会用 functools.partial(func, 实例) 绑定
    # （star_manager.py）；这里手工实例化，需要自己补这一步
    for h in hook_handlers:
        if not isinstance(h.handler, functools.partial):
            h.handler = functools.partial(h.handler, plugin)

    req2 = ProviderRequest(prompt="雷伊种族值多少")
    stopped = await call_event_hook(Event("雷伊种族值多少"), EventType.OnLLMRequestEvent, req2)
    check("【你会的】" in req2.system_prompt, "经 call_event_hook 真实触发后已注入")
    check(stopped is False, "注入不会中断事件")

    print("\n[12] 否认能力的回复兜底（真实 MessageEventResult）")
    from astrbot.core.message.message_event_result import MessageEventResult

    ev = Event("你可以查赛尔号的数据吗")
    ev.result = MessageEventResult().message("查不了，我这儿没接图鉴数据")
    await plugin.patch_capability_denial(ev)
    check(len(ev.result.chain) == 2, "真实结果对象上追加了引导")
    check(
        "精灵 雷伊" in getattr(ev.result.chain[-1], "text", ""),
        "引导语包含指令示例",
    )

    ev2 = Event("精灵 雷伊")
    ev2.result = MessageEventResult().message("查不了")
    await plugin.patch_capability_denial(ev2)
    check(len(ev2.result.chain) == 1, "指令消息的回复不会被追加")

    print("\n[14] 配置图存取（真实文件系统）")
    import struct
    import zlib

    from astrbot.core.message.components import Image as RealImage
    from astrbot_plugin_seer_info.data import register_local_database

    # 前面几个临时实例 terminate 时 dispose_all() 会把库释放掉，这里重新加载
    if not db_manager.is_database_loaded("seerapi"):
        register_local_database("seerapi")
        check(db_manager.is_database_loaded("seerapi"), "重新加载数据库成功")

    def _tiny_png() -> bytes:
        def chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        raw = b"\x00\xff\x00\x00\xff"
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b"")
        )

    root = plugin._pet_config_handler.root
    root.mkdir(parents=True, exist_ok=True)
    target = root / "70.png"
    target.write_bytes(_tiny_png())
    try:
        results = await collect(plugin._pet_config_handler.query(Event("配置图 70"), "70"))
        got_image = any(
            isinstance(comp, RealImage)
            for item in results
            if isinstance(item, tuple) and item[0] == "chain"
            for comp in item[1]
        )
        check(got_image, "配置图 70 返回了图片组件")
    finally:
        target.unlink(missing_ok=True)

    results = await collect(plugin._pet_config_handler.query(Event("配置图 70"), "70"))
    missing_hint = any(
        isinstance(item, tuple)
        and (
            (item[0] == "plain" and "暂未收录" in str(item[1]))
            or (
                item[0] == "chain"
                and "暂未收录" in str(getattr(item[1][0], "text", ""))
            )
        )
        for item in results
    )
    check(missing_hint, "图片删除后提示「暂未收录」")

    print("\n[15] 收录权限判定")
    from astrbot_plugin_seer_info.core.pet_config_handler import PetConfigHandler

    class _Who:
        def __init__(self, admin=False, role="member", sid="stranger"):
            self.role = role
            self._admin = admin
            self._sid = sid

        def is_admin(self):
            return self._admin

        def get_sender_id(self):
            return self._sid

    handler = PetConfigHandler(plugin._pet_config_handler.root.parent, uploaders={"abc123"})
    check(handler.can_upload(_Who(admin=True)), "AstrBot 管理员可收录")
    check(handler.can_upload(_Who(role="owner")), "群主可收录")
    check(handler.can_upload(_Who(role="admin")), "平台上报的群管理员可收录")
    check(handler.can_upload(_Who(sid="abc123")), "白名单 ID 可收录")
    check(not handler.can_upload(_Who()), "路人不可收录")
    check(not handler.can_upload(_Who(sid="")), "拿不到 ID 时不可收录")

    print("\n[16] 配置图多套 + 作者备注")
    import shutil

    from astrbot.core.message.components import Plain as RealPlain

    class _Img:
        def __init__(self, path):
            self._path = path

        async def convert_to_file_path(self):
            return str(self._path)

    class _SaveEvent:
        def __init__(self, text, img_path):
            self._text = text
            self._img = _Img(img_path)
            self.role = "member"

        def get_message_str(self):
            return self._text

        def get_messages(self):
            return [self._img]

        def is_admin(self):
            return True

        def get_sender_id(self):
            return "uidadmin"

        def get_sender_name(self):
            return "管理员"

        def plain_result(self, text):
            return ("plain", str(text))

    cfg = plugin._pet_config_handler
    test_img = cfg.root.parent / "_tiny_test.png"
    test_img.write_bytes(_tiny_png())

    def _captions(results):
        out = []
        for item in results:
            if not isinstance(item, tuple):
                continue
            if item[0] == "plain":
                out.append(str(item[1]))
            elif item[0] == "chain":
                for comp in item[1]:
                    if isinstance(comp, RealPlain):
                        out.append(str(comp.text))
        return out

    try:
        await collect(
            cfg.save(_SaveEvent("收录配置图 70 [作者:张三] [备注:物攻流]", test_img), "70")
        )
        await collect(
            cfg.save(_SaveEvent("收录配置图 70 [作者:李四] [备注:双防流]", test_img), "70")
        )
        check(len(cfg.variants(70)) == 2, f"两个作者各占一套（实际 {len(cfg.variants(70))}）")

        old_timeout = cfg.WAIT_TIMEOUT
        cfg.WAIT_TIMEOUT = 0.2
        try:
            results = await collect(cfg.query(Event("配置图 70"), "70"))
        finally:
            cfg.WAIT_TIMEOUT = old_timeout

        images = [
            comp
            for item in results
            if isinstance(item, tuple) and item[0] == "chain"
            for comp in item[1]
            if isinstance(comp, RealImage)
        ]
        joined = "\n".join(_captions(results))
        check(len(images) == 0, "多套时先问作者，不直接发图")
        check("张三" in joined and "李四" in joined, "菜单里两套作者都在")
        check("物攻流" in joined and "双防流" in joined, "菜单里两套备注都在")

        # 菜单发出后生成器被关闭，会话仍要保持注册（真实环境踩过的坑）
        from astrbot.core.utils.session_waiter import USER_SESSIONS, SessionWaiter

        agen = cfg.query(Event("配置图 70"), "70")
        await agen.__anext__()
        await asyncio.sleep(0.2)
        registered = bool(USER_SESSIONS)
        await agen.aclose()
        await asyncio.sleep(0.05)
        check(registered and bool(USER_SESSIONS), "菜单后生成器关闭，会话仍保持注册")

        pick = Event("2")
        if USER_SESSIONS:
            await SessionWaiter.trigger(next(iter(USER_SESSIONS)), pick)
            await asyncio.sleep(0.2)
        check(
            any(isinstance(item, tuple) and item[0] == "chain" for item in pick.sent),
            "回序号后能收到图片",
        )

        # 选完不退出：可以接着看第二套，输入 0 才结束
        agen2 = cfg.query(Event("配置图 70"), "70")
        await agen2.__anext__()
        await asyncio.sleep(0.2)
        sid2 = next(iter(USER_SESSIONS))
        p1 = Event("1")
        await SessionWaiter.trigger(sid2, p1)
        await asyncio.sleep(0.1)
        still_open = bool(USER_SESSIONS)
        p2 = Event("2")
        await SessionWaiter.trigger(sid2, p2)
        await asyncio.sleep(0.1)
        got1 = any(isinstance(i, tuple) and i[0] == "chain" for i in p1.sent)
        got2 = any(isinstance(i, tuple) and i[0] == "chain" for i in p2.sent)
        check(got1 and got2, "连着输入 1、2 都能出图")
        check(still_open, "看第一套后会话仍然开着")
        p0 = Event("0")
        await SessionWaiter.trigger(sid2, p0)
        await asyncio.sleep(0.3)
        await agen2.aclose()

        await collect(
            cfg.save(_SaveEvent("收录配置图 70 [作者:张三] [备注:高速流]", test_img), "70")
        )
        variants = cfg.variants(70)
        check(len(variants) == 2, "同作者重传不新增套数")
        check(any(v.meta.note == "高速流" for v in variants), "同作者重传更新了备注")

        await collect(
            cfg.remark(_SaveEvent("配置备注 70 [作者:李四] [备注:低速]", test_img), "70")
        )
        author_notes = {v.meta.author: v.meta.note for v in cfg.variants(70)}
        check(
            author_notes.get("李四") == "低速",
            f"按作者改备注（实际 {author_notes!r}）",
        )

        one = await collect(cfg.query(Event("配置图 70 2"), "70 2"))
        check(
            len(one) == 1 and isinstance(one[0][1][0], RealImage),
            "配置图 70 2 只发第二套",
        )
    finally:
        shutil.rmtree(cfg.root / "70", ignore_errors=True)
        for _suffix in (".png", ".jpg", ".jpeg", ".webp"):
            (cfg.root / f"70{_suffix}").unlink(missing_ok=True)
        (cfg.root / "70.json").unlink(missing_ok=True)
        test_img.unlink(missing_ok=True)

    print("\n[17] 完整参数提取（绕开 CommandFilter 只传首词）")
    from astrbot_plugin_seer_info.core.pet_config_meta import parse_upload_arg as _parse

    ev3 = Event("收录配置图 艾夏拉 [作者:佑栖] [备注:5级秘咒净灵咒] [图片]")
    full = plugin._pet_config_handler._full_arg(ev3, "艾夏拉")
    check(
        "作者:佑栖" in full and "备注:5级秘咒净灵咒" in full,
        f"提取到完整参数（实际 {full!r}）",
    )
    check("[图片]" not in full, "去掉尾部 [图片] 占位")
    name3, meta3 = _parse(full)
    check(
        name3 == "艾夏拉" and meta3.author == "佑栖" and "秘咒" in meta3.note,
        f"解析出作者与备注（实际 {name3!r}/{meta3.author!r}/{meta3.note!r}）",
    )

    print("\n[19] 自定义外号")
    from astrbot_plugin_seer_info.data import (
        PetDataGetter as _PetGetter,
    )
    from astrbot_plugin_seer_info.data import (
        register_custom_aliases as _reg_aliases,
    )
    from astrbot_plugin_seer_info.data import (
        register_local_database as _reg_local,
    )

    # 前面的临时实例 terminate 时把别名库也 dispose 了，这里补回来
    if not db_manager.is_database_loaded("aliases"):
        _reg_local("aliases")
        check(db_manager.is_database_loaded("aliases"), "别名库重新加载成功")

    applied = _reg_aliases({"二哈": "雷伊", "南霜外号": "4511"})
    check(applied == 2, f"两条外号都写入（实际 {applied}）")

    sessions = db_manager.get_all_sessions()
    pets = _PetGetter(sessions, "二哈")
    check(
        len(pets) >= 1 and any(int(p.id) == 70 for p in pets),
        f"「二哈」能查到雷伊（实际 {[int(p.id) for p in pets]}）",
    )
    pets = _PetGetter(sessions, "南霜外号")
    check(
        any(int(p.id) == 4511 for p in pets),
        f"ID 型外号生效（实际 {[int(p.id) for p in pets]}）",
    )
    pets = _PetGetter(sessions, "不存在的别名")
    check(pets == [] or len(pets) == 0, "不存在的别名查不到东西")

    # 模拟一次远程同步：重新加载别名库后，外号应该被 post-load 钩子自动补回
    _reg_local("aliases")
    pets = _PetGetter(db_manager.get_all_sessions(), "二哈")
    check(
        any(int(p.id) == 70 for p in pets),
        f"别名库重新加载后外号仍然生效（实际 {[int(p.id) for p in pets]}）",
    )

    print("\n[20] 外号查询")

    def _plain_text(results):
        return "".join(
            str(item[1]) for item in results if isinstance(item, tuple) and item[0] == "plain"
        )

    alias_handler = plugin._alias_handler

    text = _plain_text(await collect(alias_handler.query(Event("外号 811"), "811")))
    check("草马" in text, f"别名库外号能查到（实际 {text!r}）")
    check("始祖灵兽" in text, "外号结果带精灵名")

    text = _plain_text(await collect(alias_handler.query(Event("外号 70"), "70")))
    check("二哈" in text, f"自定义外号也出现在列表里（实际 {text!r}）")

    text = _plain_text(await collect(alias_handler.query(Event("外号 2000"), "2000")))
    check("没有收录外号" in text, f"没有外号的精灵给出提示（实际 {text!r}）")

    text = _plain_text(await collect(alias_handler.query(Event("外号"), "")))
    check("条外号" in text and "只精灵" in text, f"不带参数显示概况（实际 {text!r}）")

    text = _plain_text(await collect(alias_handler.query(Event("外号 不存在的东西"), "不存在的东西")))
    check("未找到匹配的精灵" in text, "查不到精灵时给出提示")

    print("\n[21] 渲染缓存")
    from astrbot_plugin_seer_info.core.renderer import render_cache_stats

    hits0, misses0, _, _ = render_cache_stats()
    started = time.perf_counter()
    await collect(
        plugin._pet_handler.pet_info(Event("精灵 圣灵谱尼"), "圣灵谱尼")
    )
    first_cost = time.perf_counter() - started
    started = time.perf_counter()
    await collect(
        plugin._pet_handler.pet_info(Event("精灵 圣灵谱尼"), "圣灵谱尼")
    )
    second_cost = time.perf_counter() - started
    hits1, misses1, files, total = render_cache_stats()

    check(hits1 > hits0, f"第二次渲染命中缓存（命中 {hits0}→{hits1}）")
    check(files >= 1, f"缓存目录里至少有 1 个文件（实际 {files}）")
    check(
        second_cost < first_cost,
        f"命中缓存后更快（{first_cost:.2f}s → {second_cost:.2f}s）",
    )
    notes.append(
        f"    渲染耗时：首次 {first_cost:.2f}s，命中缓存 {second_cost:.2f}s，"
        f"缓存 {files} 个文件 / {total / 1024:.0f} KB"
    )

    print("\n[22] 专属效果与开启消耗")
    from sqlalchemy import text as _sql_text

    from astrbot_plugin_seer_info.core.pet_renderer import render_pet_info_data
    from astrbot_plugin_seer_info.data import PetDataGetter as _PetGetter2

    _sessions = db_manager.get_all_sessions()
    _conn = _sessions["seerapi"].connection()
    effect_row = _conn.execute(
        _sql_text("select pet_id from pet_special_effect group by pet_id order by count(*) desc limit 1")
    ).fetchone()
    check(effect_row is not None, "库里存在专属效果数据")
    if effect_row is not None:
        _pets = _PetGetter2(_sessions, str(effect_row[0]))
        _data = await render_pet_info_data(_pets[0])
        effects = _data.get("special_effects") or []
        check(
            len(effects) >= 1,
            f"渲染数据带上专属效果（实际 {len(effects)} 条）",
        )
        check(
            any(fx.get("desc") for fx in effects),
            "专属效果带描述文本",
        )

    price_row = _conn.execute(
        _sql_text(
            "select pet_id from skillinpetorm where skill_activation_item_id is not null "
            "and skill_activation_item_id in (select item_id from item_exchange_price) limit 1"
        )
    ).fetchone()
    if price_row is not None:
        _pets = _PetGetter2(_sessions, str(price_row[0]))
        _data = await render_pet_info_data(_pets[0])
        priced = [
            sk
            for group in ("fifth_skills", "advanced_skills", "special_skills", "level_skills")
            for sk in _data[group]
            if sk.get("activation_price")
        ]
        check(
            bool(priced),
            f"开启消耗渲染到技能上（实际 {[(s['name'], s['activation_price']) for s in priced][:3]}）",
        )

    print("\n[25] 裸名精确匹配 + 等待器按人隔离")
    # 拼音模糊命中（「肥鱼」→ 费约克/绯云…）不能触发裸名查询
    check(plugin._auto_lookup("肥鱼") is None, "「肥鱼」不触发裸名查询")
    check(plugin._auto_lookup("今天吃什么") is None, "闲聊不触发裸名查询")
    hit = plugin._auto_lookup("影翼裁决")
    check(hit is not None, f"真正的套装名仍能识别（实际 {hit}）")

    from astrbot_plugin_seer_info.core.common_handler import _SenderOnlyFilter

    class _Who:
        def __init__(self, sender):
            self._sender = sender
            self.unified_msg_origin = "default:GroupMessage:G1"

        def get_sender_id(self):
            return self._sender

    flt = _SenderOnlyFilter()
    key_a = flt.filter(_Who("USER_A"))
    key_b = flt.filter(_Who("USER_B"))
    check(key_a != key_b, "不同用户的等待器 key 不同")
    check(key_a == flt.filter(_Who("USER_A")), "同一用户 key 稳定")
    check(key_a.startswith("default:GroupMessage:G1"), "key 里带会话前缀")

    print("\n[24] 奶龙图")
    import shutil as _shutil2
    import tempfile as _tempfile2

    from astrbot_plugin_seer_info.core.milk_dragon_handler import MilkDragonHandler

    _milk_dir = Path(_tempfile2.mkdtemp(prefix="seer_milk_"))
    milk = MilkDragonHandler(_milk_dir, can_upload=lambda _e: True)
    _milk_img = _milk_dir / "_milk_test.png"
    _milk_img.write_bytes(_tiny_png())
    try:
        outs = await collect(
            milk.upload(_SaveEvent("上传奶龙图 奶政 嬴政", _milk_img), "奶政 嬴政")
        )
        text = str(outs[0][1]) if outs else ""
        check("已收录奶龙图" in text, f"上传奶龙图成功（实际 {text[:60]!r}）")
        check(any(e.name == "奶政" for e in milk._store.all()), "图库里有「奶政」")

        outs = await collect(
            milk.upload(
                _SaveEvent("上传奶龙图 奶霜 不破帝·南霜", _milk_img), "奶霜 不破帝·南霜"
            )
        )
        check("已收录奶龙图" in str(outs[0][1]), "第二张也收录")

        direct = await collect(milk.query(Event("奶龙精灵查询 奶政"), "奶政"))
        got_image = any(
            isinstance(item, tuple)
            and item[0] == "chain"
            and any(isinstance(comp, RealImage) for comp in item[1])
            for item in direct
        )
        check(got_image, "按名字直接出图")

        old_timeout = milk.WAIT_TIMEOUT
        milk.WAIT_TIMEOUT = 0.2
        try:
            menu = await collect(milk.query(Event("奶龙精灵查询"), ""))
        finally:
            milk.WAIT_TIMEOUT = old_timeout
        menu_text = _plain_text(menu)
        check("1 奶政（始皇帝·嬴政）" in menu_text, f"菜单按顺序列出（实际 {menu_text[:80]!r}）")
        check("2 奶霜（不破帝·南霜）" in menu_text, "菜单第二项")
    finally:
        _shutil2.rmtree(_milk_dir, ignore_errors=True)

    print("\n[23] 重名时收录：同名优先 + 候选列表")
    import shutil as _shutil

    _dup_img = cfg.root.parent / "_dup_test.png"
    _dup_img.write_bytes(_tiny_png())
    try:
        outs = await collect(
            cfg.save(_SaveEvent("收录配置图 雷伊 [作者:自动]", _dup_img), "雷伊")
        )
        text = str(outs[0][1]) if outs else ""
        check("雷伊（70）" in text, f"重名时优先取同名精灵（实际 {text[:80]!r}）")

        outs = await collect(
            cfg.save(_SaveEvent("收录配置图 雷 [作者:自动]", _dup_img), "雷")
        )
        text = str(outs[0][1]) if outs else ""
        check("匹配到" in text and "只精灵" in text, "太模糊时提示匹配到多只")
        check("70. 雷伊" in text, "候选列表里给出 ID 和名字")

        results = await collect(cfg.query(Event("配置图 雷伊"), "雷伊"))
        images = [
            comp
            for item in results
            if isinstance(item, tuple) and item[0] == "chain"
            for comp in item[1]
            if isinstance(comp, RealImage)
        ]
        check(len(images) >= 1, "查询也按同名优先直接出图")
    finally:
        _shutil.rmtree(cfg.root / "70", ignore_errors=True)
        _dup_img.unlink(missing_ok=True)

    await plugin.terminate()
    # Playwright 的子进程管道在解释器退出时才回收，会打印 ResourceWarning，
    # 这里主动给事件循环一点时间，让退出码保持干净
    import gc

    gc.collect()
    await asyncio.sleep(0.3)


try:
    asyncio.run(main())
except Exception as exc:  # pragma: no cover
    import traceback

    traceback.print_exc()
    failures.append(f"主流程异常: {exc}")


# --------------------------------------------------------------------------- #

print("\n" + "=" * 60)
for note in notes:
    print(note)
if failures:
    print(f"FAILED ({len(failures)})")
    for item in failures:
        print("  -", item)
    sys.stdout.flush()
    sys.stderr.flush()
    # Playwright 的 driver 是 Node 子进程，解释器正常退出时它的管道回收会打出
    # 一堆与本测试无关的 ResourceWarning；这里直接结束进程，保持输出干净。
    os._exit(1)

print("OK: 真实 AstrBot 环境下全部通过")
sys.stdout.flush()
sys.stderr.flush()
os._exit(0)
