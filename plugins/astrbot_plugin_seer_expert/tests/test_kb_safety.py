# -*- coding: utf-8 -*-
"""kb.sqlite 数据安全回归 —— 2026-11-04 加的保护，别再改回去。

覆盖：
- 破坏性写入前自动备份（快照可用、内容完整）
- 备份保留上限
- delete_facts 的误删护栏
- build_effect_tags 在 seerapi 读不到时不清空旧数据
"""
import os
import sqlite3

import pytest


# ── 备份 ─────────────────────────────────────────────────────────────────
def test_backup_creates_restorable_snapshot(kb_copy):
    facts_before = kb_copy._kb.execute("select count(*) from facts").fetchone()[0]
    path = kb_copy._backup_kb("pytest")
    assert path and os.path.isfile(path)

    con = sqlite3.connect(path)
    try:
        assert con.execute("select count(*) from facts").fetchone()[0] == facts_before
        assert con.execute("select count(*) from pet_build").fetchone()[0] >= 1
    finally:
        con.close()


def test_backup_is_throttled(kb_copy):
    first = kb_copy._backup_kb("pytest-a")
    second = kb_copy._backup_kb("pytest-b")   # 一分钟内第二次应被节流
    assert first is not None
    assert second is None


def test_backup_retention_limit(kb_copy, monkeypatch):
    import db as db_mod

    monkeypatch.setattr(db_mod, "KB_BACKUP_KEEP", 2)
    backup_dir = os.path.join(os.path.dirname(kb_copy._kb_path), "kb_backups")
    for i in range(4):
        kb_copy._last_backup_at = 0.0        # 绕过节流
        kb_copy._backup_kb(f"retention-{i}")
        # 文件名精确到秒，同秒会覆盖，这里靠 mtime 排序也能验证淘汰
    files = [f for f in os.listdir(backup_dir) if f.endswith(".sqlite")]
    assert len(files) <= 2


def test_set_moveset_triggers_backup(kb_copy):
    backup_dir = os.path.join(os.path.dirname(kb_copy._kb_path), "kb_backups")
    assert not os.path.isdir(backup_dir) or not os.listdir(backup_dir)
    kb_copy.set_moveset(999999, ["测试技能"], "pytest")
    assert os.path.isdir(backup_dir) and os.listdir(backup_dir)


# ── delete_facts 护栏 ────────────────────────────────────────────────────
def test_delete_facts_rejects_short_keyword_with_many_hits(kb_copy):
    before = kb_copy._kb.execute("select count(*) from facts").fetchone()[0]
    deleted = kb_copy.delete_facts("的")     # 单字，命中数远超阈值
    after = kb_copy._kb.execute("select count(*) from facts").fetchone()[0]
    assert deleted == []
    assert after == before


def test_delete_facts_allows_precise_keyword(kb_copy):
    row = kb_copy._kb.execute("select topic from facts limit 1").fetchone()
    assert row, "facts 表为空，测试数据不对"
    topic = row[0]
    deleted = kb_copy.delete_facts(topic)
    assert deleted == [topic]
    remaining = kb_copy._kb.execute(
        "select count(*) from facts where topic = ?", (topic,)
    ).fetchone()[0]
    assert remaining == 0


def test_delete_facts_empty_result_keeps_everything(kb_copy):
    before = kb_copy._kb.execute("select count(*) from facts").fetchone()[0]
    assert kb_copy.delete_facts("绝对不存在的关键词-zzz-qqq") == []
    assert kb_copy._kb.execute("select count(*) from facts").fetchone()[0] == before


# ── tags 重建保护 ────────────────────────────────────────────────────────
def test_build_effect_tags_keeps_old_rows_when_source_missing(kb_copy, tmp_path):
    import db as db_mod

    before = kb_copy._kb.execute("select count(*) from tags").fetchone()[0]
    assert before > 0, "tags 本来就是空的，测不出保护效果"

    # 把 seer 路径换成一个不存在的文件，模拟 seerapi 不可读
    broken = db_mod.SeerKnowledge(str(tmp_path / "missing.sqlite"), kb_copy._kb_path)
    try:
        made = broken.build_effect_tags(force=True)
        after = broken._kb.execute("select count(*) from tags").fetchone()[0]
        assert made == 0
        assert after == before, "source 缺失时不该清空 tags"
    finally:
        broken._kb.close()


def test_build_effect_tags_normal_path_rebuilds(kb_copy):
    before = kb_copy._kb.execute("select count(*) from tags").fetchone()[0]
    made = kb_copy.build_effect_tags(force=True)
    after = kb_copy._kb.execute("select count(*) from tags").fetchone()[0]
    assert made > 0
    assert after > 0
    # 重建后条数可能因为上游更新而变，但不能塌成 0 或掉一半
    assert after >= before * 0.5


# ── resolve_pending ──────────────────────────────────────────────────────
def test_resolve_pending_does_not_raise(kb_copy):
    kb_copy.add_pending("pytest-键", "测试问题")
    kb_copy.resolve_pending("pytest-键")
    row = kb_copy._kb.execute(
        "select resolved from pending where key like ?", ("%pytest-键%",)
    ).fetchone()
    assert row is not None and row[0] == 1
