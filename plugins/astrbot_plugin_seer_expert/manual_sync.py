"""从「火の辞典」的更新仓库同步数据包（只下数据库，不含图片）。

仓库结构：
  https://cnb.cool/SeerFire/update_cnb/-/tree/main/generated_files/db
  ├ effectDes_20261023142631.db
  ├ pets_20261023142631.db
  └ …
文件名带 14 位时间戳，取每个前缀里最新的那个。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time

import aiohttp

# 标准 logging（不是 astrbot.api.logger）：manual_sync 也可能被 CLI 侧调用，
# 用插件 logger 会把没有 astrbot 的环境炸掉。
logger = logging.getLogger(__name__)

TREE_URL = "https://cnb.cool/SeerFire/update_cnb/-/tree/main/generated_files/db"
RAW_BASE = "https://cnb.cool/SeerFire/update_cnb/-/git/raw/main/generated_files/db"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}
PATTERN = re.compile(r"([A-Za-z_]+)_(\d{14})\.db")
# 异常状态插图：SeerAPI 的 Unity 资源仓库，按 effectDes.icon 索引取图
ASSET_BASE = (
    "https://cnb.cool/SeerAPI/seer-unity-assets/-/git/raw/main/"
    "newseer/assets/art/ui/assets/battleeffect/abnormal"
)
# ★ mintmark 2026-11-03 加入。
#   刻印是 lookup_mintmark 的优先级①数据源（MaxAttriValue 已含隐藏加成），
#   原先不在列表里，导致上游新增刻印永远进不来、面板算不出新印。
#   入库前已比对：本地 mintmark.db 与上游 mintmark_20261030163020.db 字节完全一致，
#   说明没有自用改动会被这次纳入覆盖。
#
# ★ 2026-11-03 全量纳入：剩余 6 个库逐一与上游比对过，均「无本地独有行」，
#   纳入不会覆盖掉自用数据；而 skill_effect 上游比本地多 35 条（2347→2382），
#   不纳入就一直拿不到新技能效果。
#   纳入前比对记录（本地独有行 / 上游新增）：
#     effectag      无 / —        effectbuff   无 / —
#     petEffectIcon 无 / —        rich_text_tree 无 / —
#     skill_effect  无 / 35 条    te_xing      无 / —
WANTED = (
    "effectDes", "pets", "moves", "effectIcon", "buff", "effectInfo",
    "pvp", "skillTypes",
    # —— 2026-11-03 补 ——
    "mintmark", "effectag", "effectbuff", "petEffectIcon",
    "rich_text_tree", "skill_effect", "te_xing",
)



def _validate_db_bytes(data: bytes) -> tuple[bool, str]:
    """下载内容落盘前验货：能否解析、有没有表、行数是否合理。"""
    if len(data) < 4096:
        return False, f"too small ({len(data)} bytes)"
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        with open(tmp, "wb") as f:
            f.write(data)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            tables = [
                r[0]
                for r in con.execute(
                    "select name from sqlite_master where type='table'"
                ).fetchall()
            ]
            if not tables:
                return False, "no tables"
            total = 0
            for t in tables:
                total += con.execute(f"select count(*) from [{t}]").fetchone()[0]
            if total == 0:
                return False, "all tables empty"
            if con.execute("pragma integrity_check").fetchone()[0] != "ok":
                return False, "integrity check failed"
        finally:
            con.close()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"invalid sqlite: {exc}"
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

def _session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        headers=HEADERS, connector=aiohttp.TCPConnector(ssl=False)
    )


async def latest_files(names: tuple[str, ...] = WANTED) -> dict[str, str]:
    """返回 {前缀: 最新文件名}。"""
    async with _session() as session:
        async with session.get(TREE_URL, timeout=30) as resp:
            if resp.status != 200:
                raise RuntimeError(f"仓库页面返回 {resp.status}")
            html = await resp.text()
    pairs = PATTERN.findall(html)
    best: dict[str, tuple[str, str]] = {}
    for prefix, stamp in pairs:
        if names and prefix not in names:
            continue
        if prefix not in best or stamp > best[prefix][1]:
            best[prefix] = (f"{prefix}_{stamp}.db", stamp)
    return {k: v[0] for k, v in best.items()}


async def sync(db_dir: str, names: tuple[str, ...] = WANTED) -> dict[str, str]:
    """下载最新数据库到 db_dir，返回 {前缀: 状态}。"""
    os.makedirs(db_dir, exist_ok=True)
    files = await latest_files(names)
    result: dict[str, str] = {}
    async with _session() as session:
        for prefix, filename in files.items():
            dest = os.path.join(db_dir, f"{prefix}.db")
            try:
                async with session.get(f"{RAW_BASE}/{filename}", timeout=120) as resp:
                    if resp.status != 200:
                        result[prefix] = f"HTTP {resp.status}"
                        continue
                    data = await resp.read()

                # 落盘前验货：不合格就丢掉，保住本地旧库
                # 放线程：sqlite 校验是同步阻塞的，直接在事件循环里跑会卡住整只 bot
                ok, why = await asyncio.to_thread(_validate_db_bytes, data)
                if not ok:
                    result[prefix] = f"丢弃（{why}）"
                    continue

                # 覆盖前备份，方便回滚
                if os.path.isfile(dest):
                    backup = dest + time.strftime(".bak-%Y%m%d-%H%M%S")
                    try:
                        await asyncio.to_thread(shutil.copy2, dest, backup)
                    except OSError as exc:
                        # 备份失败还继续覆盖 = 「声称有备份、实际没有」，必须让人看见
                        logger.warning(
                            "[manual_sync] %s 备份失败（%s），本次仍会覆盖，回滚将不可用",
                            prefix,
                            exc,
                        )

                # 原子替换：先写临时文件再 rename，避免半截文件
                # 写盘 + 替换一起放线程：几十 MB 写入会明显卡事件循环
                def _atomic_write(path: str, blob: bytes) -> None:
                    tmp = path + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(blob)
                    os.replace(tmp, path)

                await asyncio.to_thread(_atomic_write, dest, data)
                result[prefix] = f"{filename} ({len(data) // 1024} KB)"
            except Exception as exc:  # noqa: BLE001
                result[prefix] = f"失败 {exc}"
    return result


async def fetch_icon(icon_id: int, dest_dir: str) -> str:
    """下载单个异常状态图标，已存在就跳过。返回本地路径或空串。"""
    path = os.path.join(dest_dir, f"abnormal_{icon_id}.png")
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    os.makedirs(dest_dir, exist_ok=True)
    try:
        async with _session() as session:
            async with session.get(f"{ASSET_BASE}/{icon_id}.png", timeout=30) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.read()
        if len(data) < 100:
            return ""
        with open(path, "wb") as f:
            f.write(data)
        return path
    except Exception:  # noqa: BLE001
        return ""


async def fetch_icons(icon_ids: list[int], dest_dir: str) -> tuple[int, int]:
    """批量下载，返回 (成功数, 尝试数)。"""
    ok = 0
    for i in icon_ids:
        if await fetch_icon(i, dest_dir):
            ok += 1
    return ok, len(icon_ids)


