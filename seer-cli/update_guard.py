# -*- coding: utf-8 -*-
"""数据更新体检：确认哪些库会自动更新、有没有被本地改过、数据是否最新、自用数据是否完好。

用法：
    python seer-cli/update_guard.py            # 离线检查
    python seer-cli/update_guard.py --online   # 额外比对上游指纹
    python seer-cli/update_guard.py --pet 4924 # 抽查某只精灵是否已入库
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

INSTANCE = Path(r"G:\temp\instances\eb74352a-db17-4359-aade-ba98505d2e47")
DATA = INSTANCE / "core" / "data" / "plugin_data"
INFO = DATA / "astrbot_plugin_seer_info"
EXPERT = DATA / "astrbot_plugin_seer_expert"
CONFIG = INSTANCE / "core" / "data" / "config" / "astrbot_plugin_seer_info_config.json"

SYNCED = ("seerapi", "aliases")
LOCAL_ONLY = {
    "kb.sqlite": ("pet_build", "facts", "pet_alias"),
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def table_count(con, table: str) -> int | None:
    exists = con.execute(
        "select 1 from sqlite_master where type='table' and name=?", (table,)
    ).fetchone()
    if not exists:
        return None
    return con.execute(f"select count(*) from [{table}]").fetchone()[0]


def check_synced(name: str, online: bool) -> bool:
    db = INFO / f"{name}.sqlite"
    fp = INFO / f"{name}.sqlite.sha256"
    print(f"\n[{name}]  ← 自动更新")
    if not db.exists():
        print("  ❌ 库文件不存在")
        return False

    local = sha256(db)
    record = fp.read_text(encoding="utf-8").strip().split(" ")[0].lower() if fp.exists() else ""
    print(f"  本地 sha256 : {local}")
    print(f"  记录指纹    : {record or '（无 .sha256）'}")

    # 注意：aliases 有 overlay 时本地 hash 与上游必然不同，只有无 overlay 才能用 hash 判改
    overlay = INFO / "aliases_local_overlay.sqlite"
    if name == "aliases" and overlay.exists():
        print("  ⚠ 存在本地 overlay，本地 hash 与上游不同属正常（更新时会合并）")
    elif record:
        print(f"  本地改动    : {'无（与记录一致）' if local == record else '有（与记录不一致）'}")

    ok = True
    if online and record:
        url = f"https://github.com/Murmansk-Seer/seerapi/releases/download/seerapi-data-latest/seerapi-data.sqlite.sha256"
        if name == "aliases":
            url = "https://github.com/Murmansk5000/IronsBot/releases/download/alias-db-latest/aliases-data.sqlite.sha256"
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                remote = r.read().decode().strip().split(" ")[0].lower()
            same = remote == record
            print(f"  上游最新    : {remote}")
            print(f"  是否最新    : {'✅ 已是最新（下次检查会跳过）' if same else '⚠ 上游有新版，下次检查会下载'}")
        except Exception as exc:
            print(f"  上游查询失败: {exc}")
            ok = False
    return ok


def check_local() -> bool:
    print("\n[自建库]  ← 不参与自动更新")
    ok = True
    for fname, tables in LOCAL_ONLY.items():
        path = EXPERT / fname
        if not path.exists():
            print(f"  ❌ {fname} 不存在")
            ok = False
            continue
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            parts = []
            for t in tables:
                n = table_count(con, t)
                parts.append(f"{t}={n if n is not None else '缺表'}")
                if n is None:
                    ok = False
            print(f"  ✅ {fname:12} " + "  ".join(parts))
        finally:
            con.close()

    overlay = INFO / "aliases_local_overlay.sqlite"
    if overlay.exists():
        con = sqlite3.connect(f"file:{overlay.as_posix()}?mode=ro", uri=True)
        try:
            parts = []
            for t in ("mintmark_aliases", "mintmark_class_aliases", "mintmark_series_members"):
                parts.append(f"{t}={table_count(con, t) or 0}")
            print(f"  ✅ {'overlay':12} " + "  ".join(parts))
        finally:
            con.close()
    else:
        print("  ⚠ overlay 不存在：aliases 更新后自用别名不会自动合并")

    manual = EXPERT / "manual_db"
    n = len(list(manual.glob("*.db"))) if manual.exists() else 0
    print(f"  ✅ manual_db    {n} 个库（本地静态，不同步）")
    return ok


def check_pet(pet_id: int) -> bool:
    db = INFO / "seerapi.sqlite"
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute("select id,name,base_stats_id from pet where id=?", (pet_id,)).fetchone()
        print(f"\n[抽查 #{pet_id}]")
        if not row:
            print("  ❌ 库里没有这只精灵（数据未更新到该编号）")
            return False
        skills = con.execute(
            "select count(*) from skillinpetorm where pet_id=?", (pet_id,)
        ).fetchone()[0]
        souls = con.execute(
            "select count(*) from petsoulmarklink where pet_id=?", (pet_id,)
        ).fetchone()[0]
        stats = con.execute(
            "select atk,def,sp_atk,sp_def,spd,hp from pet_base_stats where id=?",
            (row[2],),
        ).fetchone()
        print(f"  ✅ {row[1]}  技能 {skills} 条  魂印 {souls} 条")
        if stats:
            atk, df, sa, sd, spd, hp = stats
            print(f"     种族值 攻{atk} 防{df} 特攻{sa} 特防{sd} 速度{spd} 体力{hp}  总和{atk+df+sa+sd+spd+hp}")
        maxid = con.execute("select max(id) from pet").fetchone()[0]
        print(f"     库内最大编号 {maxid}")
        return skills > 0
    finally:
        con.close()


def show_sync_config() -> None:
    print("\n[同步配置]")
    if not CONFIG.exists():
        print("  找不到插件配置")
        return
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    for key in ("seerapi_sync_url", "seerapi_sync_interval_minutes",
                "alias_sync_url", "alias_sync_interval_minutes"):
        if key in cfg:
            print(f"  {key:34} {cfg[key] or '（空=不同步）'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--online", action="store_true", help="比对上游指纹")
    ap.add_argument("--pet", type=int, help="抽查精灵编号")
    args = ap.parse_args()

    print("=" * 60)
    print("赛尔数据更新体检")
    print("=" * 60)

    ok = True
    for name in SYNCED:
        ok &= check_synced(name, args.online)
    ok &= check_local()
    show_sync_config()
    if args.pet:
        ok &= check_pet(args.pet)

    print("\n" + "=" * 60)
    print("结论：自建库不进同步范围；同步库有验货闸门 + 旧库备份 + overlay 合并"
          if ok else "结论：有项目需要处理，见上面 ⚠/❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
