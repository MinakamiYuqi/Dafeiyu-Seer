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


def _full_cfg() -> dict:
    """满配参数 —— 魂印派生必须在这个口径下验，裸配置的增量是假的。

    鱼片 2026-11-17 19:35：
      「你怎么不把其他加成都加上再算，刻印数值和其他加成都不算再算期望有什么意义」

    实例：无极圣武的「双攻差」
      裸配置  攻341/特攻251 → 差90 → 增益67
      满配    攻439/特攻456 → 差17 → 增益12   ← 真实值
    刻印/套装会把双攻拉近，裸配置算出来的期望完全偏离实际。
    """
    return dict(
        nature="固执",
        ev="atk=254,hp=252,spd=4",
        mints=["黄金残影·反抗", "黄金残影·反抗", "赤洲龙子·嘲风"],
        suit="晨曦",
        title="神话",
        eye="漫游者-AH",
    )


SOULMOD_CASES = [
    ("无为觉者", "均分", "soulmod_average", True),
    ("天堂极龙兽", "体力20", "soulmod_pct", 0.20),
    ("无极圣武", "双攻差5", "soulmod_atk_diff", 5),
    ("镇世神岳·玄武", "双防15", "soulmod_double_def_pct", 0.15),
]


def test_soulmod_matches_config_view(k):
    """满配下，四种魂印派生：stat_view 和 config_view 六项必须完全一致。

    ★ 满配参数来自 _full_cfg()，不是裸配置 —— 见那个函数的注释。
    ★ nature 也必须显式传，否则会被另一个原有的默认值差异干扰
      （stat_view(nature="") 选坦率、config_view 选固执，是坑 25 的实例）。
    """
    FULL = _full_cfg()
    for pet, label, kname, kval in SOULMOD_CASES:
        kw = dict(FULL)
        kw[kname] = kval
        a = k.stat_view(pet, **kw)
        b = k.config_view(pet, **kw)
        ar = {r["key"]: r["final"] for r in a["rows"]}
        br = {r2["key"]: r2["value"] for r2 in b["battle"]}
        for key in ("atk", "def", "sp_atk", "sp_def", "spd", "hp"):
            assert ar.get(key) == br.get(key), (
                f"{pet}（{label}）的 {key}: stat={ar.get(key)} config={br.get(key)}"
            )


def test_soulmod_delta_matches_formula(k):
    """满配下，四种派生的增量必须符合各自公式。

    各自公式（都以「含刻印/套装/称号/目镜之后」的面板为基数）：
      ① 体力20%    体力 −(上限×20%)，其余五项各 +同量
      ② 英雄之耀   |攻-特攻| × 15% × 层数 → 加到 防/特防/速/体（双攻不变）
      ③ 总和均分   每项 = 六项和 / 2
      ④ 双防转化   (防+特防) × pct → 加到「速度外」五项
    """
    FULL = _full_cfg()
    KEYS = ("atk", "def", "sp_atk", "sp_def", "spd", "hp")

    for pet, label, kname, kval in SOULMOD_CASES:
        base_v = k.stat_view(pet, **FULL)
        kw = dict(FULL)
        kw[kname] = kval
        sm_v = k.stat_view(pet, **kw)
        base = {r["key"]: r["final"] for r in base_v["rows"]}
        sm = {r["key"]: r["final"] for r in sm_v["rows"]}

        if label == "均分":
            S = sum(base.values())
            half = S // 2
            assert all(sm[k2] == half for k2 in KEYS), (
                f"{pet} 均分应为每项 {half}（S={S}），实际 {sm}"
            )
        elif label == "体力20":
            cut = int(base["hp"] * kval)
            assert sm["hp"] == base["hp"] - cut, (
                f"{pet} 体力应 −{cut}（{base['hp']}→{sm['hp']}）"
            )
            for k2 in ("atk", "def", "sp_atk", "sp_def", "spd"):
                assert sm[k2] == base[k2] + cut, (
                    f"{pet} {k2} 应 +{cut}，实际 +{sm[k2] - base[k2]}"
                )
        elif label == "双攻差5":
            d = abs(base["atk"] - base["sp_atk"])
            gain = int(d * 0.15 * kval)
            assert sm["atk"] == base["atk"], f"{pet} 英雄之耀不该改攻击"
            assert sm["sp_atk"] == base["sp_atk"], f"{pet} 英雄之耀不该改特攻"
            for k2 in ("def", "sp_def", "spd", "hp"):
                assert sm[k2] == base[k2] + gain, (
                    f"{pet} {k2} 应 +{gain}（双攻差{d}×15%×{kval}），"
                    f"实际 +{sm[k2] - base[k2]}"
                )
        else:  # 双防15
            sf = base["def"] + base["sp_def"]
            gain = int(sf * kval)
            assert sm["spd"] == base["spd"], f"{pet} 双防转化不该改速度"
            for k2 in ("atk", "def", "sp_atk", "sp_def", "hp"):
                assert sm[k2] == base[k2] + gain, (
                    f"{pet} {k2} 应 +{gain}（双防和{sf}×{kval:.0%}），"
                    f"实际 +{sm[k2] - base[k2]}"
                )
