"""伤害估算：给出拆解，不假装精确。

赛尔号的伤害公式与宝可梦同源：基础值由等级、威力、攻防决定，
再乘本系、克制、暴击、随机浮动与其他修正（魂印/特性/天气等）。

这里只实现「可计算的部分」，并把不可知修正显式列出来，
避免模型把估算值当成确定事实。
"""
from __future__ import annotations

import math



def apply_resist(amount: float, resist: float) -> float:
    """按赛尔号口径过一个伤害抗性：实扣 = floor(伤害量 × 抗性)，剩余 = 伤害量 - 实扣。

    例：1 点固伤/粉伤过 35% 抗性 → 1×0.35=0.35 → floor=0 → 扣 0 → 实际打出 1 点。
    """
    if amount <= 0:
        return 0.0
    if resist <= 0:
        return amount
    # +1e-9 防止浮点误差把 245.0 算成 244.99999 导致 floor 少 1
    return amount - math.floor(amount * resist + 1e-9)


def stage_mult(stage: int) -> float:
    """能力等级 → 面板系数（《赛学必修1》cv34339526）。

    +1~+6 = 150% / 200% / 250% / 300% / 350% / 400%  → (1 + 0.5n)
    -1~-6 = 66.6% / 50% / 40% / 33.3% / 28.5% / 25% → 1 / (1 + 0.5n)

    注意下降**不是**「每级减 0.5」（那样 -2 会归零）。
    上下限 ±6。命中另有一套系数，不用于伤害计算。
    """
    s = max(-6, min(6, int(stage or 0)))
    if s >= 0:
        return 1.0 + 0.5 * s
    return 1.0 / (1.0 + 0.5 * abs(s))


def base_damage(level: int, power: int, atk: int, defense: int) -> float:
    """基础伤害 = (((等级×0.4+2)×威力×攻击/防御)/50+2)"""
    if defense <= 0 or power <= 0:
        return 0.0
    return (((level * 0.4 + 2) * power * atk / defense) / 50.0) + 2


def estimate(
    level: int,
    power: int,
    atk: int,
    defense: int,
    *,
    type_mult: float = 1.0,
    stab: float = 1.0,
    crit: float = 1.0,
    random_factor: float = 1.0,
    other: float = 1.0,
) -> dict:
    base = base_damage(level, power, atk, defense)
    total = base * type_mult * stab * crit * random_factor * other
    # ★ 浮动下限必须是 217/255（=0.850980…），不能用 0.85 近似。
    #   赛尔号伤害浮动是 (217~255)/255 的 39 个离散整数档，每档 1/39，
    #   不是 [0.85, 1.0] 的连续区间。用 0.85 会低估最低伤害约 0.115%，
    #   且区间写法会让人误以为是连续取值。
    return {
        "base": round(base, 1),
        "type_mult": type_mult,
        "stab": stab,
        "crit": crit,
        "random_factor": random_factor,
        "other": other,
        "final": round(total, 1),
        "min": round(total * (217 / 255), 1),
        "max": round(total * 1.0, 1),
        "float_min_num": 217,
        "float_max_num": 255,
        "float_levels": 39,
    }


def explain(result: dict, level: int, power: int, atk: int, defense: int) -> str:
    lines = [
        f"基础值 = (({level}×0.4+2)×{power}×{atk}/{defense})/50+2 = {result['base']}",
        f"克制 ×{result['type_mult']}｜本系 ×{result['stab']}｜暴击 ×{result['crit']}"
        f"｜浮动 ×{result['random_factor']}｜其他 ×{result['other']}",
        f"估算伤害 ≈ {result['final']}（浮动区间 {result['min']}~{result['max']}）",
    ]
    return "\n".join(lines)


def settle(
    level: int,
    power: int,
    atk: int,
    defense: int,
    *,
    type_mult: float = 1.0,
    type_mult_cap: float = 0.0,
    stab: float = 1.0,
    crit: float = 1.0,
    random_factor: float = 1.0,
    add_pct: float = 0.0,
    mul_pct: float = 0.0,
    reduce_pct: float = 0.0,
    general_add_pct: float = 0.0,
    extra_add_pct: float = 0.0,
    extra_add_more: list[float] | None = None,
    general_reduce_pct: float = 0.0,
    extra_reduce_pct: float = 0.0,
    extra_reduce_more: list[float] | None = None,
    crit_reduce_pct: float = 0.35,
    flat_damage: float = 0.0,
    flat_reduce: float = 0.35,
    percent_damage: float = 0.0,
    percent_reduce: float = 0.35,
    flat_segments: list[float] | None = None,
    percent_segments: list[float] | None = None,
    true_damage: float = 0.0,
    attr_damage: float = 0.0,
    attr_mult: float = 1.0,
    other: float = 1.0,
    target_hp: float = 0.0,
) -> dict:
    """多通道伤害结算（参考「伤害计算器 1.1」的输入维度）。

    通道 1 攻击伤害（红伤）：基础 × 克制 × 本系 × 暴击 × (1+通用增伤之和) × Π(1+额外增伤)
                            × 浮动 × other → 减伤 → 暴击减免
    通道 2 固定伤害（固伤）：数值 → 固定伤害抗性
    通道 3 百分比伤害（粉伤）：目标最大体力 × 比例 → 百分比伤害抗性
    通道 4 真实伤害（真伤）：不参与任何减免
    通道 5 属性伤害：基础值 × 克制倍率，floor 后直通

    增减伤归类（玩家确认）：
      通用增伤 / 通用减伤 —— 同类之间【相加】；
      额外增伤 / 额外减伤 —— 每个都是【独立乘区】，彼此之间【连乘】，再与通用相乘。
      总减伤 = 1 - (1 - 通用减伤之和) × Π(1 - 额外减伤ᵢ)
      例：两个额外增伤 30% + 20% → 1.30×1.20 = 1.56（不是 1.50）。
      增伤与减伤是两个方向，不允许互相抵扣。

    通道 5 的口径（玩家确认，见 facts 226/228/229）：250 点属性伤害
    **吃克制、不吃抗性、不吃本系加成 1.5、护盾与护罩都不挡、不吃锁伤/晨曦**。
    因此这里只做 floor(属性值 × 克制倍率)，不过任何减免，也不吃增伤。
    克制倍率由调用方算好传入 `attr_mult`；是否受「克制倍数封顶」影响由调用方决定。

    抗性口径：实扣 = floor(伤害量 × 抗性)，余数保留（1 点粉伤过 35% → 打出 1 点）。
    多段固伤/粉伤必须逐段过抗再相加，不能先合计再乘一次抗性。

    默认前提：对手三项抗性（暴击伤害 / 固定伤害 / 百分比伤害）按满值 35% 处理，
    与赛尔号抗性上限一致；若对手未点满，显式传更小的值覆盖。
    """
    base = base_damage(level, power, atk, defense)
    if type_mult_cap and type_mult_cap > 0:
        # 例：战之狂热形态「受到的攻击克制倍数封顶2倍」
        type_mult = min(type_mult, type_mult_cap)
    # 增伤：通用之间【相加】；额外是独立乘区，多个额外之间【也连乘】，再与通用相乘。
    # 例：两个额外 30% / 20% → 1.30×1.20=1.56，不是 1+0.3+0.2=1.50（玩家 2026-10-28 确认）
    general_add = add_pct + general_add_pct
    extra_factors = [1 + mul_pct, 1 + extra_add_pct] + [1 + float(x) for x in (extra_add_more or [])]
    extra_factor = 1.0
    for f in extra_factors:
        extra_factor *= f
    extra_add = extra_factor - 1.0
    # 减伤：通用之间相加；额外独立乘区，多个额外减伤按同一规则连乘
    # 总减伤 = 1 - (1-通用) × Π(1-额外ᵢ)
    general_reduce = reduce_pct + general_reduce_pct
    reduce_factor = 1 - min(general_reduce, 1.0)
    for e in [extra_reduce_pct] + [float(x) for x in (extra_reduce_more or [])]:
        reduce_factor *= 1 - min(e, 1.0)
    total_reduce = 1 - reduce_factor
    attack = base * type_mult * stab * crit * (1 + general_add) * extra_factor * random_factor * other
    after_reduce = attack * (1 - total_reduce)
    if crit > 1:
        # 暴击抗性口径（已与用户确认）：实际倍率 = 2 × (1-抗性)，即整段 ×(1-抗性)
        after_reduce *= 1 - crit_reduce_pct
    # 固伤：多段分别过抗性，每段先把「量」取整再过抗，最后相加
    flat_list = [float(x) for x in (flat_segments or [])]
    if flat_damage:
        flat_list.insert(0, float(flat_damage))
    flat_parts = [apply_resist(math.floor(x), flat_reduce) for x in flat_list]
    flat = sum(flat_parts)

    # 百分比伤害：多段分别过抗性（每段 = 最大体力 × 系数）
    # 【口径】先把「量」向下取整，再过抗：floor(H×系数) 后按 floor(量×抗) 扣。
    # 依据：战狂粉「floor((H+攻击)×25%) 再按 floor(量×抗)」、龙琰醒「floor(H/3) 再过抗」。
    # 例 H=1100 的 1/8 = 137.5 → 先 floor 成 137 → 实扣 137-47=90；
    # 若不先取整直接用 137.5 过抗会得 89，差 1 点。
    pct_coeffs = [float(x) for x in (percent_segments or [])]
    if percent_damage:
        pct_coeffs.insert(0, float(percent_damage))
    pct_parts = [apply_resist(math.floor(target_hp * c), percent_reduce) for c in pct_coeffs]
    pct = sum(pct_parts)

    # 属性伤害：只吃克制，不过抗性、不吃本系、不吃增伤、不挡盾罩
    attr = math.floor(float(attr_damage) * float(attr_mult)) if attr_damage else 0

    # 最终一律向下取整（与游戏一致）
    after_reduce = math.floor(after_reduce)
    flat = math.floor(flat)
    pct = math.floor(pct)
    true_damage = math.floor(true_damage)
    total = after_reduce + flat + pct + true_damage + attr
    return {
        "base": round(base, 1),
        "type_mult": type_mult,
        "type_mult_cap": type_mult_cap,
        "general_add": round(general_add, 3),
        "extra_add": round(extra_add, 3),
        "general_reduce": round(general_reduce, 3),
        "extra_reduce": round(extra_reduce_pct, 3),
        "total_reduce": round(total_reduce, 3),
        "other": round(other, 3),
        "attack_channel": round(after_reduce, 1),
        "flat_channel": round(flat, 1),
        "flat_parts": [round(x, 1) for x in flat_parts],
        "percent_channel": round(pct, 1),
        "percent_parts": [round(x, 1) for x in pct_parts],
        "true_channel": round(true_damage, 1),
        "attr_channel": round(attr, 1),
        "attr_mult": attr_mult,
        "total": round(total, 1),
    }


def combo_damage(
    level: int,
    power: int,
    atk: int,
    defense: int,
    *,
    stab: float = 1.0,
    type_mult: float = 1.0,
    crit: float = 1.0,
    crit_reduce_pct: float = 0.35,
    crit_add_pct: float = 0.0,
    general_add: float = 0.0,
    extra_add_pct: float = 0.0,
    atk_stage: int = 0,
    defense_stage: int = 0,
    random_factor: float | None = None,
    hit_count: int = 1,
    reduce_total: float = 0.0,
) -> dict:
    """新版连击结算（赛学扩展4，cv46281052）。

    公式：
      [(L×0.4+2)×威力×攻击÷防御÷50+2] × 本系 × 克制 × (217~255)/255
      × 暴击抗性 × 暴击系数 × 连击次数

    取整规则（照抄专栏）：
      · 全部向下取整；方括号内是整体不可拆分；
      · **伤害浮动算完要取整，从浮动之后每一步计算都要向下取整**。

    所以连击的伤害不是「每段各算各的再相加」，而是
    「算一次单段 → 中间取整 → 最后乘连击次数」，浮动只掷一次。
    低威力高连击会被取整吃掉大量伤害（冰王先三恒定 90，只有浮动 255 才 180）。

    random_factor=None 时按区间返回 min(217/255) 与 max(255/255)。
    reduce_total 是额外叠加的总减伤，**专栏公式未包含增减伤**，
    这里按「乘完连击次数后再减」处理，属推断。
    """
    # 能力等级先换算成实际面板值再代入（《赛学扩展4》要求 / 必修1 系数表）
    atk = int(round(atk * stage_mult(atk_stage)))
    defense = max(1, int(round(defense * stage_mult(defense_stage))))
    base = base_damage(level, power, atk, defense)
    after_stab = base * stab * type_mult
    # 第 1 次取整：本系 × 克制之后（方括号整体算完）
    step1 = float(math.floor(after_stab))

    def _through(rf: float) -> tuple[float, float, float]:
        # 第 2 次取整：伤害浮动之后
        after_float = float(math.floor(step1 * rf))
        # 【暴击环·单一乘区】过抗与暴击系数合成一个乘区一次 floor：
        #   暴击过抗 = 暴击系数 × (1 − 暴击抗性)，例 2.0×0.65 = 1.3
        # 之前分成「×0.65 floor → ×2.0 floor」两步，会多吃一次小数（玩家指正）。
        after_crit = after_float
        if crit > 1:
            after_crit = float(
                math.floor(after_float * crit * (1 - crit_reduce_pct))
            )
        # 【通用增伤·统一乘区】爆伤增伤(crit_add_pct) **不是**独立乘区，
        # 它与晨曦等其他通用增伤**相加**后作为同一乘区一次乘、一次 floor。
        # 例：爆伤25% + 晨曦20% → ×1.45（不是 ×1.25 再 ×1.20）。
        # 【连击】先把单段乘成总伤
        after_hits = float(math.floor(after_crit * max(1, int(hit_count))))
        # 【增伤·全部作用在「总伤」之后】玩家口径：
        # 通用增伤（爆伤宝石/强能宝石/绝命宝石/晨曦…）与额外增伤（强袭/精神…）
        # 都**不是**乘在单段上的——单段只有 1~2 时会被 floor 吃掉
        # （floor(1×1.25)=1、floor(2×1.25)=2，看着像"完全无效"），
        # 乘在总伤上才体现（90×1.25=112、180×1.25=225）。
        # 通用之间相加成一个乘区；额外之间连乘（各乘一次）。
        _add_total = float(general_add or 0.0) + float(crit_add_pct or 0.0)
        if _add_total:
            after_hits = float(math.floor(after_hits * (1 + _add_total)))
        _extra = float(extra_add_pct or 0.0)
        if _extra:
            after_hits = float(math.floor(after_hits * (1 + _extra)))
        if reduce_total:
            after_hits = float(math.floor(after_hits * (1 - min(reduce_total, 1.0))))
        return after_float, after_crit, after_hits

    if random_factor is None:
        lo_float, lo_crit, lo_total = _through(217 / 255)
        hi_float, hi_crit, hi_total = _through(255 / 255)
    else:
        rf = max(0.0, min(float(random_factor), 1.0))
        lo_float, lo_crit, lo_total = _through(rf)
        hi_float, hi_crit, hi_total = lo_float, lo_crit, lo_total

    return {
        "base": round(base, 4),
        "after_stab": round(after_stab, 4),
        "step1": step1,
        "single_min": lo_float,
        "single_max": hi_float,
        "crit_single_min": lo_crit,
        "crit_single_max": hi_crit,
        "atk_real": atk,
        "defense_real": defense,
        "atk_stage_mult": round(stage_mult(atk_stage), 4),
        "def_stage_mult": round(stage_mult(defense_stage), 4),
        "add_total": round(float(general_add or 0.0) + float(crit_add_pct or 0.0), 4),
        "extra_add_pct": round(float(extra_add_pct or 0.0), 4),
        "crit_mult_net": round(crit * (1 - crit_reduce_pct), 4) if crit > 1 else 1.0,
        "hit_count": int(hit_count),
        "total_min": lo_total,
        "total_max": hi_total,
        "crit": crit,
        "crit_reduce_pct": crit_reduce_pct,
        "reduce_total": reduce_total,
        "random_given": random_factor is not None,
    }


def explain_combo(r: dict) -> str:
    """把连击结算拆成人能看的过程。"""
    lines = [
        f"连击结算（新版口径，cv46281052）",
        f"① 方括号内 = {r['base']}",
        f"② ×本系×克制 = {r['after_stab']} → 取整 {r['step1']:.0f}",
    ]
    if r["random_given"]:
        lines.append(f"③ ×浮动 → 取整 {r['single_min']:.0f}（单段）")
    else:
        lines.append(
            f"③ ×浮动217~255 → 单段 {r['single_min']:.0f}~{r['single_max']:.0f}"
        )
    if r["crit"] > 1:
        lines.append(
            f"④ ×暴击抗性(1-{r['crit_reduce_pct']:.0%})×暴击系数{r['crit']:g}"
            f" → 单段 {r['crit_single_min']:.0f}~{r['crit_single_max']:.0f}"
        )
    lines.append(f"⑤ ×连击次数 {r['hit_count']}  → 合计 {r['total_min']:.0f}~{r['total_max']:.0f}")
    if r["reduce_total"]:
        lines.append(f"⑥ 再叠总减伤 {r['reduce_total']:.1%}（专栏公式未含，属推断）")
    lines.append("注：浮动只掷一次、连击次数最后只乘一次；低威力连击会被取整大幅削掉。")
    return "\n".join(lines)


def explain_settle(
    r: dict,
    *,
    crit: float = 1.0,
    crit_reduce_pct: float = 0.35,
    flat_reduce: float = 0.35,
    percent_reduce: float = 0.35,
) -> str:
    crit_note = (
        f"暴击已扣 {crit_reduce_pct:.0%} 抗性"
        if crit > 1
        else "未暴击，不扣暴击抗性"
    )
    return (
        f"前提：对手抗性按满值处理（暴击 {crit_reduce_pct:.0%}，仅在暴击时生效 / "
        f"固伤 {flat_reduce:.0%} / 百分比 {percent_reduce:.0%}）\n"
        f"乘区：通用增伤 +{r.get('general_add', 0):.0%}｜额外增伤 ×{1 + r.get('extra_add', 0):.3f}"
        f"｜通用减伤 {r.get('general_reduce', 0):.0%}｜额外减伤 {r.get('extra_reduce', 0):.0%}"
        f"→ 总减伤 {r.get('total_reduce', 0):.1%}（增伤与减伤不互相抵扣）\n"
        f"攻击伤害通道 ≈ {r['attack_channel']}（基础 {r['base']}，{crit_note}）\n"
        f"固定伤害通道 ≈ {r['flat_channel']}"
        + (f"（分段 {r.get('flat_parts')}，各自过抗后相加）" if len(r.get("flat_parts") or []) > 1 else "")
        + "\n"
        f"百分比伤害通道 ≈ {r['percent_channel']}"
        + (f"（分段 {r.get('percent_parts')}，各自过抗后相加）" if len(r.get("percent_parts") or []) > 1 else "")
        + "\n"
        f"真实伤害通道 ≈ {r['true_channel']}\n"
        + (f"属性伤害通道 ≈ {r['attr_channel']}（克制 ×{r.get('attr_mult', 1.0)}，不过抗性/不吃本系/不挡盾罩）\n"
           if r.get("attr_channel") else "")
        + f"合计 ≈ {r['total']}"
    )
