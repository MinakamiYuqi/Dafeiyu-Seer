# -*- coding: utf-8 -*-
"""创建 aliases 的本地 overlay 库，并把当前库里的「本地独有」别名行迁进去。

overlay 与上游 aliases.sqlite 分开存：
  aliases.sqlite              ← 上游原样（会被自动更新覆盖）
  aliases_local_overlay.sqlite ← 自用别名（更新时由 sqlite 合并回来，不会被丢）
"""
import sqlite3
import sys
from pathlib import Path

BASE = Path(r"G:\temp\instances\eb74352a-db17-4359-aade-ba98505d2e47\core\data\plugin_data\astrbot_plugin_seer_info")
LIVE = BASE / "aliases.sqlite"
OVERLAY = BASE / "aliases_local_overlay.sqlite"
TABLES = ("mintmark_aliases", "mintmark_class_aliases", "mintmark_series_members")

DDL = "create table if not exists [{t}] (name TEXT NOT NULL, target_id INTEGER NOT NULL, PRIMARY KEY(name,target_id))"


def table_rows(con, table):
    exists = con.execute(
        "select 1 from sqlite_master where type='table' and name=?", (table,)
    ).fetchone()
    if not exists:
        return set()
    cols = [r[1] for r in con.execute(f"pragma table_info([{table}])")]
    if "name" not in cols or "target_id" not in cols:
        return set()
    return {(str(n), int(t)) for n, t in con.execute(f"select name,target_id from [{table}]")}


def main():
    if not LIVE.exists():
        print(f"!! 找不到 {LIVE}")
        return 1

    live = sqlite3.connect(f"file:{LIVE.as_posix()}?mode=ro", uri=True)
    ov = sqlite3.connect(OVERLAY)

    existing = {
        t: table_rows(ov, t) for t in TABLES
    } if OVERLAY.exists() else {t: set() for t in TABLES}

    for t in TABLES:
        ov.execute(DDL.format(t=t))

    added_total = 0
    for t in TABLES:
        live_rows = table_rows(live, t)
        # 已存在的 overlay 行保留；这里把当前库里的行作为「自用基线」写入
        # （当前库里这些行与上游一致，写进去等价于给它们上了保险）
        merged = live_rows | existing[t]
        new = merged - existing[t]
        if new:
            ov.executemany(
                f"insert or ignore into [{t}] (name,target_id) values (?,?)", sorted(new)
            )
        added_total += len(new)
        print(f"  {t:26} live={len(live_rows):3d} overlay={len(merged):3d} 新增={len(new):3d}")

    ov.commit()
    check = ov.execute("pragma integrity_check").fetchone()[0]
    ov.close()
    live.close()

    if check != "ok":
        print(f"!! overlay 完整性检查失败: {check}")
        return 1

    print(f"\n✅ overlay 已生成: {OVERLAY}")
    print(f"   本次写入 {added_total} 行，完整性检查: {check}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
