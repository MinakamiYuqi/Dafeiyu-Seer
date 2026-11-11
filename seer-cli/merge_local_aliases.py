"""Protect local alias tables while refreshing the upstream aliases database."""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

TABLES = ("mintmark_aliases", "mintmark_class_aliases", "mintmark_series_members")


def rows(con: sqlite3.Connection, table: str) -> set[tuple[str, int]]:
    exists = con.execute(
        "select 1 from sqlite_master where type='table' and name=?", (table,)
    ).fetchone()
    if not exists:
        return set()
    return {(str(name), int(target)) for name, target in con.execute(f"select name,target_id from [{table}]")}


def merge(old_path: Path, new_path: Path) -> dict[str, int]:
    old = sqlite3.connect(f"file:{old_path.as_posix()}?mode=ro", uri=True)
    new = sqlite3.connect(new_path)
    counts: dict[str, int] = {}
    try:
        new.execute("begin immediate")
        for table in TABLES:
            old_rows = rows(old, table)
            new_rows = rows(new, table)
            missing = old_rows - new_rows
            if missing and not new_rows:
                new.execute(
                    f"create table if not exists [{table}] (name TEXT NOT NULL, target_id INTEGER NOT NULL, PRIMARY KEY(name,target_id))"
                )
            if missing:
                new.executemany(
                    f"insert or ignore into [{table}] (name,target_id) values (?,?)",
                    sorted(missing),
                )
            counts[table] = len(missing)
        new.commit()
        integrity = new.execute("pragma integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"merged DB integrity check: {integrity}")
    except Exception:
        new.rollback()
        raise
    finally:
        old.close()
        new.close()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old", type=Path, help="previous aliases.sqlite")
    parser.add_argument("new", type=Path, help="downloaded aliases.sqlite to merge into")
    args = parser.parse_args()
    before = hashlib.sha256(args.new.read_bytes()).hexdigest()
    counts = merge(args.old, args.new)
    print(f"Before SHA256 (expected to change after overlay): {before}")
    for table, count in counts.items():
        print(f"{table}: restored {count} local-only rows")
    print(f"Merged SHA256: {hashlib.sha256(args.new.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
