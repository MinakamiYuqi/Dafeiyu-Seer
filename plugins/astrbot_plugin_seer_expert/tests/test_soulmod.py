# -*- coding: utf-8 -*-
"""魂印派生：两个入口必须算出同一个数（2026-11-17 十号鱼加）。

背景 —— 这一天八号鱼把四种派生搬进 stat_view 后用自己编的配装去验，
期望值是假的，于是整段回滚。回滚后：
    stat_view 不算派生 / config_view 算
    → 无为觉者 1934 vs 5802、无极圣武 3013 vs 4537、玄武 3134 vs 4024
    同一只精灵、「面板多少」两个答案，差 890~3868。这就是坑 25。

现在派生只有一份实现（db.resolve_soulmod + db._apply_soulmod），
两个入口都调它，所以这组测试盯的是：
  ① 六只 pet_build 精灵，stat 的六项和 == 配置图的六项和（默认口径）
  ② 传 soulmod="无" 时两边都回到裸面板（关得掉）
  ③ 派生说明里写清了算的是哪一种（不然没人知道数是怎么来的）
  ④ 「双防15」不会被当成体力换属性（这个坑踩过）
"""
import pytest


def _rows(k, name: str, **kw):
    v = k.stat_view(name, **kw)
    if not v.get("found"):
        pytest.skip(f"{name} 库里没有")
    return v


def _build_pets(k):
    """pet_build 里有记录、且记了魂印派生的精灵。"""
    rows = k._kb.execute(
        "select pet_id, pet_name, soulmod from pet_build where "
        "coalesce(soulmod,'') != ''"
    ).fetchall()
    return [(r[0], str(r[1] or ""), str(r[2] or "")) for r in rows]


def _own_kw(k, pid: int) -> dict:
    own = k.pet_build(pid) or {}
    return dict(
        mints=list(own.get("mints") or []),
        nature=own.get("nature") or "",
        ev=own.get("ev") or "",
        suit=own.get("suit") or "",
        title=own.get("title") or "",
        eye=own.get("eye") or "",
        team_skip=own.get("team_skip") or "",
        hpup=20,
    )


def test_stat_and_config_agree_with_soulmod(k):
    """六只手工调过配装的精灵：两个入口的六项和必须相等（默认带派生）。"""
    pets = _build_pets(k)
    assert len(pets) >= 4, f"pet_build 里带 soulmod 的只有 {len(pets)} 只，样本太少"
    bad = []
    for pid, name, tag in pets:
        kw = _own_kw(k, pid)
        sv = _rows(k, name, **kw)
        cv = k.config_view(name, **kw)
        if not cv.get("found"):
            continue
        st = sum(r["final"] for r in sv["rows"])
        ct = int(cv.get("total") or 0)
        if st != ct:
            bad.append(f"{name}（{tag}）：stat {st} ≠ 配置图 {ct}（差 {st - ct}）")
    assert not bad, "魂印派生口径又漂了：\n  " + "\n  ".join(bad)


def test_soulmod_can_be_turned_off(k):
    """soulmod="无" 必须回到裸面板，且两边的「裸」也是同一个数。"""
    pets = _build_pets(k)
    checked = 0
    for pid, name, tag in pets:
        kw = _own_kw(k, pid)
        auto = _rows(k, name, **kw)
        bare = _rows(k, name, soulmod="无", **kw)
        cv_bare = k.config_view(name, soulmod="无", **kw)
        st_auto = sum(r["final"] for r in auto["rows"])
        st_bare = sum(r["final"] for r in bare["rows"])
        assert st_bare <= st_auto or tag == "均分", (
            f"{name}：关掉派生后反而变大了（{st_bare} vs {st_auto}）"
        )
        if cv_bare.get("found"):
            assert st_bare == int(cv_bare.get("total") or 0), (
                f"{name}：裸面板口径不一致 stat {st_bare} vs 配置图 {cv_bare.get('total')}"
            )
        if st_bare != st_auto:
            checked += 1
    assert checked >= 3, f"只有 {checked} 只精灵真的受派生影响，测试可能没验到东西"


def test_soulmod_notes_explain_numbers(k):
    """自动算派生时必须给出说明行，不然数从哪来没人知道。"""
    pets = _build_pets(k)
    for pid, name, tag in pets:
        v = _rows(k, name, **_own_kw(k, pid))
        notes = v.get("soulmod_notes") or []
        assert notes, f"{name} 自动算了「{tag}」派生却没有说明行"
        joined = "".join(str(x) for x in notes)
        key = {"均分": "均分", "双攻差5": "英雄之耀", "体力20": "体力",
               "双防15": "双防"}.get(tag, "")
        if key:
            assert key in joined, f"{name} 的说明里没有「{key}」：{joined}"


def test_parse_soulmod_str_order(k):
    """「双防15」不能被当成体力换属性 —— 两个都带数字，顺序判断错了就废。"""
    assert k.parse_soulmod_str("双防15")["soulmod_double_def_pct"] == pytest.approx(0.15)
    assert k.parse_soulmod_str("双防15")["soulmod_pct"] == 0
    assert k.parse_soulmod_str("均分")["soulmod_average"] is True
    assert k.parse_soulmod_str("双攻差5")["soulmod_atk_diff"] == 5
    assert k.parse_soulmod_str("英雄之耀")["soulmod_atk_diff"] == 5
    assert k.parse_soulmod_str("体力20")["soulmod_pct"] == pytest.approx(0.20)
    for off in ("", "无", "关", "0"):
        got = k.parse_soulmod_str(off)
        assert not any((got["soulmod_pct"], got["soulmod_atk_diff"],
                        got["soulmod_average"], got["soulmod_double_def_pct"]))
