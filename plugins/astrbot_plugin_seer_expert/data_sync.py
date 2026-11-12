# -*- coding: utf-8 -*-
"""seerapi / aliases 两个同步库的下载与更新。

从 seer_info 解耦出来，让 seer_expert 自己维护这两个库，
不再依赖第三方插件（astrbot_plugin_seer_info，GPL v3 改造版）。

设计参照 manual_sync.py：标准 logging + aiohttp，CLI 侧也能直接调用。

数据来源
--------
seerapi.sqlite   赛尔号游戏数据镜像（160 表）
    https://github.com/Murmansk-Seer/seerapi
    releases/download/seerapi-data-latest/seerapi-data.sqlite
    指纹：同目录 seerapi-data.sqlite.sha256

aliases.sqlite   精灵别名库
    https://github.com/Murmansk5000/IronsBot
    releases/download/alias-db-latest/aliases-data.sqlite
    指纹：同目录 aliases-data.sqlite.sha256

update
------
两个库都会每小时比对一次上游 .sha256。流程：

    取远端指纹 → 与本地记录的指纹比对
      ├─ 一致：跳过（不发一次 DB 请求）
      └─ 不一致：下载 → 验货闸门 → 备份旧库 → 原子替换

**验货闸门是硬性的**：上游曾经漏发 34 张表（含 pet_special_effect），
同步逻辑无校验直接覆盖，导致专属名词查询全部静默失效。
宁可不更新，也不接受一个残缺的库。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import time

import aiohttp

logger = logging.getLogger(__name__)

# ── 上游地址（改这里就能换源；留空则跳过该类同步）─────────────────
SEERAPI_URL = (
    "https://github.com/Murmansk-Seer/seerapi/releases/download/"
    "seerapi-data-latest/seerapi-data.sqlite"
)
SEERAPI_SHA_URL = SEERAPI_URL + ".sha256"

ALIAS_URL = (
    "https://github.com/Murmansk5000/IronsBot/releases/download/"
    "alias-db-latest/aliases-data.sqlite"
)
ALIAS_SHA_URL = ALIAS_URL + ".sha256"

SEERAPI_INTERVAL_MIN = 60
ALIAS_INTERVAL_MIN = 60

HEADERS = {"User-Agent": "seer-expert/1.0 (+astrbot plugin)"}

# ── 验货闸门参数 ──────────────────────────────────────────────
SEERAPI_MIN_BYTES = 30 * 1024 * 1024      # 完整库约 62MB，不到 30MB 视为残缺
SEERAPI_MIN_PET_ROWS = 1000
SEERAPI_REQUIRED_TABLES = (
    "pet",
    "skill",
    "pet_skin",
    "suit",
    "suit_bonus",
    "pet_special_effect",
)
ALIAS_MIN_BYTES = 30 * 1024   # 实际约 52KB，低于 30KB 视为残缺
BACKUP_KEEP = 5                            # 每个库最多保留几份 .bak


def _session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(headers=HEADERS)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _backup(db_path: str) -> str | None:
    """覆盖前备份，保留最近 BACKUP_KEEP 份。"""
    if not os.path.isfile(db_path):
        return None
    dst = f"{db_path}.bak-{_stamp()}"
    try:
        shutil.copy2(db_path, dst)
    except OSError as exc:
        logger.warning("[data_sync] 备份失败（继续更新）：%s", exc)
        return None

    base = os.path.basename(db_path)
    olds = sorted(
        (
            os.path.join(os.path.dirname(db_path), n)
            for n in os.listdir(os.path.dirname(db_path))
            if n.startswith(base + ".bak-")
        ),
        key=os.path.getmtime,
        reverse=True,
    )
    for extra in olds[BACKUP_KEEP:]:
        try:
            os.unlink(extra)
        except OSError:
            pass
    return dst


def _validate_seerapi(path: str) -> tuple[bool, str]:
    """验货闸门：体积 + 必备表 + pet 行数。不合格整包丢弃。"""
    import sqlite3

    size = os.path.getsize(path)
    if size < SEERAPI_MIN_BYTES:
        return False, f"体积仅 {size/1024/1024:.1f}MB，低于 {SEERAPI_MIN_BYTES/1024/1024:.0f}MB"

    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return False, f"打不开：{exc}"
    try:
        names = {
            r[0]
            for r in con.execute(
                "select name from sqlite_master where type='table'"
            )
        }
        missing = [t for t in SEERAPI_REQUIRED_TABLES if t not in names]
        if missing:
            return False, f"缺表 {missing}（共 {len(names)} 表）"
        rows = con.execute("select count(*) from pet").fetchone()[0]
        if rows < SEERAPI_MIN_PET_ROWS:
            return False, f"pet 表只有 {rows} 行，低于 {SEERAPI_MIN_PET_ROWS}"
        return True, f"{len(names)} 表 / pet {rows} 行 / {size/1024/1024:.1f}MB"
    except sqlite3.Error as exc:
        return False, f"查询失败：{exc}"
    finally:
        con.close()


def _validate_aliases(path: str) -> tuple[bool, str]:
    import sqlite3

    size = os.path.getsize(path)
    if size < ALIAS_MIN_BYTES:
        return False, f"体积仅 {size/1024:.0f}KB"
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            con.execute("select count(*) from pet_aliases").fetchone()
        finally:
            con.close()
        return True, f"{size/1024:.0f}KB"
    except sqlite3.Error as exc:
        return False, f"校验失败：{exc}"


async def _remote_sha(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        text = (await resp.text()).strip()
    # 文件内容可能整行就是 hash，也可能是 "<hash>  filename"
    return text.split()[0].lower() if text else ""


def _recorded_sha(db_path: str) -> str:
    p = db_path + ".sha256"
    if not os.path.isfile(p):
        return ""
    try:
        with open(p, encoding="utf-8") as f:
            return f.read().strip().split()[0].lower()
    except OSError:
        return ""


def _write_sha(db_path: str, sha: str) -> None:
    try:
        with open(db_path + ".sha256", "w", encoding="utf-8") as f:
            f.write(sha)
    except OSError as exc:
        logger.warning("[data_sync] 指纹落盘失败：%s", exc)


async def _download(
    session: aiohttp.ClientSession, url: str, dest: str, timeout_sec: int = 300
) -> None:
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    async with session.get(url, timeout=timeout) as resp:
        resp.raise_for_status()
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    f.write(chunk)
            await asyncio.to_thread(os.replace, tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


async def _sync_one(
    name: str,
    url: str,
    sha_url: str,
    db_path: str,
    validate,
) -> str:
    """单个库的同步流程。返回一句状态描述。"""
    if not url:
        return f"{name}: 未配置上游，跳过"
    if not os.path.isdir(os.path.dirname(db_path)):
        return f"{name}: 目录不存在，跳过"

    async with _session() as session:
        try:
            remote = await _remote_sha(session, sha_url)
        except Exception as exc:  # noqa: BLE001
            return f"{name}: 取远端指纹失败（{type(exc).__name__}），跳过"

        if not remote:
            return f"{name}: 远端指纹为空，跳过"

        # 已下载的文件和指纹同时存在才算「已是最新」
        if remote == _recorded_sha(db_path) and os.path.isfile(db_path):
            # 放线程：62MB 的 sha256 同步算要几十秒，会卡死事件循环
            local = await asyncio.to_thread(_sha256_file, db_path)
            if local == remote:
                return f"{name}: 已是最新（{remote[:12]}）"
            logger.warning("[data_sync] %s 指纹一致但内容不符，重新下载", name)

        try:
            await _download(session, url, db_path)
        except Exception as exc:  # noqa: BLE001
            return f"{name}: 下载失败（{type(exc).__name__}: {exc}）"

    # 放线程：校验要开 sqlite、查表、数行，同步跑会卡事件循环
    ok, detail = await asyncio.to_thread(validate, db_path)
    if not ok:
        # 不合格就整包丢弃，绝不覆盖旧库
        try:
            os.unlink(db_path)
        except OSError:
            pass
        logger.error("[data_sync] %s 验货未通过，已丢弃下载文件：%s", name, detail)
        return f"{name}: 验货未通过（{detail}），已丢弃、保留旧库"

    await asyncio.to_thread(_write_sha, db_path, remote)
    return f"{name}: 已更新 → {detail}"


async def sync_seerapi(db_path: str) -> str:
    status = await _sync_one(
        "seerapi", SEERAPI_URL, SEERAPI_SHA_URL, db_path, _validate_seerapi
    )
    logger.info("[data_sync] %s", status)
    return status


async def sync_aliases(db_path: str) -> str:
    status = await _sync_one(
        "aliases", ALIAS_URL, ALIAS_SHA_URL, db_path, _validate_aliases
    )
    logger.info("[data_sync] %s", status)
    return status


async def sync_all(data_dir: str) -> dict[str, str]:
    """data_dir 里放 seerapi.sqlite / aliases.sqlite。

    两个库都会在覆盖前自动备份（保留最近 BACKUP_KEEP 份）。
    aliases 的本地改动请写进 aliases_local_overlay.sqlite，
    否则会被上游版本覆盖掉。
    """
    out: dict[str, str] = {}
    seerapi_path = os.path.join(data_dir, "seerapi.sqlite")
    alias_path = os.path.join(data_dir, "aliases.sqlite")

    if os.path.isfile(seerapi_path):
        await asyncio.to_thread(_backup, seerapi_path)
    out["seerapi"] = await sync_seerapi(seerapi_path)

    if os.path.isfile(alias_path):
        await asyncio.to_thread(_backup, alias_path)
    out["aliases"] = await sync_aliases(alias_path)
    return out
