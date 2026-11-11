"""奶龙图查询与上传。

- `奶龙精灵查询`：先列出「序号 奶龙名（精灵原名）」，回序号发图；
- `上传奶龙图`：同一条消息里「图片 + 奶龙名 + 精灵序号或名称」。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.utils.session_waiter import (
    SessionController,
    SessionFilter,
    session_waiter,
)

from ..data import PetDataGetter, db_manager
from .milk_dragon import MAX_BYTES, SUFFIXES, MilkDragonStore, MilkEntry
from .pet_config_handler import _name_key

__all__ = ["MilkDragonHandler"]

MAX_MENU = 40


class _SenderSessionFilter(SessionFilter):
    """按「会话 + 发送者」隔离，避免吞掉群里别人的消息。"""

    def filter(self, event) -> str:
        getter = getattr(event, "get_sender_id", None)
        sender = ""
        if callable(getter):
            try:
                sender = str(getter() or "")
            except Exception:
                sender = ""
        return f"{event.unified_msg_origin}:{sender}"


class MilkDragonHandler:
    WAIT_TIMEOUT: int = 60
    COMMANDS = ("奶龙精灵查询", "奶龙查询", "上传奶龙图")

    def __init__(self, data_dir: Path | str, can_upload=None):
        self._store = MilkDragonStore(Path(data_dir) / "milk_dragon")
        self._store.load()
        # 复用配置图的权限判定（管理员 / 群管理 / 白名单）
        self._can_upload = can_upload
        self._pending: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #

    @staticmethod
    def _sender_id(event) -> str:
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            try:
                return str(getter() or "")
            except Exception:
                return ""
        return ""

    def _full_arg(self, event, arg: str) -> str:
        text = ""
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            try:
                text = str(getter() or "").strip()
            except Exception:
                text = ""
        body = text.lstrip("/／").strip()
        for cmd in self.COMMANDS:
            if body.startswith(cmd):
                rest = body[len(cmd) :].strip()
                for junk in ("[图片]", "[Image]", "[image]"):
                    if rest.endswith(junk):
                        rest = rest[: -len(junk)].strip()
                return rest or (arg or "").strip()
        return (arg or "").strip()

    @staticmethod
    def _pick_image(event):
        getter = getattr(event, "get_messages", None)
        messages = getter() if callable(getter) else []
        for comp in messages or []:
            if callable(getattr(comp, "convert_to_file_path", None)):
                return comp
        return None

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return task

    def allowed(self, event) -> bool:
        if callable(self._can_upload):
            try:
                return bool(self._can_upload(event))
            except Exception:
                return False
        return False

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def _menu_text(self, entries: list[MilkEntry]) -> str:
        lines = [f"🐲 奶龙图鉴（{len(entries)} 张）："]
        for index, entry in enumerate(entries[:MAX_MENU], 1):
            lines.append(f"{index} {entry.label()}")
        if len(entries) > MAX_MENU:
            lines.append(f"……还有 {len(entries) - MAX_MENU} 张，发「奶龙精灵查询 名字」直接查")
        lines.append("\n💬 回序号看图 · 0 退出")
        return "\n".join(lines)

    def _image_chain(self, entry: MilkEntry):
        path = self._store.path_of(entry)
        if path is None:
            return None
        return Comp.Image.fromFileSystem(str(path)), Comp.Plain(f"🐲 {entry.label()}")

    async def query(self, event: AstrMessageEvent, arg: str = ""):
        entries = self._store.all()
        if not entries:
            yield event.plain_result(
                "🐲 还没有奶龙图。发「上传奶龙图 奶政 嬴政」+ 图片就能加。"
            )
            return

        name = self._full_arg(event, arg).strip()
        if name:
            entry = self._store.find(name)
            if entry is None:
                yield event.plain_result(f"❌没找到「{name}」这张奶龙图。")
                return
            chain = self._image_chain(entry)
            if chain is None:
                yield event.plain_result(f"❌「{entry.name}」的图片文件不见了。")
                return
            yield event.chain_result(list(chain))
            return

        mapping = {
            str(index): entry for index, entry in enumerate(entries[:MAX_MENU], 1)
        }
        task = self._spawn(self._ask(event, mapping))
        yield event.plain_result(self._menu_text(entries))
        await task

    async def _ask(self, event, mapping: dict[str, MilkEntry]):
        @session_waiter(timeout=self.WAIT_TIMEOUT, record_history_chains=False)
        async def waiter(controller: SessionController, evt: AstrMessageEvent):
            text = evt.message_str.strip()
            if text == "0":
                await evt.send(evt.plain_result("❌已退出"))
                controller.stop()
                return
            entry = mapping.get(text)
            if entry is None:
                await evt.send(evt.plain_result("⚠️序号无效，请重新输入"))
                controller.keep(timeout=self.WAIT_TIMEOUT, reset_timeout=True)
                return
            chain = self._image_chain(entry)
            if chain is None:
                await evt.send(evt.plain_result(f"❌「{entry.name}」的图片文件不见了。"))
                controller.stop()
                return
            await evt.send(
                evt.chain_result(
                    [
                        *chain,
                        Comp.Plain(
                            f"\n（还能输入其他序号 · 共 {len(mapping)} 张 · 0 退出）"
                        ),
                    ]
                )
            )
            controller.keep(timeout=self.WAIT_TIMEOUT, reset_timeout=True)

        try:
            await waiter(event, _SenderSessionFilter())
        except TimeoutError:
            await event.send(event.plain_result("⏰选择超时，已退出"))

    # ------------------------------------------------------------------ #
    # 上传
    # ------------------------------------------------------------------ #

    def _resolve_pet(self, target: str):
        pets = PetDataGetter(db_manager.get_all_sessions(), target)
        if not pets:
            return None, f"❌未找到匹配的精灵: {target}"
        if len(pets) > 1:
            exact = None
            key = _name_key(target)
            for pet in pets:
                if _name_key(str(pet.name)) == key:
                    exact = pet
                    break
            if exact is not None:
                return exact, ""
            lines = [f"❌「{target}」匹配到 {len(pets)} 只精灵，请写完整名字或用 ID："]
            for pet in pets[:15]:
                lines.append(f"{pet.id}. {pet.name}")
            return None, "\n".join(lines)
        return pets[0], ""

    async def upload(self, event: AstrMessageEvent, arg: str = ""):
        if not self.allowed(event):
            yield event.plain_result(
                "❌你没有上传奶龙图的权限（白名单或 AstrBot 管理员）。"
            )
            return
        if not db_manager.is_database_loaded("seerapi"):
            yield event.plain_result("❌数据库未加载，请稍后再试")
            return

        text = self._full_arg(event, arg)
        if not text:
            yield event.plain_result(
                "❌用法：/上传奶龙图 <奶龙名> <精灵序号或名称>\n"
                "把图片和这条指令放在同一条消息里发送。"
            )
            return

        parts = text.split(maxsplit=1)
        milk_name = parts[0].strip()
        target = parts[1].strip() if len(parts) > 1 else ""
        if not milk_name or not target:
            yield event.plain_result(
                "❌要这样写：/上传奶龙图 奶政 嬴政（奶龙名在前，精灵名或序号在后）"
            )
            return

        pet, error = self._resolve_pet(target)
        if pet is None:
            yield event.plain_result(error)
            return

        image = self._pick_image(event)
        if image is None:
            yield event.plain_result("❌消息里没有图片，请把图和指令一起发。")
            return
        try:
            src = Path(await image.convert_to_file_path())
        except Exception as e:
            logger.warning(f"奶龙图读取失败: {e}")
            yield event.plain_result(f"❌读取图片失败: {e}")
            return
        if not src.is_file():
            yield event.plain_result("❌图片文件不存在，可能已被缓存清理。")
            return

        size = src.stat().st_size
        if size > MAX_BYTES:
            yield event.plain_result(
                f"❌图片太大（{size / 1024 / 1024:.1f} MB），请压到 8 MB 以内。"
            )
            return

        suffix = src.suffix.lower()
        if suffix not in SUFFIXES:
            suffix = ".png"
        try:
            data = await asyncio.to_thread(src.read_bytes)
            entry = await asyncio.to_thread(
                self._store.add,
                milk_name,
                int(pet.id),
                str(pet.name),
                data,
                suffix,
                uploader=self._sender_id(event),
            )
        except Exception as e:
            logger.error(f"奶龙图保存失败: {e}")
            yield event.plain_result(f"❌保存失败: {e}")
            return

        total, _ = self._store.stats()
        yield event.plain_result(
            f"✅已收录奶龙图「{entry.label()}」"
            f"（{size / 1024:.0f} KB，共 {total} 张）"
        )
