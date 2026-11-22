# -*- coding: utf-8 -*-
"""监控心跳的「该不该新鲜」判断：关门以后不许再报「监控挂了」。

背景（2026-11-22 23:08 真实事故）：
    竞技 23:30 收盘后监控进空闲、心跳不再更新，而 bot 只看 `age_min > 15`
    就报「⚠ 监控已经 36 分钟没刷新了，可能挂了」—— 群里当场被指出是白报。
    更糟的是每天 10:55 起采前，任何人问榜都会看到「600+ 分钟没刷新」。
    假警报一多，真故障反而没人信。

现在的心跳里带一份作息表（`schedule`，由监控写、bot 只读），
bot 先用当前时间算出「这个点该不该有新鲜数据」，再决定报不报警。
"""
from __future__ import annotations

from datetime import datetime

from seer_arena_view import monitor_state

#: 和 _re/xm/seer_loop.py 的 SCHEDULE 同结构（监控写进心跳的那份）
SCHEDULE = {
    "weekday": [["11:00", "15:00"], ["18:00", "22:00"]],
    "weekend": [["11:00", "23:30"]],
    "lead_min": 5,
    "seal_min": 3,
}

SAT = 21   # 2026-11-21 周六
SUN = 22   # 2026-11-22 周日
MON = 23   # 2026-11-23 周一


def _hb(*, ts_iso: str = "2026-11-22 23:32:17", age: float = 36.0,
        sched: dict | None = SCHEDULE, collect: str | None = None) -> dict:
    hb = {"iso": ts_iso, "age_min": age}
    if collect is not None:
        hb["last_collect"] = collect
    if sched is not None:
        hb["schedule"] = sched
    return hb


def _at(day: int, hh: int, mm: int) -> datetime:
    return datetime(2026, 11, day, hh, mm)


# ── 关门时段：不该报警 ────────────────────────────────────────
def test_周末收盘后不算故障():
    """23:30 收盘、23:35 进空闲，00:08 时 age=36 分钟 —— 这是正常作息。"""
    st = monitor_state(_hb(age=36), now=_at(SUN, 0, 8))
    assert st["should_be_fresh"] is False
    assert st["window_end"] == "23:30"


def test_周末收盘后给出下个窗口():
    st = monitor_state(_hb(), now=_at(SUN, 23, 40))
    assert st["should_be_fresh"] is False
    assert st["next_open_hm"] == "10:55"     # 11:00 开盘 - LEAD_MIN 5


def test_早上没开盘不算故障():
    """每天起采前问榜，原来会报「600+ 分钟没刷新」。"""
    st = monitor_state(_hb(age=600), now=_at(SUN, 8, 0))
    assert st["should_be_fresh"] is False
    assert st["next_open_hm"] == "10:55"


def test_周间午休不算故障():
    """周间 15:00-18:00 是间歇，心跳不动正常。"""
    st = monitor_state(_hb(age=25), now=_at(MON, 15, 30))
    assert st["should_be_fresh"] is False
    assert st["next_open_hm"] == "17:55"     # 18:00 - 5


def test_周间夜间隔天算下个上午():
    st = monitor_state(_hb(age=90), now=_at(MON, 23, 0))
    assert st["should_be_fresh"] is False
    assert st["next_open_hm"] == "10:55"


# ── 该新鲜的时段：心跳停了就是真故障 ──────────────────────────
def test_窗口内该新鲜():
    st = monitor_state(_hb(age=20), now=_at(SUN, 12, 0))
    assert st["should_be_fresh"] is True


def test_开盘前基线轮也算该新鲜():
    """10:55 起采（提前 5 分钟建基线），10:56 心跳该是新的。"""
    st = monitor_state(_hb(age=1), now=_at(SUN, 10, 56))
    assert st["should_be_fresh"] is True


def test_封账三分钟内还算该新鲜():
    st = monitor_state(_hb(age=2), now=_at(SUN, 23, 31))
    assert st["should_be_fresh"] is True


def test_周间晚上窗口内该新鲜():
    st = monitor_state(_hb(age=20), now=_at(MON, 19, 0))
    assert st["should_be_fresh"] is True


def test_窗内关窗户之间不收尾立刻算关门():
    """15:00 整收盘后还有 3 分钟封账轮，15:04 才算关门。"""
    assert monitor_state(_hb(), now=_at(MON, 15, 2))["should_be_fresh"] is True
    assert monitor_state(_hb(), now=_at(MON, 15, 4))["should_be_fresh"] is False


# ── 数据时刻 vs 心跳时刻 ──────────────────────────────────────
def test_数据年龄按last_collect算():
    """监控刚重启：心跳 age=1，但 last_collect 还是三小时前 —— 数据其实不新。

    这条是「进程重启写了新心跳」引入的坑：光看心跳年龄，重启一下就能把
    三小时前的数据说成「刚刚」，等于又编了一个时间。
    """
    st = monitor_state(_hb(age=1.0, collect="2026-11-22 09:00:00"), now=_at(SUN, 12, 0))
    assert st["should_be_fresh"] is True          # 窗口内，本该新鲜
    assert st["data_age_min"] == 180.0            # 数据其实 3 小时没动 → bot 该报警
    assert st["last_collect"] == "2026-11-22 09:00:00"


def test_老心跳没有last_collect退回iso():
    """旧版心跳没有 last_collect：解析出负数（时钟/构造不一致）时信 age_min。"""
    st = monitor_state(_hb(age=42.0), now=_at(SUN, 12, 0))
    assert st["data_age_min"] == 42.0


def test_关门后用last_collect说数据停在哪():
    st = monitor_state(_hb(age=40.0, collect="2026-11-22 23:32:17"), now=_at(SUN, 23, 55))
    assert st["should_be_fresh"] is False
    assert st["last_collect"] == "2026-11-22 23:32:17"
    assert st["window_end"] == "23:30"


# ── 兼容老心跳 ────────────────────────────────────────────────
def test_旧心跳没有作息表不下结论():
    """旧版监控写的心跳没有 schedule —— 保持原来的报警行为，别静默吞掉真故障。"""
    st = monitor_state(_hb(sched=None), now=_at(SUN, 12, 0))
    assert st["should_be_fresh"] is None


def test_没有心跳不崩():
    st = monitor_state(None, now=_at(SUN, 12, 0))
    assert st["should_be_fresh"] is None
    assert st["age_min"] is None
