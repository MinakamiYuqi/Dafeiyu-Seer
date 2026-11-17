#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""mint_picker.py — 赛尔号刻印自动选择

规则（鱼片口述 2026-11-01）：
  · 同系列刻印最多同时装 2 个
  · PVP 一般给五角刻印（部分精灵必须二角/三角）
  · 按种族自动判断物攻向 / 特攻向（有例外，如卫岳双攻都有用）
  · 几角 = 提供几项能力值的全能刻印
  · 俗称：xx爆攻 / xx爆速 / xx爆体；系列简称：龙子 / k13 / v10 / 十年

数据：手册 mintmark.db 的 MintMark 表
  AttriValue / BaseAttriValue / MaxAttriValue  六项 [攻,防,特攻,特防,速,体]
  MaxAttriValue 已含隐藏值，是满级值 —— 一律用它
"""
from __future__ import annotations

import json
import os
import sqlite3

MANUAL_DIRS = [r"<workspace>\_manual_all", r"<workspace>\_apk\assets"]

# ★ 2026-11-09 加（P2）：别名规则搬进 kb.sqlite 后的读取路径。
#   放在 kb 里而不是手册里，是因为 manual_db 会被 manual_sync 覆盖，
#   而 kb.sqlite 永远不同步。读写都走这张表，代码里只留兜底常量。
KB_DB = (r"G:\temp\instances\<instance-id>"
         r"\core\data\plugin_data\astrbot_plugin_seer_expert\kb.sqlite")


# 六项顺序（手册固定）
KEYS = ("atk", "def", "sp_atk", "sp_def", "spd", "hp")
CN = {"atk": "攻", "def": "防", "sp_atk": "特攻", "sp_def": "特防", "spd": "速", "hp": "体"}

# 系列简称 → MintMarkClass 里的正式名
SERIES_ALIAS = {
    "龙子": "赤洲龙子系列",
    "k13": "K13系列",
    "v10": "V10系列",
    "十年": "十周年系列",
    "精灵王": "精灵王系列",
    "王誓": "精灵王誓约系列",
    "泰坦": "泰坦系列",
    "衡": "衡系列",
    "巅峰": "巅峰圣战系列",
    "星璨": "星璨灵籁系列",
    "群星": "群星系列",
    "沧吟": "沧吟星海系列",
    "瓏域": "瓏域系列",
    "赫星": "赫星千年系列",
    "洛林卡": "洛林卡系列",
    "露希欧": "露希欧之梦系列",
    "神耀": "神耀系列",
    "英雄圣殿": "英雄圣殿系列",
    "英雄之证": "英雄之证系列",
    "决战": "决战系列",
    "征战": "征战系列",
    "生肖": "生肖系列",
    "节日": "节日欢庆系列",
    "十二年": "十二年之约系列",
}

# 易得刻印：巅峰圣战系列等常驻可刷，优先进方案
EASY_SERIES = ("巅峰圣战系列", "瓏域系列", "泰坦系列", "衡系列", "神耀系列")

# 310 大总伤刻印：限定/昂贵，只作「可选」备注，不默认进方案
PREMIUM_TOTAL = 300

# 只能获取一个的刻印（同名不能装两个）
# 鱼片 2026-11-01：穹明拾光目前没开放第二套；星辉系列只能获取一套
UNIQUE_ONCE = ("穹明拾光", "星辉")

# 可以获取两套的系列（同款可装两个）
# 鱼片 2026-11-01：瓏系列在周年纪念馆免费换，解绑不花钱，分解后还能换，共两套
TWO_SETS_SERIES = ("瓏域系列",)

# 流派权重：攻/特攻是主项，体力次之，速度再之，双防垫底
WEIGHTS = {
    "physical": {"atk": 1.0, "hp": 0.60, "spd": 0.50, "def": 0.30, "sp_def": 0.30, "sp_atk": 0.05},
    "special":  {"sp_atk": 1.0, "hp": 0.60, "spd": 0.50, "def": 0.30, "sp_def": 0.30, "atk": 0.05},
    "dual":     {"atk": 0.80, "sp_atk": 0.80, "hp": 0.60, "spd": 0.50, "def": 0.30, "sp_def": 0.30},
    # 盾体向：双防+体力为主，输出项几乎不计
    "shield":   {"def": 1.0, "sp_def": 1.0, "hp": 0.85, "spd": 0.20, "atk": 0.10, "sp_atk": 0.10},
    # 极差向（无极圣武这类「双攻差值决定收益」）：
    # 只堆主攻项和体力，另一攻给负权重——任何加另一攻的刻印都要扣分
    "maxdiff_physical": {"atk": 1.0, "hp": 0.70, "spd": 0.20, "def": 0.10, "sp_def": 0.10, "sp_atk": -2.0},
    "maxdiff_special":  {"sp_atk": 1.0, "hp": 0.70, "spd": 0.20, "def": 0.10, "sp_def": 0.10, "atk": -2.0},
    # 卷速向：速度优先，特攻/攻击兼顾（如修罗铠甲配裁决卷速）
    "speed_special":    {"spd": 1.2, "sp_atk": 0.9, "hp": 0.40, "def": 0.15, "sp_def": 0.15, "atk": 0.0},
    "speed_physical":   {"spd": 1.2, "atk": 0.9, "hp": 0.40, "def": 0.15, "sp_def": 0.15, "sp_atk": 0.0},
}


# ── 刻印简称 / 代指解析 ────────────────────────────────────────────
# 群友黑话（鱼片 2026-11-01）：
#   成套刻印里「哪一项比同套其他只更高」就叫什么。例「九天体」= 九天系列里体力最高的那只（九霄之钟/灵）。
#   战令 = 巅峰战令系列（幻灵）；v8 = V8系列（有进阶取进阶）；圣战 = 圣战α（有 [Ⅰ] 取 [Ⅰ]）
SERIES_WORD = {
    "九天": "九天系列",
    "战令": "巅峰战令系列",
    "幻灵": "巅峰战令系列",
    "v8": "V8系列",
    "V8": "V8系列",
    "圣战": "巅峰圣战系列",
    "泰坦": "泰坦系列",
    "衡": "衡系列",
    "偃月": "偃月系列",
    "神耀": "神耀系列",
    "龙子": "赤洲龙子系列",
    "十年": "十周年系列",
    "k13": "K13系列",
    "K13": "K13系列",
}
STAT_WORD = {"攻": "atk", "攻攻": "atk", "特攻": "sp_atk", "速": "spd",
             "体": "hp", "防": "def", "特防": "sp_def", "双防": "_both_def"}

# 独立别名（不参与「属性最高」命名规则的特定刻印）
# 鱼片 2026-11-01：圣战无限是适配特殊精灵的刻印，一般直接叫「无限」
FIXED_ALIAS = {
    "无限": ("巅峰圣战系列", "圣战之无限"),      # 双攻 攻50 特攻50 体113
    "无限α": ("巅峰圣战系列", "圣战之无限"),
    "九天": ("九天系列", "九曲之刃"),
}

# 某些系列里要排除出「属性命名」的基名（如圣战之无限独立成「无限」）
EXCLUDE_FROM_ATTR_NAMING = {
    "巅峰圣战系列": {"圣战之无限", "圣战之究极"},
}

# 表示「进阶」的名字标记，数值越大越新
ADV_MARK = ("[Ⅰ]", "αS", "α")


_SERIES_WORD_CACHE: dict[str, str] | None = None


def all_series_word() -> dict[str, str]:
    """静态 SERIES_WORD + 手册里**全部**系列的合并表。

    ★ 2026-11-09 加（P1：CLI 系列词表收口）：
      上面 SERIES_WORD 只有 14 条硬编码，而 `manual_db/mintmark.db`
      的 `MintMarkClass` 里有 97 个系列 —— 「九天攻」「圣战体」认识，
      「瓏域体」「鎏金攻」「四君子攻」这类就漏，因为不在表里。

      这里把系列名从库里读出来，**去掉「系列」后缀**当 key：
         九天系列      → 九天（静态表里已有，优先）
         瓏域系列      → 瓏域
         赤洲龙子系列  → 赤洲龙子
      静态表优先（它有人工调过的别名，如「幻灵」→ 巅峰战令系列、
      「战令」→ 巅峰战令系列，这些光靠去后缀推不出来）。
    """
    global _SERIES_WORD_CACHE
    if _SERIES_WORD_CACHE is not None:
        return _SERIES_WORD_CACHE

    merged = dict(SERIES_WORD)
    # ★ 2026-11-09：把 SERIES_ALIAS（24 条系列简称）也并进来。
    #   原先它**只**在 compose() 里做「系列名归一」，从没参与 resolve_mint 的
    #   系列识别 —— 所以「v10」「王誓」「赫星」「沧吟」这些简称在
    #   `seer.py mint <精灵>` 里全都认不出，而「v8」能认（它在 SERIES_WORD 里）。
    #   SERIES_ALIAS 的格式正好就是「简称 → 系列全名」，可以直接用。
    #   顺序：SERIES_WORD 优先（人工调过的别名更准），SERIES_ALIAS 补缺。
    for _k, _v in SERIES_ALIAS.items():
        merged.setdefault(_k, _v)
    merged.update(SERIES_WORD)
    try:
        con = _manual_db("mintmark.db")
        if con:
            try:
                for (nm,) in con.execute("select ClassName from MintMarkClass"):
                    nm = (nm or "").strip()
                    if not nm:
                        continue
                    merged.setdefault(nm, nm)
                    # ★ 2026-11-09：阈值从 >3 改成 >2。
                    #   手册里有一批**单字系列**（星系列 / 月系列 / 阳系列 / 庆系列），
                    #   名字长度正好 3，用 >3 会被全部漏掉 → 「星攻」查不出来。
                    if nm.endswith("系列") and len(nm) > 2:
                        merged.setdefault(nm[:-2], nm)
            finally:
                con.close()
    except Exception:  # noqa: BLE001
        pass

    # 长词优先（「赤洲龙子」必须排在「龙子」前面，否则会被截成「赤洲」+「龙子」）
    _SERIES_WORD_CACHE = dict(sorted(merged.items(), key=lambda x: -len(x[0])))
    return _SERIES_WORD_CACHE



def _series_pool(mints: list[dict], series: str) -> list[dict]:
    """取属于某个系列的刻印。

    ★ 2026-11-09 加（P1）：原来一律写 `series in class_name`，是**子串匹配**，
      会连环误伤：
        "月系列" in "偃月系列"   → True  「月攻」查成了偃月系列
        "星系列" in "群星系列"   → True  「星攻」查成了群星系列
        "庆系列" in "节日欢庆系列" → True
      先精确等值，取不到才退回子串（兼容 class_name 带修饰的情况）。
    """
    exact = [m for m in mints if (m.get("class_name") or "") == series]
    if exact:
        return exact
    return [m for m in mints if series in (m.get("class_name") or "")]


def _strip_adv(nm: str) -> str:
    """去掉进阶标记，得到基名。"""
    for mk in ("[Ⅰ]", "αS", "α"):
        nm = nm.replace(mk, "")
    return nm.strip()


def _adv_rank(m: dict) -> int:
    """进阶层级（越大越新）：αS=3, [Ⅰ]=2（不论带不带 α）, α=1, 无=0。"""
    nm = m["name"]
    if "αS" in nm:
        return 3
    if "[Ⅰ]" in nm:
        return 2
    if "α" in nm:
        return 1
    return 0


def _by_name_stem(q: str, mints: list[dict], stat: str | None,
                  style: str) -> dict | None:
    """按**刻印名前缀**匹配（系列识别失败或系列池为空时的兜底）。

    ★ 2026-11-09 加：有些简称根本不是系列名，而是刻印名前缀。
      V10 就是典型 —— 16 条刻印叫 `V10-01`…`V10-08`，但它们的
      `MintmarkClass` 是「十周年系列」（和「十年·光阴」混在一起）。
      按系列查会把「十年·光阴」也捞进来，挑出来的是错的。
      这里直接按名字前缀匹配，谁的名字以它开头就算谁。

    调用点有两处（缺一不可）：
      ① `series` 完全没识别出来
      ② `series` 识别出来了但**池子是空的**（如 v10 → V10系列，手册里没这系列）
    """
    stem = (q or "").strip()
    if not stem:
        return None
    # 去掉尾巴上的系列简称（避免 "龙子攻" 这种被误当名字前缀）
    for w in (load_alias_rules().get("series") or {}):
        if stem.endswith(w) and len(stem) > len(w):
            stem = stem[: -len(w)].strip()
            break
    # 去掉尾巴上的属性词（v10攻 → v10）
    for w in sorted(STAT_WORD.keys(), key=len, reverse=True):
        if stem.endswith(w) and len(stem) > len(w):
            stem = stem[: -len(w)].strip()
            break
    if len(stem) < 2:
        return None

    named = [m for m in mints
             if str(m.get("name") or "").upper().startswith(stem.upper())]
    if not named:
        return None
    if stat and stat != "_both_def":
        top = max(m["vals"][stat] for m in named)
        named = [m for m in named if m["vals"][stat] == top]
    if len(named) > 1:
        if style == "special" or style.endswith("special"):
            named.sort(key=lambda m: (-m["vals"]["sp_atk"], -m["total"]))
        else:
            named.sort(key=lambda m: (-m["vals"]["atk"], -m["total"]))
    return named[0]


def resolve_mint(query: str, mints: list[dict], style: str = "physical") -> dict | None:
    """把群友的简称解析成具体刻印。

    支持：
      "九天体"    九天系列里体力最高的（按 style 选物攻/特攻版）
      "圣战"      圣战系列里最高进阶（有 [Ⅰ] 取 [Ⅰ]，否则取 α）
      "v8"        V8 系列，有进阶取进阶
      "幻灵攻"    战令系列里攻击最高的
      "九天速"    九天系列里速度最高的
    也支持完整名字，直接匹配。
    """
    q = (query or "").strip()
    if not q:
        return None

    # ① 先按完整名字精确匹配
    for m in mints:
        if m["name"] == q:
            return m

    # ①.5 独立别名（无限 等）
    _rules = load_alias_rules()
    _fixed = _rules.get("fixed") or {}
    if q in _fixed:
        series, base = _fixed[q]
        pool0 = _series_pool(mints, series)
        same = [m for m in pool0 if _strip_adv(m["name"]) == base]
        if same:
            same.sort(key=lambda m: -_adv_rank(m))
            return same[0]

    # ② 拆出「系列词 + 属性词」
    #
    # ★ 2026-11-09：从 SERIES_WORD（14 条硬编码）换成 all_series_word()
    #   （静态 14 条 + 手册 97 个系列动态补全）。已按长度降序排好，
    #   保证「赤洲龙子」先于「龙子」被匹配。
    series = None
    stat = None
    for w, full in all_series_word().items():
        if w in q:
            series = full
            rest = q.replace(w, "")
            break
    else:
        rest = q
    for w, key in sorted(STAT_WORD.items(), key=lambda x: -len(x[0])):
        if w in rest:
            stat = key
            break

    if not series:
        return _by_name_stem(q, mints, stat, style)

    pool = _series_pool(mints, series)
    if not pool:
        # ★ 2026-11-09：系列名认出来了但手册里没这个系列 ——
        #   「v10」被 SERIES_ALIAS 映射成「V10系列」，而手册里
        #   V10 刻印实际挂在「十周年系列」下，池子必然为空。
        #   这里再走一次名字前缀兜底，否则会直接判「查不到」。
        return _by_name_stem(q, mints, stat, style)

    # ③ 同一「基名」只留一版：取六项总值最高的那版
    #    ★ 2026-11-08 修正：原来按名字里的进阶符号定序（αS=3 > [Ⅰ]=2），有两个坑——
    #      ① 泰坦系列 αS 总值 220，比 α[Ⅰ] 的 230 还低，却因符号排第一被选中；
    #      ② 同名多条（特级全能刻印×7、烈焰纹章×5、卡修斯专属×2）符号全一样，
    #         max() 取到哪条看运气。
    #      现在：符号只用来「分组」（判断是不是同一只的升级版），定序一律按数值。
    def base_name(nm: str) -> str:
        for mk in ("[Ⅰ]", "αS", "α"):
            nm = nm.replace(mk, "")
        return nm.strip()

    def _total_of(m: dict) -> int:
        v = m.get("vals") or {}
        return sum(int(v.get(k) or 0) for k in KEYS)

    excl = (load_alias_rules().get("exclude") or {}).get(series, set())
    best_by_base: dict[str, dict] = {}
    for m in pool:
        b = base_name(m["name"])
        if b in excl:
            continue
        cur = best_by_base.get(b)
        if cur is None:
            best_by_base[b] = m
            continue
        mt, ct = _total_of(m), _total_of(cur)
        # 总值相同时才用进阶符号收尾（纯 tie-break，不再主导排序）
        if mt > ct or (mt == ct and _adv_rank(m) > _adv_rank(cur)):
            best_by_base[b] = m
    cands = list(best_by_base.values())
    if not cands:                      # 全被排除时退回全集
        cands = pool

    # ④ 按属性词挑：找该项数值最高的
    if stat:
        if stat == "_both_def":
            key_fn = lambda m: m["vals"]["def"] + m["vals"]["sp_def"]
        else:
            key_fn = lambda m: m["vals"][stat]
        top = max(key_fn(m) for m in cands)
        cands = [m for m in cands if key_fn(m) == top]
        # 多个并列时按 style 选主攻项
        if len(cands) > 1:
            if style == "special" or style.endswith("special"):
                cands.sort(key=lambda m: -m["vals"]["sp_atk"])
            else:
                cands.sort(key=lambda m: -m["vals"]["atk"])
            return cands[0]
        return cands[0]

    # ⑤ 没给属性词：返回按 style 主攻项最高的
    if style == "special" or style.endswith("special"):
        cands.sort(key=lambda m: (-m["vals"]["sp_atk"], -m["total"]))
    else:
        cands.sort(key=lambda m: (-m["vals"]["atk"], -m["total"]))
    return cands[0]


def _manual_db(fname: str):
    for d in MANUAL_DIRS:
        f = os.path.join(d, fname)
        if os.path.isfile(f):
            return sqlite3.connect("file:" + f + "?mode=ro", uri=True)
    return None


def _kb_db():
    """连 kb.sqlite（别名规则的存放处）。"""
    if KB_DB and os.path.isfile(KB_DB):
        try:
            return sqlite3.connect("file:" + KB_DB + "?mode=ro", uri=True, timeout=3)
        except sqlite3.Error:
            return None
    return None


_RULES_CACHE: dict | None = None


def load_alias_rules() -> dict:
    """读 kb.mint_alias_rules 的三类规则；表不存在或读失败就用下面的硬编码兜底。

    ★ 2026-11-09 加（P2：别名规则迁表）。

      三类规则原先都写死在这个文件里：

        kind='fixed'    独立别名  「无限」→（巅峰圣战系列, 圣战之无限）
                                   —— 这类名字光靠「系列+属性词」推不出来
        kind='series'   系列简称  「龙子」→ 赤洲龙子系列
                                   —— 有些简称跟系列名差很远（v10→V10系列）
        kind='exclude'  属性命名排除  「圣战之无限」不从「圣战之X」里参与属性命名

      搬进 kb 之后，加规则不用改代码，直接往表里插一行。
      **表读不到时仍然用常量兜底**，所以这个文件单独拿去跑也不会崩。

    返回:
        {"fixed": {简称: (系列, 基名)},
         "series": {简称: 全名},
         "exclude": {系列: set(基名)}}
    """
    global _RULES_CACHE
    if _RULES_CACHE is not None:
        return _RULES_CACHE

    out = {
        "fixed": dict(FIXED_ALIAS),
        "series": dict(SERIES_ALIAS),
        "exclude": {k: set(v) for k, v in EXCLUDE_FROM_ATTR_NAMING.items()},
    }

    con = _kb_db()
    if con is not None:
        try:
            rows = con.execute(
                "select kind, name, arg1, arg2 from mint_alias_rules"
            ).fetchall()
            from_db = {"fixed": {}, "series": {}, "exclude": {}}
            for kind, name, a1, a2 in rows:
                if not kind or not name:
                    continue
                if kind == "fixed":
                    from_db["fixed"][name] = (a1, a2)
                elif kind == "series":
                    from_db["series"][name] = a1
                elif kind == "exclude":
                    from_db["exclude"].setdefault(a1, set()).add(a2 or name)
            for k, v in from_db.items():
                if v:
                    out[k] = v          # 表里有数据就以表为准
        except sqlite3.Error:
            pass
        finally:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass

    _RULES_CACHE = out
    return out



def _parse_vals(attri: str, maxim: str) -> list[int] | None:
    """MaxAttriValue 优先（含隐藏、满级），退而用 AttriValue。"""
    for raw in (maxim, attri):
        if not raw or raw == "[]":
            continue
        try:
            v = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(v, list) and len(v) == 6:
            return [int(x or 0) for x in v]
    return None


def load_mintmarks() -> list[dict]:
    """载入全部刻印，统一成 dict。"""
    con = _manual_db("mintmark.db")
    if not con:
        return []
    rows = con.execute(
        "select ID, Des, EffectDes, AttriValue, MaxAttriValue, Grade, MintmarkClass, Hide, Quality "
        "from MintMark"
    ).fetchall()
    classes = {r[0]: r[1] for r in con.execute("select ID, ClassName from MintMarkClass")}
    con.close()

    out = []
    seen: dict[tuple, dict] = {}      # (名字, 六项值) → 去重
    for id_, des, eff, attri, maxim, grade, cls, hide, q in rows:
        v = _parse_vals(attri, maxim)
        if not v:
            continue
        nz = sum(1 for x in v if x)
        if nz == 0:
            continue
        sig = ((des or ""), tuple(v))
        if sig in seen:
            continue
        seen[sig] = True
        cname = classes.get(cls, "") or ""
        out.append({
            "id": id_,
            "name": des or "",
            "effect": eff or "",
            "vals": dict(zip(KEYS, v)),
            "corner": nz,               # 几角
            "total": sum(v),
            "grade": grade,
            "class_id": cls,
            "class_name": cname,
            "hide": hide,
            "easy": any(e in cname for e in EASY_SERIES),
            "unique": any(u in (des or "") for u in UNIQUE_ONCE),
        })
    return out


def needs_310(stats: dict, soulmarks: list[str] | None = None) -> tuple[bool, str]:
    """判断这只精灵是否值得上 310 刻印（赤洲龙子·螭吻 / 帝皇·无极）。

    310 只有两只，共同点是「双攻各给 50+」，所以只对「双攻都吃收益」的精灵有价值。
    但注意：双攻种族相同 ≠ 一定要双刃印，要看魂印是不是把双攻「拉平」了。

    ⭐ 值得上 310 的三种情况：
      ① 种族总和 = 0（无为觉者）——面板全靠刻印，总和高最划算
      ② 魂印「双攻值视为二者之和」（星辰万象·天启星魂）——和越大越好，双刃印 +110 赢过单攻 +75
      ③ 攻 == 特攻 且 魂印没有「拉平到最高」的机制（修罗铠甲 111/111、卫岳 145/145）
         ——双攻都是面板真实值，两路都用得上

    ❌ 不值得上 310 的情况：
      · 魂印「攻击和特攻等于其中较高的一项」（天启星魂 #3661）
        ——拉平机制会把低的那项自动抬到高的，给单攻印时「另一项跟着一起涨」，
          收益 = 单印值 + 拉平补偿，反而比双刃印高。
      · 单向型（攻远大于特攻或反之）

    返回 (是否值得, 理由)
    """
    atk = int(stats.get("atk") or 0)
    spa = int(stats.get("sp_atk") or 0)
    total = sum(int(stats.get(k) or 0) for k in KEYS)
    sms = [t or "" for t in (soulmarks or [])]

    # ① 种族全 0
    if total == 0:
        return True, "种族全 0，面板完全由刻印决定，总和高最划算"

    # 先看魂印里有没有「拉平到最高」——有的话双刃印反而亏
    flatten = False
    for t in sms:
        if ("攻击和特攻" in t or "攻击、特攻" in t) and ("较高" in t or "最高" in t):
            flatten = True
            break
    # ② 双攻和结算 → 双刃印赢
    for t in sms:
        if "双攻" in t and "之和" in t:
            return True, "魂印把双攻视为「二者之和」，双刃印 +110 > 单攻印 +75"
        if "双攻" in t and "总和" in t:
            return True, "魂印按双攻总和结算，双刃印收益更高"

    # ③ 静态等高且无拉平机制
    if atk > 0 and atk == spa and not flatten:
        return True, f"双攻种族等高（{atk}/{spa}）且魂印不拉平，两路都要吃，双刃印收益翻倍"

    if flatten:
        high = max(atk, spa)
        low = min(atk, spa)
        return False, (
            f"魂印把双攻拉平到较高项（{high}）：给单{'攻' if atk>spa else '特攻'}印时，"
            f"另一项（{low}）会跟着涨到 {high}+印值，总收益反而超过双刃印"
        )

    return False, ""


def detect_style(atk: int, sp_atk: int) -> str:
    """判流派：物攻 / 特攻 / 双攻。"""
    if atk <= 0 and sp_atk <= 0:
        return "physical"
    if abs(atk - sp_atk) <= 10 and min(atk, sp_atk) > 0:
        return "dual"
    return "physical" if atk >= sp_atk else "special"


def score(mm: dict, style: str) -> float:
    """流派加权分。易得刻印（巅峰可刷）额外 +8，让实战首选排前面。"""
    w = WEIGHTS[style]
    base = sum(mm["vals"][k] * w.get(k, 0) for k in KEYS)
    return base + (8.0 if mm.get("easy") else 0.0)


def pick(
    mints: list[dict],
    style: str,
    corner: int | None = 5,
    series: str | None = None,
    top: int = 5,
    per_series: int = 2,
    allow_premium: bool = False,
    corners: list[int] | None = None,
) -> list[dict]:
    """按流派挑刻印，返回排序后的候选（未组合）。

    allow_premium=False 时排除总分 >= PREMIUM_TOTAL 的（310 那两只），
    因为实战提升很小且限定难获取。
    """
    want = set(corners) if corners else ({corner} if corner else {5})
    pool = [m for m in mints if m["corner"] in want]
    if not allow_premium:
        pool = [m for m in pool if m["total"] < PREMIUM_TOTAL]
    if series:
        target = (load_alias_rules().get("series") or {}).get(series, series)
        pool = [m for m in pool if target in (m.get("class_name") or "") or series == (m.get("class_name") or "") or series in (m.get("class_name") or "")]

    # 流派过滤：物攻型不要「缺攻击」的；特攻型不要「缺特攻」的
    def fit(m: dict) -> bool:
        v = m["vals"]
        if style == "shield":
            # 盾体向：要么双防都给了，要么体力特别高
            return (v["def"] > 0 and v["sp_def"] > 0) or v["hp"] >= 100
        if style.startswith("speed_"):
            # 卷速向：必须有速度，主攻项可为零但要有速度
            return v["spd"] > 0 and (v["sp_atk"] > 0 or v["atk"] > 0)
        if style.startswith("maxdiff"):
            # 极差向：绝不能加另一攻
            other = "sp_atk" if style.endswith("physical") else "atk"
            main = "atk" if style.endswith("physical") else "sp_atk"
            return v[other] == 0 and v[main] > 0
        if style == "physical":
            return v["atk"] > 0 and v["sp_atk"] == 0 or v["atk"] >= v["sp_atk"]
        if style == "special":
            return v["sp_atk"] > 0 and v["atk"] == 0 or v["sp_atk"] >= v["atk"]
        return v["atk"] > 0 and v["sp_atk"] > 0

    pool = [m for m in pool if fit(m)]
    for m in pool:
        m["_score"] = score(m, style)
    pool.sort(key=lambda m: (-m["_score"], -m["total"]))
    return pool[: max(top * 3, 30)]


def compose(cands: list[dict], per_series: int = 2, allow_dup: bool = True) -> list[list[dict]]:
    """组三刻印方案。

    · 同系列最多 per_series 个（默认 2）
    · allow_dup=True 时允许装两个**同名**刻印（游戏里可以，如「双无限α」）
    """
    plans: list[list[dict]] = []
    used_combos: set[tuple[int, ...]] = set()

    def key_of(plan: list[dict]) -> tuple:
        return tuple(sorted(m["id"] for m in plan))

    def backtrack(cur: list[dict], start: int) -> None:
        if len(cur) == 3:
            k = key_of(cur)
            if k not in used_combos:
                used_combos.add(k)
                plans.append(list(cur))
            return
        if len(plans) >= 6:
            return
        for i in range(start, len(cands)):
            m = cands[i]
            cn = m["class_name"]
            if cn and sum(1 for x in cur if x["class_name"] == cn) >= per_series:
                continue
            # 同名重复上限：
            #   · unique（只能获取一个的，如穹明拾光）→ 1
            #   · 五角 → per_series（游戏里可以装两个同款五角）
            #   · 四/三/二角 → 1
            if m.get("unique"):
                dup_limit = 1
            elif any(t in (m["class_name"] or "") for t in TWO_SETS_SERIES):
                dup_limit = 2          # 瓏域：两套，同款可装两个
            elif m["corner"] == 5:
                dup_limit = per_series
            else:
                dup_limit = 1
            if sum(1 for x in cur if x["id"] == m["id"]) >= dup_limit:
                continue
            cur.append(m)
            _can_dup = (m["corner"] == 5 or any(t in (m["class_name"] or "") for t in TWO_SETS_SERIES))
            if allow_dup and _can_dup and not m.get("unique"):
                # 允许下一轮从同一个 i 开始（装第二个同名）
                backtrack(cur, i)
            else:
                backtrack(cur, i + 1)
            cur.pop()
            if len(plans) >= 6:
                return

    backtrack([], 0)
    return plans


def format_mm(m: dict) -> str:
    parts = [f"{CN[k]}{m['vals'][k]}" for k in KEYS if m["vals"][k]]
    return f"{m['name']}（{m['corner']}角 总{m['total']}）{' '.join(parts)}"


def format_plan(plan: list[dict], style: str) -> str:
    tot = {k: sum(m["vals"][k] for m in plan) for k in KEYS}
    detail = "  ".join(f"{CN[k]}+{tot[k]}" for k in KEYS if tot[k])
    return detail
