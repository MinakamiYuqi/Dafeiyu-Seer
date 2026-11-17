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


# ══════════════════════════════════════════════════════════════════
# 魂印派生是「另设窗口」—— 默认不启用，显式传参数才生效
#
# 2026-11-18 加。鱼片的要求：
#   「做，但要确保是正优化。如果因为这几个特例影响泛用性，不如另设窗口」
#
# 所以这里的契约是：
#   · 不传 soulmod_* → stat_view 的输出必须和加这个功能之前**完全一致**
#   · 传了 → 结果必须和 config_view 用同样参数算的一致
# ══════════════════════════════════════════════════════════════════


def test_soulmod_default_is_noop(k):
    """不传 soulmod_* 时，stat_view 的输出不该受这个功能影响。

    用「传全默认值」和「用位置参数不传」两种写法对比 —
    如果新参数有默认值以外的副作用，这两个就会不一致。
    """
    for pet in ("无为觉者", "圣灵谱尼", "南霜"):
        a = k.stat_view(pet)
        b = k.stat_view(
            pet,
            soulmod_pct=0.0,
            soulmod_atk_diff=0,
            soulmod_average=False,
            soulmod_double_def_pct=0.0,
        )
        assert [r["final"] for r in a["rows"]] == [r["final"] for r in b["rows"]], (
            f"{pet}: 显式传全默认值后结果变了 —— 说明新参数有副作用"
        )
        assert a["total"] == b["total"], f"{pet}: 六项和变了"


def test_soulmod_applies_when_explicit(k):
    """传 average=True 时，stat_view 必须真的把「六项之和/2」算进去。"""
    base = k.stat_view("无为觉者")
    sm = k.stat_view("无为觉者", soulmod_average=True)

    S = sum(r["final"] for r in base["rows"])
    half = S // 2

    for r in sm["rows"]:
        assert r["final"] == half, (
            f"{r['cn']} 应为 {half}（S={S} 的一半），实际 {r['final']}"
        )
    assert sm["total"] == half * 6
    assert sm.get("soulmod_applied") is True
    # notes 里要有说明，别静默改数字
    assert any("总和均分" in str(n) for n in (sm.get("notes") or []))


def test_soulmod_matches_config_view(k):
    """四种魂印派生：stat_view 和 config_view 传同样参数，六项必须一致。

    ★ 必须显式传 nature —— 否则会被一个**原有的**默认值差异干扰：
      stat_view(nature="") 选坦率，config_view(nature="") 选固执。
      那不是魂印派生的问题，是坑 25 的另一个实例（默认值两处不一致）。
      这里传 nature="固执" 把变量固定住，只验派生。
    """
    CASES = [
        ("无为觉者", {"soulmod_average": True}),        # ⑧.6 总和均分
        ("天堂极龙兽", {"soulmod_pct": 0.20}),          # ⑧   体力上限 20%
        ("无极圣武", {"soulmod_atk_diff": 5}),          # ⑧.5 英雄之耀
        ("镇世神岳·玄武", {"soulmod_double_def_pct": 0.15}),  # ⑧.7 双防转化
    ]
    for pet, kw in CASES:
        a = k.stat_view(pet, nature="固执", **kw)
        b = k.config_view(pet, nature="固执", **kw)
        ar = {r["key"]: r["final"] for r in a["rows"]}
        br = {r2["key"]: r2["value"] for r2 in b["battle"]}
        for key in ("atk", "def", "sp_atk", "sp_def", "spd", "hp"):
            assert ar.get(key) == br.get(key), (
                f"{pet}（{kw}）的 {key}: stat={ar.get(key)} config={br.get(key)}"
            )


def test_soulmod_delta_is_correct(k):
    """四种派生的**增量**必须符合各自公式（不依赖 nature，更硬的约束）。

      ① 体力20%    体力 −cut，其余五项各 +cut
      ② 英雄之耀   双攻差 × 15% × 层数 → 加到 体力/防/特防/速（双攻不变）
      ③ 总和均分   每项变 S/2
      ④ 双防转化   双防和 × pct → 加到「速度外」五项
    """
    KEYS = ("atk", "def", "sp_atk", "sp_def", "spd", "hp")

    def finals(**kw):
        v = k.stat_view("圣灵谱尼", **kw)      # 用没有特殊魂印的精灵当底
        return {r["key"]: r["final"] for r in v["rows"]}

    base = finals()
    S = sum(base.values())

    # ③ 总和均分
    avg = finals(soulmod_average=True)
    half = S // 2
    assert all(avg[k2] == half for k2 in KEYS), f"均分应为每项 {half}，实际 {avg}"

    # ② 英雄之耀：双攻差 × 15% × 5
    d = abs(base["atk"] - base["sp_atk"])
    gain = int(d * 0.15 * 5)
    if gain:
        atk5 = finals(soulmod_atk_diff=5)
        assert atk5["atk"] == base["atk"], "英雄之耀不该改攻击"
        assert atk5["sp_atk"] == base["sp_atk"], "英雄之耀不该改特攻"
        for k2 in ("hp", "def", "sp_def", "spd"):
            assert atk5[k2] == base[k2] + gain, (
                f"英雄之耀：{k2} 应 +{gain}，实际 {atk5[k2] - base[k2]}"
            )

    # ④ 双防转化：双防和 × 15%，速度不加
    sf = base["def"] + base["sp_def"]
    g2 = int(sf * 0.15)
    if g2:
        dd = finals(soulmod_double_def_pct=0.15)
        assert dd["spd"] == base["spd"], "双防转化不该改速度"
        for k2 in ("atk", "def", "sp_atk", "sp_def", "hp"):
            assert dd[k2] == base[k2] + g2, (
                f"双防转化：{k2} 应 +{g2}，实际 {dd[k2] - base[k2]}"
            )

    # ① 体力20%：体力减 cut，其余五项各加 cut
    cut = int(base["hp"] * 0.20)
    if cut:
        pc = finals(soulmod_pct=0.20)
        assert pc["hp"] == base["hp"] - cut, (
            f"体力20%：体力应 −{cut}，实际 {base['hp'] - pc['hp']}"
        )
        for k2 in ("atk", "def", "sp_atk", "sp_def", "spd"):
            assert pc[k2] == base[k2] + cut, (
                f"体力20%：{k2} 应 +{cut}，实际 {pc[k2] - base[k2]}"
            )
