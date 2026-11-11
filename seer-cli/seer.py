#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""seer.py — 赛尔号知识库命令行查询器

把 astrbot_plugin_seer_expert 的 SeerKnowledge 包装成 CLI，
让任何 agent（DSH / Claude / 脚本）都能用 shell 查库。

用法：
    python seer.py pet 灵巢
    python seer.py skill 千魂之结
    python seer.py type 轮回 自然/超能
    python seer.py facts 转移
    python seer.py glossary 最优系别
    python seer.py alias 古龙
    python seer.py tags 4599
    python seer.py pool 灵巢
    python seer.py status
    python seer.py raw "select count(*) from facts"

    --json    输出 JSON（给程序解析）
    --data D  指定 plugin_data 目录
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from typing import Any

# ─── 定位插件与数据 ────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_CANDIDATES = [
    # ★ 运行中的插件目录放第一位：验证必须验「群里实际跑的那份」，
    #   否则会出现「改了 A 却验了 B、两边悄悄分叉」的事故（2026-11-02 踩过）。
    r"G:\temp\instances\eb74352a-db17-4359-aade-ba98505d2e47\core\data\plugins\astrbot_plugin_seer_expert",
    os.path.join(HERE, "..", "astrbot_plugin_seer_expert"),
    r"G:\dsh workspace\astrbot_plugin_seer_expert",
]
DATA_CANDIDATES = [
    r"G:\temp\instances\eb74352a-db17-4359-aade-ba98505d2e47\core\data\plugin_data",
]


def _find_plugin_dir() -> str:
    for p in PLUGIN_CANDIDATES:
        p = os.path.abspath(p)
        if os.path.isfile(os.path.join(p, "db.py")):
            return p
    raise SystemExit("找不到 astrbot_plugin_seer_expert 目录（含 db.py）")


def _find_data_dir(override: str | None) -> str:
    if override:
        if not os.path.isdir(override):
            raise SystemExit(f"--data 目录不存在: {override}")
        return override
    env = os.environ.get("SEER_DATA_DIR")
    if env and os.path.isdir(env):
        return env
    for p in DATA_CANDIDATES:
        if os.path.isdir(os.path.join(p, "astrbot_plugin_seer_expert")):
            return p
    raise SystemExit("找不到 plugin_data 目录，请用 --data 或 SEER_DATA_DIR 指定")


def open_kb(data_dir: str):
    """打开 SeerKnowledge。"""
    sys.path.insert(0, _find_plugin_dir())
    import db  # type: ignore

    seer = os.path.join(data_dir, "astrbot_plugin_seer_info", "seerapi.sqlite")
    kb = os.path.join(data_dir, "astrbot_plugin_seer_expert", "kb.sqlite")
    if not os.path.isfile(seer):
        raise SystemExit(f"seerapi.sqlite 不存在: {seer}")
    if not os.path.isfile(kb):
        raise SystemExit(f"kb.sqlite 不存在: {kb}")
    return db.SeerKnowledge(seer, kb)


# ─── 输出helper ──────────────────────────────────────────────────
def out(text: str = "") -> None:
    # Windows 的 cmd/PowerShell 默认 GBK 代码页，直接 write ✅/❌ 会抛
    # UnicodeEncodeError（selftest 就是这么挂的）。编码不了就替换成 '?'，
    # 绝不能因为一个符号把整个命令炸掉。
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.write(text.encode(enc, "replace").decode(enc) + "\n")


def dump(obj: Any) -> None:
    out(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def section(title: str) -> None:
    out(f"\n【{title}】")


# ─── 子命令 ──────────────────────────────────────────────────────
def cmd_pet(k: Any, args) -> None:
    """精灵全景：属性/种族/魂印/技能/简介。

    ★ 数据与判定全部来自 k.pet_view()（语义层，bot 和 CLI 共用一份），
      这里只负责排版。这样「形态选择、同名警告、数据不全警告」这些判定
      不会在两边分叉 —— 以前两边各写一份，改一处就漏一处。
    """
    v = k.pet_view(args.name)
    if not v:
        out(f"未找到精灵：{args.name}")
        return
    if args.json:
        dump({
            "pet_id": v["pid"],
            "name": v["name"],
            "types": v["types"],
            "stats": v["stats"],
            "stats_base": v["stats_base"],
            "soulmarks": [f.get("desc") for f in v["soulmark_forms"]],
            "skills": v["skills"],
            "aliases": v["aliases"],
            "moveset": v["moveset"],
            "candidates": v["candidates"],
            "form_hint": v["form_hint"],
            "exact_warn": v["exact_warn"],
        })
        return

    out(f"# {v['name']}  #{v['pid']}")

    # ── 判定字段：两边都必须渲染（selftest 有契约检查）──
    for key in ("form_hint", "exact_warn", "alias_chain_hint",
                "alias_note", "other_aliases_hint", "sparse_warn"):
        if v.get(key):
            out(v[key])

    out(f"属性：{'/'.join(v['types']) if v['types'] else '?'}")

    st = v["stats"] or {}
    if st:
        tot = sum(x for kk, x in st.items() if kk != "percent" and isinstance(x, (int, float)))
        label = "种族（觉醒后）" if v["stats_label_awakened"] else "种族"
        out(
            f"{label}：攻{st.get('atk')} 防{st.get('def')} 特攻{st.get('sp_atk')} "
            f"特防{st.get('sp_def')} 速{st.get('spd')} 体{st.get('hp')} 和{tot}"
        )
    if v["stats_base"]:
        b = v["stats_base"]
        out(
            f"觉醒前：攻{b.get('atk')} 防{b.get('def')} 特攻{b.get('sp_atk')} "
            f"特防{b.get('sp_def')} 速{b.get('spd')} 体{b.get('hp')}"
        )

    if v["aliases"]:
        out(f"俗称：{'、'.join(v['aliases'])}")

    if v["moveset"]:
        out(f"✅ 已定型配招：{'、'.join(v['moveset'])}")

    # 魂印：多版本（标出当前生效 / 原始参考）
    if v["soulmark_forms"]:
        section("魂印")
        for f in v["soulmark_forms"]:
            tag = "当前生效" if f.get("primary") else "原始/参考"
            out(f"〔{tag}·{f.get('form')}·{f.get('pve')}〕{f.get('desc')}")
    if v["soulmark_bonus"]:
        section("羁绊强化")
        for u in v["soulmark_bonus"]:
            out(f"- 「{u.get('group')}」（需{u.get('need')}只）：原→{u.get('before')}｜后→{u.get('after')}")
    if v["soulmark_refs"]:
        section("魂印里提到的专属效果")
        for e in v["soulmark_refs"]:
            out(f"- {e.get('name')}={e.get('desc')}")
    if v["effects"]:
        section("专属效果")
        for e in v["effects"]:
            out(f"- {e.get('name')}={e.get('desc')}")

    sk = v["skills"]
    if sk:
        section(f"技能（{len(sk)} 条）")
        # ★ Lv0 技能 = 靠道具/兑换开启的后期补强（2026-11-03）
        #   王·联盟审判 威350、魔·黑天暗罚 威350、零时诳语 改魂印……
        #   这些不靠升级学，混在常规技能里极容易漏，所以打上标记。
        _lv0n = sum(1 for s in sk if s.get("level") == 0)
        if _lv0n:
            out(f"  （其中 {_lv0n} 条是 Lv0·道具开启的补强技能，见【Lv0】标记）")
        for s in sk:
            parts = []
            if s.get("fifth"):
                parts.append("【第五】")
            if s.get("level") == 0:
                parts.append("【Lv0·道具】")
            parts.append(s.get("name") or "?")
            parts.append(f"威{s.get('power')}")
            if s.get("type"):
                parts.append(str(s["type"]))
            if s.get("priority"):
                parts.append(f"先制{s['priority']:+d}")
            out("- " + " ".join(parts))

    if v["intro"]:
        section("图鉴")
        out(v["intro"][:600])

    if v["pet_rules"]:
        section(f"玩家已确认规则（{len(v['pet_rules'])} 条）")
        for topic, content in v["pet_rules"]:
            out(f"- {topic}：{str(content)[:160]}")



def cmd_skill(k: Any, args) -> None:
    """技能详情（含固有效果 vs 命中的〔固有〕〔命中〕标记）。

    查不到的判定（尤其是「这是精灵不是技能」的引导）来自 k.skill_view()，
    和 bot 侧共用一份。
    """
    v = k.skill_view(args.name)
    if args.json:
        dump(v)
        return
    if not v["found"]:
        out(v.get("empty_hint") or f"未收录技能「{args.name}」。")
        return

    out(f"# {v['name']}")
    extra = ""
    if v.get("atk_num") and v["atk_num"] != 1:
        extra += f" ｜ 攻击 {v['atk_num']} 次"
    if v.get("crit_rate") and v["crit_rate"] != 1:
        extra += " ｜ 暴击率 " + ("1/16" if v["crit_rate"] == 6.25 else f"×{v['crit_rate']}")
    out(
        "威力 {p} ｜ 类别 {c} ｜ 系别 {t} ｜ PP {pp} ｜ 命中 {acc} ｜ 先制 {pri} ｜ 必中 {mh}{ex}".format(
            p=v["power"], c=v["category"], t=v["type"], pp=v["pp"],
            acc=v["accuracy"], pri=v["priority"],
            mh="是" if v["must_hit"] else "否", ex=extra,
        )
    )
    if v["info"]:
        out(f"说明：{v['info']}")
    if v["effects"]:
        section("效果")
        for e in v["effects"]:
            out(f"  {e}")
    # 复合技能：本技能「拥有别的技能的效果」时，把被引用的技能列出来
    # （神诏之诀别引用 7 个、超越式·变革律引用 4 个协议）
    _refs = v.get("referenced") or []
    if _refs:
        section("★ 本技能引用了其他技能" + ("（构造效果）" if any(r.get("via") == "构造" for r in _refs) else ""))
        for r in _refs:
            _pw = f"威{r['power']}（按25%计）" if r.get("power") else "威0"
            out(f"  · {r['name']} {_pw}")
        out("  ⚠ 只继承「效果」与 25% 威力值，先制/命中/PP 这些技能属性不继承")
        out("  ★ 按原文「每获得1层」＝累计获得计数：获得过 N 层就永久拥有前 N 个，")
        out("    层数每回合减半只影响神灵系/免PP/反伤，不回退已解锁的效果。")
    # 连击提示：base 是裸次数，带修正时必须代入场景
    ch = v.get("combo_hint")
    if ch:
        section("连击")
        out(f"  句首次数：{ch['base']}" + ("（固定，可直接用）" if ch["fixed"] else "（**带修正，不能直接用**）"))
        out(f"  原文：{ch['text']}")
        if ch.get("warn"):
            out(f"  ⚠ {ch['warn']}")



def cmd_type(k: Any, args) -> None:
    """克制计算。seer.py type 轮回 自然/超能

    倍率、拆解、合成规则、缺失警告全部来自 k.type_view()（语义层），
    这里只排版 —— bot 侧 seer_type_chart 调同一个 type_view，
    所以「双2特例 / 含0除4 / 其余平均」这套规则不会两边分叉。
    """
    v = k.type_view(args.atk, args.dfd)
    if v.get("error") or not v["atk"] or not v["dfd"]:
        out(f"属性解析失败：{v.get('error') or args.atk + ' / ' + args.dfd}")
        out("可用属性：")
        out("  " + "、".join(v.get("available") or k.type_names()))
        return

    if args.json:
        dump(v)
        return

    out(f"# {'/'.join(v['atk'])}  →  {'/'.join(v['dfd'])}")
    out(f"克制倍率：×{v['mult']}（{v['feel']}）")

    if v.get("missing_hint"):
        out(v["missing_hint"])

    pairs = v.get("pairs") or []
    if len(pairs) > 1 or len(v["atk"]) > 1 or len(v["dfd"]) > 1:
        section("拆解")
        for a, dd, m in pairs:
            out(f"  {a}→{dd} = ×{m}")
        if v.get("rule_hint"):
            out("  合成规则：" + v["rule_hint"])



def cmd_facts(k: Any, args) -> None:
    """facts 检索（玩家口述的规则）。

    命中与「没搜到」的判定都来自 k.facts_view()（语义层，和 bot 共用）。
    """
    v = k.facts_view(args.keyword, limit=getattr(args, "limit", 5))
    if args.json:
        dump(v)
        return
    if not v["hits"]:
        out(v.get("empty_hint") or "没有匹配的 facts。")
        return
    out(f"# facts 命中 {len(v['hits'])} 条")
    for h in v["hits"]:
        out(chr(10) + "## " + h["topic"])
        out(h["content"])
    if v.get("rules_text"):
        section("解析节点清单")
        out(v["rules_text"][:1200])



def cmd_topics(k: Any, args) -> None:
    """列出全部 facts 主题。"""
    topics = k.fact_topics()
    out(f"# facts 主题（{len(topics)} 条）")
    for i, t in enumerate(topics, 1):
        out(f"{i:>3}. {t}")


def cmd_glossary(k: Any, args) -> None:
    """官方词条：语法解释 / 时点解释 / 精灵专属名词 / 异常状态 / 规则名词。

    来源 manual_db/effectDes.db（手册），比 seerapi.glossary_entry 镜像更全
    （异常 91 条 vs 48 条，且多一类「免疫」）。
    类别的枚举、空结果提示、参数占位说明全部来自 k.glossary_view()。
    """
    kind = (getattr(args, "kind", "") or "").strip()
    kw = (getattr(args, "keyword", "") or "").strip()
    pet = (getattr(args, "pet", "") or "").strip()
    v = k.glossary_view(kw, kind, limit=int(getattr(args, "limit", 6) or 6), pet=pet)

    if args.json:
        dump(v)
        return

    # ── 无参数：列类别 ──
    if v["mode"] == "categories":
        out("官方词条类别（seer.py glossary --kind <类别> 列出来）")
        out("")
        for label, desc in v["categories"]:
            out(f"  {label:<6}{desc}")
        out("")
        out("查具体词条：seer.py glossary 未触发")
        return

    # ── 空结果：直接用语义层的提示 ──
    if v.get("empty_hint"):
        out(v["empty_hint"])
        return

    # ── 按精灵查专属名词（effectDes.monster 逗号串关联）──
    if v["mode"] == "pet":
        _p = v.get("pet") or {}
        out(f"# {_p.get('name')}  #{_p.get('id')}  专属名词 共 {len(v['rows'])} 条")
        for r in v["rows"]:
            out("")
            out(f"## {r.get('name')}")
            out(r.get("desc") or "")
        return

    # ── 按类别列 ──
    if v["mode"] == "kind":
        out(f"# {v['kind']} 共 {len(v['rows'])} 条")
        for r in v["rows"]:
            out("")
            out(f"## {r.get('name') or '(无名字)'}")
            out(r.get("desc") or "")
        return

    # ── 宝石特判 ──
    if v["mode"] == "gem":
        out(f"# 宝石·{v['keyword']} 共 {len(v['rows'])} 条")
        for g in v["rows"]:
            _nm = str(g.get("gem") or "")
            lv = "" if "lv" in _nm.lower() else (f"lv{g['level']}" if g.get("level") else "")
            out(f"- {g.get('gem')}{lv}：{g.get('effect')}（参数 {g.get('args')}）")
        return

    # ── 普通词条 ──
    for r in v["rows"]:
        src_tag = r.get("src") or ""
        out("")
        out(f"## {r.get('name') or '(无名字)'}" + (f"  [{src_tag}]" if src_tag else ""))
        out(r.get("desc") or "")
    if v.get("args_hint"):
        out("")
        out(v["args_hint"])



def cmd_besttype(k: Any, args) -> None:
    """最优系别（官方 #489）：候选 = 组合本身 + 各子系别，攻击取最高 / 受击取最低。

    规则与案例来源：kb.facts「最优系别·候选集合到底怎么构成」等 3 条。
    """
    atk = [x for x in str(getattr(args, "atk", "") or "").replace("·", "/").split("/") if x.strip()]
    groups = []
    for raw in (getattr(args, "groups", None) or []):
        s = str(raw or "").strip()
        if not s:
            continue
        groups.append([x for x in s.replace("·", "/").split("/") if x.strip()])
    pick = (getattr(args, "pick", "max") or "max").strip()

    r = k.optimal_type_mult(atk, groups, pick)
    if args.json:
        dump(r)
        return
    if r.get("mult") is None:
        out("最优系别算不出来：" + "；".join(r.get("steps") or ["参数不足"]))
        return
    out(f"# 最优系别 · {'攻击方取最高' if pick == 'max' else '受击方取最低'}")
    out(f"攻击方：{'/'.join(atk)}")
    out("")
    out("【候选】")
    for c in r.get("candidates") or []:
        mark = "  ← 选中" if c["types"] == r.get("chosen") else ""
        out(f"  {c['types']:<12} ×{c['mult']:g}{mark}")
    out("")
    out(f"【结果】{r['chosen']} → ×{r['mult']:g}")


def cmd_timeline(k: Any, args) -> None:
    """回合流程 / 时点判定（kb.doc_rules 的「【战斗时点】」系列，21 条 / 11 章节）。

    不做参数时列出章节；给关键词则输出该章节全文。
    涉及「什么时候结算」「先算什么后算什么」「这个效果在哪个时点」时先跑这个。
    """
    kw = (getattr(args, "section", "") or "").strip()
    try:
        rows = k._kb.execute(
            """select title, condition, conclusion from doc_rules
               where title like '%战斗时点%' order by id"""
        ).fetchall()
    except Exception as exc:
        out(f"查不到战斗时点规则：{exc}")
        return

    if not rows:
        out("库里没有「战斗时点」系列规则。")
        return

    # 按 title 分组
    groups: dict[str, list[str]] = {}
    for title, _cond, concl in rows:
        name = str(title or "").replace("【战斗时点】", "").strip()
        groups.setdefault(name, []).append(str(concl or "").strip())

    if not kw:
        out("# 回合流程 / 时点判定（kb.doc_rules · 战斗时点系列）")
        out("")
        out(f"共 {len(rows)} 条，分 {len(groups)} 节。给关键词看某一节：")
        out("  seer.py timeline 全局主线")
        out("  seer.py timeline 出手流程")
        out("")
        for name, parts in groups.items():
            out(f"  · {name}（{len(parts)} 条）")
        return

    hit = [(n, p) for n, p in groups.items() if kw in n]
    if not hit:
        out(f"没有含「{kw}」的章节。可用：")
        for name in groups:
            out(f"  · {name}")
        return

    for name, parts in hit:
        out(f"# {name}")
        out("")
        for p in parts:
            out(p)
            out("")


def cmd_selftest(k: Any, args) -> None:
    """自检：两套克制实现是否一致 + 关键函数能否跑通。

    db.py 里有两套克制计算：
      · type_mult(atk_ids, def_ids)               —— CLI 走这条（收 id）
      · type_multiplier_detail(atk_atk, def, mode) —— bot 走这条（收名字，mode='seer'）
    两者是两份独立实现，改了一边忘了另一边就会不一致。这里逐例对比。
    """
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        out(("  ✅ " if ok else "  ❌ ") + name + (("  " + detail) if detail and not ok else ""))
        if not ok:
            fails.append(name)

    out("# 赛尔知识库自检")
    out("")

    # ── 1. 数据库完整性 ──
    out("【1】数据源")
    try:
        n_pet = k._q("select count(*) from pet")[0][0]
        check(f"seerapi.pet = {n_pet} 行", n_pet > 4000)
    except Exception as exc:
        check("seerapi.pet 可查", False, str(exc))
    try:
        n_f = k._kb.execute("select count(*) from facts").fetchone()[0]
        check(f"kb.facts = {n_f} 行", n_f > 300)
    except Exception as exc:
        check("kb.facts 可查", False, str(exc))
    for db in ("effectDes", "effectIcon", "mintmark"):
        try:
            conn = k._open_manual(db)
            ok = conn is not None
            if conn:
                conn.close()
            check(f"manual_db/{db}.db 可打开", ok)
        except Exception as exc:
            check(f"manual_db/{db}.db", False, str(exc))

    # ── 2. 两套克制实现对比 ──
    out("")
    out("【2】克制算法·两套实现对比")
    cases = [
        (["水"], ["火"]),                     # 单→单
        (["光"], ["草"]),                     # 单→单 含 0
        (["草", "战斗"], ["水"]),              # 双→单 其他÷2
        (["机械", "龙"], ["邪灵"]),             # 双→单 双2得4
        (["次元", "龙"], ["冰", "暗影"]),        # 双→双（库内标准例 0.75）
        (["神秘", "战斗"], ["虚空"]),           # 双→单
        (["自然", "圣灵"], ["混沌", "战斗"]),      # 双→双
    ]
    for atk, dfn in cases:
        try:
            ids_a = [k._type_id(a) for a in atk]
            ids_d = [k._type_id(d) for d in dfn]
            v1 = k.type_mult(ids_a, ids_d).get("mult")
            v2 = k.type_multiplier_detail(atk, dfn, "seer").get("mult")
            same = (v1 is not None and v2 is not None and abs(v1 - v2) < 0.001)
            check(f"{'/'.join(atk)} → {'/'.join(dfn)}", same,
                  f"type_mult={v1} vs detail={v2}")
        except Exception as exc:
            check(f"{'/'.join(atk)} → {'/'.join(dfn)}", False, str(exc))

    # ── 3. 最优系别·库内已验证案例 ──
    out("")
    out("【3】最优系别（对照 kb.facts 的官方两例）")
    try:
        r = k.optimal_type_mult(["自然", "圣灵"], [["机械", "草"], ["混沌", "战斗"]], "max")
        check("攻击方向 取草×4", r.get("mult") is not None and abs(r["mult"] - 4) < 0.01,
              f"算出 {r.get('mult')}")
        r2 = k.optimal_type_mult(["虚空"], [["神秘", "战斗"]], "min")
        check("受击方向 取神秘×2", r2.get("mult") is not None and abs(r2["mult"] - 2) < 0.01,
              f"算出 {r2.get('mult')}")
    except Exception as exc:
        check("最优系别", False, str(exc))

    # ── 4. 关键功能函数存在性 ──
    out("")
    out("【4】关键函数")
    for fn in ("type_mult", "type_multiplier_detail", "optimal_type_mult",
               "pet_effects", "_name_standalone", "glossary_terms_in",
               "type_query_answer", "king_status", "_facts_by_keyword",
               "annotate_terms", "term_lookup_any"):
        check(fn, hasattr(k, fn))

    # ── 5. 词条独立词校验（失体误挂应被过滤） ──
    out("")
    out("【5】词条误挂校验")
    try:
        eff = k.pet_effects(3524)
        names = [e["name"] for e in eff]
        check("乔列尼德只剩【逆命之威】【荒古之慑】",
              names == ["逆命之威", "荒古之慑"] or set(names) == {"逆命之威", "荒古之慑"},
              f"实际 {names}")
        eff2 = k.pet_effects(4499)
        names2 = [e["name"] for e in eff2]
        check("德布比特保留【回体】【失体】",
              {"回体", "失体"} <= set(names2), f"实际 {names2}")
    except Exception as exc:
        check("pet_effects 校验", False, str(exc))

    out("")
    # ── 8. 呈现契约：语义层的判定字段，两边都必须渲染 ──
    # 背景（2026-11-03）：这些功能原来 bot 和 CLI 各写一份渲染，判定逻辑被复制，
    # 改一处漏一处。现在判定只写在 db 的 *_view()，呈现层各自排版，
    # 但「必须渲染哪些字段」用契约钉死：哪边删了，这里就红。
    out("【8】呈现契约（*_view 判定字段必须两边都渲染）")

    # (功能名, CLI 函数起点, bot 函数标记, 语义层方法名, 必须渲染的字段)
    CONTRACTS = [
        ("pet", "def cmd_pet(", '@llm_tool(name="seer_pet")', "pet_view",
         ("form_hint", "exact_warn", "alias_note",
          "alias_chain_hint", "other_aliases_hint", "sparse_warn")),
        ("type", "def cmd_type(", '@llm_tool(name="seer_type_chart")', "type_view",
         ("rule_hint", "missing_hint")),
        ("facts", "def cmd_facts(", '@llm_tool(name="seer_rules")', "facts_view",
         ("empty_hint", "rules_text")),
        ("tags", "def cmd_tags(", '@llm_tool(name="seer_tag")', "tag_view",
         ("empty_hint", "missing")),
        ("glossary", "def cmd_glossary(", '@llm_tool(name="seer_glossary")', "glossary_view",
         ("empty_hint", "args_hint", "categories")),
        ("skill", "def cmd_skill(", '@llm_tool(name="seer_skill")', "skill_view",
         ("empty_hint",)),
        ("pool", "def cmd_pool(", '@llm_tool(name="seer_pool")', "pool_view",
         ("empty_hint",)),
        ("dmg", "def cmd_dmg(", '@llm_tool(name="seer_damage")', "dmg_view",
         ("empty_hint", "float_hint", "slot_hints")),
    ]

    def _slice(src: str, kw_start: str, kw_end: str) -> str:
        i = src.find(kw_start)
        if i < 0:
            return ""
        j = src.find(kw_end, i + len(kw_start))
        return src[i:j if j > 0 else len(src)]

    try:
        here = os.path.abspath(__file__)
        cli_src = open(here, encoding="utf-8").read()
    except Exception as exc:
        cli_src = ""
        check("读取 CLI 源码", False, str(exc))

    bot_src = ""
    try:
        plugin = _find_plugin_dir()
        bot_src = open(os.path.join(plugin, "main.py"), encoding="utf-8").read()
    except Exception as exc:
        check("读取 bot 源码", False, str(exc))

    # bot 各函数的结束标记：下一个 @llm_tool
    def _bot_body(marker: str) -> str:
        i = bot_src.find(marker)
        if i < 0:
            return ""
        j = bot_src.find("@llm_tool(name=", i + len(marker))
        return bot_src[i:j if j > 0 else len(bot_src)]

    for feat, cli_start, bot_marker, view_name, keys in CONTRACTS:
        # CLI 函数体：必须按「行首」匹配，否则会先撞到上面 CONTRACTS 表里的同名字符串
        ci = cli_src.find(chr(10) + cli_start)
        if ci >= 0:
            ci += 1  # 跳过前导换行，指到 def 本身
            cj = cli_src.find(chr(10) + "def cmd_", ci + len(cli_start))
            cli_body = cli_src[ci:cj if cj > 0 else len(cli_src)]
        else:
            cli_body = ""
        bot_body = _bot_body(bot_marker)

        check(f"{feat}: CLI 与 bot 函数体都能取到", bool(cli_body) and bool(bot_body))
        check(f"{feat}: CLI 调用 {view_name}", f"k.{view_name}(" in cli_body)
        check(f"{feat}: bot 调用 {view_name}", f"self.kb.{view_name}(" in bot_body)
        for key in keys:
            check(f"{feat}: CLI 渲染 {key}", f'"{key}"' in cli_body)
            check(f"{feat}: bot 渲染 {key}", f'"{key}"' in bot_body)

    out("")
    if fails:
        out(f"❌ 共 {len(fails)} 项不通过：{'；'.join(fails)}")
    else:
        out("✅ 全部通过")


def cmd_alias_add(k: Any, args) -> None:
    """加/改一条外号 → 精灵的映射（写 kb.pet_alias，同名会覆盖）。"""
    alias = str(getattr(args, "alias", "") or "").strip()
    target = str(getattr(args, "pet", "") or "").strip()
    note = str(getattr(args, "note", "") or "").strip()
    if not alias or not target:
        out("用法：seer.py alias-add <外号> <精灵名或ID> [--note 备注]")
        return

    # 解析目标：先当 ID，再当名字
    pid = None
    if target.isdigit():
        row = k._q("select id from pet where id=?", (int(target),))
        if row:
            pid = int(row[0][0])
    if pid is None:
        pid = k.alias_lookup(target)
    if pid is None:
        hits = k.find_pets(target)
        if hits:
            pid = int(hits[0][0])
    if pid is None:
        out(f"找不到精灵「{target}」，先确认名字或换用 pet_id。")
        return

    row = k._q("select name from pet where id=?", (pid,))
    pname = row[0][0] if row else f"#{pid}"

    # 冲突提示：这个外号原本指向别处？
    old = k.alias_lookup(alias)
    k.add_alias(alias, pid, note)
    out(f"✅ 「{alias}」→ {pname}（#{pid}）" + (f"  备注：{note}" if note else ""))
    if old and old != pid:
        row2 = k._q("select name from pet where id=?", (old,))
        out(f"   （原来是 → {row2[0][0] if row2 else '#'+str(old)}，已覆盖）")


def cmd_alias_of(k: Any, args) -> None:
    """查某只精灵身上挂了哪些外号。"""
    target = str(getattr(args, "pet", "") or "").strip()
    if not target:
        out("用法：seer.py alias-of <精灵名或ID>")
        return
    pid = None
    if target.isdigit():
        pid = int(target)
    if pid is None:
        pid = k.alias_lookup(target)
    if pid is None:
        hits = k.find_pets(target)
        if hits:
            pid = int(hits[0][0])
    if pid is None:
        out(f"找不到精灵「{target}」")
        return
    row = k._q("select name from pet where id=?", (pid,))
    pname = row[0][0] if row else f"#{pid}"
    aliases = k.alias_of(pid)
    out(f"# {pname}（#{pid}）的外号")
    if not aliases:
        out("  （没有登记外号）")
        return
    for a in aliases:
        out(f"  · {a}")


def cmd_alias(k: Any, args) -> None:
    """俗称 → 精灵（带备注/梗来历）。"""
    pid = k.alias_lookup(args.name)
    if pid:
        hits = k.find_pets(str(pid))
        name = hits[0][1] if hits else "?"
        out(f"「{args.name}」→ #{pid} {name}")
        # 带出备注（社区梗、多义提示等）
        try:
            row = k._kb.execute(
                "select note from pet_alias where alias=?", (args.name,)
            ).fetchone()
        except Exception:
            row = None
        if row and row[0]:
            out(f"   备注：{row[0]}")
        # 同一只身上的其他外号，方便对照
        try:
            others = [a for a in (k.alias_of(pid) or []) if a != args.name]
        except Exception:
            others = []
        if others:
            out(f"   同只的其他叫法：{'、'.join(others)}")
        return
    hits = k.find_pets(args.name)
    if hits:
        for pid2, name2 in hits:
            out(f"#{pid2} {name2}")
    else:
        out(f"未找到：{args.name}")


def cmd_tags(k: Any, args) -> None:
    """功能标签：三种模式，判定全部来自 k.tag_view()（语义层，和 bot 共用）。

      ① 只给 pet            → 这只精灵的功能覆盖（含空白项）
      ② 只给 --tag          → 全库反查哪些技能/魂印带这个标签
      ③ pet + --tag 都给     → 这只精灵身上带该标签的技能/魂印
    """
    v = k.tag_view(getattr(args, "tag", ""), getattr(args, "pet", ""))
    if args.json:
        dump(v)
        return

    # 判定字段：任何空结果/用法提示都直接用语义层给的话
    if v.get("empty_hint"):
        out(v["empty_hint"])
        return

    if v["mode"] == "coverage":
        cov = v["coverage"] or {}
        out(f"# {v['pname']}（#{v['pid']}）的功能标签")
        skills = cov.get("skills") or {}
        soulmarks = cov.get("soulmarks") or {}
        if skills:
            out(chr(10) + "【技能覆盖】")
            for tag, names in sorted(skills.items(), key=lambda kv: -len(kv[1])):
                out(f"  {tag:<10}{len(names):>2} 条  {'、'.join(names[:6])}")
        if soulmarks:
            out(chr(10) + "【魂印覆盖】")
            for tag, refs in sorted(soulmarks.items(), key=lambda kv: -len(kv[1])):
                out(f"  {tag:<10}{len(refs):>2} 条  {'、'.join(refs[:6])}")
        if v["missing"]:
            out(chr(10) + "【空白·完全没有】" + "、".join(v["missing"]))
            out("说明：空白项＝技能池与魂印都没有这个功能，是它的定位短板。")
        return

    if v["mode"] == "intersect":
        out(f"# {v['pname']}·{v['tag']} 共 {len(v['rows'])} 条")
        for r in v["rows"]:
            out(f"- {r['kind']}｜{r['name']}")
        return

    if v["mode"] == "reverse":
        out(f"# 标签·{v['tag']} 共 {v['total']} 条（仅示前 {len(v['rows'])} 条）")
        for r in v["rows"]:
            kind = "技能" if r.get("kind") == "skill" else "魂印"
            out(f"- {kind}｜{r.get('name')}")



def cmd_suit(k: Any, args) -> None:
    """套装效果（seerapi.suit + suit_bonus）。

    ★ 关联键：suit_bonus.id = suit.id，不要走 equip.bonus_id（那列大部分是 NULL，
      晨曦 #447 / 裁决 #462 的部件 bonus_id 全是 None，按它查必然查空）。
    """
    name = (getattr(args, "name", "") or "").strip()
    kw = (getattr(args, "search", "") or "").strip()

    try:
        rows = k._q(
            """select s.id, s.name, b.desc
               from suit s left join suit_bonus b on b.id = s.id
               order by s.id"""
        )
    except Exception as exc:
        out(f"查套装失败：{exc}")
        return
    if not rows:
        out("suit 表读不到数据。")
        return

    def clean(s: str) -> str:
        import re as _re
        return _re.sub(r"\[/?[^\]]*\]", "", str(s or "")).replace("|", "\n　　").strip()

    # 有关联效果的
    withfx = [(i, n, d) for i, n, d in rows if d]

    if not name and not kw:
        out("# 套装效果一览（有战斗效果的 %d 套 / 全部 %d 套）" % (len(withfx), len(rows)))
        out("")
        out("　查详情：seer.py suit 晨曦　｜　按效果搜：seer.py suit --search 减伤")
        out("")
        for i, n, d in withfx:
            first = clean(d).split("\n")[0][:60]
            out(f"  #{i:<4} {n:<12} {first}")
        out("")
        out("  ※ 其余套装在 suit 表里只有名字和背景故事，没有战斗效果数据")
        return

    if kw:
        hit = [(i, n, d) for i, n, d in withfx if kw in clean(d)]
        if not hit:
            out(f"没有含「{kw}」效果的套装。")
            return
        out(f"# 效果含「{kw}」的套装：{len(hit)} 套")
        for i, n, d in hit:
            out("")
            out(f"【#{i} {n}】")
            out("　　" + clean(d))
        return

    # 精确/模糊找名字
    hit = [r for r in rows if name in str(r[1])]
    if not hit:
        out(f"没找到套装「{name}」。试试 seer.py suit 看列表。")
        return
    for i, n, d in hit:
        out(f"# #{i} {n}")
        if d:
            out("　　" + clean(d))
        else:
            out("　　（这套在库里只有名字和背景故事，没有战斗效果数据）")
            for r in k._q("select suit_desc from suit where id=?", (i,)):
                out("　　" + clean(r[0]))
        out("")


def cmd_ls(k: Any, args) -> None:
    """宝石/技能石等「附加件」的等级数值表。

    ★ 为什么需要这个命令：2026-11-03 DSH 把「6级威力」误读成「攻击强化+6」，
      因为库里从没有一层把「威力宝石Lv6 = +17 威力」摆出来给它看。
      数据链：seerapi.gem(id 1800121-1800130) → gemeffectlink → skill_effect_in_use(121-130)
    """
    kind = (getattr(args, "kind", "") or "").strip()
    kw = (getattr(args, "keyword", "") or "").strip()

    if kind != "gem":
        out("目前只支持：seer.py ls gem [关键词]")
        return

    def clean(s: str) -> str:
        return re.sub(r"\[/?[^\]]*\]", "", str(s or "")).replace("|", " / ").strip()

    try:
        cats = {r[0]: r[1] for r in k._q("select id, name from gem_category")}
        rows = k._q(
            """select g.id, g.name, g.level, g.category_id,
                      (select u.info from gemeffectlink l
                         join skill_effect_in_use u on u.id = l.skill_effect_in_use_id
                        where l.gem_id = g.id limit 1)
               from gem g order by g.category_id, g.level"""
        )
    except Exception as exc:
        out(f"查宝石失败：{exc}")
        return

    if not rows:
        out("gem 表读不到数据。")
        return

    picked = [r for r in rows if (not kw) or (kw in str(r[1])) or (kw in str(cats.get(r[3], "")))]
    if not picked:
        out(f"没有匹配「{kw}」的宝石。")
        return

    cur_cat = None
    for _id, name, level, cat, info in picked:
        cname = cats.get(cat, f"类别{cat}")
        if cname != cur_cat:
            out("")
            out(f"【{cname}】")
            cur_cat = cname
        out(f"  Lv{level:<2} {name:<16} {clean(info)}")


def cmd_pool(k: Any, args) -> None:
    """巅峰池：按精灵查它在哪些池 / 按池查有哪些精灵 / 列出全部池。

    三种模式的判定都来自 k.pool_view()（语义层，和 bot 共用）。
    """
    v = k.pool_view(getattr(args, "pool", ""), getattr(args, "pet", ""))
    if args.json:
        dump(v)
        return

    if v.get("empty_hint"):
        out(v["empty_hint"])
        return

    if v["mode"] == "pet":
        for p in v["hits"]:
            out(f"{p['label']}（{len(p['pets'])} 只，截止 {p['deadline'] or '未标注'}）")
        return

    if v["mode"] == "list":
        out("# 巅峰池")
        for p in v["pools"]:
            out(f"- {p['label']}：{len(p['pets'])} 只（截止 {p['deadline'] or '未标注'}）")
        return

    # match
    mp = v["matched"]
    out(f"# {mp['label']}（{len(mp['pets'])} 只）")
    out("、".join(mp["pets"]))



def cmd_status(k: Any, args) -> None:
    """异常状态列表。"""
    rows = k.status_list(category=args.category or "", limit=args.limit)
    if args.json:
        dump(rows)
        return
    for r in rows:
        out(f"  {r.get('name', '?'):<8} {r.get('desc', '')[:60]}")


def cmd_dmg(k: Any, args) -> None:
    """伤害计算（走统一语义层 dmg_view）。

    克制倍率、浮动档位、增伤/减伤归区的判定全部来自 k.dmg_view()，
    和 bot 侧共用一份口径。
    """
    v = k.dmg_view(
        power=args.power, atk=args.atk, defense=args.defense,
        attack_type=getattr(args, "attack_type", ""),
        defend_type=getattr(args, "defend_type", ""),
        type_mult=getattr(args, "type_mult", 0.0) or 0.0,
        type_mult_cap=getattr(args, "type_mult_cap", 0.0) or 0.0,
        stab=1.5 if args.stab else 1.0,
        crit=2.0 if args.crit else 1.0,
        random=getattr(args, "random", "range"),
        power_boost=getattr(args, "power_boost", 0.0),
        power_flat=getattr(args, "power_flat", 0.0),
        variable_power=getattr(args, "variable_power", False),
        general_add=getattr(args, "general_add", 0.0),
        extra_add=getattr(args, "extra_add", ""),
        general_reduce=getattr(args, "general_reduce", 0.0),
        extra_reduce=getattr(args, "extra_reduce", ""),
        resist=getattr(args, "resist", "full"),
        flat=getattr(args, "flat", ""),
        percent=getattr(args, "percent", ""),
        true_damage=getattr(args, "true_damage", 0.0),
        atk_stage=getattr(args, "atk_stage", 0),
        defense_stage=getattr(args, "defense_stage", 0),
        hits=getattr(args, "hits", 1),
        target_hp=getattr(args, "target_hp", 0.0),
    )
    if args.json:
        dump(v)
        return
    if not v["ok"]:
        out(v.get("empty_hint") or "计算失败。")
        return

    r = v["result"]
    inp = v["inputs"]
    out("# 伤害计算")
    out(
        "威力 {p} ｜ 攻击 {a} ｜ 防御 {d} ｜ 克制 ×{t} ｜ 本系 {s} ｜ 连击 {h} 段".format(
            p=inp["power"], a=inp["atk"], d=inp["defense"],
            t=inp["type_mult"], s="1.5" if inp["stab"] == 1.5 else "1.0", h=inp["hits"],
        )
    )
    if inp.get("power_real") and inp["power_real"] != inp["power"]:
        out(f"威力加成后 {inp['power']} → {inp['power_real']:.0f}")
    if inp.get("atk_real") != inp["atk"] or inp.get("def_real") != inp["defense"]:
        out(f"等级修正：攻 {inp['atk']}→{inp['atk_real']:.0f} ｜ 防 {inp['defense']}→{inp['def_real']:.0f}")

    # 判定字段：乘区归区 + 浮动口径
    if v.get("slot_hints"):
        section("乘区")
        for h in v["slot_hints"]:
            out(f"  · {h}")
    if v.get("float_hint"):
        out(f"浮动：{v['float_hint']}")

    section("结果")
    ch = lambda n: r.get(n)
    parts = []
    if ch("attack_channel") is not None:
        parts.append(f"红伤 {ch('attack_channel')}")
    if ch("flat_channel"):
        parts.append(f"固伤 {ch('flat_channel')}")
    if ch("percent_channel"):
        parts.append(f"百分比 {ch('percent_channel')}")
    if ch("attr_channel"):
        parts.append(f"属性伤害 {ch('attr_channel')}")
    if ch("true_channel"):
        parts.append(f"真伤 {ch('true_channel')}")
    for line in parts:
        out(f"  {line}")
    if ch("total") is not None:
        out(f"  ── 合计：{ch('total')}")
    if inp.get("target_hp") and ch("total") is not None:
        out(f"  占目标体力：{ch('total') / inp['target_hp']:.1%}")
    if ch("total_reduce"):
        out(f"  （总减伤 {ch('total_reduce'):.0%}）")
    out("")
    out("口径：固伤/百分比各段分别过抗性；真伤不吃减免；属性伤害只吃克制；")


# 性格 → (加项, 减项)。同名项=中性（1.0）
NATURE_TABLE = {
    "固执": ("atk", "sp_atk"), "开朗": ("spd", "sp_atk"),
    "勇敢": ("atk", "spd"), "调皮": ("atk", "sp_def"),
    "大胆": ("def", "atk"), "淘气": ("def", "sp_atk"),
    "悠闲": ("def", "spd"), "乐天": ("def", "sp_def"),
    "保守": ("sp_atk", "atk"), "沉着": ("sp_def", "atk"),
    "马虎": ("sp_atk", "sp_def"), "冷静": ("sp_atk", "spd"),
    "稳重": ("sp_atk", "def"), "温和": ("sp_def", "sp_atk"),
    "狂妄": ("sp_def", "spd"), "慎重": ("sp_def", "sp_atk"),
    "胆小": ("spd", "atk"), "急躁": ("spd", "def"),
    "爽朗": ("spd", "sp_atk"), "天真": ("spd", "sp_def"),
    "认真": ("atk", "atk"), "坦率": ("def", "def"),
}

# 六项顺序（与 db.py 的 _STAT_KEYS 一致）
STAT_KEYS = ("atk", "def", "sp_atk", "sp_def", "spd", "hp")
STAT_CN = {"atk": "攻击", "def": "防御", "sp_atk": "特攻",
           "sp_def": "特防", "spd": "速度", "hp": "体力"}


def _nature_mult(nature: str, key: str) -> float:
    """返回性格对该项的修正系数。空/未知/中性 = 1.0。"""
    if not nature:
        return 1.0
    pair = NATURE_TABLE.get(nature.strip())
    if not pair:
        return 1.0
    up, down = pair
    if up == down:
        return 1.0
    if key == up:
        return 1.1
    if key == down:
        return 0.9
    return 1.0


def stat_value(base: int, talent: int, ev: int, nature_mult: float = 1.0, is_hp: bool = False) -> int:
    """满级（100）单项能力值。

    性格加成按**四舍五入**取整（实测：南霜攻 395×1.1=434.5 → 435）。
    体力公式为「种族×2 + 学习力÷4 + 个体 + 110」，不吃性格修正。
    """
    ev4 = max(0, int(ev)) // 4
    if is_hp:
        return int(base * 2 + ev4 + int(talent) + 110)
    raw = (base * 2 + ev4 + int(talent) + 5) * nature_mult
    if nature_mult == 1.0:
        return int(raw)
    return int(raw + 0.5)      # 四舍五入


MANUAL_DIRS = [r"G:\dsh workspace\_manual_all", r"G:\dsh workspace\_apk\assets"]
# 刻印六项顺序：攻、防、特攻、特防、速度、体力
MINT_ORDER = ("atk", "def", "sp_atk", "sp_def", "spd", "hp")


def _manual_db(fname: str):
    for d in MANUAL_DIRS:
        f = os.path.join(d, fname)
        if os.path.isfile(f):
            return sqlite3.connect("file:" + f + "?mode=ro", uri=True)
    return None


def lookup_mintmark(name: str) -> dict:
    """按名字查刻印，返回满级六项（MaxAttriValue 已含隐藏值）。

    支持群友简称：九天体 / 圣战 / v8 / 幻灵攻 等，由 mint_picker.resolve_mint 解析。
    """
    # 先试简称解析
    try:
        sys.path.insert(0, os.path.join(HERE, "..", "tools"))
        import mint_picker as _mp
        _all = _mp.load_mintmarks()
        hit = _mp.resolve_mint(name, _all)
        if hit:
            return {"id": hit["id"], "name": hit["name"], "effect": hit["class_name"],
                    "vals": hit["vals"]}
    except Exception:
        pass

    con = _manual_db("mintmark.db")
    if not con:
        return {}
    try:
        row = con.execute(
            "select ID, Des, EffectDes, AttriValue, MaxAttriValue, ExtraAttriValue "
            "from MintMark where Des = ? or Des like ? order by Grade desc limit 1",
            (name, "%" + name + "%"),
        ).fetchone()
    except Exception:
        con.close()
        return {}
    con.close()
    if not row:
        return {}
    try:
        mx = json.loads(row[4]) if row[4] and row[4] != "[]" else json.loads(row[3] or "[]")
    except Exception:
        mx = []
    if len(mx) != 6:
        return {}
    return {"id": row[0], "name": row[1], "effect": row[2],
            "vals": dict(zip(MINT_ORDER, mx))}


def lookup_suit(name: str) -> dict:
    """按名字查套装加成（suit_bonus.desc 是文本，suit_bonus_attr 是数值）。"""
    out_d = {}
    con = _manual_db("mintmark.db")
    if con:
        con.close()
    # 套装在 seerapi：suit / suit_bonus
    api = os.path.join(_find_data_dir(None), "astrbot_plugin_seer_info", "seerapi.sqlite")
    if not os.path.isfile(api):
        return {}
    try:
        c2 = sqlite3.connect("file:" + api + "?mode=ro", uri=True)
        row = c2.execute(
            "select s.id, s.name, b.desc from suit s left join suit_bonus b on b.id=s.id "
            "where s.name = ? or s.name like ? limit 1", (name, "%" + name + "%")
        ).fetchone()
        c2.close()
    except Exception:
        return {}
    if not row:
        return {}
    return {"id": row[0], "name": row[1], "desc": row[2]}


# 战队满级加成（鱼片口述 2026-11-01）
TEAM_MAX = {"hp": 30, "sp_atk": 15, "atk": 15, "def": 15, "sp_def": 15, "spd": 10}
# 年费加成：全能力 +10
ANNUAL_BONUS = 10

SUIT_CN2KEY = {"攻击": "atk", "防御": "def", "特攻": "sp_atk", "特防": "sp_def",
               "速度": "spd", "体力": "hp"}

# 套装 → 默认称号/目镜（鱼片口述 2026-11-01）
# 群友只说了套装名时按这套兜底
SUIT_DEFAULTS = {
    "晨曦": {"title": "神话", "eyepiece": "漫游者-AH"},
    "裁决": {"title": "星光铸就者", "eyepiece": "漫游者-AH"},
}



def parse_suit_stats(desc: str) -> dict:
    """从套装描述里抠「速度、体力+60」这类纯数值加成。

    只取「XX+N」「XX、YY+N」这种直接加面板的写法；
    百分比（+10%）和各种触发效果一律不碰。
    """
    res = {}
    if not desc:
        return res
    # 抓「A、B、C+N」或「A+N」
    word = r"(?:攻击|防御|特攻|特防|速度|体力)"
    pat = re.compile(word + r"(?:[、，]" + word + r")*\s*\+\s*(\d+)")
    for m in pat.finditer(desc):
        whole, num = m.group(0), int(m.group(1))
        names = re.findall(word, whole)
        for nm in names:
            key = SUIT_CN2KEY.get(nm)
            if key:
                res[key] = res.get(key, 0) + num
    return res


def parse_soulmod(expr: str, base: dict) -> tuple[dict, str]:
    """解析魂印面板修正表达式。

    支持写法：
      "双防*0.15->非速度"    双防各自×0.15，加到除速度外的五项
      "双防和*0.15->非速度"  (防+特防)×0.15，同上
      "双攻差*0.15->其他"    攻击与特攻之差×0.15
      "双防+100"             直接加法
      "攻击+200" / "体力+400" 单项加法
    返回 (加值字典, 说明文本)
    """
    res = {kk: 0 for kk in STAT_KEYS}
    note = ""
    e = expr.replace(" ", "")
    NON_SPD = ("hp", "atk", "def", "sp_atk", "sp_def")

    # 总和均分型：每项变成「六项总和的一半」
    # 用于 无为觉者 #4661（种族全 0）魂印「战斗开始时，自身所有能力值变为所有能力值之和的一半」
    # 这是「替换」不是「加法」，所以返回的加值 = 目标值 − 当前值。
    if e in ("总和均分", "总和/2", "总和÷2", "均分S2", "每项总和一半", "S/2"):
        S = sum(int(base.get(kk, 0) or 0) for kk in STAT_KEYS)
        target = S // 2
        for kk in STAT_KEYS:
            res[kk] = target - int(base.get(kk, 0) or 0)
        note = ("六项总和 S=%d，魂印「所有能力值变为总和的一半」→ 每项 = S/2 = %d"
                "（总和 %d→%d，×%.1f）" % (S, target, S, target * 6, target * 6 / S if S else 0))
        return res, note

    # 血换五维型：消耗 n% 体力上限，其他五项各 + 等量
    # 用于 天堂极龙兽 #4921 魂印「消耗20%的体力上限并提升自身的其他能力值等量的数值」
    # 默认 0.2；可写「体力换属性*0.2」
    m0 = re.match(r"^(?:体力换属性|血换五维|体力换五维)(?:\*([0-9.]+))?$", e)
    if m0:
        pct = float(m0.group(1) or 0.2)
        cut = int(int(base.get("hp", 0) or 0) * pct)
        res["hp"] -= cut
        for kk in ("atk", "def", "sp_atk", "sp_def", "spd"):
            res[kk] += cut
        note = ("体力上限 %d × %.0f%% = %d：体力 −%d，攻击/防御/特攻/特防/速度 各 +%d（五项共 +%d）"
                % (int(base.get("hp", 0) or 0), pct * 100, cut, cut, cut, cut * 5))
        return res, note

    # 差值型 + 层数：双攻差*0.15*5->其他（无极圣武·英雄之耀五层）
    m = re.match(r"^(双攻差|双防差)\*([0-9.]+)(?:\*([0-9]+)层?)?->(其他|非速度|速度外|非双攻)$", e)
    if m:
        kind, k, layers, dst = m.group(1), float(m.group(2)), int(m.group(3) or 1), m.group(4)
        if kind == "双攻差":
            gap = abs(base["atk"] - base["sp_atk"])
            label = "双攻差 |%d-%d| = %d" % (base["atk"], base["sp_atk"], gap)
        else:
            gap = abs(base["def"] - base["sp_def"])
            label = "双防差 |%d-%d| = %d" % (base["def"], base["sp_def"], gap)
        # 先乘层数再取整：无极英雄之耀 508*0.15=76.2，五层是 381 而不是 76*5=380
        per = int(gap * k)
        amt = int(gap * k * layers)
        if layers > 1:
            note = label + " × " + str(k) + " × " + str(layers) + "层 = " + str(amt)
            if per * layers != amt:
                note += "（先乘层数再取整；按每层取整会得 " + str(per * layers) + "）"
        else:
            note = label + " × " + str(k) + " = " + str(amt)
        if dst == "非双攻":
            for kk in ("hp", "def", "sp_def", "spd"):
                res[kk] += amt
            note += "，加到体力/防御/特防/速度（双攻不变）"
        elif dst in ("非速度", "速度外"):
            for kk in NON_SPD:
                res[kk] += amt
            note += "，加到体力/攻击/防御/特攻/特防"
        else:
            for kk in ("atk", "sp_atk", "def", "sp_def"):
                res[kk] += amt
            note += "，加到双攻双防"
        return res, note

    m = re.match(r"^(双防|双攻|双防和|双攻和)\*([0-9.]+)->(非速度|其他|速度外)$", e)
    if m:
        src, k, dst = m.group(1), float(m.group(2)), m.group(3)
        if src == "双防":
            amt = int(base["def"] * k) + int(base["sp_def"] * k)
            note = f"双防 {base['def']}/{base['sp_def']} 各×{k} 相加 = {amt}"
        elif src == "双防和":
            amt = int((base["def"] + base["sp_def"]) * k)
            note = f"(双防 {base['def']}+{base['sp_def']})×{k} = {amt}"
        elif src == "双攻":
            amt = int(base["atk"] * k) + int(base["sp_atk"] * k)
            note = f"双攻 各×{k} 相加 = {amt}"
        else:
            amt = int((base["atk"] + base["sp_atk"]) * k)
            note = f"(双攻和)×{k} = {amt}"
        if dst in ("非速度", "速度外"):
            for kk in NON_SPD:
                res[kk] += amt
            note += "，加到体力/攻击/防御/特攻/特防"
        else:
            for kk in ("atk", "sp_atk", "def", "sp_def"):
                res[kk] += amt
            note += "，加到双攻双防"
        return res, note

    m = re.match(r"^(双防|双攻)\+([0-9]+)$", e)
    if m:
        src, n = m.group(1), int(m.group(2))
        tgt = ("def", "sp_def") if src == "双防" else ("atk", "sp_atk")
        for kk in tgt:
            res[kk] += n
        return res, f"{src} 各 +{n}"

    m = re.match(r"^([攻击防特速体]{2,4})\+([0-9]+)$", e)
    if m:
        nm, n = m.group(1), int(m.group(2))
        key = SUIT_CN2KEY.get(nm)
        if key:
            res[key] += n
            return res, f"{nm} +{n}"
    return {}, ""


TITLE_CN2KEY = {"攻击": "atk", "防御": "def", "特攻": "sp_atk", "特防": "sp_def",
                "速度": "spd", "体力": "hp"}


def lookup_title(name: str) -> dict:
    """按名字查称号，返回六项加成（title_part + title_attr_bonus）。"""
    api = os.path.join(_find_data_dir(None), "astrbot_plugin_seer_info", "seerapi.sqlite")
    if not os.path.isfile(api):
        return {}
    try:
        c = sqlite3.connect("file:" + api + "?mode=ro", uri=True)
        row = c.execute(
            "select t.id, t.name, b.atk, b.def, b.sp_atk, b.sp_def, b.spd, b.hp "
            "from title_part t left join title_attr_bonus b on b.id = t.id "
            "where t.name = ? or t.name like ? order by t.id limit 1",
            (name, "%" + name + "%"),
        ).fetchone()
        c.close()
    except Exception:
        return {}
    if not row:
        return {}
    vals = {"atk": int(row[2] or 0), "def": int(row[3] or 0), "sp_atk": int(row[4] or 0),
            "sp_def": int(row[5] or 0), "spd": int(row[6] or 0), "hp": int(row[7] or 0)}
    return {"id": row[0], "name": row[1], "vals": vals, "total": sum(vals.values())}


def lookup_eyepiece(name: str) -> dict:
    """按名字查目镜（equip → bonus_id → equip_bonus_attr）。"""
    api = os.path.join(_find_data_dir(None), "astrbot_plugin_seer_info", "seerapi.sqlite")
    if not os.path.isfile(api):
        return {}
    try:
        c = sqlite3.connect("file:" + api + "?mode=ro", uri=True)
        row = c.execute(
            "select e.id, e.name, b.atk, b.def, b.sp_atk, b.sp_def, b.spd, b.hp "
            "from equip e left join equip_bonus_attr b on b.id = e.bonus_id "
            "where (e.name = ? or e.name like ?) and (e.name like '%目镜%' or e.name like '%漫游者%' or e.name like '%护目镜%') "
            "order by e.id limit 1",
            (name, "%" + name + "%"),
        ).fetchone()
        c.close()
    except Exception:
        return {}
    if not row:
        return {}
    vals = {"atk": int(row[2] or 0), "def": int(row[3] or 0), "sp_atk": int(row[4] or 0),
            "sp_def": int(row[5] or 0), "spd": int(row[6] or 0), "hp": int(row[7] or 0)}
    return {"id": row[0], "name": row[1], "vals": vals, "total": sum(vals.values())}


def cmd_stat(k: Any, args) -> None:
    """能力值计算：种族值 → 满级面板（可叠刻印/套装/称号）。"""
    hits = k.find_pets(args.name)
    if not hits:
        out(f"找不到精灵：{args.name}")
        return
    pid, petname = hits[0]
    stats = k.pet_stats(pid) or {}
    if not stats:
        out(f"{petname} 没有种族值数据")
        return

    talent = int(args.talent)
    nature = args.nature or ""

    # 学习力：支持单值（全部同刷）与分项写法 "atk=254,sp_def=32,hp=148,spd=76"
    EV_ALIAS = {"攻": "atk", "防": "def", "特攻": "sp_atk", "特防": "sp_def",
                "速": "spd", "体": "hp", "hp": "hp", "atk": "atk", "def": "def",
                "sp_atk": "sp_atk", "sp_def": "sp_def", "spd": "spd"}
    ev_raw = str(args.ev).strip()
    ev_by_key = {}
    if "=" in ev_raw:
        for piece in re.split(r"[,，]", ev_raw):
            piece = piece.strip()
            if not piece or "=" not in piece:
                continue
            kk, vv = piece.split("=", 1)
            key = EV_ALIAS.get(kk.strip())
            if key and vv.strip().isdigit():
                ev_by_key[key] = int(vv)
        ev = 0          # 分项时默认不刷未指定的项
    else:
        ev = int(ev_raw)
    base_atk = int(stats.get("atk") or 0)
    base_spa = int(stats.get("sp_atk") or 0)

    rows = []
    for key in STAT_KEYS:
        base = stats.get(key)
        if base is None:
            base = stats.get(key.replace("sp_", "sp"))
        if base is None:
            continue
        base = int(base)
        mult = 1.0 if key == "hp" else _nature_mult(nature, key)
        ev_key = ev_by_key.get(key, ev) if ev_by_key else ev
        val = stat_value(base, talent, ev_key, mult, key == "hp")
        rows.append({"key": key, "cn": STAT_CN[key], "base": base, "mult": mult,
                     "value": val, "ev": ev_key})

    # ── 叠加刻印 / 套装 ──
    extra = {kk: 0 for kk in STAT_KEYS}
    suit_vals = {}
    used_mints = []
    for mn in (args.mint or [])[:3]:
        info = lookup_mintmark(mn)
        if not info:
            out(f"刻印未找到：{mn}")
            continue
        used_mints.append(info)
        for kk, v in info["vals"].items():
            extra[kk] += int(v or 0)
    suit_info = None
    if args.suit:
        suit_info = lookup_suit(args.suit)
        if not suit_info:
            out(f"套装未找到：{args.suit}")
        else:
            suit_vals = parse_suit_stats(suit_info.get("desc") or "")
    for kk, vv in suit_vals.items():
        extra[kk] = extra.get(kk, 0) + vv

    # 体力上限（永久药剂，最高 20）
    if getattr(args, "hpup", 0):
        hp_add = max(0, min(20, int(args.hpup)))
        extra["hp"] = extra.get("hp", 0) + hp_add
        if int(args.hpup) > 20:
            out(f"提示：体力上限药剂单只最多 20 点，已按 20 计（你给了 {args.hpup}）")

    # 战队加成
    if getattr(args, "team", ""):
        t = args.team.replace(" ", "")
        if t in ("满级", "满", "max"):
            for kk, vv in TEAM_MAX.items():
                extra[kk] = extra.get(kk, 0) + vv
            out("战队加成：满级（体30 攻15 防15 特攻15 特防15 速10）")
            t = ""
        elif t in ("maxdiff", "极差"):
            # 极差向：不点「被性格削弱的那一项攻击」。
            # 固执(+攻-特攻) → 跳过特攻；保守(+特攻-攻) → 跳过攻击；
            # 未指定性格时按种族值判断哪项是「另一攻」。
            up, down = NATURE_TABLE.get((nature or "").strip(), (None, None))
            if down in ("atk", "sp_atk"):
                skip = down
            else:
                skip = "sp_atk" if base_atk >= base_spa else "atk"
            for kk, vv in TEAM_MAX.items():
                if kk == skip:
                    continue
                extra[kk] = extra.get(kk, 0) + vv
            out(f"战队加成：极差向（跳过{STAT_CN[skip]}，其余照点）")
            t = ""
        elif t.startswith("全+"):
            try:
                v = int(t[2:])
                for kk in STAT_KEYS:
                    extra[kk] = extra.get(kk, 0) + v
            except ValueError:
                out(f"战队加成写法没认出来：{args.team}")
        else:
            t = t.replace("，", ",")
            for m in re.finditer(r"([攻击防特速体]{2,4})(\d+)", t):
                nm, v = m.group(1), int(m.group(2))
                key = SUIT_CN2KEY.get(nm)
                if key:
                    extra[key] = extra.get(key, 0) + v
                else:
                    out(f"战队加成里的项没认出来：{nm}")

    # 套装兜底：只说套装名时自动补默认称号/目镜
    _suit_name = getattr(args, "suit", "") or ""
    _dflt = None
    for _k, _v in SUIT_DEFAULTS.items():
        if _k in _suit_name:
            _dflt = _v
            break
    if _dflt:
        if not getattr(args, "title", ""):
            args.title = _dflt["title"]
            out(f"（未指定称号，按套装默认 → {_dflt['title']}）")
        if not getattr(args, "eyepiece", ""):
            args.eyepiece = _dflt["eyepiece"]
            out(f"（未指定目镜，按套装默认 → {_dflt['eyepiece']}）")

    # 称号
    title_info = None
    if getattr(args, "title", ""):
        title_info = lookup_title(args.title)
        if not title_info:
            out(f"称号未找到：{args.title}")
        else:
            for kk, vv in title_info["vals"].items():
                extra[kk] = extra.get(kk, 0) + vv

    # 目镜
    eye_info = None
    if getattr(args, "eyepiece", ""):
        eye_info = lookup_eyepiece(args.eyepiece)
        if not eye_info:
            out(f"目镜未找到：{args.eyepiece}")
        else:
            for kk, vv in eye_info["vals"].items():
                extra[kk] = extra.get(kk, 0) + vv

    # 年费加成：全能力 +10
    if getattr(args, "annual", False):
        for kk in STAT_KEYS:
            extra[kk] = extra.get(kk, 0) + ANNUAL_BONUS

    # 魂印面板修正（基于裸值 + 刻印 + 套装 之后的面板）
    soul_notes = []
    if getattr(args, "soulmod", None):
        cur = {r["key"]: r["value"] + extra.get(r["key"], 0) for r in rows}
        for expr in args.soulmod:
            addm, note = parse_soulmod(expr, cur)
            if addm:
                for kk, vv in addm.items():
                    extra[kk] = extra.get(kk, 0) + vv
                soul_notes.append(note)
            else:
                out(f"魂印修正写法没认出来：{expr}")

    if args.json:
        dump({"pet": petname, "id": pid, "talent": talent, "ev": ev,
              "nature": nature, "stats": rows,
              "mintmarks": used_mints, "suit": suit_info, "extra": extra})
        return

    out(f"# {petname}（#{pid}）满级面板")
    if ev_by_key:
        ev_sum = sum(ev_by_key.values())
        detail = "  ".join(f"{STAT_CN[k]}{v}" for k, v in ev_by_key.items())
        out(f"天赋 {talent} ｜ 学习力：{detail}  共{ev_sum} ｜ 性格 {nature or '未指定'}")
        # ★ 2026-11-08 修：不能按 `v % 4` 判浪费。
        #   面板公式保留小数 —— 254/4 = 63.5 会多出 0.5 级，
        #   实测伏尔格安速度 252→432、254→433（多 1 点）。
        #   真正的浪费只有「总点数没吃到 510」。
        if ev_sum < 510:
            out(f"提示：学习力只用了 {ev_sum}/510，还差 {510 - ev_sum} 点未分配")
        if ev_sum > 510:
            out(f"⚠️ 学习力合计 {ev_sum} 超过 510 上限")
    else:
        out(f"天赋 {talent} ｜ 学习力 {ev}（实际计入 {ev // 4 * 4}）｜ 性格 {nature or '未指定'}")
        if ev > 255:
            out(f"提示：学习力 {ev} 超出单项 255 上限")
    section("能力值")
    for r in rows:
        nm = f" ×{r['mult']:.1f}" if r["mult"] != 1.0 else ""
        add = extra.get(r["key"], 0)
        addtxt = f"  +{add:>3} = {r['value'] + add:>4}" if add else ""
        out(f"{r['cn']:<4} {r['base']:>4} ×2 = {r['base'] * 2:>4}  →  {r['value']:>4}{nm}{addtxt}")
    base_total = sum(r["value"] for r in rows)
    total = base_total + sum(extra.values())
    if soul_notes:
        section("魂印面板修正")
        for n in soul_notes:
            out(n)
    if used_mints:
        section("刻印")
        mint_sum = {kk: 0 for kk in STAT_KEYS}
        for m in used_mints:
            parts = "  ".join(f"{STAT_CN[kk]}+{m['vals'][kk]}" for kk in STAT_KEYS if m["vals"].get(kk))
            out(f"{m['name']:<20} {parts}")
            for kk in STAT_KEYS:
                mint_sum[kk] += int(m["vals"].get(kk, 0))
        ms = "  ".join(f"{STAT_CN[kk]}+{mint_sum[kk]}" for kk in STAT_KEYS if mint_sum[kk])
        out(f"{'刻印合计':<20} {ms}")
        out(f"{'（合计值 ' + str(sum(mint_sum.values())) + '）':<20}")
    if title_info:
        section("称号")
        tv = "  ".join(f"{STAT_CN[kk]}+{title_info['vals'][kk]}" for kk in STAT_KEYS if title_info["vals"][kk])
        out(f"{title_info['name']:<12} {tv}  （总{title_info['total']}）")
    if eye_info:
        section("目镜")
        ev_ = "  ".join(f"{STAT_CN[kk]}+{eye_info['vals'][kk]}" for kk in STAT_KEYS if eye_info["vals"][kk])
        out(f"{eye_info['name']:<12} {ev_}  （总{eye_info['total']}）")
    if suit_info:
        section("套装")
        out(f"{suit_info['name']}")
        if suit_vals:
            out("面板加成：" + "、".join(f"{STAT_CN[kk]}+{vv}" for kk, vv in suit_vals.items()))
        else:
            out("（未解析出直接面板加成）")
    out("")
    out(f"裸值合计 {base_total}  ｜  含刻印 {total}")


# ── 配装规则：唯一来源在 db.py ───────────────────────────────────
#   SeerKnowledge.PRESET_MINTS / EV_BY_PRESET / EV_MAX_AGAINST / NATURE_BY_STYLE
# 这里只留「预设名字」，供 argparse 的 choices 用，不含任何配置内容。
#
# 2026-11-08 去重：原先这四张表在本文件与 db.py 各存一份（逐字相同，共 84 行），
#   改一处漏一处。现在取用一律走 k.<表名>，本文件不再保存副本。
PRESET_NAMES = ("爆攻", "爆体", "爆盾", "高速", "双刃")

VS_CN = {"phys": "物理", "spec": "特殊"}


def cmd_build(k: Any, args) -> None:
    """一键配装：按预设选三刻印 + 算最终面板。

    群友问「XX 给最好的爆攻是什么面板」→ build XX --preset 爆攻
    「爆特攻」这种说法不常见，所以预设只写「爆攻」，方向由种族自动判。
    """
    preset = args.preset
    # ★ 先过别名（2026-11-08）：爆速/速攻/极速→高速、物攻→爆攻、肉盾→爆盾。
    #   别名表在 db.PRESET_ALIAS，这里和 build_view 各有一道检查，必须在两道之前生效，
    #   否则 CLI 会先拦下来说「预设没有：爆速」。
    preset = k.PRESET_ALIAS.get(str(preset or "").strip(),
                                str(preset or "").strip()) or "爆攻"
    if preset not in k.PRESET_MINTS:
        out(f"预设没有：{preset}。可选：{'/'.join(k.PRESET_MINTS)}")
        return

    hits = k.find_pets(args.name)
    if not hits:
        out(f"找不到精灵：{args.name}")
        return
    pid, petname = hits[0]
    st = k.pet_stats(pid) or {}
    atk = int(st.get("atk") or 0)
    spa = int(st.get("sp_atk") or 0)

    variants = k.PRESET_MINTS[preset]
    if "dual" in variants:
        style = "dual"
        names = variants["dual"]
    else:
        if abs(atk - spa) <= 10 and min(atk, spa) > 0:
            style = "dual"
        else:
            style = "physical" if atk >= spa else "special"
        names = variants.get(style) or variants.get("physical") or variants.get("special")

    nature = args.nature or k.NATURE_BY_STYLE.get(style, {}).get(preset, "坦率")

    # ★ 学习力：优先级 = 显式 --ev > --vs 对线特化 > 预设通用分配
    #   2026-11-03 之前的 bug：--ev 默认 255 = 六项同刷（1512 点，超上限 510 三倍），
    #   非主项（特防/速度）全部虚高，拿它算伤害会得出偏乐观的结论。
    ev_note = ""
    vs = (getattr(args, "vs", "") or "").strip().lower()
    if vs and vs not in k.EV_MAX_AGAINST:
        out(f"--vs 只支持 phys / spec，收到：{vs}")
        return
    if not getattr(args, "ev", ""):
        if vs:
            # 对线特化：极限扛住某一端
            ev_plan = k.EV_MAX_AGAINST[vs]
            # 性格同步改成「加这一端的防御、减没用的那一攻」
            if not args.nature:
                if vs == "phys":
                    nature = "顽皮" if style == "physical" else "大胆"
                else:
                    nature = "慎重" if style == "physical" else "沉着"
            ev_note = (f"对线特化（极限抗{VS_CN[vs]}）："
                       + "  ".join(f"{STAT_CN.get(kk, kk)}{vv}" for kk, vv in ev_plan.items()))
        else:
            # ★ 2026-11-08：改调 k._preset_ev()，和 bot 侧共用一套。
            #   原来直接读 k.EV_BY_PRESET 拿硬编码 252 → 学习力只分 508，
            #   而库里权威表 ev_table 说主属性极限是 254（唐三这种末位 8/4 都够），
            #   254 比 252 面板还多 1 点。两处各算一次就会不一致。
            ev_plan = k._preset_ev(pid, preset, style)
            if ev_plan:
                ev_note = f"学习力自动分配（{sum(ev_plan.values())} 点）：" + "  ".join(
                    f"{STAT_CN.get(kk, kk)}{vv}" for kk, vv in ev_plan.items())
        if ev_plan:
            args.ev = ",".join(f"{kk}={vv}" for kk, vv in ev_plan.items())

    out(f"# {petname}（#{pid}）· {preset}预设" + (f" · 对线抗{VS_CN[vs]}" if vs else ""))
    dir_cn = {"physical": "物攻向", "special": "特攻向", "dual": "双攻向"}[style]
    out(f"种族 攻{atk} / 特攻{spa} → 自动判为 **{dir_cn}**")
    out(f"刻印：{' + '.join(names)}    ｜ 性格：{nature or '中性'} ｜ {args.suit or '无套装'}")
    if ev_note:
        out(ev_note)
    out("")

    args.nature = nature
    args.mint = names
    args.corner = "5"
    cmd_stat(k, args)


def cmd_mintmark(k: Any, args) -> None:
    """刻印查询：名字 / 别名 / 简称 → 满级六项（含隐藏加成）。

    三种写法都收（2026-11-08 补：原先 CLI 没有对应命令，
    bot 侧叫 seer_mintmark，照名字在 CLI 里敲会 invalid choice）：
      完整名     seer.py mintmark 九曲之刃
      单只别名   seer.py mintmark 青龙          （aliases.mintmark_aliases）
      系列+方向  seer.py mintmark 九天攻        （该系列该项最高）
    """
    q = str(args.name or "").strip()
    if not q:
        out("用法：seer.py mintmark <刻印名 / 别名 / 简称>")
        out("例：seer.py mintmark 九曲之刃 ｜ seer.py mintmark 青龙 ｜ seer.py mintmark 九天攻")
        return

    info = {}
    note = ""
    # ⓪ 系列/组合名最优先（2026-11-08）
    #    一次命中多条就按系列列出来 —— 「奥特曼」「和平星」「铠甲勇士」这类
    #    都不该只返回第一条。判断标准就是「命中 >1 条」，不用维护外号表：
    #      · 单条刻印（九曲之刃/梅花/毛毛）→ 1 条，继续走下面的单条明细
    #      · 系列名（和平星 13 条、光与暗 6 条）→ 直接列全
    try:
        _rows0 = k.mintmark_manual_search(q, 20)
    except Exception:
        _rows0 = []
    if len(_rows0) > 1:
        out(f"# 「{q}」共 {len(_rows0)} 条刻印")
        out("")
        for _r in _rows0:
            _ex = sum(_r["extra"])
            _exs = f"  +隐藏{_ex}" if _ex else ""
            out(f"  {_r['name']:<14} 六项和 {_r['total']:>3}{_exs}  "
                f"（含隐藏 {_r['total_with_extra']}）")
        out("")
        out("要点：加方向词可筛，如 奥特曼攻 / 和平星体；完整名看单条明细")
        return
    # ① 先走简称解析：系列名 / 系列+方向 / 独立别名，且按「最高进阶」取。
    #    ★ 必须排在 lookup_mintmark 之前 —— 它的 like 模糊匹配会把
    #      "v8" 吃成 V8-01、"圣战" 吃成圣战之巅（不分进阶、不加优先级）。
    try:
        sys.path.insert(0, os.path.join(HERE, "..", "tools"))
        import mint_picker as _mp
        _alls = _mp.load_mintmarks()
        hit = _mp.resolve_mint(q, _alls)
        if hit:
            got = k.lookup_mintmark(hit["name"]) or {}
            info = {
                "id": got.get("id") or hit.get("id"),
                "name": hit["name"],
                "vals": got.get("vals") or hit.get("vals") or {},
                "extra": got.get("extra"),
                "source": got.get("source") or "mint_picker",
            }
            if hit["name"] != q:
                note = f"简称解析：{q} → {hit['name']}"
    except Exception as exc:  # noqa: BLE001
        out(f"简称解析失败（{type(exc).__name__}: {exc}），只用精确名查")

    # ② 精确名 / 单只别名（青龙 → 苍魂之魄）
    if not (info and info.get("vals")):
        info = k.lookup_mintmark(q)

    # ③ 系列名兜底（2026-11-08 加）
    #    刻印名通常不含系列名：「光与暗系列」的 6 条叫
    #    大地神迹/辉光永恒/深渊瞥视/恶念缚魂/超限审判/时空秩守，
    #    按「光与暗」查名字永远是 0 条。走 db 的 manual_search（它已按
    #    MintMarkClass 反查成员），CLI 和 bot 共用同一份逻辑。
    if not (info and info.get("vals")):
        _rows = k.mintmark_manual_search(q, 12)
        if _rows:
            out(f"# 系列「{q}」共 {len(_rows)} 条刻印")
            out("")
            for _r in _rows:
                _ex = sum(_r["extra"])
                _exs = f"  +隐藏{_ex}" if _ex else ""
                out(f"  {_r['name']:<14} 六项和 {_r['total']:>3}{_exs}  "
                    f"（含隐藏 {_r['total_with_extra']}）")
            out("")
            out("要点：完整名可查单条明细，如 seer.py mintmark 大地神迹")
            return

    if not (info and info.get("vals")):
        out(f"没查到刻印：{q}")
        out("可以试：完整名（九曲之刃）｜ 单只别名（青龙）｜ 系列+方向（九天攻）｜ 系列名（光与暗）")
        out("或 seer.py mint <精灵名> 看这只精灵该装哪三个")
        return

    vals = info["vals"]
    if getattr(args, "json", False):
        dump({"query": q, "id": info.get("id"), "name": info.get("name"),
              "vals": vals, "source": info.get("source")})
        return

    if note:
        out(note)
    total = sum(int(vals.get(x) or 0) for x in MINT_ORDER)
    out(f"# {info.get('name')}  #{info.get('id')}")
    out("   " + "  ".join(f"{STAT_CN[x]} {int(vals.get(x) or 0):>3}" for x in MINT_ORDER))
    out(f"   六项和 {total}｜来源 {info.get('source') or '?'}")
    extra = info.get("extra")
    if extra:
        out(f"   隐藏加成 {extra}（已含在上面数值里）")

    # 成对版提示：九天/圣战这类系列里，物攻版和特攻版是「除攻/特攻外完全相同」的一对。
    # 本命令没有精灵上下文，只能给默认那版；但必须告诉人另一版叫什么，
    # 否则特攻精灵照抄这个结果就会少一大截面板。
    try:
        _cls = k.mint_class(info.get("name") or "") or ""
        if _cls:
            v1 = vals
            for m in _alls:
                if m["name"] == info.get("name"):
                    continue
                if (m.get("class_name") or "") != _cls:
                    continue
                v2 = m.get("vals") or {}
                if (int(v2.get("atk") or 0) == int(v1.get("sp_atk") or 0)
                        and int(v2.get("sp_atk") or 0) == int(v1.get("atk") or 0)
                        and all(int(v2.get(k) or 0) == int(v1.get(k) or 0)
                                for k in ("def", "sp_def", "spd", "hp"))):
                    out(f"   ⚠ 成对版：{info.get('name')} / {m['name']} —— "
                        f"按精灵流派选，特攻精灵别用物攻版")
                    break
    except Exception:
        pass


def cmd_mint(k: Any, args) -> None:
    """刻印推荐：按精灵种族自动判流派，给三刻印方案（同系列最多 2 个）。"""
    sys.path.insert(0, os.path.join(HERE, "..", "tools"))
    import mint_picker as mp

    hits = k.find_pets(args.name)
    if not hits:
        out(f"找不到精灵：{args.name}")
        return
    pid, petname = hits[0]
    st = k.pet_stats(pid) or {}
    atk = int(st.get("atk") or 0)
    spa = int(st.get("sp_atk") or 0)
    style = mp.detect_style(atk, spa)
    _mode = getattr(args, "mode", "auto")
    if _mode == "shield":
        style = "shield"
    elif _mode == "maxdiff":
        style = "maxdiff_physical" if atk >= spa else "maxdiff_special"
    elif _mode == "speed":
        style = "speed_physical" if atk >= spa else "speed_special"
    elif _mode == "speed-spa":
        style = "speed_special"
    elif _mode == "speed-phy":
        style = "speed_physical"
    STYLE_CN = {"physical": "物攻向", "special": "特攻向", "dual": "双攻向", "shield": "盾体向",
                "maxdiff_physical": "极差向·物攻", "maxdiff_special": "极差向·特攻",
                "speed_physical": "卷速向·物攻", "speed_special": "卷速向·特攻"}

    mints = mp.load_mintmarks()
    if not mints:
        out("载入刻印库失败")
        return

    # 310 判定：只对「双攻都有效」的精灵给
    sm = k.pet_soulmarks(pid) or []
    worth310, why310 = mp.needs_310(st, sm)

    corners = [int(x) for x in str(args.corner).replace("，", ",").split(",") if x.strip().isdigit()]
    if not corners:
        corners = [5]
    corner = corners[0]
    if getattr(args, "corner", None) == 5 and worth310 and style != "shield" and getattr(args, "with310", False):
        all5 = mp.pick(mints, style, corners=corners, series=args.series or None, top=6, allow_premium=False)
        top310 = [m for m in mints if m["corner"] == 5 and m["total"] >= mp.PREMIUM_TOTAL]
        if args.series:
            tgt = mp.SERIES_ALIAS.get(args.series, args.series)
            top310 = [m for m in top310 if tgt in m["class_name"]]
        cands = top310 + [m for m in all5 if m not in top310]
    else:
        cands = mp.pick(mints, style, corners=corners, series=args.series or None, top=6, allow_premium=False)
    if not cands:
        out(f"没有匹配的刻印（{corner}角 / 系列={args.series or '不限'}）")
        return

    plans = mp.compose(cands, per_series=2, allow_dup=getattr(args, 'dup', False))

    if args.json:
        dump({
            "pet": petname, "id": pid, "atk": atk, "sp_atk": spa, "style": style,
            "corner": corner,
            "candidates": [{kk: m[kk] for kk in ("id", "name", "class_name", "corner", "total", "vals")}
                           for m in cands[:10]],
            "plans": [[m["name"] for m in p2] for p2 in plans],
        })
        return

    out(f"# {petname}（#{pid}）刻印推荐")
    out(f"种族 攻{atk} / 特攻{spa} → **{STYLE_CN[style]}** ｜ {"/".join(map(str,corners))}角 ｜ 同系列最多 2 个")
    if worth310:
        out(f"⚑ 双攻都吃收益：{why310}")
        if args.with310:
            out("   （已把 310 的螭吻/无极纳入候选）")
        else:
            out("   （310 = 赤洲龙子·螭吻 / 帝皇·无极，总伤只高一点、且限定难获取，默认不推）")
            out("    想换就加 --with310，但不建议")
    if args.series:
        out(f"限定系列：{args.series}")
    section("候选（按该流派加权评分）")

    def word(m: dict) -> str:
        v = m["vals"]
        if style == "shield":
            if v["def"] >= 50 and v["sp_def"] >= 50:
                return "盾印"
            if v["hp"] >= 100:
                return "体印"
            return "盾体"
        if style.startswith("speed_"):
            if v["spd"] >= 43:
                return "极速"
            if v["spd"] >= 35:
                return "高速"
            return "卷速"
        if style.startswith("maxdiff"):
            main = v["atk"] if style.endswith("physical") else v["sp_atk"]
            if main >= 85:
                return "极攻"
            if v["hp"] >= 110:
                return "极体"
            return "差值"
        if style == "physical":
            main = v["atk"]
        elif style == "special":
            main = v["sp_atk"]
        else:
            main = max(v["atk"], v["sp_atk"])
        others = [x for x in (v["def"], v["sp_def"], v["spd"], v["hp"]) if x]
        if main >= 70:
            tag = "爆攻" if style == "physical" else ("爆特攻" if style == "special" else "高双攻")
        elif v["hp"] >= 130:
            tag = "爆体"
        elif v["spd"] >= 40:
            tag = "高速"
        elif v["def"] >= 50 and v["sp_def"] >= 50:
            tag = "高坦"
        else:
            tag = "均衡"
        return tag

    for m in cands[:6]:
        vals = "  ".join(f"{CN}{m['vals'][kk]}" for kk, CN in
                         (("atk", "攻"), ("def", "防"), ("sp_atk", "特攻"),
                          ("sp_def", "特防"), ("spd", "速"), ("hp", "体"))
                         if m["vals"][kk])
        out(f"{m['name']:<22} [{word(m):<4}] 总{m['total']:>3}  {vals}")

    section("方案")
    for i, plan in enumerate(plans[: args.plans], 1):
        out(f"方案{i}：" + " + ".join(m["name"] for m in plan))
        out(f"       {mp.format_plan(plan, style)}")


def cmd_raw(k: Any, args) -> None:
    """直接跑 SQL（只读）。

    默认在 kb.sqlite 上跑；查 seerapi 的表（pet/skill/soulmark…）要加 --db seerapi。
    """
    which = (getattr(args, "db", "kb") or "kb").strip().lower()
    if which == "seerapi":
        db_path = getattr(k, "seer_path", None)
    else:
        db_path = getattr(k, "_kb_path", None)
    if not db_path or not os.path.isfile(db_path):
        out(f"拿不到 {which} 库的路径（{db_path}）")
        return
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.execute(args.sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    except sqlite3.Error as exc:
        # 最常见：在 kb 上查 seerapi 的表
        out(f"SQL 执行失败（库={which}）：{exc}")
        out("提示：pet / skill / soulmark / elementtyperelationorm 这些表在 seerapi 库，")
        out("      请加 --db seerapi 再跑，例如：")
        out('      seer.py raw "select name from pet limit 3" --db seerapi')
        return
    finally:
        con.close()
    if args.json:
        dump([dict(zip(cols, r)) for r in rows])
        return
    out("\t".join(cols))
    for r in rows[: args.limit]:
        out("\t".join("" if v is None else str(v) for v in r))
    if len(rows) > args.limit:
        out(f"…（共 {len(rows)} 行，只显示前 {args.limit}）")


# ─── main ───────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="seer",
        description="赛尔号知识库查询 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python seer.py pet 灵巢
  python seer.py skill 千魂之结
  python seer.py type 轮回 自然/超能
  python seer.py facts 转移
  python seer.py glossary 最优系别
""",
    )
    p.add_argument("--data", help="plugin_data 目录")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("pet", help="精灵全景")
    s.add_argument("name")
    s.set_defaults(fn=cmd_pet)

    s = sub.add_parser("skill", help="技能详情")
    s.add_argument("name")
    s.set_defaults(fn=cmd_skill)

    s = sub.add_parser("type", help="克制计算")
    s.add_argument("atk", help="攻击方属性，如 轮回 或 自然,圣灵")
    s.add_argument("dfd", help="防守方属性，如 自然/超能")
    s.set_defaults(fn=cmd_type)

    s = sub.add_parser("facts", help="facts 检索")
    s.add_argument("keyword")
    s.add_argument("--limit", type=int, default=5)
    s.set_defaults(fn=cmd_facts)

    s = sub.add_parser("topics", help="列出 facts 主题")
    s.set_defaults(fn=cmd_topics)

    s = sub.add_parser("glossary", help="官方词条（语法/时点/专属名词/异常/规则）")
    s.add_argument("keyword", nargs="?", default="", help="词条名，可省略")
    s.add_argument(
        "--kind",
        default="",
        help="按类别列出：语法/时点/专属/异常/规则/伤害/免疫",
    )
    s.add_argument("--limit", type=int, default=0)
    s.add_argument(
        "--pet",
        default="",
        help="按精灵查它的专属名词（可再带 keyword 过滤），如 --pet 阴灵魂姬",
    )
    s.set_defaults(fn=cmd_glossary)

    s = sub.add_parser("besttype", help="最优系别（攻击取最高／受击取最低）")
    s.add_argument("atk", help="攻击方/技能系别，如 自然/圣灵")
    s.add_argument("groups", nargs="+", help="候选来源：每组一个系别，如 机械/草 混沌/战斗")
    s.add_argument("--pick", default="max", choices=["max", "min"],
                   help="max=攻击方视角取最高（默认）；min=受击方视角取最低")
    s.set_defaults(fn=cmd_besttype)

    s = sub.add_parser("timeline", help="回合流程/时点判定（战斗时点系列）")
    s.add_argument("section", nargs="?", default="", help="章节关键词，如 全局主线 / 出手流程 / 回合结束后；留空列章节")
    s.set_defaults(fn=cmd_timeline)

    s = sub.add_parser("selftest", help="自检：两套克制实现一致性 + 关键函数 + 数据完整性")
    s.set_defaults(fn=cmd_selftest)

    s = sub.add_parser("alias", help="外号 → 精灵（查）")
    s.add_argument("name")
    s.set_defaults(fn=cmd_alias)

    s = sub.add_parser("alias-add", help="加/改外号（写 kb.pet_alias，同名覆盖）")
    s.add_argument("alias", help="外号/俗称")
    s.add_argument("pet", help="目标精灵名或 ID")
    s.add_argument("--note", default="", help="备注，如「群友叫法」")
    s.set_defaults(fn=cmd_alias_add)

    s = sub.add_parser("alias-of", help="查某只精灵有哪些外号")
    s.add_argument("pet", help="精灵名或 ID")
    s.set_defaults(fn=cmd_alias_of)

    s = sub.add_parser("tags", help="功能标签：给精灵查覆盖 / 给标签全库反查")
    s.add_argument("pet", nargs="?", default="", help="精灵名（可省略）")
    s.add_argument("--tag", default="", help="功能标签名，如 先制 / 增伤 / 免疫异常")
    s.set_defaults(fn=cmd_tags)

    s = sub.add_parser("suit", help="套装效果（名称/效果搜索）")
    s.add_argument("name", nargs="?", default="", help="套装名，如 晨曦/裁决；留空列全部有战斗效果的套装")
    s.add_argument("--search", default="", help="按效果关键词搜，如 减伤/固伤/速度")
    s.set_defaults(fn=cmd_suit)

    s = sub.add_parser("ls", help="宝石等级数值表（如威力宝石Lv6=+17威力）")
    s.add_argument("kind", nargs="?", default="", help="目前支持 gem")
    s.add_argument("keyword", nargs="?", default="", help="按名字筛选，如 威力 / 真伤")
    s.set_defaults(fn=cmd_ls)

    s = sub.add_parser("pool", help="巅峰池")

    s.add_argument("pet", nargs="?", default="", help="精灵名（查它在哪些池）")
    s.add_argument("--pool", default="", help="池子名，如 35 / 专家 / 禁用")
    s.set_defaults(fn=cmd_pool)

    s = sub.add_parser("status", help="异常状态")
    s.add_argument("category", nargs="?", default="")
    s.add_argument("--limit", type=int, default=60)
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("dmg", help="伤害计算（走统一语义层 dmg_view）")
    s.add_argument("--power", type=int, required=True, help="技能面板威力")
    s.add_argument("--atk", type=int, required=True, help="攻击/特攻面板")
    s.add_argument("--defense", type=int, required=True, help="对手防御/特防面板")
    s.add_argument("--type", dest="type_mult", type=float, default=0.0, help="克制倍率（如 2.25）；留空则按属性自动算")
    s.add_argument("--type-cap", dest="type_mult_cap", type=float, default=0.0, help="克制倍率封顶（战螳螂「最高2倍」传 2.0）")
    s.add_argument("--attack-type", default="", help="技能属性（支持双属性，如 冰/暗影），配合 --defend-type 自动算克制")
    s.add_argument("--defend-type", default="", help="防守方属性（如 圣灵/次元）")
    s.add_argument("--stab", action="store_true", help="本系加成 ×1.5")
    s.add_argument("--crit", action="store_true", help="暴击 ×2（过抗性）")
    s.add_argument("--random", default="range", help="浮动：range/min/max/avg 或 217~255 的整数档")
    s.add_argument("--power-boost", type=float, default=0.0, help="威力加成比例（如 0.1 = 冰霜+10%%）")
    s.add_argument("--power-flat", type=float, default=0.0,
                   help="威力固定加成点数（威力宝石：Lv1~10 = 1/4/7/10/14/17/20/24/27/30）")
    s.add_argument("--variable-power", action="store_true",
                   help="这一发是变威力技能：power_boost 与 power_flat 一律不生效")
    s.add_argument("--general-add", type=float, default=0.0, help="通用增伤（相加），如 0.25")
    s.add_argument("--extra-add", default="", help="额外增伤（连乘），逗号分隔，如 '0.2,1.0'")
    s.add_argument("--general-reduce", type=float, default=0.0, help="通用减伤（相加）")
    s.add_argument("--extra-reduce", default="", help="额外减伤（连乘），逗号分隔，如 '0.1'")
    s.add_argument("--resist", default="full", help="抗性档位 full/none/具体值")
    s.add_argument("--flat", default="", help="固定伤害，'5' 或 '1,1,1'")
    s.add_argument("--percent", default="", help="百分比伤害系数，'0.125' 或 '0.125,0.0625'")
    s.add_argument("--true", dest="true_damage", type=float, default=0.0, help="真实伤害")
    s.add_argument("--atk-stage", type=int, default=0, help="攻击方能力等级 -6~+6")
    s.add_argument("--defense-stage", type=int, default=0, help="防守方能力等级 -6~+6")
    s.add_argument("--hits", type=int, default=1, help="连击次数")
    s.add_argument("--hp", dest="target_hp", type=float, default=0.0, help="目标体力（算百分比用）")
    s.set_defaults(fn=cmd_dmg)

    s = sub.add_parser("stat", help="能力值计算（种族值→满级面板）")
    s.add_argument("name", help="精灵名/俗称/ID")
    s.add_argument("--talent", type=int, default=31, help="个体/天赋，默认 31")
    s.add_argument("--ev", default="255",
                   help="学习力。单值（全项同刷）或分项写法 \"atk=254,sp_def=32,hp=148,spd=76\"")
    s.add_argument("--nature", default="", help="性格名，如 固执/胆小；留空=中性")
    s.add_argument("--mint", action="append", default=[], help="刻印名，可重复最多 3 次")
    s.add_argument("--suit", default="", help="套装名：晨曦之星战甲 / 影翼裁决")
    s.add_argument("--title", default="",
                   help="称号：星光之旅(速最快总和最高) / 神话(纯肉度) / 岁业(肉+速) / 星光铸就者(攻高体高)")
    s.add_argument("--eyepiece", default="",
                   help="目镜：漫游者-AH(最常用 攻30特攻30体40) / 漫游者-AS(攻30特攻30速15) / 漫游者-S(速20)")
    s.add_argument("--hpup", type=int, default=0,
                   help="体力上限加成（药剂，0~20，永久生效）")
    s.add_argument("--team", default="",
                   help="战队加成（可自行选择点哪几项，点错能用药剂洗）："
                        "\"满级\"全部点 / \"maxdiff\"跳过另一攻（极差向用）/ \"攻15,体30\"只点指定项")
    s.add_argument("--annual", action="store_true", help="年费加成：全能力 +10")
    s.add_argument("--soulmod", action="append", default=[],
                   help='魂印面板修正，如 "双防*0.15->非速度" / "双防+100" / "攻击+200" / '
                        '"双攻差*0.15*5->非双攻"（无极圣武） / '
                        '"总和均分"（每项变成六项总和的一半，无为觉者 #4661）')
    s.set_defaults(fn=cmd_stat)

    s = sub.add_parser("build", help="一键配装：预设刻印 + 算最终面板（群友问「给爆攻什么面板」用）")
    s.add_argument("name", help="精灵名/俗称/ID")
    s.add_argument("--preset", default="爆攻",
                    help="预设：爆攻 / 爆体 / 爆盾 / 高速 / 双刃（方向按种族自动判）。"
                         "也吃群友说法：爆速/速攻/极速→高速、物攻→爆攻、肉盾→爆盾（别名表在 db.PRESET_ALIAS）")
    s.add_argument("--talent", type=int, default=31)
    s.add_argument("--ev", default="", help="留空=按预设自动分配合法学习力；也可显式写 255 或 \"atk=252,hp=252\"")
    s.add_argument("--vs", default="", choices=["", "phys", "spec"],
                   help="对线特化：phys=极限抗物理(防252+体252)、spec=极限抗特殊(特防252+体252)；"
                        "留空=按预设通用分配（爆盾默认双防各252）")
    s.add_argument("--nature", default="", help="留空则按预设给默认性格")
    s.add_argument("--suit", default="", help="套装：晨曦 / 裁决")
    s.add_argument("--title", default="")
    s.add_argument("--eyepiece", default="")
    s.add_argument("--hpup", type=int, default=0)
    s.add_argument("--team", default="")
    s.add_argument("--annual", action="store_true")
    s.add_argument("--soulmod", action="append", default=[])
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("mintmark", help="刻印查询：名字/别名/简称 → 满级六项（含隐藏加成）")
    s.add_argument("name", help="刻印名 / 单只别名（青龙）/ 系列+方向（九天攻）")
    s.set_defaults(fn=cmd_mintmark)

    s = sub.add_parser("mint", help="刻印推荐（按种族判流派，同系列最多 2 个）")
    s.add_argument("name", help="精灵名/俗称/ID")
    s.add_argument("--corner", default="5",
                   help="几角刻印，默认 5；可写 \"5,4\" 混装（如 双无限 + 穹明拾光）")
    s.add_argument("--series", default="", help="限定系列，如 龙子 / k13 / 十年 / 泰坦")
    s.add_argument("--plans", type=int, default=3, help="输出几套方案，默认 3")
    s.add_argument("--dup", action="store_true",
                   help="允许同一刻印装两个（如「双无限α」）。默认不装同名")
    s.add_argument("--with310", action="store_true",
                   help="把 310 大总伤刻印（螭吻/无极）也纳入候选。默认不纳入——实战提升很小，且限定难获取")
    s.add_argument("--mode", default="auto",
                   choices=["auto", "attack", "shield", "maxdiff", "speed", "speed-spa", "speed-phy"],
                   help="auto=按种族判；shield=盾体向；maxdiff=极差向；"
                        "speed=卷速向（自动判方向）；speed-spa=卷速·特攻印；speed-phy=卷速·物攻印")
    s.set_defaults(fn=cmd_mint)

    s = sub.add_parser("raw", help="直接跑 SQL（只读，默认 kb 库，--db seerapi 查游戏数据）")
    s.add_argument("sql")
    s.add_argument(
        "--db",
        default="kb",
        choices=["kb", "seerapi"],
        help="在哪个库上跑：kb（默认，事实/专栏）或 seerapi（精灵/技能/魂印）",
    )
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_raw)

    return p


def main() -> None:
    args = build_parser().parse_args()
    data_dir = _find_data_dir(args.data)
    k = open_kb(data_dir)
    try:
        args.fn(k, args)
    finally:
        k.close()


if __name__ == "__main__":
    main()





