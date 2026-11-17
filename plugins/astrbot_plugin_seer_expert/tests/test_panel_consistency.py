# -*- coding: utf-8 -*-
"""面板口径一致性：stat_view / config_view 用同一组参数必须算出同一个数。

2026-11-16 一天里因为「同一参数在不同入口默认值不同」踩了三次：
  · hpup：config_view 默认 20、stat_view 默认 0 → 均分型精灵每项差 10
  · team/annual：stat_view 默认开、CLI 默认关 → 差 80
  · pet_build 记录：build_view 里 _own 压过传参 → 传圣芒回显晨曦

这个测试把「两边算法必须相等」钉死。

注意字段层级：stat_view 的 rows[].value 是**基础值**，
含加成的在 rows[].final；config_view 的 battle[].value 已经是最终值。
"""
import inspect

import pytest


def _stat_total(k, name: str, **kw) -> int:
    v = k.stat_view(name, **kw)
    if not v.get("found"):
        pytest.skip(f"{name} 库里没有")
    return sum(r["final"] for r in v["rows"])


# 三种典型：普通精灵 / 种族全 0 的均分型 / 带战队跳项的
CASES = [
    ("圣光灵神", dict(talent=31, nature="固执", ev="atk=252,hp=252")),
    ("无为觉者", dict(talent=31, nature="勇敢", ev="atk=254,hp=252,spd=4",
                      mints=["赤洲龙子·螭吻", "赤洲龙子·螭吻", "帝皇·无极"],
                      suit="圣芒", title="星光之旅", eye="漫游者-AH")),
    ("无极圣武", dict(talent=31, nature="固执", ev="atk=254,hp=252,spd=4",
                      mints=["瓏·寅勇", "瓏·寅勇", "星辉·相伴"], team_skip="特攻")),
]


@pytest.mark.parametrize("name,kw", CASES)
def test_stat_and_config_agree(k, name: str, kw: dict):
    """stat_view 的 sum(final) 必须等于 config_view（不含魂印派生）的 total。"""
    stat_total = _stat_total(k, name, **kw)

    cv = k.config_view(name, **kw)
    if not cv.get("found"):
        pytest.skip(f"{name} 库里没有")
    cfg_total = cv["total"]

    assert stat_total == cfg_total, (
        f"{name}：stat_view 六项和 {stat_total} ≠ config_view {cfg_total}"
        f"（差 {stat_total - cfg_total}）—— 两个入口的默认值又漂了，"
        f"检查 hpup / team / year_bonus"
    )


def test_hpup_default_is_consistent():
    """两个入口的 hpup 默认值必须相同（今天差 10 的根源）。"""
    from db import SeerKnowledge

    p_stat = inspect.signature(SeerKnowledge.stat_view).parameters["hpup"].default
    p_cfg = inspect.signature(SeerKnowledge.config_view).parameters["hpup"].default
    assert p_stat == p_cfg, f"stat_view hpup 默认={p_stat}，config_view 默认={p_cfg}"


def test_hpup_actually_applies(k):
    """传 hpup 必须真的影响面板（否则又是一个「参数声明了但没用」）。"""
    kw = dict(talent=31, nature="勇敢", ev="atk=254,hp=252,spd=4",
              mints=["赤洲龙子·螭吻", "赤洲龙子·螭吻", "帝皇·无极"],
              suit="圣芒", title="星光之旅", eye="漫游者-AH")
    a = _stat_total(k, "无为觉者", hpup=20, **kw)
    b = _stat_total(k, "无为觉者", hpup=0, **kw)
    assert a - b == 20, f"hpup 20 vs 0 的差值应正好 20，实际 {a - b}"


def test_build_respects_explicit_params(k):
    """build_view 里用户传的套装/称号/目镜不能被 pet_build 表里的记录盖掉。"""
    v = k.build_view("无为觉者", "爆攻", suit="圣芒", title="星光之旅", eye="漫游者-AH")
    if not v.get("found"):
        pytest.skip("无为觉者 库里没有")
    assert v.get("suit") == "圣芒", f"套装被覆盖成 {v.get('suit')!r}"
    assert v.get("title") == "星光之旅", f"称号被覆盖成 {v.get('title')!r}"
    assert v.get("eye") == "漫游者-AH", f"目镜被覆盖成 {v.get('eye')!r}"

    # 不传时应该回落到 pet_build 的记录（无为觉者存的是晨曦/神话/AH）
    v2 = k.build_view("无为觉者", "爆攻")
    assert v2.get("suit") and v2.get("suit") != "圣芒", "不传时应回落到表里的配装"
