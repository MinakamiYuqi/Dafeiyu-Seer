# -*- coding: utf-8 -*-
"""面板/配装计算回归 —— 原 `_re/verify_panel.py` 的 pytest 版。

12 组断言全部保留，只是换成 pytest 的收集方式：
    pytest astrbot_plugin_seer_expert/tests -q
    pytest astrbot_plugin_seer_expert/tests -q -k panel

改 db.py 的公式、刻印、魂印、性格相关逻辑后，这一套必须全绿。
"""
import pytest

# 第 1 组用的统一外设（计算器截图实测点的条件）
CFG = dict(suit="晨曦之星战甲", title="神话", eye="漫游者-AH", team_skip="特攻", hpup=20)


def _battle(v):
    return {b["key"]: b["value"] for b in v["battle"]}


# ── 1. 计算器截图实测点（权威） ────────────────────────────────────────────
@pytest.mark.parametrize(
    "name,nature,ev,want_atk",
    [
        ("南霜", "固执", "atk=252", 489),
        ("南霜", "固执", "atk=254", 490),
        ("不灭战魂·盖亚", "固执", "atk=253", 516),
        ("不灭战魂·盖亚", "固执", "atk=255", 516),
    ],
)
def test_screenshot_attack_points(k, name, nature, ev, want_atk):
    v = k.config_view(name, talent=31, nature=nature, ev=ev, mints=[], **CFG)
    assert _battle(v)["atk"] == want_atk


def test_wuji_shengwu_two_angle_attack(k):
    v = k.config_view(
        "无极圣武", talent=31, nature="固执", ev="atk=254,hp=252,spd=4",
        mints=["瓏·寅勇", "星辉·相伴", "瓏·寅勇"], team_skip="特攻",
        soulmod_atk_diff=5, hpup=20,
        suit="晨曦之星战甲", title="神话", eye="漫游者-AH",
    )
    assert _battle(v)["atk"] == 724


# ── 2. 玄武 #4995 完整六项 ────────────────────────────────────────────────
def test_xuanwu_full_panel(k):
    v = k.config_view(
        "玄武", talent=31, nature="顽皮", ev="def=253,sp_def=252,hp=5",
        mints=["刑天·威临", "刑天·威临", "黄金残影·信仰"],
        suit="晨曦之星战甲", title="神话", eye="漫游者-AH",
        team_skip="特攻", soulmod_double_def_pct=0.15, hpup=20,
    )
    d = _battle(v)
    assert d["atk"] == 559
    assert d["def"] == 791
    assert d["sp_atk"] == 421
    assert d["sp_def"] == 757
    assert d["spd"] == 361
    assert d["hp"] == 1135


# ── 3. 专属配装（pet_build 表） ───────────────────────────────────────────
@pytest.mark.parametrize(
    "name,want_mints",
    [
        ("无极圣武", ["瓏·寅勇", "瓏·寅勇", "星辉·相伴"]),
        ("无为觉者", ["赤洲龙子·螭吻", "赤洲龙子·螭吻", "帝皇·无极"]),
        ("天堂极龙兽", ["黄金残影·创造", "黄金残影·创造", "赤洲龙子·赑屃"]),
        ("玄武", ["刑天·威临", "刑天·威临", "黄金残影·信仰"]),
    ],
)
def test_pet_build_mints(k, name, want_mints):
    assert k.build_view(name, "爆攻").get("mints") == want_mints


# ── 4. 魂印自动识别 ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "name,field,want",
    [
        ("天堂极龙兽", "pct", 0.2),
        ("无为觉者", "average", True),
        ("无极圣武", "atk_diff_flag", True),
        ("玄武", "double_def_pct", 0.15),
    ],
)
def test_soulmod_detection(k, name, field, want):
    assert k.detect_soulmod(name).get(field) == want


# ── 5. 性格表 25 种齐全 ──────────────────────────────────────────────────
def test_nature_table_complete(k):
    assert len(k._NATURE_TABLE) == 25


@pytest.mark.parametrize(
    "name,pair",
    [
        ("顽皮", ("def", "sp_atk")),
        ("开朗", ("spd", "sp_atk")),
        ("无虑", ("def", "sp_def")),
        ("温顺", ("sp_def", "def")),
        ("慎重", ("sp_def", "sp_atk")),
        ("孤独", ("atk", "def")),
    ],
)
def test_nature_pairs(k, name, pair):
    assert k._NATURE_TABLE.get(name) == pair


# ── 6. 战队跳过推断 ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "name,want",
    [("南霜", "特攻"), ("天堂极龙兽", ""), ("无为觉者", "")],
)
def test_suggest_team_skip(k, name, want):
    assert k.suggest_team_skip(name) == want


# ── 7. 刻印校验（防静默少装） ─────────────────────────────────────────────
def test_mint_name_validation(k):
    chk = k.check_mints(["黄金残影·希望", "黄金残子·蒲牢", "黄金残影·希望"])
    assert bool(chk["bad"]) is True
    assert len(chk["bad"]) == 1


# ── 8. 性格按学习力侧重自动选 ─────────────────────────────────────────────
@pytest.mark.parametrize(
    "ev,want",
    [
        ("def=253,sp_def=252,hp=5", None),        # 主刷防 → 保持顽皮
        ("sp_def=253,def=252,hp=5", "慎重"),      # 主刷特防 → 改
        ("sp_def=252,hp=252,spd=4", "慎重"),      # 单刷特防 → 更要改
        ("def=252,sp_def=252,hp=4", None),        # 完全并列 → 不改
    ],
)
def test_pick_nature_by_ev(k, ev, want):
    assert k.pick_nature_by_ev(4995, ev, fallback="顽皮").get("nature") == want


# ── 9. 隐藏数值（ExtraAttriValue） ────────────────────────────────────────
def test_mintmark_hidden_values(k):
    info = k.lookup_mintmark("雷之霆威")
    vals = info.get("vals") or {}
    assert vals.get("atk") == 53
    assert vals.get("spd") == 35
    assert info.get("extra") == [3, 3, 0, 3, 5, 3]


def test_mintmark_without_hidden_values(k):
    assert k.lookup_mintmark("赤洲龙子·狴犴").get("extra") == []


# ── 10. 刻印系列 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "name,want",
    [
        ("刑天·威临", "铠甲勇士系列"),
        ("帝皇·无极", "铠甲勇士系列"),
        ("赤洲龙子·狴犴", "赤洲龙子系列"),
    ],
)
def test_mint_class(k, name, want):
    assert k.mint_class(name) == want


# ── 11. 攻击倾向（查技能表，不看种族值） ──────────────────────────────────
@pytest.mark.parametrize(
    "name,want",
    [
        ("玄武", "physical"),
        ("南霜", "physical"),
        ("无极圣武", "physical"),
        ("无为觉者", "special"),
        ("天堂极龙兽", "physical"),
        ("修罗铠甲", "dual"),
        ("天启星魂", "special"),
        ("星辰万象·天启星魂", "dual"),
    ],
)
def test_attack_orientation(k, name, want):
    hits = k.find_pets(name)
    assert hits, f"找不到精灵 {name}"
    assert k.attack_orientation(hits[0][0]).get("orient") == want


# ── 12. 双攻机制识别 ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "name,want",
    [("修罗铠甲", "skill"), ("星辰万象·天启星魂", "soul"), ("天启星魂", "flatten")],
)
def test_dual_attack_mechanism(k, name, want):
    hits = k.find_pets(name)
    assert hits, f"找不到精灵 {name}"
    assert k.attack_orientation(hits[0][0]).get("dual_mech") == want
