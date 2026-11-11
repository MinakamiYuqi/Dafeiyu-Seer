# -*- coding: utf-8 -*-
"""边界行为回归 —— 2026-11-04 深度审查发现的问题，锁死别再退回去。

这些用例对应真实 bug：
- 负威力被夹成 1 威力，最后报「2 点伤害」
- 负连击被当成 1 段，报出正常数值
- tag_search("") 走 like '%%' 命中整张 tags 表（38959 条）
- ev=9999 能算出攻击 3139，talent=-5 能算出比 0 还低的数
- lookup_mintmark 每次调用漏一个 manual_db 连接
"""
import psutil
import pytest


# ── dmg_view 参数校验 ────────────────────────────────────────────────────
def test_negative_power_rejected(k):
    v = k.dmg_view(power=-50, atk=100, defense=100)
    assert v["ok"] is False
    assert "power" in v["empty_hint"]


def test_negative_hits_rejected(k):
    v = k.dmg_view(power=100, atk=100, defense=100, hits=-3)
    assert v["ok"] is False
    assert "hits" in v["empty_hint"]


def test_zero_defense_rejected(k):
    v = k.dmg_view(power=100, atk=100, defense=0)
    assert v["ok"] is False
    assert "defense" in v["empty_hint"]


def test_normal_damage_still_works(k):
    v = k.dmg_view(power=100, atk=100, defense=100)
    assert v["ok"] is True
    assert v["result"]["attack_channel"] > 0


# ── tag_search 空串 ──────────────────────────────────────────────────────
def test_tag_search_empty_returns_nothing(k):
    total, rows = k.tag_search("")
    assert total == 0
    assert rows == []


def test_tag_search_blank_whitespace(k):
    total, rows = k.tag_search("   ")
    assert total == 0


def test_tag_search_real_tag(k):
    total, rows = k.tag_search("先制")
    assert total > 0
    assert len(rows) > 0


# ── 面板计算的输入夹取 ───────────────────────────────────────────────────
def _atk(v):
    return next((b["value"] for b in v["battle"] if b["key"] == "atk"), None)


def test_ev_capped_at_255(k):
    at_255 = _atk(k.config_view("南霜", talent=31, nature="固执", ev="atk=255", mints=[]))
    at_9999 = _atk(k.config_view("南霜", talent=31, nature="固执", ev="atk=9999", mints=[]))
    assert at_9999 == at_255, "学习力超过 255 应被夹住"


def test_talent_capped_at_0(k):
    at_0 = _atk(k.config_view("南霜", talent=0, nature="固执", ev="atk=252", mints=[]))
    at_neg = _atk(k.config_view("南霜", talent=-5, nature="固执", ev="atk=252", mints=[]))
    assert at_neg == at_0, "天赋为负应被夹到 0"


def test_talent_capped_at_31(k):
    at_31 = _atk(k.config_view("南霜", talent=31, nature="固执", ev="atk=252", mints=[]))
    at_99 = _atk(k.config_view("南霜", talent=99, nature="固执", ev="atk=252", mints=[]))
    assert at_99 == at_31, "天赋超过 31 应被夹住"


# ── 不存在的东西不崩 ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "call",
    [
        lambda k: k.config_view("绝对不存在xyz", talent=31, nature="固执", ev="atk=252", mints=[]),
        lambda k: k.build_view("绝对不存在xyz", "爆攻"),
        lambda k: k.pet_view("绝对不存在xyz"),
        lambda k: k.lookup_mintmark("绝对不存在的刻印xyz"),
        lambda k: k.lookup_mintmark(None),
        lambda k: k.find_pets(""),
        lambda k: k.glossary_search(""),
        lambda k: k.recall(""),
        lambda k: k.facts_search(""),
    ],
)
def test_missing_input_does_not_crash(k, call):
    r = call(k)
    assert r is not None or r is None   # 只要不抛异常即可


# ── 连接泄漏 ─────────────────────────────────────────────────────────────
def test_lookup_mintmark_does_not_leak_handles(k):
    """200 次调用后句柄数不应增长（原来每次漏一个 manual_db 连接）。"""
    try:
        proc = psutil.Process()
        before = proc.num_handles()
    except Exception:
        pytest.skip("当前平台不支持句柄计数")

    for _ in range(200):
        k.lookup_mintmark("雷之霆威")

    after = proc.num_handles()
    assert after - before < 10, f"句柄增长了 {after - before} 个，疑似连接泄漏"
