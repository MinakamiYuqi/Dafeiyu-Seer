"""精灵配置图：按精灵序号存取本地图片，支持同一只精灵有多套配置。

目录结构（放在插件自己的数据目录，重启/缓存清理都不会动）::

    <插件数据目录>/pet_configs/3673/1.jpg   + 1.json
    <插件数据目录>/pet_configs/3673/2.png   + 2.json

旧版的单图布局 `<id>.png` + `<id>.json` 仍然能读，第一次写入时会自动迁移到
`<id>/1.<后缀>`，所以老数据不用手动搬。

每套配置按「作者」区分：同一个作者再收录就是替换他自己那套，换作者就是新增一套。
"""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
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
from .pet_config_meta import (
    ConfigMeta,
    clean_text,
    merge_meta,
    parse_upload_arg,
)
from .pet_config_meta import loads as meta_loads
from .pet_config_meta import dumps as meta_dumps

# 与上游 IronsBot 的 pet_config 保持一致
SUFFIXES: tuple[str, ...] = (".png", ".webp", ".jpg", ".jpeg")
MAX_BYTES = 8 * 1024 * 1024
_NORM_RE = re.compile(r"[\s\u3164\u200b-\u200f\ufeff]+")
_NAME_NOISE_RE = re.compile(r"[\s·・\-—_()（）\[\]【】<>《》]+")


def _ni(name: str) -> str:
    return _NORM_RE.sub("", (name or "").strip()).lower()


def _name_key(name: str) -> str:
    """精灵名的宽松比较键：去空格/标点/括号，忽略大小写。"""
    return _NAME_NOISE_RE.sub("", (name or "").strip()).lower()


class _SenderSessionFilter(SessionFilter):
    """按「会话 + 发送者」隔离，避免把群里其他人的消息也吞掉。"""

    def filter(self, event) -> str:
        getter = getattr(event, "get_sender_id", None)
        sender = ""
        if callable(getter):
            try:
                sender = str(getter() or "")
            except Exception:
                sender = ""
        return f"{event.unified_msg_origin}:{sender}"


@dataclass
class Variant:
    index: int
    image: Path
    meta: ConfigMeta


class PetConfigHandler:
    WAIT_TIMEOUT: int = 60

    def __init__(self, data_dir: Path | str, uploaders=None, store=None):
        self._root = Path(data_dir) / "pet_configs"
        self._uploaders = {str(x).strip() for x in (uploaders or ()) if str(x).strip()}
        # store 支持「昵称=UID」写法，并能在运行时增删
        self._store = store
        # 强引用正在等待输入的会话任务，避免被垃圾回收
        self._pending: set[asyncio.Task] = set()

    def _spawn(self, coro) -> asyncio.Task:
        """把等待输入的逻辑丢到独立任务里跑。

        菜单是在 handler 里 yield 出去的；如果 pipeline 在发完消息后直接关闭
        这个异步生成器，生成器后面的代码根本不会执行，会话也就不会注册。
        所以先 create_task 把等待器挂起来（注册是同步完成的），再去 yield 菜单。
        """
        task = asyncio.get_running_loop().create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return task

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------------ #
    # 发送者信息
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

    @staticmethod
    def _sender_name(event) -> str:
        getter = getattr(event, "get_sender_name", None)
        if callable(getter):
            try:
                return str(getter() or "").strip()
            except Exception:
                return ""
        return ""

    SUB_COMMANDS = ("收录配置图", "配置备注", "配置图", "配置")

    def _full_arg(self, event, arg: str) -> str:
        """取完整参数。

        AstrBot 的 CommandFilter 对 ``arg: str`` 只会传第一个空格前的词，
        「收录配置图 艾夏拉 [作者:佑栖]」进到 handler 时 arg 只有「艾夏拉」，
        所以这里直接读整条消息文本，把指令词去掉。
        """
        text = ""
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            try:
                text = str(getter() or "").strip()
            except Exception:
                text = ""
        body = text.lstrip("/／").strip()
        for cmd in self.SUB_COMMANDS:
            if body.startswith(cmd):
                rest = body[len(cmd) :].strip()
                for junk in ("[图片]", "[Image]", "[image]"):
                    if rest.endswith(junk):
                        rest = rest[: -len(junk)].strip()
                return rest or (arg or "").strip()
        return (arg or "").strip()

    # ------------------------------------------------------------------ #
    # 收录权限
    # ------------------------------------------------------------------ #

    def can_upload(self, event) -> bool:
        """能不能收录配置图。

        依次判断：
        1. AstrBot 管理员（admins_id）
        2. 平台上报的群主/群管理员（aiocqhttp 这类适配器会给 event.role）
        3. 配置里的 ID 白名单（QQ 官方机器人不上报角色，只能靠它）
        """
        is_admin = getattr(event, "is_admin", None)
        if callable(is_admin):
            try:
                if is_admin():
                    return True
            except Exception:
                pass
        role = str(getattr(event, "role", "") or "").strip().lower()
        if role in {"admin", "owner"}:
            return True
        sender_id = self._sender_id(event)
        if not sender_id:
            return False
        if self._store is not None:
            return self._store.contains(sender_id)
        return sender_id in self._uploaders

    # ------------------------------------------------------------------ #
    # 存储：多套配置
    # ------------------------------------------------------------------ #

    def _pet_dir(self, pet_id: int) -> Path:
        return self._root / str(pet_id)

    def _legacy_image(self, pet_id: int) -> Path | None:
        for suffix in SUFFIXES:
            path = self._root / f"{pet_id}{suffix}"
            if path.is_file():
                return path
        return None

    def _legacy_meta(self, pet_id: int) -> ConfigMeta:
        path = self._root / f"{pet_id}.json"
        if not path.is_file():
            return ConfigMeta()
        try:
            return meta_loads(path.read_text(encoding="utf-8"))
        except Exception:
            return ConfigMeta()

    def _image_at(self, folder: Path, index: int) -> Path | None:
        for suffix in SUFFIXES:
            path = folder / f"{index}{suffix}"
            if path.is_file():
                return path
        return None

    def _meta_at(self, folder: Path, index: int) -> ConfigMeta:
        path = folder / f"{index}.json"
        if not path.is_file():
            return ConfigMeta()
        try:
            return meta_loads(path.read_text(encoding="utf-8"))
        except Exception:
            return ConfigMeta()

    def save_meta(self, pet_id: int, meta: ConfigMeta, index: int = 1) -> None:
        """兼容旧接口：写第 index 套的元数据。"""
        folder = self._pet_dir(pet_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{index}.json").write_text(meta_dumps(meta), encoding="utf-8")

    def load_meta(self, pet_id: int, index: int = 1) -> ConfigMeta:
        variants = self.variants(pet_id)
        for variant in variants:
            if variant.index == index:
                return variant.meta
        return variants[0].meta if variants else ConfigMeta()

    def variants(self, pet_id: int) -> list[Variant]:
        folder = self._pet_dir(pet_id)
        found: list[Variant] = []
        if folder.is_dir():
            indexes = {
                int(path.stem)
                for path in folder.iterdir()
                if path.is_file()
                and path.suffix.lower() in SUFFIXES
                and path.stem.isdigit()
            }
            for index in sorted(indexes):
                image = self._image_at(folder, index)
                if image is not None:
                    found.append(Variant(index, image, self._meta_at(folder, index)))
        if not found:
            legacy = self._legacy_image(pet_id)
            if legacy is not None:
                found.append(Variant(1, legacy, self._legacy_meta(pet_id)))
        return found

    def _migrate_legacy(self, pet_id: int) -> None:
        folder = self._pet_dir(pet_id)
        if folder.is_dir():
            return
        legacy = self._legacy_image(pet_id)
        if legacy is None:
            return
        folder.mkdir(parents=True, exist_ok=True)
        suffix = legacy.suffix.lower()
        if suffix not in SUFFIXES:
            suffix = ".png"
        shutil.copyfile(legacy, folder / f"1{suffix}")
        meta = self._legacy_meta(pet_id)
        (folder / "1.json").write_text(meta_dumps(meta), encoding="utf-8")
        legacy.unlink(missing_ok=True)
        (self._root / f"{pet_id}.json").unlink(missing_ok=True)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def _caption(self, pet, variant: Variant, total: int) -> str:
        head = f"🧩【{pet.name}配置"
        head += f" {variant.index}/{total}】" if total > 1 else "】"
        lines = [head, *variant.meta.lines()]
        return "\n".join(lines)

    def _components(self, pet, variant: Variant, total: int) -> list:
        return [
            Comp.Image.fromFileSystem(str(variant.image)),
            Comp.Plain(self._caption(pet, variant, total)),
        ]

    def _variant_menu(self, pet, variants: list[Variant]) -> str:
        lines = [f"🧩【{pet.name}】收录了 {len(variants)} 套配置，想看哪套？"]
        for variant in variants:
            author = variant.meta.author or "未署名"
            note = variant.meta.note or "（无备注）"
            lines.append(f"{variant.index}. {author} — {note}")
        lines.append("\n💬 输入序号查看 · 输入 0 退出")
        return "\n".join(lines)

    def _pet_menu(self, pets) -> str:
        lines = ["请问你想看哪只精灵的配置？"]
        for i, pet in enumerate(pets, 1):
            lines.append(f"{i}. {pet.name}（{pet.id}）")
        lines.append("\n💬 输入序号选择 · 输入 0 退出")
        return "\n".join(lines)

    @staticmethod
    def _split_index(arg: str) -> tuple[str, int | None]:
        text = (arg or "").strip()
        parts = text.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].isdigit():
            return parts[0].strip(), int(parts[1])
        return text, None

    async def _ask_pet(self, event, mapping: dict[str, object]):
        picked: dict[str, object] = {}

        @session_waiter(timeout=self.WAIT_TIMEOUT, record_history_chains=False)
        async def waiter(controller: SessionController, evt: AstrMessageEvent):
            text = evt.message_str.strip()
            if text == "0":
                await evt.send(evt.plain_result("❌已退出查询"))
                controller.stop()
                return
            pet = mapping.get(text)
            if pet is None:
                await evt.send(evt.plain_result("⚠️序号无效，请重新输入"))
                controller.keep(timeout=self.WAIT_TIMEOUT, reset_timeout=True)
                return
            picked["pet"] = pet
            controller.stop()

        try:
            await waiter(event, _SenderSessionFilter())
        except TimeoutError:
            await event.send(event.plain_result("⏰选择超时，已退出"))
        return picked.get("pet")

    async def _ask_variant(self, event, pet, mapping: dict[str, Variant], total: int):
        """选配置图。

        选完**不退出**，继续等下一个序号，方便连着看几套；
        输入 0 或超时才结束。会话按「群 + 人」隔离，不会影响别人聊天。
        """

        @session_waiter(timeout=self.WAIT_TIMEOUT, record_history_chains=False)
        async def waiter(controller: SessionController, evt: AstrMessageEvent):
            text = evt.message_str.strip()
            if text == "0":
                await evt.send(evt.plain_result("❌已退出查询"))
                controller.stop()
                return
            variant = mapping.get(text)
            if variant is None:
                await evt.send(evt.plain_result("⚠️序号无效，请重新输入"))
                controller.keep(timeout=self.WAIT_TIMEOUT, reset_timeout=True)
                return
            await evt.send(
                evt.chain_result(
                    [
                        *self._components(pet, variant, total),
                        Comp.Plain(f"\n（还能输入其他序号 · 共 {total} 套 · 0 退出）"),
                    ]
                )
            )
            controller.keep(timeout=self.WAIT_TIMEOUT, reset_timeout=True)

        try:
            await waiter(event, _SenderSessionFilter())
        except TimeoutError:
            await event.send(event.plain_result("⏰选择超时，已退出"))

    # 解析失败时给群友看的正确格式
    UPLOAD_HELP = (
        "📷 收录配置图的正确格式（图和指令要放在同一条消息里）：\n"
        "· 配置图 雷伊\n"
        "· 配置图 雷伊 作者:张三 备注:物攻流\n"
        "· 配置图 雷伊 物攻流（张三）\n"
        "· 配置图 雷伊（张三）\n"
        "\n精灵名和作者之间空格可有可无，备注也可以不写。"
    )

    @staticmethod
    def _trim_candidates(name: str):
        """从长到短给出候选名字，用于「雷伊物攻流」这种名字和备注连写的兜底。"""
        text = (name or "").strip()
        if not text:
            return
        yield text
        for cut in range(len(text) - 1, 1, -1):
            yield text[:cut]

    def _resolve_pets(self, name: str):
        """查精灵；查不到就逐字削尾再试。返回 (pets, 实际命中的名字)。"""
        sessions = db_manager.get_all_sessions()
        for cand in self._trim_candidates(name):
            try:
                pets = PetDataGetter(sessions, cand)
            except Exception:
                pets = None
            if pets:
                if cand != name:
                    logger.info(f"配置图名字回退匹配: {name!r} -> {cand!r}")
                return pets, cand
        return [], name

    async def query(self, event: AstrMessageEvent, arg: str = ""):
        if not db_manager.is_database_loaded("seerapi"):
            yield event.plain_result("❌数据库未加载，请稍后再试")
            return
        name, index = self._split_index(self._full_arg(event, arg) or arg)
        if not name:
            yield event.plain_result("❌用法：/配置图 <精灵名或ID>")
            return
        pets, name = self._resolve_pets(name)
        if not pets:
            yield event.plain_result(f"❌未找到匹配的精灵: {name}")
            return
        if len(pets) > 20:
            yield event.plain_result("❌重名超过20个，请重新检索关键词！")
            return

        pet = pets[0]
        if len(pets) > 1:
            # 同名/多形态一律弹面板让群友选，不再悄悄取「完全同名」那只。
            # 以前「配置 雷伊」会直接跳过面板回 70 号那只，和别的查询指令行为不一致。
            mapping = {str(i + 1): p for i, p in enumerate(pets)}
            task = self._spawn(self._ask_pet(event, mapping))
            yield event.plain_result(self._pet_menu(pets))
            picked = await task
            if picked is None:
                return
            pet = picked

        variants = self.variants(int(pet.id))
        if not variants:
            yield event.plain_result(
                f"❌暂未收录精灵 {pet.name}（{pet.id}）的配置图。"
            )
            return

        if index is not None:
            for variant in variants:
                if variant.index == index:
                    yield event.chain_result(
                        self._components(pet, variant, len(variants))
                    )
                    return
            yield event.plain_result(
                f"❌【{pet.name}】没有第 {index} 套配置（共 {len(variants)} 套）。"
            )
            return

        if len(variants) == 1:
            yield event.chain_result(self._components(pet, variants[0], 1))
            return

        mapping = {str(variant.index): variant for variant in variants}
        task = self._spawn(self._ask_variant(event, pet, mapping, len(variants)))
        yield event.plain_result(self._variant_menu(pet, variants))
        await task

    # ------------------------------------------------------------------ #
    # 收录
    # ------------------------------------------------------------------ #

    @staticmethod
    def _prefer_exact(pets, name: str):
        """多个匹配时，优先取完全同名的那个（「雷伊」→ 雷伊(70)）。"""
        key = _name_key(name)
        for pet in pets:
            if _name_key(str(pet.name)) == key:
                return pet
        return None

    @staticmethod
    def _ambiguous_text(name: str, pets) -> str:
        lines = [f"❌「{name}」匹配到 {len(pets)} 只精灵，请写完整名字或用 ID 重发："]
        for pet in pets[:20]:
            lines.append(f"{pet.id}. {pet.name}")
        if len(pets) > 20:
            lines.append(f"……共 {len(pets)} 只，只列前 20 条")
        return "\n".join(lines)

    @staticmethod
    def _pick_image(event: AstrMessageEvent):
        """从消息里找图片组件（鸭子类型，兼容不同 AstrBot 版本）。"""
        getter = getattr(event, "get_messages", None)
        messages = getter() if callable(getter) else []
        for comp in messages or []:
            if callable(getattr(comp, "convert_to_file_path", None)):
                return comp
        return None

    @staticmethod
    def _resolve_author(meta: ConfigMeta, sender_name: str, sender_id: str) -> str:
        return (
            meta.author
            or clean_text(sender_name)
            or (sender_id[-6:] if sender_id else "匿名")
        )

    async def save(self, event: AstrMessageEvent, arg: str = ""):
        if not self.can_upload(event):
            yield event.plain_result(
                "❌你没有收录配置图的权限。\n"
                "让 AstrBot 管理员把你的 ID 加进插件配置「收录配置图白名单」"
                "（发 /sid 查看自己的 ID）。"
            )
            return
        name, meta = parse_upload_arg(self._full_arg(event, arg))
        if not name:
            yield event.plain_result(self.UPLOAD_HELP)
            return
        if not db_manager.is_database_loaded("seerapi"):
            yield event.plain_result("❌数据库未加载，请稍后再试")
            return

        pets, name = self._resolve_pets(name)
        if not pets:
            yield event.plain_result(
                f"❌没认出精灵名：{name}\n\n" + self.UPLOAD_HELP
            )
            return
        if len(pets) > 1:
            exact = self._prefer_exact(pets, name)
            if exact is None:
                yield event.plain_result(self._ambiguous_text(name, pets))
                return
            pets = [exact]
        pet = pets[0]
        pet_id = int(pet.id)

        image = self._pick_image(event)
        if image is None:
            yield event.plain_result(
                "❌消息里没有图片，图要和指令放在同一条消息里。\n\n" + self.UPLOAD_HELP
            )
            return

        try:
            src = Path(await image.convert_to_file_path())
        except Exception as e:
            logger.warning(f"配置图读取失败: {e}")
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

        sender_id = self._sender_id(event)
        author = self._resolve_author(meta, self._sender_name(event), sender_id)

        try:
            await asyncio.to_thread(self._migrate_legacy, pet_id)
            existing = self.variants(pet_id)
            author_key = _ni(author)
            target_index = None
            for variant in existing:
                if _ni(variant.meta.author) == author_key:
                    target_index = variant.index
                    break
            replaced = target_index is not None
            if target_index is None:
                target_index = (max(v.index for v in existing) + 1) if existing else 1

            folder = self._pet_dir(pet_id)
            folder.mkdir(parents=True, exist_ok=True)
            for old in folder.glob(f"{target_index}.*"):
                if old.suffix.lower() in SUFFIXES:
                    old.unlink(missing_ok=True)

            final = ConfigMeta(
                author=author,
                note=meta.note,
                uploader=sender_id,
                updated_at=meta.updated_at,
            )
            await asyncio.to_thread(shutil.copyfile, src, folder / f"{target_index}{suffix}")
            (folder / f"{target_index}.json").write_text(
                meta_dumps(final), encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"配置图保存失败: {e}")
            yield event.plain_result(f"❌保存失败: {e}")
            return

        total = len(self.variants(pet_id))
        action = "已替换" if replaced else "已收录"
        detail = "，".join(final.lines()) if final.lines() else "未填写作者/备注"
        yield event.plain_result(
            f"✅{action}【{pet.name}（{pet_id}）】第 {target_index} 套配置"
            f"（{size / 1024:.0f} KB，共 {total} 套）\n{detail}"
        )

    async def remark(self, event: AstrMessageEvent, arg: str = ""):
        """只改某套配置的作者/备注，不动图片。"""
        if not self.can_upload(event):
            yield event.plain_result(
                "❌你没有修改配置图备注的权限（白名单或 AstrBot 管理员）。"
            )
            return
        name, patch = parse_upload_arg(self._full_arg(event, arg))
        if not name:
            yield event.plain_result(
                "❌用法：/配置备注 <精灵名或ID> [作者:某某] [备注:文字]"
            )
            return
        if not db_manager.is_database_loaded("seerapi"):
            yield event.plain_result("❌数据库未加载，请稍后再试")
            return
        pets = PetDataGetter(db_manager.get_all_sessions(), name)
        if not pets:
            yield event.plain_result(f"❌未找到匹配的精灵: {name}")
            return
        if len(pets) > 1:
            exact = self._prefer_exact(pets, name)
            if exact is None:
                yield event.plain_result(self._ambiguous_text(name, pets))
                return
            pets = [exact]
        pet = pets[0]
        pet_id = int(pet.id)
        variants = self.variants(pet_id)
        if not variants:
            yield event.plain_result(
                f"❌精灵 {pet.name}（{pet_id}）还没有收录配置图，先发图收录吧。"
            )
            return
        if not patch.author and not patch.note:
            yield event.plain_result(
                "❌没有要修改的内容，试试「作者:张三」或「备注:物攻流」。"
            )
            return

        target = None
        if patch.author:
            for variant in variants:
                if _ni(variant.meta.author) == _ni(patch.author):
                    target = variant
                    break
        elif len(variants) == 1:
            target = variants[0]

        if target is None:
            listing = "、".join(
                f"{v.index}.{v.meta.author or '未署名'}" for v in variants
            )
            hint = (
                f"❌没找到作者为「{patch.author}」的那套配置。现有：{listing}"
                if patch.author
                else f"❌这只精灵有 {len(variants)} 套配置，请带「作者:某某」指定要改哪套。现有：{listing}"
            )
            yield event.plain_result(hint)
            return

        meta = merge_meta(target.meta, author=patch.author, note=patch.note)
        try:
            folder = self._pet_dir(pet_id)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{target.index}.json").write_text(
                meta_dumps(meta), encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"配置图备注保存失败: {e}")
            yield event.plain_result(f"❌保存失败: {e}")
            return
        yield event.plain_result(
            f"✅已更新【{pet.name}（{pet_id}）】第 {target.index} 套配置的备注\n"
            + "\n".join(meta.lines())
        )
