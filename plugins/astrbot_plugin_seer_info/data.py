import asyncio
import os
import re
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Final, Generic, Literal, Protocol, TypeVar

import aiohttp
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from pypinyin import lazy_pinyin
from seerapi_models import (
    BattleEffectORM,
    ElementTypeORM,
    EquipORM,
    GemORM,
    MintmarkORM,
    PetORM,
    PetSkinORM,
    SuitORM,
    TitlePartORM,
    TypeCombinationORM,
)
from seerapi_models.build_model import BaseResModel
from sqlalchemy import text
from sqlalchemy.engine.base import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Field, SQLModel, and_, col, create_engine, func, or_, select
from sqlmodel import Session as SQLModelSession

from .core.type_calc import invalidate_relation_cache

_ALIASES_DB = "aliases"


class PetAliasORM(SQLModel, table=True):
    __tablename__ = "pet_aliases"
    __table_args__ = {"extend_existing": True}
    name: str = Field(primary_key=True)
    target_id: int = Field(primary_key=True)


class AliasModelProtocol(Protocol):
    name: str
    target_id: int


def get_plugin_db_path(db_name: str) -> str:
    plugin_data_path = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_seer_info"
    plugin_data_path.mkdir(parents=True, exist_ok=True)
    return str(plugin_data_path / f"{db_name}.sqlite")


class DatabaseManager:
    def __init__(self):
        self._engines: dict[str, Engine] = {}
        self._sessions: dict[str, SQLModelSession] = {}
        self._post_load_hooks: dict[str, list[Callable]] = {}

    @staticmethod
    def _create_memory_engine() -> Engine:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        _register_strip_func(engine)
        return engine

    def register_post_load_hook(self, name: str, hook: Callable[[Engine], None]) -> None:
        self._post_load_hooks.setdefault(name, []).append(hook)

    def load_from_file(self, name: str, file_path: str) -> None:
        new_engine = self._create_memory_engine()
        source = sqlite3.connect(file_path)
        try:
            raw_conn = new_engine.raw_connection()
            try:
                source.backup(raw_conn.dbapi_connection)
            finally:
                raw_conn.close()
        finally:
            source.close()

        for hook in self._post_load_hooks.get(name, []):
            try:
                hook(new_engine)
            except Exception:
                logger.warning(f"数据库 '{name}' 的 post-load 钩子执行失败")

        old_session = self._sessions.pop(name, None)
        if old_session is not None:
            old_session.close()
        old_engine = self._engines.get(name)
        self._engines[name] = new_engine
        if old_engine is not None:
            old_engine.dispose()
        if name == "seerapi":
            invalidate_relation_cache()
        logger.info(f"已从文件导入数据到内存数据库 '{name}'")

    def get_engine(self, name: str) -> Engine | None:
        return self._engines.get(name)

    def get_session(self, name: str) -> SQLModelSession | None:
        engine = self._engines.get(name)
        if engine is None:
            return None
        if name not in self._sessions:
            self._sessions[name] = SQLModelSession(engine)
        return self._sessions[name]

    def get_all_sessions(self) -> dict[str, SQLModelSession]:
        return {name: self.get_session(name) for name in self._engines}

    @property
    def registered_names(self) -> list[str]:
        return list(self._engines.keys())

    def is_database_loaded(self, name: str) -> bool:
        return name in self._engines

    def dispose_all(self) -> None:
        for s in self._sessions.values():
            s.close()
        self._sessions.clear()
        for name, engine in self._engines.items():
            engine.dispose()
            logger.info(f"已释放数据库引擎 '{name}'")
        self._engines.clear()
        invalidate_relation_cache()


db_manager: Final[DatabaseManager] = DatabaseManager()
_sync_tasks: dict[str, asyncio.Task] = {}
_sync_specs: dict[str, tuple[str, Callable | None]] = {}
_sync_locks: dict[str, asyncio.Lock] = {}
SyncResult = Literal["updated", "skipped", "failed"]


def register_database(
    name: str,
    *,
    sync_url: str,
    sync_interval_minutes: int = 60,
    get_fingerprint: Callable[[aiohttp.ClientSession], Any] | None = None,
):
    _sync_specs[name] = (sync_url, get_fingerprint)

    # 先用本地文件顶上，再让后台任务检查更新。
    # 远端有更新时要下载几十 MB（实测 ~120 KB/s，25 MB 约 3.6 分钟），
    # 这段时间里 is_database_loaded() 一直是 False，用户会收到「数据库未加载」；
    # 下载失败时更是永远等不到数据。先加载本地旧库可以把这个窗口降到 0。
    if not db_manager.is_database_loaded(name):
        try:
            local_path = get_plugin_db_path(name)
            if Path(local_path).exists():
                db_manager.load_from_file(name, local_path)
                logger.info(f"已用本地数据先行加载数据库 '{name}'，后台继续检查更新")
        except Exception as e:
            logger.warning(f"本地数据库 '{name}' 预加载失败: {e}")

    async def sync_task():
        try:
            while True:
                t0 = time.monotonic()
                await sync_database(name)
                elapsed = time.monotonic() - t0
                sleep_sec = max(sync_interval_minutes * 60 - elapsed, 0)
                logger.info(f"数据库 '{name}' 将在 {sleep_sec / 60:.1f} 分钟后再次检查")
                await asyncio.sleep(sleep_sec)
        except asyncio.CancelledError:
            raise

    _sync_tasks[name] = asyncio.create_task(sync_task())


async def cancel_sync_tasks() -> None:
    tasks = list(_sync_tasks.values())
    _sync_tasks.clear()
    if not tasks:
        return
    pending = [t for t in tasks if not t.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def register_local_database(name: str):
    _sync_specs.pop(name, None)
    file_path = get_plugin_db_path(name)
    if not Path(file_path).exists():
        logger.warning(f"本地文件 '{file_path}' 不存在，跳过注册 {name}")
        return
    db_manager.load_from_file(name, file_path)


async def sync_database(
    name: str,
    sync_url: str | None = None,
    get_fingerprint: Callable | None = None,
) -> SyncResult | None:
    if sync_url is None:
        spec = _sync_specs.get(name)
        if spec is None:
            return None
        sync_url, get_fingerprint = spec
    if not sync_url:
        return "failed"

    lock = _sync_locks.setdefault(name, asyncio.Lock())
    async with lock:
        return await _sync_database_locked(name, sync_url, get_fingerprint)


# ── 下载内容闸门 ────────────────────────────────────────────────
# 背景（2026-11-02 事故）：上游 seerapi-data-latest 某次发布的包少了 34 张表
# （pet_special_effect / soulmark_icon / effect_description 等），同步逻辑不带校验
# 直接 write_bytes 覆盖本地库，导致 pet_effects() 查表抛错被 _q 静默吞成空列表，
# 「失体」这类专属名词校验连同真名词一起消失，且毫无报错。
# 这里在落盘前验货：体积、能否解析、必备表是否齐全、pet 行数兜底。不合格就丢弃。
_DB_REQUIRED_TABLES: Final = {
    "seerapi": (
        "pet",
        "skill",
        "soulmark",
        "skilleffectlink",
        "pet_special_effect",
        "soulmark_icon",
    ),
    "aliases": ("pet_aliases",),
}
_DB_MIN_BYTES: Final = {
    "seerapi": 30 * 1024 * 1024,
    "aliases": 10 * 1024,
}


def _apply_local_alias_overlay(candidate_path: str, overlay_path: str) -> tuple[bool, str]:
    """Merge explicitly local alias rows into an upstream aliases candidate."""
    if not os.path.exists(overlay_path):
        return True, ""
    tables = ("mintmark_aliases", "mintmark_class_aliases", "mintmark_series_members")
    try:
        overlay = sqlite3.connect(f"file:{overlay_path}?mode=ro", uri=True)
        candidate = sqlite3.connect(candidate_path)
        try:
            candidate.execute("begin immediate")
            overlay_tables = {
                row[0] for row in overlay.execute(
                    "select name from sqlite_master where type='table'"
                )
            }
            candidate_tables = {
                row[0] for row in candidate.execute(
                    "select name from sqlite_master where type='table'"
                )
            }
            for table in tables:
                if table not in overlay_tables:
                    continue
                if table not in candidate_tables:
                    candidate.execute(
                        f"create table [{table}] (name TEXT NOT NULL, target_id INTEGER NOT NULL, PRIMARY KEY(name,target_id))"
                    )
                rows = overlay.execute(f"select name,target_id from [{table}]").fetchall()
                candidate.executemany(
                    f"insert or replace into [{table}] (name,target_id) values (?,?)", rows
                )
            candidate.commit()
            check = candidate.execute("pragma integrity_check").fetchone()[0]
            if check != "ok":
                return False, f"overlay integrity check failed: {check}"
        finally:
            overlay.close()
            candidate.close()
        return True, ""
    except Exception as exc:
        return False, f"local alias overlay failed: {exc}"


def _local_alias_overlay_path() -> str:
    """本地自用别名的 overlay 库（与上游 aliases.sqlite 分开存，更新时不会被丢）。"""
    return str(Path(get_plugin_db_path("aliases")).with_name("aliases_local_overlay.sqlite"))


def _merge_alias_overlay_bytes(data: bytes, overlay_path: str) -> bytes:
    """把 overlay 合并进下载到的 aliases 字节流；失败时返回上游原样。"""
    if not os.path.exists(overlay_path):
        return data
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        Path(tmp).write_bytes(data)
        ok, why = _apply_local_alias_overlay(tmp, overlay_path)
        if not ok:
            logger.warning(f"别名 overlay 合并失败，本次按上游原样落盘：{why}")
            return data
        return Path(tmp).read_bytes()
    except Exception as exc:
        logger.warning(f"别名 overlay 合并异常，本次按上游原样落盘：{exc}")
        return data
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _validate_downloaded_db(name: str, data: bytes) -> tuple[bool, str]:
    """校验下载到的库是否完整可用。返回 (是否通过, 不合格原因)。"""
    min_bytes = _DB_MIN_BYTES.get(name, 0)
    if min_bytes and len(data) < min_bytes:
        return False, f"体积仅 {len(data) / 1048576:.1f}MB，低于下限 {min_bytes / 1048576:.1f}MB"

    required = _DB_REQUIRED_TABLES.get(name)
    if not required:
        return True, ""

    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        Path(tmp).write_bytes(data)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            have = {
                r[0]
                for r in con.execute(
                    "select name from sqlite_master where type='table'"
                ).fetchall()
            }
            missing = [t for t in required if t not in have]
            if missing:
                return False, f"缺表 {missing}"
            if "pet" in have:
                n_pet = con.execute("select count(*) from pet").fetchone()[0]
                if n_pet < 1000:
                    return False, f"pet 表只有 {n_pet} 行"
        finally:
            con.close()
        return True, ""
    except Exception as exc:
        return False, f"无法解析为库：{exc}"
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


async def _sync_database_locked(
    name: str,
    sync_url: str,
    get_fingerprint: Callable | None,
) -> SyncResult:

    plugin_db_path = get_plugin_db_path(name)
    plugin_db_file = Path(plugin_db_path)
    sha256_path = plugin_db_path + ".sha256"

    try:
        async with aiohttp.ClientSession() as session:
            db_exists = await asyncio.to_thread(plugin_db_file.exists)
            if db_exists and get_fingerprint:
                try:
                    remote_fingerprint = await get_fingerprint(session)
                except Exception as exc:
                    # 2026-11-04（四号鱼）：区分「网络到不了」和「拿到了但不对」。
                    # Clash TUN / 断网下每分钟都会失败一次，降级用本地数据本来就是正常路径，
                    # 一律 warning 只会把真问题刷走（实测今天 18:07/19:29/19:30 各刷一次）。
                    transient = isinstance(
                        exc, (aiohttp.ClientConnectorError, asyncio.TimeoutError, TimeoutError)
                    ) or "timeout" in type(exc).__name__.lower()
                    if transient:
                        logger.debug(
                            "数据库 '%s' 指纹检查暂时不可达（%s），沿用本地数据",
                            name,
                            type(exc).__name__,
                        )
                    else:
                        logger.warning(
                            "数据库 '%s' 指纹检查失败（%s: %s），沿用本地数据",
                            name,
                            type(exc).__name__,
                            exc,
                        )
                    if not db_manager.is_database_loaded(name):
                        await asyncio.to_thread(db_manager.load_from_file, name, plugin_db_path)
                    return "failed"

                local_fingerprint = None
                sha256_exists = await asyncio.to_thread(Path(sha256_path).exists)
                if sha256_exists:
                    text_data = await asyncio.to_thread(Path(sha256_path).read_text)
                    local_fingerprint = text_data.strip()

                if remote_fingerprint and remote_fingerprint == local_fingerprint:
                    if db_manager.is_database_loaded(name):
                        logger.info(f"数据库 '{name}' 指纹未变化，跳过更新")
                    else:
                        logger.info(f"数据库 '{name}' 指纹未变化，使用本地数据")
                        await asyncio.to_thread(db_manager.load_from_file, name, plugin_db_path)
                    return "skipped"

            logger.info(f"开始下载数据库 '{name}'...")
            async with session.get(sync_url, allow_redirects=True) as resp:
                resp.raise_for_status()
                data = await resp.read()

            # 落盘前先验货，不合格就整包丢弃，保住本地旧库（见 _validate_downloaded_db）
            ok, why = await asyncio.to_thread(_validate_downloaded_db, name, data)
            if not ok:
                logger.error(
                    f"数据库 '{name}' 下载内容不合格（{why}），已丢弃，继续使用本地旧库"
                )
                if not db_manager.is_database_loaded(name) and await asyncio.to_thread(
                    plugin_db_file.exists
                ):
                    await asyncio.to_thread(db_manager.load_from_file, name, plugin_db_path)
                return "failed"

            # aliases：把自用别名 overlay 合并进候选库，再落盘。
            # 上游指纹仍按远端写回，所以下次比对不会因为我们合并过而反复重下。
            if name == "aliases":
                data = await asyncio.to_thread(
                    _merge_alias_overlay_bytes, data, _local_alias_overlay_path()
                )

            # 覆盖前留一份旧库，方便回滚
            if await asyncio.to_thread(plugin_db_file.exists):
                backup = plugin_db_path + time.strftime(".bak-%Y%m%d-%H%M%S")
                try:
                    await asyncio.to_thread(shutil.copy2, plugin_db_path, backup)
                    logger.info(f"已备份旧库: {backup}")
                except Exception as exc:
                    logger.warning(f"备份旧库失败: {exc}")

            await asyncio.to_thread(plugin_db_file.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(plugin_db_file.write_bytes, data)

            if get_fingerprint:
                try:
                    remote_fp = await get_fingerprint(session)
                    await asyncio.to_thread(Path(sha256_path).write_text, remote_fp.strip())
                    logger.info(f"已保存指纹: {remote_fp.strip()}")
                except Exception as e:
                    logger.warning(f"保存 {name} 指纹失败: {e}")

            logger.info(f"数据库 '{name}' 已下载，大小: {len(data) / (1024 * 1024):.2f} MB")
            db_manager.load_from_file(name, plugin_db_path)
            return "updated"

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"数据库 '{name}' 同步失败: {e}")
        return "failed"


_IGNORED_CHARS = ".·・•‧∙⋅。—–-_/ "
_STRIP_SPECIAL_RE = re.compile(f"[{re.escape(_IGNORED_CHARS)}]")


def _strip_special(text: str) -> str:
    return _STRIP_SPECIAL_RE.sub("", text)


def _register_strip_func(engine: Engine) -> None:
    def _sqlite_strip(text):
        if text is None:
            return None
        for ch in _IGNORED_CHARS:
            text = text.replace(ch, "")
        return text

    with engine.connect() as conn:
        conn.connection.connection.create_function("_strip", 1, _sqlite_strip)
        conn.commit()


def _col_strip_special(column: Any) -> Any:
    return func._strip(column)


_T_Model = TypeVar("_T_Model", bound=BaseResModel)


class IdResolver(Generic[_T_Model]):
    def __init__(self, model: type[_T_Model], *, db_name: str = "seerapi"):
        self.model = model
        self.db_name = db_name

    def __call__(self, sessions: dict, arg: str) -> tuple[_T_Model] | tuple:
        if not arg.isdigit():
            return ()
        session = sessions.get(self.db_name)
        if session is None:
            return ()
        obj = session.get(self.model, int(arg))
        return (obj,) if obj else ()


class NameResolver(Generic[_T_Model]):
    def __init__(
        self,
        model: type[_T_Model],
        *,
        db_name: str = "seerapi",
        name_column: str = "name",
    ):
        if not hasattr(model, name_column):
            logger.warning(f"Model {model} has no column {name_column}")
        self.model = model
        self.db_name = db_name
        self.name_column = getattr(model, name_column, None)

    def __call__(self, sessions: dict, arg: str) -> Iterable[_T_Model]:
        if not self.name_column:
            return ()
        session = sessions.get(self.db_name)
        if session is None:
            return ()
        stripped_arg = _strip_special(arg)
        statement = select(self.model).where(
            _col_strip_special(col(self.name_column)).like(f"%{stripped_arg}%")
        )
        return session.exec(statement).all()


class AliasResolver(Generic[_T_Model]):
    def __init__(
        self,
        model: type[_T_Model],
        alias_model: type[AliasModelProtocol],
        *,
        alias_db: str = _ALIASES_DB,
        data_db: str = "seerapi",
    ):
        self.model = model
        self.alias_model = alias_model
        self.alias_db = alias_db
        self.data_db = data_db

    def __call__(self, sessions: dict, arg: str) -> Iterable[_T_Model]:
        alias_session = sessions.get(self.alias_db)
        if alias_session is None:
            return ()
        stripped_arg = _strip_special(arg)
        try:
            statement = select(self.alias_model).where(
                _col_strip_special(col(self.alias_model.name)).like(f"%{stripped_arg}%")
            )
            aliases = alias_session.exec(statement).all()
            ids = {alias.target_id for alias in aliases}
        except Exception:
            return ()
        if not ids:
            return ()
        data_session = sessions.get(self.data_db)
        if data_session is None:
            return ()
        return data_session.exec(select(self.model).where(col(self.model.id).in_(ids))).all()


_PINYIN_FTS_TABLE = "pinyin_fts"
_PINYIN_FTS_SOURCES: dict[str, str] = {
    "pet": "SELECT id, name FROM pet",
    "pet_skins": "SELECT id, name FROM pet_skins",
}


class PinyinResolver(Generic[_T_Model]):
    @staticmethod
    def _to_pinyin_needle(arg: str) -> tuple[str, list[str] | None] | None:
        stripped = _strip_special(arg)
        if stripped.isascii():
            if not stripped.isalpha():
                return None
            return (stripped.lower(), None)
        syllables = [s.lower() for s in lazy_pinyin(stripped)]
        needle = "".join(syllables)
        return (needle, syllables)

    def __init__(
        self,
        model: type[_T_Model],
        *,
        source_table: str,
        db_name: str = "seerapi",
    ):
        self.model = model
        self.source_table = source_table
        self.db_name = db_name

    def __call__(self, sessions: dict, arg: str) -> Iterable[_T_Model]:
        needle_data = self._to_pinyin_needle(arg)
        if not needle_data:
            return ()
        needle, input_syllables = needle_data
        session = sessions.get(self.db_name)
        if session is None:
            return ()
        try:
            with session.connection().engine.connect() as conn:
                result = conn.execute(
                    text(
                        f"SELECT rowid FROM [{_PINYIN_FTS_TABLE}] "
                        "WHERE source_table = :src "
                        "AND (pinyin_full MATCH :q OR pinyin_initials MATCH :q)"
                    ),
                    {"src": self.source_table, "q": f'"{needle}"'},
                )
                rowids = [row[0] for row in result.fetchall()]
        except Exception:
            return ()
        if not rowids:
            return ()
        return session.exec(select(self.model).where(col(self.model.id).in_(rowids))).all()


class Getter(Generic[_T_Model]):
    def __init__(
        self,
        model: type[_T_Model],
        *resolvers,
        filter_func: Callable[[_T_Model], bool] | None = None,
    ):
        self.model = model
        self.resolvers = resolvers
        self.filter_func = filter_func

    def get(self, session: SQLModelSession, id_: int) -> _T_Model | None:
        return session.get(self.model, id_)

    def __call__(self, sessions: dict, arg: str) -> tuple[_T_Model, ...]:
        if not arg:
            return ()
        seen: dict[int, _T_Model] = {}
        for resolver in self.resolvers:
            for obj in resolver(sessions, arg):
                seen.setdefault(obj.id, obj)
        results = tuple(seen.values())
        if self.filter_func:
            results = tuple(obj for obj in results if self.filter_func(obj))
        return results


PetDataGetter = Getter(
    PetORM,
    IdResolver(PetORM),
    NameResolver(PetORM),
    AliasResolver(PetORM, PetAliasORM),
    PinyinResolver(PetORM, source_table="pet"),
)

PetSkinDataGetter = Getter(
    PetSkinORM,
    IdResolver(PetSkinORM),
    NameResolver(PetSkinORM),
    PinyinResolver(PetSkinORM, source_table="pet_skins"),
)

MintmarkDataGetter = Getter(
    MintmarkORM,
    IdResolver(MintmarkORM),
    NameResolver(MintmarkORM),
    filter_func=lambda mm: not getattr(mm, "connected_universal_parts", None),
)

GemDataGetter = Getter(
    GemORM,
    IdResolver(GemORM),
    NameResolver(GemORM),
)

SuitDataGetter = Getter(
    SuitORM,
    IdResolver(SuitORM),
    NameResolver(SuitORM),
)

EquipDataGetter = Getter(
    EquipORM,
    IdResolver(EquipORM),
    NameResolver(EquipORM),
)


class TypeCombinationResolver:
    def __init__(self, *, db_name: str = "seerapi"):
        self.db_name = db_name

    def __call__(self, sessions: dict, arg: str) -> Iterable[TypeCombinationORM]:
        session = sessions.get(self.db_name)
        if session is None:
            logger.warning("TypeCombinationResolver: 未找到数据库会话")
            return ()
        stripped = _strip_special(arg)
        if not stripped:
            return ()
        all_types = session.exec(select(ElementTypeORM)).all()
        name_to_id: dict[str, int] = {t.name: t.id for t in all_types}
        if stripped in name_to_id:
            tid = name_to_id[stripped]
            results = list(
                session.exec(
                    select(TypeCombinationORM).where(
                        TypeCombinationORM.primary_id == tid,
                        TypeCombinationORM.secondary_id is None,
                    )
                ).all()
            )
            if results:
                return results
        found: dict[int, TypeCombinationORM] = {}
        for i in range(1, len(stripped)):
            left, right = stripped[:i], stripped[i:]
            if left not in name_to_id or right not in name_to_id:
                continue
            a, b = name_to_id[left], name_to_id[right]
            combos = session.exec(
                select(TypeCombinationORM).where(
                    or_(
                        and_(
                            TypeCombinationORM.primary_id == a,
                            TypeCombinationORM.secondary_id == b,
                        ),
                        and_(
                            TypeCombinationORM.primary_id == b,
                            TypeCombinationORM.secondary_id == a,
                        ),
                    )
                )
            ).all()
            for combo in combos:
                found.setdefault(combo.id, combo)
        return tuple(found.values())


TypeCombinationDataGetter = Getter(
    TypeCombinationORM,
    IdResolver(TypeCombinationORM),
    NameResolver(TypeCombinationORM),
    TypeCombinationResolver(),
)

BattleEffectDataGetter = Getter(
    BattleEffectORM,
    IdResolver(BattleEffectORM),
    NameResolver(BattleEffectORM),
)

TitleDataGetter = Getter(
    TitlePartORM,
    IdResolver(TitlePartORM),
    NameResolver(TitlePartORM),
)


def _build_pinyin_fts(engine: Engine) -> None:
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS pinyin_fts USING fts5("
                    "source_table, pinyin_full, pinyin_initials, "
                    "tokenize='trigram')"
                )
            )
            existing_tables = {
                row[0]
                for row in conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                ).fetchall()
            }
            for source_table, query in _PINYIN_FTS_SOURCES.items():
                if source_table not in existing_tables:
                    continue
                try:
                    rows = conn.execute(text(query)).fetchall()
                    for row_id, name in rows:
                        syllables = lazy_pinyin(_strip_special(name))
                        full = "".join(syllables)
                        initials = "".join(s[0] for s in syllables if s)
                        conn.execute(
                            text(
                                f"INSERT INTO [{_PINYIN_FTS_TABLE}]"
                                " (rowid, source_table, pinyin_full, pinyin_initials)"
                                " VALUES (:rowid, :src, :full, :initials)"
                            ),
                            {
                                "rowid": row_id,
                                "src": source_table,
                                "full": full,
                                "initials": initials,
                            },
                        )
                except Exception as e:
                    logger.error(f"拼音 FTS 索引填充失败 for {source_table}: {e}")
            conn.commit()
    except Exception as e:
        logger.error(f"拼音 FTS 索引构建失败: {e}")


db_manager.register_post_load_hook("seerapi", _build_pinyin_fts)


# --------------------------------------------------------------------------- #
# 用户自定义外号
#
# 远程同步会把 aliases 库整个换掉，所以自定义外号不能直接写文件：
# 写进内存库，并挂一个 post-load 钩子，每次重新加载后自动补回来。
# --------------------------------------------------------------------------- #

_custom_pet_aliases: dict[str, str] = {}


def register_custom_aliases(mapping: dict[str, str], *, apply_now: bool = True) -> int:
    """登记配置里的自定义外号，返回立即生效的条数。"""
    _custom_pet_aliases.clear()
    _custom_pet_aliases.update(
        {
            str(alias).strip(): str(target).strip()
            for alias, target in (mapping or {}).items()
            if str(alias).strip() and str(target).strip()
        }
    )
    if not _custom_pet_aliases:
        return 0
    db_manager.register_post_load_hook(_ALIASES_DB, _apply_custom_aliases)
    if apply_now:
        return apply_custom_aliases()
    return 0


def apply_custom_aliases() -> int:
    """把自定义外号写进当前内存库（aliases 库还没加载时返回 0）。"""
    engine = db_manager.get_engine(_ALIASES_DB)
    if engine is None:
        return 0
    return _apply_custom_aliases(engine)


def _resolve_alias_target(engine: Engine, target: str) -> int | None:
    if target.isdigit():
        return int(target)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("select id, name from pet where name like :q"),
                {"q": f"%{target}%"},
            ).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    stripped = _strip_special(target)
    for row in rows:
        if _strip_special(str(row[1])) == stripped:
            return int(row[0])
    return int(rows[0][0])


def _apply_custom_aliases(engine: Engine) -> int:
    if not _custom_pet_aliases:
        return 0
    seerapi = db_manager.get_engine("seerapi")
    if seerapi is None:
        return 0
    applied = 0
    try:
        with engine.begin() as conn:
            for alias, target in _custom_pet_aliases.items():
                pet_id = _resolve_alias_target(seerapi, target)
                if pet_id is None:
                    logger.warning(f"自定义外号「{alias}」的目标「{target}」没找到，已跳过")
                    continue
                conn.execute(
                    text(
                        "insert or replace into pet_aliases (name, target_id) "
                        "values (:name, :target_id)"
                    ),
                    {"name": alias, "target_id": pet_id},
                )
                applied += 1
    except Exception as e:
        logger.error(f"写入自定义外号失败: {e}")
        return applied
    if applied:
        logger.info(f"已加载 {applied} 条自定义外号")
    return applied
