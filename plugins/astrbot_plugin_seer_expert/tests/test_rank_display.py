# -*- coding: utf-8 -*-
"""段位展示：单位必须跟着段位走（圣皇=星，往下=分）。

规则来自鱼片 2026-11-21 的说明，并且用真实号对上过：
    竞技 当前 rk=4 star=0    → 圣皇 0星
    竞技 最高 rk=4 star=161  → 宇宙圣皇 161星
    狂野 最高 rk=4 star=36   → 圣皇 36星
    大师 当前 rk=0 star=54   → 学徒 54分   ← 以前错写成「学徒 54星」
"""
from __future__ import annotations

from seer_pvp import RANK_NAMES, rank_display


def test_圣皇用星():
    assert rank_display(4, 0) == "圣皇 0星"
    assert rank_display(4, 36) == "圣皇 36星"


def test_圣皇满百显示宇宙圣皇():
    assert rank_display(4, 100) == "宇宙圣皇 100星"
    assert rank_display(4, 161) == "宇宙圣皇 161星"


def test_低段位用分():
    assert rank_display(0, 54) == "学徒 54分"
    assert rank_display(1, 57) == "猛将 57分"
    assert rank_display(2, 48) == "天骄 48分"
    assert rank_display(3, 67) == "王者 67分"


def test_零分也是分():
    """0 分不能说成 0 星 —— 低段位根本没有星。"""
    assert rank_display(0, 0) == "学徒 0分"
    assert rank_display(1, 0) == "猛将 0分"


def test_未知段位不崩():
    assert rank_display(9, 3) == "段位9 3分"
    assert rank_display(-1, 0) == "段位-1 0分"


def test_段位表完整():
    """RANK_NAMES 必须覆盖 0~4，否则 rank_display 会掉进 fallback。"""
    for rk in range(5):
        assert rk in RANK_NAMES, f"RANK_NAMES 缺 rk={rk}"
