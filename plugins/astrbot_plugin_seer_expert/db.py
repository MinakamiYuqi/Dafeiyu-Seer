"""赛尔号知识访问层：只读 SeerAPI 本地库 + 可写的问答记忆库。

设计原则：
1. 事实类问题全部走 SQL，不让模型凭空回忆；
2. 查不到就明确返回「未收录」，由模型去问人，答案写进 kb 库；
3. kb 库里的答案优先级高于静态库（人工校正过）。
"""
# ════════════════════════════════════════════════════════════════════════
# 数据源地图 —— 新鱼必读，别自己瞎摸（2026-11-02 整理）
# ════════════════════════════════════════════════════════════════════════
#
# 【一句话口诀】
#     一个词的定义   → manual_db/effectDes.db
#     一只精灵的数据 → seerapi.sqlite
#     一条实战结论   → kb.sqlite
#
# ── ① manual_db/*.db ── 官方词条手册（同步自游戏资源，最权威）
#    访问：self._open_manual(库名) → 临时只读连接，用完即关
#          self._manual_q(库名, sql) → 带解析缓存的单值查询
#
#     effectDes   640 条  ★官方词条总表，kind 分七类：
#                          kind=0 免疫类      2   （镜像里没有）
#                          kind=1 精灵专属名词 443
#                          kind=2 异常状态     91  （镜像只有 48，少 43 条）
#                          kind=3 伤害类型      1
#                          kind=4 语法解释     71  ← 未触发/执行/之后/然后/以/转化/全属性
#                          kind=5 时点解释     21  ← 回合开始时/被击败/登场时/使用技能时
#                          kind=6 规则名词     11  ← 秒杀/放逐/驱逐/最优系别
#     effectIcon 2155 条  魂印图标 + 说明文本
#     effectInfo 2393 条  效果模板解析版
#     skill_effect 2347   技能效果语法模板（含 {0}{1} 参数占位）
#     buff       3537 条  通用 buff 词条（吸血/减伤/致命一击…）
#     mintmark   3837 条  刻印（MintMark + MintMarkClass）
#     pets       6539 条  巅峰池/精灵名映射（走 _manual_q）
#     pvp           9 条  巅峰池名单与截止时间（走 _manual_q）
#     skillTypes  139 条  技能类型（走 _manual_q）
#     te_xing     400 条  特性词条
#     effectag     20 条  效果标签
#     effectbuff    3 条  套装/印记
#
# ── ② seerapi.sqlite ── 第三方游戏数据镜像（61MB / 160 表，seer_info 每 60 分钟同步）
#    访问：self._q(sql) → 临时只读连接；★ 查不到表名/出错会静默返回 []，不报错
#    内容：pet / pet_base_stats / skill / skillinpetorm / soulmark /
#          elementtyperelationorm / mintmark / gem / suit / equip …
#
#    ⚠️ 坑 1：glossary_entry(595 条) 只是 effectDes 的子集镜像
#             异常状态只 48 条、没有免疫类。查词条优先 effectDes，
#             glossary_entry 仅作 fallback（万一 manual_db 缺失）。
#
#    ⚠️ 坑 2：pet_special_effect.primary_rule 会子串误挂
#             direct_pet_glossary → 精灵自己的名词，可信
#             text_exact_name     → 按字面匹配，中文无词边界，极易误挂
#             实例：「失体」挂到 199 只精灵，只因为魂印含「已损失体力值」
#                   真主人是 #4499 德布比特；古龙 #3524 真名词只有
#                   【逆命之威】【荒古之慑】两个
#             pet_effects() 已内置 _name_standalone 独立词校验，别绕过它直查。
#
# ── ③ kb.sqlite ── 玩家知识库（是「结论」，不是「官方定义」）
#    访问：self._kb（长连接，可写）
#    内容：facts 401 条玩家口述/实测结论 ／ doc_concepts 181 专栏概念
#          doc_rules 322 条条件→结论 ／ articles 31 篇专栏原文
#          pet_alias 俗称 ／ movesets 定型配招 ／ seen 查询痕迹
#
#    ⚠️ 引用 kb 的结论时要说清是「玩家结论」，别当官方判定通报。
#
# ── 已隔离（manual_db/_unused/，代码不查，别再用）──
#     ❌ 2026-11-02 此判断已作废：三个库经逐条比对，全部有 seerapi 没有的独有数据，
#        已恢复现役。教训：不能只看「代码有没有引用」就判定重复，必须比对内容字段。
#     moves.db 6.64MB      27376 条技能，比 seerapi.skill(27394) 多 8 个字段：
#                          EffectText / side_effect / side_effect_arg /
#                          friend_side_effect / ordinary / hide / atk_type / mon_id
#                          → seerapi 的 info 只是叙事化短描述，结构化效果字段在这
#     petEffectIcon.db 0.98MB  1200 条「专属特性」分层文本，按
#                          常驻效果 / 触发效果 / 限定效果 分段，
#                          seerapi 的 pet_special_effect 里没有这种分层表述
#     rich_text_tree.db 1.62MB  同上的树形结构版（level/sprites/segments/children）
#
# ── 技能效果的两个关键判读（别靠文案猜）──
#     ① 固有效果 vs 命中效果 → 看 seerapi.skill_effect_in_use.analyze_info
#        里的 [sprite name=iconInherent] 图标（52808 条中 1637 条带此图标）
#        判定函数见 _effect_lines()，输出前缀〔固有〕〔命中〕
#     ② 技能的完整结构化效果 → manual_db/moves.db 的 EffectText / side_effect 字段
#
# ════════════════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import ast
import os
import re
import sqlite3
import threading
import time
from typing import Any

# 标准 logging，不用 astrbot.api.logger：seer-cli/seer.py 会直接 import 本模块，
# 那个环境里没有 astrbot，用插件 logger 会把 CLI 一起炸掉。
logger = logging.getLogger(__name__)

# kb.sqlite 是唯一不可再生的自建数据（不同步、不覆盖），破坏性写入前拍快照。
KB_BACKUP_KEEP = 10          # 保留最近 10 份
KB_BACKUP_MIN_INTERVAL = 60.0  # 同一分钟内只拍一次，避免高频写入刷爆目录
# delete_facts 护栏：关键词短且命中过多时，几乎一定是要误删。
DELETE_FACTS_MAX_HITS = 20
DELETE_FACTS_MIN_KEYWORD = 2

_STAT_KEYS = ("atk", "def", "sp_atk", "sp_def", "spd", "hp")


class SeerKnowledge:
    def __init__(self, seer_db_path: str, kb_db_path: str):
        self.seer_path = seer_db_path
        # 注意：不长期持有连接。seer_info 会整体覆盖 seerapi.sqlite，
        # Windows 下持有句柄会导致它写入失败（PermissionError）。
        self._seer = None
        os.makedirs(os.path.dirname(kb_db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._kb_path = kb_db_path
        self._kb = sqlite3.connect(kb_db_path, check_same_thread=False)
        self._kb.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen (
                key TEXT PRIMARY KEY,
                kind TEXT,
                count INTEGER NOT NULL DEFAULT 1,
                first_at REAL NOT NULL,
                last_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS summaries (
                key TEXT PRIMARY KEY,
                kind TEXT,
                src_hash TEXT,
                summary TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending (
                key TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                created_at REAL NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS movesets (
                pet_id INTEGER PRIMARY KEY,
                skills TEXT NOT NULL,
                note TEXT,
                detail TEXT,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS facts (
                topic TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source TEXT,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pet_alias (
                alias TEXT PRIMARY KEY,
                pet_id INTEGER NOT NULL,
                note TEXT,
                updated_at REAL NOT NULL
            );
            """
        )
        self._kb.commit()

    # ---------------- 备份保护 ----------------
    #
    # kb.sqlite 是 479 条 facts + pet_build 的家，seerapi/aliases 坏了能重下，它坏了没得救。
    # 破坏性写入（改配招 / 删 facts / 重建 tags / 批量改 pending）之前先拍快照，
    # 用 sqlite3 的 backup API（原子、不会拿到写了一半的页），保留最近 KB_BACKUP_KEEP 份。

    def _backup_kb(self, reason: str = "auto") -> str | None:
        """破坏性写入前的快照。返回备份路径，被节流或失败时返回 None。"""
        now = time.time()
        if now - getattr(self, "_last_backup_at", 0.0) < KB_BACKUP_MIN_INTERVAL:
            return None
        backup_dir = os.path.join(os.path.dirname(self._kb_path), "kb_backups")
        try:
            os.makedirs(backup_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
            dest = os.path.join(backup_dir, f"kb-{stamp}-{reason}.sqlite")
            src = sqlite3.connect(self._kb_path)
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            self._last_backup_at = now
            self._prune_kb_backups(backup_dir)
            logger.info("[kb] 已备份 %s", os.path.basename(dest))
            return dest
        except Exception as exc:  # 备份失败不能拖垮主流程，但必须留痕
            logger.warning("[kb] 备份失败（%s）：%s", reason, exc)
            return None

    def _prune_kb_backups(self, backup_dir: str) -> None:
        """只留最近 KB_BACKUP_KEEP 份，按修改时间淘汰。"""
        try:
            files = [
                os.path.join(backup_dir, name)
                for name in os.listdir(backup_dir)
                if name.startswith("kb-") and name.endswith(".sqlite")
            ]
        except OSError:
            return
        files.sort(key=lambda p: os.path.getmtime(p))
        for old in files[: max(0, len(files) - KB_BACKUP_KEEP)]:
            try:
                os.remove(old)
            except OSError:
                pass

    # ---------------- 静态库 ----------------

    def available(self) -> bool:
        return os.path.isfile(self.seer_path)

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        """每次查询临时开只读连接；遇到 locked/busy 短暂重试，失败返回空。"""
        if not os.path.isfile(self.seer_path):
            return []
        for attempt in range(3):
            con = None
            try:
                con = sqlite3.connect(
                    f"file:{self.seer_path}?mode=ro", uri=True, timeout=3
                )
                return con.execute(sql, args).fetchall()
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    time.sleep(0.15 * (attempt + 1))
                    continue
                return []
            except sqlite3.Error:
                return []
            finally:
                if con is not None:
                    con.close()
        return []



    # ---------------- 名词/语法词条检索（手册 buff/effectDes/skill_effect/te_xing） ----------------

    _EFFECTDES_KINDS = {
        0: "免疫类",
        1: "精灵专属名词",
        2: "异常状态",
        3: "伤害类型",
        4: "语法解释",
        5: "时点解释",
        6: "规则名词",
    }

    # ────────────────────────────────────────────────────────────
    # 语义层：glossary_view —— 官方词条的唯一判定源
    #
    # 两边的能力原来各缺一半：
    #   · CLI 能「按类别列词条」「无参数列类别」，bot 没有
    #   · bot 有「宝石」特判（走 gem_search），CLI 没有
    #   · bot 会附「参数占位说明」，CLI 没有
    # 现在统一到这里，呈现层只决定排版。
    # ────────────────────────────────────────────────────────────
    GLOSSARY_CATEGORIES = [
        ("语法", "未触发/执行/转化/全属性/之后/然后/以 这类判定用语"),
        ("时点", "回合开始时/被击败/击败/登场时/使用技能时 这类时点"),
        ("专属", "精灵专属名词（四象门/六芒阵/失体…）"),
        ("异常", "异常状态定义（中毒/流血/麻痹…）"),
        ("规则", "规则名词（秒杀/放逐/驱逐/最优系别…）"),
        ("伤害", "伤害类型（固定/百分比/真实/致死…）"),
        ("免疫", "免疫类词条"),
    ]

    def glossary_view(self, keyword: str = "", kind: str = "", limit: int = 6,
                      pet: str = "") -> dict[str, Any]:
        """官方词条的语义层。

        判定字段：
          categories  —— 无参数时该列出哪些类别（附说明）
          empty_hint  —— 任何空结果的提示原文
          args_hint   —— 参数占位 {0}/{1} 的说明（有词条时才给）

        ★ 2026-11-08 新增 pet 参数：按精灵查专属名词。
          原来只能 `seer.py pet <名>` 看【专属效果】段，
          但「查词条」这一类操作应该都能在 glossary 下完成，不用记两个命令。
          底层就是 pet_effects()（effectDes.monster 逗号串匹配）。
        """
        kw = str(keyword or "").strip()
        kd = str(kind or "").strip()
        pt = str(pet or "").strip()

        view: dict = {
            "mode": "none",
            "keyword": kw,
            "kind": kd,
            "pet": None,
            "rows": [],
            "categories": [],
            "empty_hint": None,
            "args_hint": None,
        }

        # ⓪ 按精灵查专属名词（最高优先，有 pet 参数就不用看 kind/keyword）
        if pt:
            view["mode"] = "pet"
            try:
                pets = self.find_pets(pt)
            except Exception:
                pets = []
            if not pets:
                view["empty_hint"] = f"未收录精灵「{pt}」，换个写法或先用 alias 查。"
                return view
            pid, pname = pets[0]
            view["pet"] = {"id": pid, "name": pname}
            try:
                effs = self.pet_effects(pid) or []
            except Exception:
                effs = []
            rows = []
            for r in effs:
                nm = (r.get("name") or "").strip()
                if not nm:
                    continue
                if kw and (kw not in nm and kw not in (r.get("desc") or "")):
                    continue
                rows.append({"name": nm, "desc": (r.get("desc") or "").strip(),
                             "kind": "专属"})
            view["rows"] = rows
            if not rows:
                if kw:
                    view["empty_hint"] = f"「{pname}」的专属名词里没有匹配「{kw}」的。"
                else:
                    view["empty_hint"] = (
                        f"「{pname}」没有专属名词条目 —— 不是每只精灵都有"
                        "（effectDes 里 monster 字段为空的就没有）。")
            return view

        # ① 按类别列
        if kd:
            view["mode"] = "kind"
            try:
                rows = self.glossary_by_kind(kd, limit=limit if limit > 10 else 300)
            except Exception:
                rows = []
            if kw:
                rows = [r for r in rows
                        if kw in (r.get("name") or "") or kw in (r.get("desc") or "")]
            view["rows"] = list(rows or [])
            if not view["rows"]:
                names = "/".join(n for n, _ in self.GLOSSARY_CATEGORIES)
                view["empty_hint"] = f"类别「{kd}」没有词条（可用：{names}）。"
            return view

        # ② 无参数 → 列类别
        if not kw:
            view["mode"] = "categories"
            view["categories"] = list(self.GLOSSARY_CATEGORIES)
            return view

        # ③ 宝石特判：效果在 gemeffectlink，不在 gem 表本体
        if "宝石" in kw:
            try:
                gems = self.gem_search(kw, 8)
            except Exception:
                gems = []
            if gems:
                view["mode"] = "gem"
                view["rows"] = list(gems)
                return view

        # ④ 普通词条搜索
        view["mode"] = "search"
        try:
            rows = self.glossary_search(kw, limit)
        except Exception:
            rows = []
        view["rows"] = list(rows or [])
        if not view["rows"]:
            view["empty_hint"] = (
                f"手册词条里没搜到「{kw}」。"
                "可以换个更短的词（如「吸血」而不是「吸血效果」）再试，"
                "或说明是哪只精灵的专属词条（专属效果用 seer_effect）。"
            )
            return view
        view["args_hint"] = (
            "（模板里的 {0}/{1}/{2} 是参数占位，具体数值看该技能/魂印的实际文本；"
            "需要某只精灵的专属效果用 seer_effect）"
        )
        return view

    def glossary_by_kind(self, label: str, limit: int = 30) -> list[dict]:
        """按类别列出词汇（label 可为：精灵专属/异常状态/语法/时点/规则/免疫/伤害类型）。"""
        mapping = {
            "精灵专属": 1, "专属名词": 1, "专属": 1,
            "异常状态": 2, "异常": 2,
            "伤害类型": 3, "伤害": 3,
            "语法": 4, "语法解释": 4,
            "时点": 5, "时点解释": 5,
            "规则": 6, "规则名词": 6,
            "免疫": 0,
        }
        key = None
        for k, v in mapping.items():
            if k in str(label or ""):
                key = v
                break
        if key is None:
            return []
        conn = self._open_manual("effectDes")
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "select coalesce(kinddes,''), coalesce(desc,'') from effectDes "
                "where kind=? order by id limit ?",
                (key, int(limit)),
            ).fetchall()
        except Exception:
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return [
            {"src": self._EFFECTDES_KINDS.get(key, "词条"), "name": kd, "desc": str(d)[:300]}
            for kd, d in rows
        ]

    def glossary_search(self, keyword: str, limit: int = 6) -> list[dict]:
        """在手册各词条库里搜「通用效果名 / 异常状态 / 特性 / 语法模板」。

        覆盖：
          buff.desc / tag            通用 buff 词条（吸血、减伤、致命一击…）
          effectDes.kinddes / desc   异常状态词条
          skill_effect.info / tagA   技能效果语法（含参数占位）
          effectInfo.info / analyze  效果解析版
          te_xing.Des / Intro        特性词条
          effectag.tag               效果标签词表
          effectbuff.Name / Desc     套装/印记类
        """
        kw = str(keyword or "").strip()
        if not kw:
            return []
        like = f"%{kw}%"
        out: list[dict] = []

        # ① 手册语法/词条定义：effectDes 里 kinddes 精确命中的条目
        #    kind=4 是「语法解释」（未触发 / 先制 / 当回合 / 致命一击…）
        conn = self._open_manual("effectDes")
        if conn is not None:
            try:
                for kd, desc, kind in conn.execute(
                    "select coalesce(kinddes,''), coalesce(desc,''), coalesce(kind,0) "
                    "from effectDes where kinddes = ? order by id",
                    (kw,),
                ).fetchall():
                    label = self._EFFECTDES_KINDS.get(int(kind or 0), "词条")
                    out.append({"src": label, "name": kd, "desc": str(desc)[:300]})
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass


        def _q(db: str, sql: str, args: tuple, fmt) -> None:
            conn = self._open_manual(db.replace(".db", ""))
            if conn is None:
                return
            try:
                for row in conn.execute(sql, args).fetchall():
                    item = fmt(row)
                    if item:
                        out.append(item)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        # buff 词条
        _q("buff", "select coalesce(tag,''), desc from buff where desc like ? or tag like ? limit ?",
           (like, like, limit),
           lambda r: {"src": "buff", "name": r[0] or "(无名)", "desc": str(r[1])[:260]})
        # 异常状态
        _q("effectDes", "select coalesce(kinddes,''), desc, coalesce(kind,0) from effectDes where desc like ? or kinddes like ? limit ?",
           (like, like, limit),
           lambda r: {"src": self._EFFECTDES_KINDS.get(int(r[2] or 0), "异常状态"),
                      "name": r[0] or "(无)", "desc": str(r[1])[:260]})
        # 技能效果语法
        _q("skill_effect", "select coalesce(tagA,''), info from skill_effect where info like ? or tagA like ? limit ?",
           (like, like, limit),
           lambda r: {"src": "技能语法", "name": r[0] or "(无标签)", "desc": str(r[1])[:260]})
        # 效果解析版
        _q("effectInfo", "select coalesce(info,''), coalesce(analyze,'') from effect where info like ? or analyze like ? limit ?",
           (like, like, limit),
           lambda r: {"src": "效果解析", "name": "(模板)", "desc": (str(r[0]) or str(r[1]))[:260]})
        # 特性
        _q("te_xing", "select coalesce(Des,''), coalesce(Desc,'') from te_xing where Des like ? or Desc like ? limit ?",
           (like, like, limit),
           lambda r: {"src": "特性", "name": r[0] or "(无)", "desc": str(r[1])[:260]})
        # 效果标签
        _q("effectag", "select coalesce(tag,''), '' from effectag where tag like ? limit ?",
           (like, limit),
           lambda r: {"src": "效果标签", "name": r[0], "desc": ""})
        # 套装/印记
        _q("effectbuff", "select coalesce(Name,''), coalesce(Desc,'') from effectbuff where Name like ? or Desc like ? limit ?",
           (like, like, limit),
           lambda r: {"src": "套装/印记", "name": r[0] or "(无)", "desc": str(r[1])[:260]})

        if not out:
            out = self.glossary_by_kind(kw, limit)

        # 去重 + 截断
        seen, uniq = set(), []
        for it in out:
            key = (it["src"], it["name"], it["desc"][:40])
            if key in seen:
                continue
            seen.add(key)
            uniq.append(it)
        return uniq[: int(limit)]

    # ---------------- 玩家俗称 → 最新形态（进化链） ----------------
    # ── aliases.sqlite（2026-11-06 起真正接上）──────────────────────────
    # 这个库由 seer_info 每小时同步，里面有：
    #   pet_aliases              368 条精灵俗称
    #   mintmark_aliases           7 条刻印简称（毛毛/梅花/青龙/白虎/朱雀/玄武）
    #   mintmark_class_aliases     9 条系列别名（九霄系列/k14/k15/泳池系列…）
    #   mintmark_series_members   16 条系列成员（十年/V10 各自 8 个 id）
    # **在此之前 expert 从来没打开过它**，所以这些简称一直查不到。
    def _aliases_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(self.seer_path)), "aliases.sqlite")

    def _alias_q(self, sql: str, args: tuple = ()) -> list[tuple]:
        """查 aliases.sqlite：**先 overlay（本地自用），空则回主库**。

        overlay = aliases_local_overlay.sqlite，seer_info 同步后会把它合并回主库，
        所以往 overlay 里加的简称不会被上游更新冲掉。
        """
        base = os.path.dirname(os.path.abspath(self.seer_path))
        for fname in ("aliases_local_overlay.sqlite", "aliases.sqlite"):
            path = os.path.join(base, fname)
            if not os.path.isfile(path):
                continue
            try:
                con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
            except sqlite3.Error:
                continue
            try:
                rows = con.execute(sql, args).fetchall()
            except sqlite3.Error:
                rows = []
            finally:
                con.close()
            if rows:
                return rows
        return []

    def mint_alias_targets(self, name: str) -> list[int]:
        """刻印简称/系列名 → 候选刻印 id 列表（aliases.sqlite 的三张表）。"""
        key = str(name or "").strip()
        if not key:
            return []
        ids: list[int] = []
        # ① 直接简称
        for (tid,) in self._alias_q(
            "select target_id from mintmark_aliases where name=?", (key,)
        ):
            ids.append(int(tid))
        if ids:
            return ids
        # ② 系列名 → 成员
        for (tid,) in self._alias_q(
            "select target_id from mintmark_series_members where name=?", (key,)
        ):
            ids.append(int(tid))
        if ids:
            return ids
        # ③ 系列别名 → 分类 id，再取该分类下所有刻印
        for (cid,) in self._alias_q(
            "select target_id from mintmark_class_aliases where name=?", (key,)
        ):
            for (mid,) in self._manual_q(
                "mintmark", "select ID from MintMark where MintmarkClass=?", (int(cid),)
            ):
                ids.append(int(mid))
        return ids

    def alias_lookup(self, name: str) -> int | None:
        """「炎魔」「上古炎兽」这类俗称/旧形态名 → 最新形态的 pet_id。"""
        key = str(name or "").strip()
        if not key:
            return None
        try:
            row = self._kb.execute(
                "select pet_id from pet_alias where alias=?", (key,)
            ).fetchone()
        except Exception:
            return None
        return int(row[0]) if row else None

    def add_alias(self, alias: str, pet_id: int, note: str = "") -> None:
        key = str(alias or "").strip()
        if not key or not pet_id:
            return
        self._kb.execute(
            "insert into pet_alias(alias, pet_id, note, updated_at) values(?,?,?,?) "
            "on conflict(alias) do update set pet_id=excluded.pet_id, "
            "note=excluded.note, updated_at=excluded.updated_at",
            (key, int(pet_id), note, time.time()),
        )
        self._kb.commit()

    def alias_of(self, pet_id: int) -> list[str]:
        try:
            rows = self._kb.execute(
                "select alias from pet_alias where pet_id=?", (int(pet_id),)
            ).fetchall()
        except Exception:
            return []
        return [r[0] for r in rows]

    def _apply_typo(self, text: str) -> str:
        """按 TYPO_MAP 纠正玩家常打错的精灵名（长键优先，避免短键抢先替换）。"""
        out = str(text or "")
        for wrong in sorted(self.TYPO_MAP, key=len, reverse=True):
            if wrong in out:
                out = out.replace(wrong, self.TYPO_MAP[wrong])
        return out

    def find_pets(self, name: str, limit: int = 5) -> list[tuple[int, str]]:
        """按名字找精灵：先看俗称映射，再按种族值最高的形态优先。

        查不到时按 TYPO_MAP 纠错再查一次（龙啖/龙淡/龙炎 → 龙琰，柯尔霍得 → 柯尔霍德…），
        纠错只补空结果，不影响正常命中。
        """
        name = (name or "").strip()
        if not name:
            return []
        alias_id = self.alias_lookup(name)
        if alias_id:
            head = self._q("select id, name from pet where id=?", (alias_id,))
            if head:
                # ★ 别名精确命中就是权威，不要再拼「名字里含这几个字」的杂项候选。
                #   2026-11-03 实例：查「神盖」→ 别名指向 #4766 灭战神决·盖亚，
                #   旧逻辑又把 find_pets_plain('神盖') 匹配到的「战神·盖亚(#2780)」
                #   「战神盖亚(#1845)」一并附上，输出成「共 3 个形态」，自己制造歧义。
                #   别名表里一条外号对应一只，就该只返回这一只。
                return [tuple(head[0])]
        found = self.find_pets_plain(name, limit)
        if found:
            return found
        fixed = self._apply_typo(name)
        if fixed and fixed != name:
            return self.find_pets(fixed, limit)
        return found

    def _pet_power(self, pet_id: int) -> int:
        """种族值总和（觉醒后优先），用于「同名多形态取最强」排序。"""
        try:
            both = self.pet_stats_both(pet_id)
        except Exception:
            return 0
        best = 0
        for key in ("awakened", "base"):
            st = both.get(key) or {}
            if not st:
                continue
            total = sum(int(st.get(k) or 0) for k in ("atk", "def", "sp_atk", "sp_def", "spd", "hp"))
            best = max(best, total)
        return best

    def find_pets_plain(self, name: str, limit: int = 5) -> list[tuple[int, str]]:
        """不带俗称跳转的原始查找：同名多形态按种族值排序。

        例：尤纳斯 → 优先「原始黑洞·尤纳斯」；索伦森 → 「混沌魔君索伦森」；
            谱尼 → 「圣灵谱尼」。
        """
        name = (name or "").strip()
        if not name:
            return []

        # 支持直接给编号（例：4875 / #4875）
        digits = name.lstrip("#").strip()
        if digits.isdigit():
            rows = self._q("select id, name from pet where id=?", (int(digits),))
            if rows:
                return rows[:limit]

        like = f"%{name}%"
        stripped = name.replace("·", "").replace("・", "").replace(" ", "")
        cands: list[tuple[int, str]] = []
        seen: set[int] = set()

        for rows in (
            self._q("select id, name from pet where name=?", (name,)),
            self._q("select id, name from pet where name like ? limit 40", (like,)),
            self._q(
                "select id, name from pet where replace(replace(name,'·',''),' ','') like ? limit 40",
                (f"%{stripped}%",),
            ),
        ):
            for pid, pname in rows:
                if pid not in seen:
                    seen.add(pid)
                    cands.append((pid, pname))

        if not cands:
            # seerapi 里没有 → 回退手册（新精灵往往手册先到）
            return self.manual_find_pets(name, limit)

        # 完全同名且只有一个候选时直接返回，省掉算种族值
        if len(cands) == 1:
            return cands[:limit]

        scored = []
        for pid, pname in cands:
            power = self._pet_power(pid)
            # 名字里带「·」的多数是进阶/异名形态，同种族值时略优先
            bonus = 1 if ("·" in pname) else 0
            scored.append((power, bonus, pid, pname))
        scored.sort(key=lambda x: (-x[0], -x[1], len(x[3])))

        # 完全同名（老版本）在种族值并列时放前面
        out = [(pid, pname) for _, _, pid, pname in scored[:limit]]
        return out


    def pet_types(self, pet_id: int) -> list[str]:
        """精灵属性：type_id 指向 element_type_combination（双属性）或 element_type（单属性）。"""
        rows = self._q(
            """select a.name, b.name from pet p
               join element_type_combination c on c.id = p.type_id
               left join element_type a on a.id = c.primary_id
               left join element_type b on b.id = c.secondary_id
               where p.id = ?""",
            (pet_id,),
        )
        if rows:
            a, b = rows[0]
            return [x for x in (a, b) if x]
        rows = self._q(
            "select e.name from pet p join element_type e on e.id=p.type_id where p.id=?",
            (pet_id,),
        )
        if rows:
            return [rows[0][0]]
        # seerapi 没这只 → 用手册的 Type 当 type_id 查属性组合（两套表 id 同源）
        mrow = self._manual_pet_row(pet_id)
        if mrow and mrow[2]:
            rows = self._q(
                """select a.name, b.name from element_type_combination c
                   left join element_type a on a.id = c.primary_id
                   left join element_type b on b.id = c.secondary_id
                   where c.id = ?""",
                (mrow[2],),
            )
            if rows:
                a, b = rows[0]
                return [x for x in (a, b) if x]
        return []

    def pet_type(self, pet_id: int) -> str:
        types = self.pet_types(pet_id)
        return "/".join(types)

    # ────────────────────────────────────────────────────────────
    # 语义层：stat_view —— 面板计算的唯一来源
    #
    # 原来这套算式只写在 seer-cli/seer.py 里（stat_value / _nature_mult /
    # lookup_mintmark），bot 侧没有对应工具，导致「算完整面板」这类问题
    # 只能转给 DSH。2026-11-03 下沉到这里，两边共用。
    # ────────────────────────────────────────────────────────────
    _STAT_CN = {"atk": "攻击", "def": "防御", "sp_atk": "特攻",
                "sp_def": "特防", "spd": "速度", "hp": "体力"}

    # ★ 25 种性格完整表（2026-11-03 修正）
    #   旧版硬编码有 5 处错名 + 漏 4 个：
    #     淘气→顽皮 · 乐天→无虑 · 温和→慎重 · 慎重(重复)→温顺 · 爽朗→开朗
    #     漏：孤独 / 害羞 / 实干 / 浮躁
    #   后果：用「顽皮」算玄武时性格完全不生效（加防、减特攻都丢了）。
    #   现在以 seerapi 的 nature+nature_attr 为准，运行时按库动态构建，
    #   这里只留一份**离线兜底表**（与库同源，照 facts 抄）。
    _NATURE_TABLE = {
        # 攻 +10%
        "孤独": ("atk", "def"), "固执": ("atk", "sp_atk"),
        "调皮": ("atk", "sp_def"), "勇敢": ("atk", "spd"),
        # 防 +10%
        "大胆": ("def", "atk"), "顽皮": ("def", "sp_atk"),
        "无虑": ("def", "sp_def"), "悠闲": ("def", "spd"),
        # 特攻 +10%
        "保守": ("sp_atk", "atk"), "稳重": ("sp_atk", "def"),
        "马虎": ("sp_atk", "sp_def"), "冷静": ("sp_atk", "spd"),
        # 特防 +10%
        "沉着": ("sp_def", "atk"), "温顺": ("sp_def", "def"),
        "慎重": ("sp_def", "sp_atk"), "狂妄": ("sp_def", "spd"),
        # 速度 +10%
        "胆小": ("spd", "atk"), "急躁": ("spd", "def"),
        "开朗": ("spd", "sp_atk"), "天真": ("spd", "sp_def"),
        # 平衡
        "害羞": ("atk", "atk"), "实干": ("atk", "atk"),
        "坦率": ("atk", "atk"), "浮躁": ("atk", "atk"),
        "认真": ("atk", "atk"),
    }

    @classmethod
    def _nature_mult(cls, nature: str, key: str) -> float:
        """性格对该项的修正：+10% / -10% / 无。"""
        if not nature:
            return 1.0
        pair = cls._NATURE_TABLE.get(str(nature).strip())
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

    @staticmethod
    def _stat_value(base: int, talent: int, ev: int,
                    nature_mult: float = 1.0, is_hp: bool = False) -> int:
        """满级（100）单项能力值。

        ★ 公式（2026-11-03 用户点破「4点学习力=1点能力」后定案）：
            五项 = int( (种族×2 + 学习力/4 + 个体 + 5) × 性格修正 )
            体力 = int( 种族×2 + 学习力/4 + 个体 + 110 )

        ★ 唯一关键：**学习力 ÷ 4 保留小数，不取整**
            被性格修正的项，这 0.25/0.5/0.75 会被 ×1.1 或 ×0.9 放大，
            所以边际收益不是严格 4:1；
            没被性格修正的项（体力、中性项），int() 把小数砍掉，
            自然退化成「每 4 点学习力 = 1 点能力」。

        ★ 验证（全部与「精灵配置计算器」截图逐项对上）
          | 精灵 | 学习力 | 裸值 ×性格 | int | +额外 | 实测 |
          |---|---|---|---|---|---|
          | 南霜 4511 攻148×1.1 | 252 | 434.50 | 434 | +55 | 489 |
          | 南霜 4511 攻148×1.1 | 254 | 435.05 | 435 | +55 | 490 |
          | 盖亚 4947 攻160×1.1 | 253 | 461.175 | 461 | +55 | 516 |
          | 盖亚 4947 攻160×1.1 | 255 | 461.725 | 461 | +55 | 516 |
          | 无极圣武 4800 攻125×1.1 | 254 | 384.45 | 384 | +340 | 724 |
          | 玄武 4995 防120×1.1 | 253 | 373.175 | 373 | +240 | 613 |
          | 玄武 4995 体125 | 5 | 392.25 | 392 | +565 | 957 |

          中性项对照（种族100、天赋31）：
            ev 5/6/7 → 237，ev 8/9 → 238，等价 floor(ev/4) —— 即「4 点 = 1 点」

        ★ 为什么 253 和 255 有时一样、252 和 254 有时差 1：
          浮点除法给出 63.25 / 63.75，乘 1.1 后分别是 461.175 / 461.725，
          int() 都落 461 → 一样；
          而 63.0 / 63.5 乘 1.1 是 434.50 / 435.05，int() 分别 434 / 435 → 差 1。
        """
        # 2026-11-04（四号鱼）：游戏规则是单项学习力 0~255、个体 0~31。
        # 原来只夹了下限（max(0, ...)），所以 ev=9999 能算出攻 3139、talent=-5 能算出比 0 还低的数值。
        # LLM 传参不校验，这层必须自己守住。
        ev = min(255, max(0, int(ev)))
        talent = min(31, max(0, int(talent)))
        evq = ev / 4.0
        if is_hp:
            return int(base * 2 + evq + talent + 110)
        raw = (base * 2 + evq + talent + 5) * nature_mult
        return int(raw)

    # ────────────────────────────────────────────────────────────
    # 语义层：build_view / mint_view —— 配装与刻印推荐的唯一来源
    #
    # 原来这两个只在 seer-cli 里（cmd_build / cmd_mint），bot 侧没有对应工具，
    # 导致「配装」这类问题只能转 DSH。2026-11-03 下沉到这里，两边共用。
    # ────────────────────────────────────────────────────────────
    PRESET_MINTS = {
        # ★ 规则：同系列可装 2 个 → 应该把「该维度最高 + 次要属性最好」的那款装两个，
        #   再搭一个别的系列。旧版三个都不同系列，白白浪费了每系列可装 2 个的额度。
        #   实例（爆盾特攻向）：刑天·破岳(防65/特防65/体100)×2 + 黄金残影·和谐(60/60/105)
        #     = 防190 特防190 体305；旧版各一个只有 防185 特防185 体305，少 10 点双防。
        "爆攻": {
            "physical": ["黄金残影·希望", "黄金残影·希望", "修罗·寂灭"],
            "special":  ["黄金残影·反抗", "黄金残影·反抗", "赤洲龙子·嘲风"],
        },
        "爆体": {
            # 体力 135 档：创造(攻49) 比 赑屃(攻47) 优 → 创造×2 + 赑屃
            "physical": ["黄金残影·创造", "黄金残影·创造", "赤洲龙子·赑屃"],
            # 自由(特攻49) 比 狻猊(特攻47) 优 → 自由×2 + 狻猊
            "special":  ["黄金残影·自由", "黄金残影·自由", "赤洲龙子·狻猊"],
        },
        # 爆盾 ＝ 双防拉满。刑天是双防最高档(65/65)，比赤洲龙子/黄金残影的 60/60 高
        "爆盾": {
            "physical": ["刑天·威临", "刑天·威临", "黄金残影·信仰"],
            "special":  ["刑天·破岳", "刑天·破岳", "黄金残影·和谐"],
        },
        "高速": {
            # 速度 45 档，睚眦/囚牛 次要属性更好
            "physical": ["赤洲龙子·囚牛", "赤洲龙子·囚牛", "炎龙·火舞"],
            "special":  ["赤洲龙子·睚眦", "赤洲龙子·睚眦", "炎龙·炽焰"],
        },
        "双刃": {
            "dual":     ["圣战之无限α", "圣战之无限α", "穹明拾光"],
        },
    }

    EV_BY_PRESET = {
        "爆攻": {
            "physical": {"atk": 252, "hp": 252, "spd": 4},
            "special":  {"sp_atk": 252, "hp": 252, "spd": 4},
        },
        "爆体": {
            "physical": {"hp": 252, "atk": 252, "spd": 4},
            "special":  {"hp": 252, "sp_atk": 252, "spd": 4},
        },
        # 爆盾 ＝ 双防拉满（通用均衡配装）
        "爆盾": {
            "physical": {"def": 252, "sp_def": 252, "hp": 4},
            "special":  {"def": 252, "sp_def": 252, "hp": 4},
        },
        "高速": {
            "physical": {"spd": 252, "atk": 252, "hp": 4},
            "special":  {"spd": 252, "sp_atk": 252, "hp": 4},
        },
        "双刃": {
            "*": {"atk": 252, "sp_atk": 252, "spd": 4},
        },
    }

    # ★ 群友说法 → 预设别名（2026-11-08 加）
    #   预设只有 5 个词，但群里说「爆速」「速攻」「极速」的非常多。
    #   不映射的后果（唐三那个 bug）：build_view 报「预设没有爆速」→
    #   bot 重试默认「爆攻」→ 先出一张爆攻配置图，再重试出高速的，连出两张。
    PRESET_ALIAS = {
        "爆速": "高速", "速攻": "高速", "极速": "高速", "高速流": "高速",
        "抢速": "高速", "速度": "高速", "满速": "高速",
        "攻击": "爆攻", "物攻": "爆攻", "特攻": "爆攻", "输出": "爆攻",
        "爆输出": "爆攻", "攻": "爆攻",
        "体力": "爆体", "血量": "爆体", "耐久": "爆体", "肉": "爆体", "满体": "爆体",
        "防御": "爆盾", "双防": "爆盾", "肉盾": "爆盾", "盾": "爆盾", "抗": "爆盾",
        "双攻": "双刃", "均衡": "双刃", "两开花": "双刃",
    }

    # ★ 每个预设的「主属性」——**只用来兜底**，真正该拿 254 的是
    #   「性格加成的那一项」（见 NATURE_UP）。
    #   教训（2026-11-08 用户点出）：预设名 ≠ 加成项。
    #   「爆体」预设的性格是固执(+攻)，体力根本不吃性格修正 ——
    #   实测 hp 252→572、hp 254→572（没变），而 atk 252→472、atk 254→473（+1）。
    #   所以爆体该把 254 给攻击，不是给体力。
    PRESET_MAIN = {
        "高速": ("spd",),
        "爆攻": ("atk", "sp_atk"),
        "爆体": ("hp",),
        "爆盾": ("def", "sp_def"),
        "双刃": ("atk", "sp_atk"),
    }

    # ★ 性格 → 被加成的属性（只有这一项吃 254 的小数收益）
    #   面板公式保留小数：254/4 = 63.5，乘 1.1 后比 63.0 多约 0.55 → 四舍五入 +1。
    #   没被性格加成的项：252/4 = 63 就已经是整数满级，254 纯浪费 2 点。
    NATURE_UP = {
        "固执": "atk", "开朗": "spd", "保守": "sp_atk", "胆小": "spd",
        "顽皮": "def", "大胆": "def", "慎重": "sp_def", "沉着": "sp_def",
        "坦率": "", "害羞": "", "认真": "", "浮躁": "",
    }

    # ★ 学习力极限表 —— 来源：鱼片 2026-11-01 提供的「获得 1.1 性格加成 学习力分配」表
    #
    # ★ 2026-11-08 内联（原来从 tools/ev_table.py 导入）：
    #   原代码是 `sys.path.insert(0, r"G:\dsh workspace\tools")` + `import ev_table`，
    #   用的是**硬编码绝对路径**。工作区一挪、或插件在实例目录里独立跑，
    #   import 就失败 → 然后**静默退回 252**，正好是最初那个 bug 的成因。
    #   表只有 10 行，内联后插件自包含，这个坑从根上没了。
    #
    # 结构：种族末位 → (极限值, [(推荐投入, 剩余可分配), ...])
    #   极限值 = 该末位下某项的学习力有效上限（加成 70 档）
    #   推荐投入 = 保证「剩余学习力能被 4 整除」的 0 浪费值
    EV_LIMIT_TABLE = {
        0: (253, [(238, 272), (198, 312), (158, 352), (118, 392)]),
        5: (253, [(238, 272), (198, 312), (158, 352), (118, 392)]),
        1: (252, [(230, 280), (190, 320), (150, 360), (110, 400)]),
        6: (252, [(230, 280), (190, 320), (150, 360), (110, 400)]),
        2: (255, [(222, 288), (182, 328), (142, 368), (102, 408)]),
        7: (255, [(222, 288), (182, 328), (142, 368), (102, 408)]),
        3: (254, [(254, 256), (214, 296), (174, 336), (134, 376)]),
        8: (254, [(254, 256), (214, 296), (174, 336), (134, 376)]),
        4: (254, [(246, 264), (206, 304), (166, 344), (126, 384)]),
        9: (254, [(246, 264), (206, 304), (166, 344), (126, 384)]),
    }

    def ev_limit(self, base_stat: int) -> int:
        """按种族值末位查该项的学习力有效上限（极限值）；查不到返回 0。"""
        try:
            row = self.EV_LIMIT_TABLE.get(int(base_stat or 0) % 10)
        except Exception:
            return 0
        return int(row[0]) if row else 0

    # ★ 学习力「加成档」详细表 —— 来源：鱼片 2026-11-01 提供的第二张表（70 行 × 5 组）
    #
    # ★ 2026-11-08 内联（和 EV_LIMIT_TABLE 同样理由）：原来只在 tools/ev_table2.py，
    #   没有任何代码引用它 —— 等于「存着但用不上」，遇到「我要 +50 加成该投多少」
    #   这类问题只能重新推。表只有 70 行，内联成类属性，插件自包含。
    #
    # 结构：每条 = (加成档, {末位组: (所需学习力, 总和)}, 无性格加成时的学习力)
    #   组键 "1,6" 表示种族末位是 1 或 6；依此类推。
    #   「总和」= 该项 种族×2 + 天赋31 + 学习力/4 的整数值（表里给的参考口径）
    EV_BONUS_GROUPS = "1,6|2,7|3,8|4,9|5,0".split("|")
    EV_BONUS_ROWS = [
        (1, {"1,6": (1, 128), "2,7": (4, 127), "3,8": (3, 127), "4,9": (3, 127), "5,0": (2, 128)}, 4),
        (2, {"1,6": (5, 128), "2,7": (8, 127), "3,8": (7, 127), "4,9": (6, 128), "5,0": (6, 128)}, 8),
        (3, {"1,6": (8, 128), "2,7": (11, 127), "3,8": (11, 127), "4,9": (10, 128), "5,0": (9, 128)}, 12),
        (4, {"1,6": (12, 128), "2,7": (15, 127), "3,8": (14, 128), "4,9": (14, 128), "5,0": (13, 128)}, 16),
        (5, {"1,6": (16, 128), "2,7": (19, 127), "3,8": (18, 128), "4,9": (17, 128), "5,0": (16, 128)}, 20),
        (6, {"1,6": (19, 128), "2,7": (22, 128), "3,8": (22, 128), "4,9": (21, 128), "5,0": (20, 128)}, 24),
        (7, {"1,6": (23, 128), "2,7": (26, 128), "3,8": (25, 128), "4,9": (24, 128), "5,0": (24, 128)}, 28),
        (8, {"1,6": (27, 128), "2,7": (30, 128), "3,8": (29, 128), "4,9": (28, 128), "5,0": (27, 128)}, 32),
        (9, {"1,6": (30, 129), "2,7": (33, 128), "3,8": (32, 128), "4,9": (32, 128), "5,0": (31, 128)}, 36),
        (10, {"1,6": (34, 129), "2,7": (37, 128), "3,8": (36, 128), "4,9": (35, 128), "5,0": (35, 128)}, 40),
        (11, {"1,6": (38, 129), "2,7": (40, 128), "3,8": (40, 128), "4,9": (39, 128), "5,0": (38, 129)}, 44),
        (12, {"1,6": (41, 129), "2,7": (44, 128), "3,8": (43, 128), "4,9": (43, 128), "5,0": (42, 129)}, 48),
        (13, {"1,6": (45, 129), "2,7": (48, 128), "3,8": (47, 129), "4,9": (46, 129), "5,0": (46, 129)}, 52),
        (14, {"1,6": (48, 129), "2,7": (51, 128), "3,8": (51, 128), "4,9": (50, 129), "5,0": (49, 129)}, 56),
        (15, {"1,6": (52, 129), "2,7": (55, 128), "3,8": (54, 129), "4,9": (54, 129), "5,0": (53, 129)}, 60),
        (16, {"1,6": (56, 129), "2,7": (59, 128), "3,8": (58, 129), "4,9": (57, 129), "5,0": (56, 129)}, 64),
        (17, {"1,6": (59, 129), "2,7": (62, 129), "3,8": (62, 129), "4,9": (61, 129), "5,0": (60, 129)}, 68),
        (18, {"1,6": (63, 129), "2,7": (66, 129), "3,8": (65, 129), "4,9": (64, 129), "5,0": (64, 129)}, 72),
        (19, {"1,6": (67, 129), "2,7": (70, 129), "3,8": (69, 129), "4,9": (68, 129), "5,0": (67, 129)}, 76),
        (20, {"1,6": (70, 130), "2,7": (73, 129), "3,8": (72, 129), "4,9": (72, 129), "5,0": (71, 129)}, 80),
        (21, {"1,6": (74, 130), "2,7": (77, 129), "3,8": (76, 129), "4,9": (75, 129), "5,0": (75, 129)}, 84),
        (22, {"1,6": (78, 130), "2,7": (80, 129), "3,8": (80, 129), "4,9": (79, 129), "5,0": (78, 130)}, 88),
        (23, {"1,6": (81, 130), "2,7": (84, 129), "3,8": (83, 129), "4,9": (83, 129), "5,0": (82, 129)}, 92),
        (24, {"1,6": (85, 130), "2,7": (88, 129), "3,8": (87, 129), "4,9": (86, 130), "5,0": (86, 130)}, 96),
        (25, {"1,6": (88, 130), "2,7": (91, 129), "3,8": (91, 129), "4,9": (90, 130), "5,0": (89, 130)}, 100),
        (26, {"1,6": (92, 130), "2,7": (95, 129), "3,8": (94, 130), "4,9": (94, 130), "5,0": (93, 130)}, 104),
        (27, {"1,6": (96, 130), "2,7": (99, 129), "3,8": (98, 130), "4,9": (97, 130), "5,0": (96, 130)}, 108),
        (28, {"1,6": (99, 130), "2,7": (102, 130), "3,8": (102, 130), "4,9": (101, 130), "5,0": (100, 130)}, 112),
        (29, {"1,6": (103, 130), "2,7": (106, 130), "3,8": (105, 130), "4,9": (104, 130), "5,0": (104, 130)}, 116),
        (30, {"1,6": (107, 130), "2,7": (110, 130), "3,8": (109, 130), "4,9": (108, 130), "5,0": (107, 130)}, 120),
        (31, {"1,6": (110, 131), "2,7": (113, 130), "3,8": (112, 130), "4,9": (112, 130), "5,0": (111, 130)}, 124),
        (32, {"1,6": (114, 131), "2,7": (117, 130), "3,8": (116, 130), "4,9": (115, 130), "5,0": (115, 130)}, 128),
        (33, {"1,6": (118, 131), "2,7": (120, 130), "3,8": (120, 130), "4,9": (119, 130), "5,0": (118, 131)}, 132),
        (34, {"1,6": (121, 131), "2,7": (124, 130), "3,8": (123, 130), "4,9": (123, 130), "5,0": (122, 130)}, 136),
        (35, {"1,6": (125, 131), "2,7": (128, 130), "3,8": (127, 130), "4,9": (126, 131), "5,0": (126, 131)}, 140),
        (36, {"1,6": (128, 131), "2,7": (131, 130), "3,8": (131, 130), "4,9": (130, 131), "5,0": (129, 131)}, 144),
        (37, {"1,6": (132, 131), "2,7": (135, 130), "3,8": (134, 131), "4,9": (134, 131), "5,0": (133, 131)}, 148),
        (38, {"1,6": (136, 131), "2,7": (139, 130), "3,8": (138, 131), "4,9": (137, 131), "5,0": (136, 131)}, 152),
        (39, {"1,6": (139, 131), "2,7": (142, 131), "3,8": (142, 131), "4,9": (141, 131), "5,0": (140, 131)}, 156),
        (40, {"1,6": (143, 131), "2,7": (146, 131), "3,8": (145, 131), "4,9": (144, 131), "5,0": (144, 131)}, 160),
        (41, {"1,6": (147, 131), "2,7": (150, 131), "3,8": (149, 131), "4,9": (148, 131), "5,0": (147, 131)}, 164),
        (42, {"1,6": (150, 132), "2,7": (153, 131), "3,8": (152, 131), "4,9": (152, 131), "5,0": (151, 131)}, 168),
        (43, {"1,6": (154, 132), "2,7": (157, 131), "3,8": (156, 131), "4,9": (155, 131), "5,0": (155, 131)}, 172),
        (44, {"1,6": (158, 132), "2,7": (160, 131), "3,8": (160, 131), "4,9": (159, 131), "5,0": (158, 132)}, 176),
        (45, {"1,6": (161, 132), "2,7": (164, 131), "3,8": (163, 131), "4,9": (163, 131), "5,0": (162, 132)}, 180),
        (46, {"1,6": (165, 132), "2,7": (168, 131), "3,8": (167, 131), "4,9": (166, 132), "5,0": (166, 132)}, 184),
        (47, {"1,6": (168, 132), "2,7": (171, 131), "3,8": (171, 131), "4,9": (170, 132), "5,0": (169, 132)}, 188),
        (48, {"1,6": (172, 132), "2,7": (175, 131), "3,8": (174, 132), "4,9": (174, 132), "5,0": (173, 132)}, 192),
        (49, {"1,6": (176, 132), "2,7": (179, 131), "3,8": (178, 132), "4,9": (177, 132), "5,0": (176, 132)}, 196),
        (50, {"1,6": (179, 132), "2,7": (182, 132), "3,8": (182, 132), "4,9": (181, 132), "5,0": (180, 132)}, 200),
        (51, {"1,6": (183, 132), "2,7": (186, 132), "3,8": (185, 132), "4,9": (184, 132), "5,0": (184, 132)}, 204),
        (52, {"1,6": (187, 132), "2,7": (190, 132), "3,8": (189, 132), "4,9": (188, 132), "5,0": (187, 132)}, 208),
        (53, {"1,6": (190, 133), "2,7": (193, 132), "3,8": (192, 132), "4,9": (192, 132), "5,0": (191, 132)}, 212),
        (54, {"1,6": (194, 133), "2,7": (197, 132), "3,8": (196, 132), "4,9": (195, 132), "5,0": (195, 132)}, 216),
        (55, {"1,6": (198, 133), "2,7": (200, 132), "3,8": (200, 132), "4,9": (199, 132), "5,0": (198, 133)}, 220),
        (56, {"1,6": (201, 133), "2,7": (204, 132), "3,8": (203, 132), "4,9": (203, 132), "5,0": (202, 133)}, 224),
        (57, {"1,6": (205, 133), "2,7": (208, 132), "3,8": (207, 132), "4,9": (206, 133), "5,0": (206, 133)}, 228),
        (58, {"1,6": (208, 133), "2,7": (211, 132), "3,8": (211, 132), "4,9": (210, 133), "5,0": (209, 133)}, 232),
        (59, {"1,6": (212, 133), "2,7": (215, 132), "3,8": (214, 133), "4,9": (214, 133), "5,0": (213, 133)}, 236),
        (60, {"1,6": (216, 133), "2,7": (219, 132), "3,8": (218, 133), "4,9": (217, 133), "5,0": (216, 133)}, 240),
        (61, {"1,6": (219, 133), "2,7": (222, 133), "3,8": (222, 133), "4,9": (221, 133), "5,0": (220, 133)}, 244),
        (62, {"1,6": (223, 133), "2,7": (226, 133), "3,8": (225, 133), "4,9": (224, 133), "5,0": (224, 133)}, 248),
        (63, {"1,6": (227, 133), "2,7": (230, 133), "3,8": (229, 133), "4,9": (228, 133), "5,0": (227, 133)}, 252),
        (64, {"1,6": (230, 134), "2,7": (233, 133), "3,8": (232, 133), "4,9": (232, 133), "5,0": (231, 133)}, 256),
        (65, {"1,6": (234, 134), "2,7": (237, 133), "3,8": (236, 133), "4,9": (235, 133), "5,0": (235, 133)}, 260),
        (66, {"1,6": (238, 134), "2,7": (240, 133), "3,8": (240, 133), "4,9": (239, 133), "5,0": (238, 134)}, 264),
        (67, {"1,6": (241, 134), "2,7": (244, 133), "3,8": (243, 133), "4,9": (243, 133), "5,0": (242, 134)}, 268),
        (68, {"1,6": (245, 134), "2,7": (248, 133), "3,8": (247, 133), "4,9": (246, 134), "5,0": (246, 134)}, 272),
        (69, {"1,6": (248, 134), "2,7": (251, 133), "3,8": (251, 133), "4,9": (250, 134), "5,0": (249, 134)}, 276),
        (70, {"1,6": (252, 134), "2,7": (255, 133), "3,8": (254, 134), "4,9": (254, 134), "5,0": (253, 134)}, 280),
    ]

    def _ev_group_key(self, base_stat: int) -> str:
        """种族值末位 → 组键（"1,6" 这种）"""
        d = int(base_stat or 0) % 10
        inv = {1: "1,6", 6: "1,6", 2: "2,7", 7: "2,7", 3: "3,8",
               8: "3,8", 4: "4,9", 9: "4,9", 5: "5,0", 0: "5,0"}
        return inv.get(d, "")

    def ev_for_bonus(self, base_stat: int, bonus: int) -> tuple[int, int] | None:
        """「我要 +N 档加成」→ 需要投多少学习力。返回 (学习力, 总和) 或 None。"""
        key = self._ev_group_key(base_stat)
        if not key:
            return None
        for b, row, _plain in self.EV_BONUS_ROWS:
            if b == int(bonus):
                return row.get(key)
        return None

    def ev_bonus_of(self, base_stat: int, ev: int) -> int | None:
        """「我投了 N 点」→ 对应最高哪一档加成。"""
        key = self._ev_group_key(base_stat)
        if not key:
            return None
        best = None
        for b, row, _plain in self.EV_BONUS_ROWS:
            if row[key][0] <= int(ev or 0):
                best = b
        return best

    def ev_max_bonus(self, base_stat: int) -> tuple[int, int, int] | None:
        """单项 255 上限内最多吃到哪档加成。返回 (加成, 学习力, 总和)。"""
        key = self._ev_group_key(base_stat)
        if not key:
            return None
        best = None
        for b, row, _plain in self.EV_BONUS_ROWS:
            ev_needed, tot = row[key]
            if ev_needed <= 255:
                best = (b, ev_needed, tot)
        return best



    EV_MAX_AGAINST = {
        "phys": {"def": 252, "hp": 252, "spd": 4},      # 极限抗物理
        "spec": {"sp_def": 252, "hp": 252, "spd": 4},   # 极限抗特殊
    }

    NATURE_BY_STYLE = {
        # 物攻向：爆攻/爆体用固执；高速开朗；爆盾用顽皮(+防-特攻)、特盾用慎重(+特防-特攻)
        "physical": {"爆攻": "固执", "爆体": "固执", "高速": "开朗", "爆盾": "顽皮"},
        # 特攻向：爆攻/爆体用保守；高速胆小；爆盾用大胆(+防-攻)、特盾用沉着(+特防-攻)
        #   ★ 爆盾/特盾要「加防御、减掉没用的那一攻」，不能用保守（保守对防御零贡献）
        "special":  {"爆攻": "保守", "爆体": "保守", "高速": "胆小", "爆盾": "大胆"},
        # 双攻向：不动任何一项攻击，用平衡性格
        "dual":     {"爆攻": "坦率", "爆体": "坦率", "高速": "坦率", "爆盾": "坦率"},
    }

    _VS_CN = {"phys": "物理", "spec": "特殊"}

    def _resolve_ev_spec(
        self,
        petname: str,
        spec: str,
        *,
        nature: str = "",
        talent: int = 31,
        mints: list[str] | None = None,
        suit: str = "",
        title: str = "",
        eye: str = "",
        team_skip: str = "",
        budget: int = 510,
        hpup: int = 0,
    ) -> tuple[dict[str, int], str]:
        """把 pet_build 的 ev 写法解析成实际学习力。

        ★ 2026-11-04 新增。支持四种（逗号分隔，键支持中文简称）：
            atk=254    普通学习力（<=255 原样采用）
            atk=max    该项拉满（254）
            hp=1040    >255 视为「目标面板值」，反解出所需学习力
            rest=spd   把剩余学习力全给这一项

        为什么需要：南霜的 ev 写的是 `atk=max,sp_def=430,hp=1040,rest=spd`，
        那是**目标面板**（特防要 430、体力要 1040），不是学习力。
        旧解析把这些 >255 的数当非法丢掉，整只精灵按 0 学习力出面板。

        反解依据：每一项只取决于自己的学习力，
        面板 = _stat_value(base, talent, ev, mult, is_hp) + 刻印 + 套装/称号/目镜 + 战队年费
        所以对每个目标值单独二分/线性找最小 ev 即可，互不影响。

        返回 (ev_plan, 说明文字)。
        """
        items: list[tuple[str, str]] = []
        rest_key = ""
        for piece in str(spec or "").replace("，", ",").split(","):
            piece = piece.strip()
            if not piece or "=" not in piece:
                continue
            k, _, v = piece.partition("=")
            k = k.strip()
            v = v.strip().lower()
            if k == "rest":
                rest_key = self._CN2KEY.get(v, v)
                continue
            key = self._CN2KEY.get(k, k)
            if key in _STAT_KEYS:
                items.append((key, v))
        if not items and not rest_key:
            return {}, ""

        # 探针：ev=0 时拿到各项常量（刻印/套装/称号/目镜/战队/年费）与性格系数
        const: dict[str, dict] = {}
        try:
            probe = self.stat_view(
                petname, talent=talent, nature=nature, ev="0",
                mints=list(mints or []), suit=suit, title=title, eye=eye,
                team_skip=team_skip,
            )
            for r in probe.get("rows") or []:
                const[r["key"]] = r
        except Exception:
            const = {}

        plan: dict[str, int] = {}
        notes: list[str] = []
        for key, raw in items:
            cn = self._STAT_CN.get(key, key)
            if raw == "max":
                plan[key] = 254
                notes.append(f"{cn}拉满(254)")
                continue
            try:
                num = int(raw)
            except (TypeError, ValueError):
                continue
            if 0 <= num <= 255:
                plan[key] = num
                continue
            # >255 → 目标面板值，反解
            row = const.get(key)
            if not row:
                continue
            base = int(row["base"])
            mint = int(row.get("mint") or 0)
            ovl = int(row.get("overlay") or 0)
            flat = int(row.get("flat") or 0)
            mult = float(row.get("mult") or 1.0)
            is_hp = key == "hp"
            # ★ 体力目标含「体力上限药剂」加成（鱼片 2026-11-04：
            #   「加完体力上限之后是 1040 体」）。10 点上限是面板外的直接加法，
            #   反解时要先从目标里扣掉，否则会多要 20/4=5 点学习力档位差。
            target = num - (hpup if is_hp else 0)
            hit = None
            for ev in range(0, 253):
                val = self._stat_value(base, talent, ev, mult, is_hp)
                if val + mint + ovl + flat >= target:
                    hit = ev
                    break
            plan[key] = 252 if hit is None else hit
            notes.append(f"{cn}{num}→学习力{plan[key]}")

        if rest_key and rest_key in _STAT_KEYS:
            used = sum(plan.values())
            left = max(0, min(255, budget - used))
            plan[rest_key] = left
            notes.append(f"剩余{left}给{self._STAT_CN.get(rest_key, rest_key)}")

        return plan, "；".join(notes)

    def _preset_ev(self, pid: int, preset: str, style: str,
                   nature: str = "") -> dict[str, int]:
        """按预设生成学习力分配：**性格加成的那一项**吃 ev_table 极限值。

        ★ 2026-11-08 两轮修正（用户连点两次）：

          ① 第一轮：预设原先硬编码 252，没用 tools/ev_table.py 的极限值 ——
             唐三（伏尔格安 速138/攻154，末位 8/4）给 252+252+4=508，浪费 2 点。

          ② 第二轮：**能拿 254 的只有「被性格加成」的那一项**。
             面板公式保留小数（254/4=63.5），乘 1.1 后四舍五入多 1 点；
             而没被加成的项 252/4=63 本身就是整数满级，254 只是白费 2 点。
             实测（伏尔格安）：
               高速·开朗(+速)：atk 252→432 / atk 254→432（不变）｜ spd 252→432 / spd 254→433（+1）
               爆攻·固执(+攻)：atk 252→472 / atk 254→473（+1）｜ hp 252→572 / hp 254→572（不变）
             所以「爆体」预设（性格固执·加攻）该拿 254 的是**攻击**，不是体力。

        返回 {"spd": 254, "atk": 252, "hp": 4} 这种，合计尽量吃到 510。
        """
        plan = dict(self.EV_BY_PRESET.get(preset, {}).get(style)
                    or self.EV_BY_PRESET.get(preset, {}).get("*") or {})
        if not plan:
            return plan
        # 先定「谁吃极限值」：用户显式给的性格 > 预设推出来的性格
        #   NATURE_BY_STYLE 是 {"physical": {"爆攻": "固执", ...}} —— style 在外层！
        _nat = str(nature or "").strip() or (
            (self.NATURE_BY_STYLE.get(style) or {}).get(preset) or "")
        _up = self.NATURE_UP.get(_nat, "")
        _target = _up if (_up and _up in plan and plan[_up] >= 252) else None
        if _target is None:
            # 性格无加成（坦率这类）或加成项没拉满 → 退回预设名义主属性
            for _k in (self.PRESET_MAIN.get(preset) or ()):
                if _k in plan and plan[_k] >= 252:
                    _target = _k
                    break
        if _target is None and _up == "":
            return plan          # 坦率/无加成：全 252，别乱加
        st = self.pet_stats(pid) or {}
        CAP = 510
        if _target:
            _k = _target
            _base = int(st.get(_k) or 0)
            if not _base:               # 种族 0（无为觉者这类）：靠小数位凑面板
                plan[_k] = 254
            else:
                # 直接查内联表（2026-11-08：不再 import tools/ev_table.py）
                _lim = self.ev_limit(_base)
                if _lim:
                    if _base % 10 in (0, 5):
                        _others = sum(v for k2, v in plan.items() if k2 != _k)
                        if _others + 254 <= CAP:
                            _lim = max(int(_lim), 254)
                    plan[_k] = int(_lim)
        # 超预算时先压非目标项，还不够就把目标项降回预算内（按 4 的整数倍向下取）
        if sum(plan.values()) > CAP:
            for _k2 in plan:
                if _k2 != _target and plan[_k2] > 252:
                    plan[_k2] = 252
        _overflow = sum(plan.values()) - CAP
        if _overflow > 0 and _target:
            # 例：爆盾 防御种族 112（末位 2）极限 255，255+252+4=511
            #     → 目标项降到 510-252-4=254（254 仍比 252 多 0.5 级）
            _others = sum(v for k2, v in plan.items() if k2 != _target)
            _allow = CAP - _others
            if _allow < plan[_target]:
                plan[_target] = max(252, (_allow // 2) * 2)
        if sum(plan.values()) > CAP:      # 极端兜底：目标项也压到 252
            plan[_target or list(plan)[0]] = 252
        # 富余补给「没拉满」的那项，按 4 的整数倍
        _used = sum(plan.values())
        if _used < CAP:
            _minor = [k for k in plan if plan[k] < 252]
            if _minor:
                _add = ((CAP - _used) // 4) * 4
                if _add:
                    plan[_minor[0]] = plan.get(_minor[0], 0) + _add
        return plan

    def build_view(
        self,
        name: str,
        preset: str = "爆攻",
        *,
        vs: str = "",
        talent: int = 31,
        nature: str = "",
        ev: str = "",
        suit: str = "",
        title: str = "",
        eye: str = "",
    ) -> dict[str, Any]:
        """一键配装：按预设选三刻印 + 算最终面板。

        vs 两种："" 常规 / "phys" 极限抗物理 / "spec" 极限抗特殊
        判定字段：empty_hint / ev_note / dir_cn
        """
        view: dict = {
            "found": False, "name": name, "pid": None, "preset": preset,
            "style": "", "dir_cn": "", "mints": [], "nature": "",
            "ev_plan": {}, "ev_note": "", "stat": None,
            "empty_hint": None, "vs": vs,
        }
        # ★ 先过一遍别名（爆速→高速 / 物攻→爆攻 …），空值回落默认爆攻
        _p = str(preset or "").strip()
        _p = self.PRESET_ALIAS.get(_p, _p) or "爆攻"
        if _p not in self.PRESET_MINTS:
            view["empty_hint"] = (
                f"预设没有「{preset}」。可选：{' / '.join(self.PRESET_MINTS)}"
            )
            return view
        preset = _p
        view["preset"] = preset
        hits = self.find_pets(name) if hasattr(self, "find_pets") else []
        if not hits:
            view["empty_hint"] = f"找不到精灵：{name}"
            return view
        pid, petname = hits[0]
        st = self.pet_stats(pid) or {}
        atk = int(st.get("atk") or 0)
        spa = int(st.get("sp_atk") or 0)

        # ★ 优先用精灵专属配装（pet_build 表）
        #   通用 PRESET_MINTS 只按物攻/特攻分两套，对无极（二角极差）、
        #   无为（三310）、玄武（双防向）全给错，必须让专属表覆盖。
        _own = self.pet_build(pid) if hasattr(self, "pet_build") else {}
        if _own.get("mints"):
            view["found"] = True
            view["pid"] = pid
            view["pet_name"] = petname
            view["mints"] = list(_own["mints"])
            view["nature"] = _own.get("nature") or nature or "固执"
            view["preset"] = "专属"
            view["suit"] = _own.get("suit") or suit
            view["title"] = _own.get("title") or title
            view["eye"] = _own.get("eye") or eye
            view["team_skip"] = _own.get("team_skip") or ""
            view["soulmod"] = _own.get("soulmod") or ""
            _evs = str(_own.get("ev") or "").strip()
            # ★ 2026-11-04：pet_build 的 ev 支持「目标面板值」写法
            #   （南霜 atk=max,sp_def=430,hp=1040,rest=spd）。
            #   旧解析只认 0~255 的数字，430/1040 被当成非法直接丢掉，
            #   结果整只精灵按 0 学习力出面板（config_view 也是全 ev=0）。
            _plan, _plan_desc = self._resolve_ev_spec(
                petname, _evs,
                nature=view["nature"], talent=talent,
                mints=list(_own["mints"]),
                suit=view["suit"], title=view["title"], eye=view["eye"],
                team_skip=view["team_skip"],
                hpup=20,
            )
            if _plan:
                view["ev_plan"] = _plan
                _resolved = ",".join(f"{_k}={_v}" for _k, _v in _plan.items())
                view["ev_raw"] = _resolved       # config_view 优先用它
                view["ev_note"] = (
                    f"学习力（{sum(_plan.values())} 点）："
                    + "  ".join(
                        f"{self._STAT_CN.get(_k, _k)}{_v}" for _k, _v in _plan.items()
                    )
                    + (f"　｜原始写法：{_evs}" if _evs else "")
                )
            else:
                view["ev_raw"] = _evs
                view["ev_note"] = _evs or "未指定学习力"
            view["ev_spec_text"] = _evs     # 保留原始写法，便于展示/排查
            view["style"] = "own"
            view["dir_cn"] = _own.get("note") or "专属配装"
            # ★ 2026-11-04 补齐：这条分支提前 return，漏了通用分支才设的
            #   atk_base / spa_base，导致 seer_build 解析 pet_build 命中
            #   的精灵时抛 KeyError: 'atk_base'（南霜/无为/无极/天堂龙兽/
            #   修罗/玄武 六只全中）。这两个字段是调用方必读的，必须两边都设。
            view["atk_base"] = atk
            view["spa_base"] = spa
            view["stat"] = self.stat_view(
                petname,
                talent=talent,
                nature=view["nature"],
                ev=view.get("ev_raw") or "0",   # 用反解后的学习力，不是原始写法
                mints=list(_own["mints"]),
                suit=view["suit"],
                title=view["title"],
                eye=view["eye"],
                team_skip=view["team_skip"],
                hpup=20,                        # 顶配默认含体力上限药剂 +20
            )
            return view

        variants = self.PRESET_MINTS[preset]
        if "dual" in variants:
            style = "dual"
            names = variants["dual"]
        else:
            if abs(atk - spa) <= 10 and min(atk, spa) > 0:
                style = "dual"
            else:
                style = "physical" if atk >= spa else "special"
            names = variants.get(style) or variants.get("physical") or variants.get("special")

        natures = self.NATURE_BY_STYLE.get(style, {})
        nat = nature or natures.get(preset, "坦率")

        vs_key = str(vs or "").strip().lower()
        if vs_key and vs_key not in self.EV_MAX_AGAINST:
            view["empty_hint"] = f"vs 只支持 phys / spec，收到：{vs}"
            return view

        ev_plan: dict = {}
        ev_note = ""
        if not ev:
            if vs_key:
                ev_plan = dict(self.EV_MAX_AGAINST[vs_key])
                if not nature:
                    if vs_key == "phys":
                        nat = "顽皮" if style == "physical" else "大胆"
                    else:
                        nat = "慎重" if style == "physical" else "沉着"
                ev_note = (
                    f"对线特化（极限抗{self._VS_CN[vs_key]}）："
                    + "  ".join(f"{self._STAT_CN.get(kk, kk)}{vv}" for kk, vv in ev_plan.items())
                )
            else:
                # ★ 2026-11-08：主属性不再固定 252，改查 tools/ev_table.py 的极限值。
                #   原来硬编码 252 的后果：唐三（伏尔格安 速138→末位8、攻154→末位4，
                #   两个极限都是 254）高速预设给 252+252+4=508，**浪费 2 点学习力**；
                #   正确是 254+252+4=510 正好用满，且速度面板不变（254//4 == 252//4）。
                ev_plan = self._preset_ev(pid, preset, style, nature=nature)
                if ev_plan:
                    ev_note = f"学习力自动分配（{sum(ev_plan.values())} 点）：" + "  ".join(
                        f"{self._STAT_CN.get(kk, kk)}{vv}" for kk, vv in ev_plan.items())
            ev = ",".join(f"{kk}={vv}" for kk, vv in ev_plan.items()) if ev_plan else "0"

        view["found"] = True
        view["name"] = petname
        view["pid"] = pid
        view["style"] = style
        view["dir_cn"] = {"physical": "物攻向", "special": "特攻向", "dual": "双攻向"}[style]
        view["mints"] = list(names or [])
        view["nature"] = nat
        view["ev_plan"] = ev_plan
        view["ev_note"] = ev_note
        view["atk_base"] = atk
        view["spa_base"] = spa
        view["stat"] = self.stat_view(
            petname, talent=talent, nature=nat, ev=ev,
            mints=list(names or []), suit=suit, title=title, eye=eye,
        )
        return view

    def mint_view(self, name: str, preset: str = "", limit: int = 3) -> dict[str, Any]:
        """刻印推荐：按精灵种族自动判流派，给三刻印方案（同系列最多 2 个）。"""
        view: dict = {
            "found": False, "name": name, "pid": None,
            "style": "", "dir_cn": "", "mints": [], "note": "",
            "empty_hint": None,
        }
        hits = self.find_pets(name) if hasattr(self, "find_pets") else []
        if not hits:
            view["empty_hint"] = f"找不到精灵：{name}"
            return view
        pid, petname = hits[0]
        st = self.pet_stats(pid) or {}
        atk = int(st.get("atk") or 0)
        spa = int(st.get("sp_atk") or 0)
        # ★ 流派判定走 attack_orientation（2026-11-06 接上）
        #   旧逻辑只看种族差 ≤10 判双攻，和 attack_orientation 那套
        #   （技能表 + 双攻和机制）是两套并行逻辑，会打架：
        #     玄武 攻100/特攻95 差 5 → 旧逻辑判「双攻」，可技能表是
        #     物理9条(最高160) vs 特殊4条(最高130)，纯物攻精灵，
        #     特攻完全是废项 —— 正是 attack_orientation 注释里举的例子。
        #   修罗铠甲/天启星魂这种真双攻靠「双攻值之和」词条识别，
        #   种族相等只是巧合。
        _or = self.attack_orientation(pid) or {}
        style = str(_or.get("orient") or "")
        if style == "mixed":
            style = "dual"
        if style not in ("physical", "special", "dual"):
            # 兜底：技能表取不到（新精灵/无技能数据）才退回种族差
            style = "dual" if (abs(atk - spa) <= 10 and min(atk, spa) > 0) else (
                "physical" if atk >= spa else "special")
        dir_cn = {"physical": "物攻向", "special": "特攻向", "dual": "双攻向"}[style]
        want = preset or "爆攻"
        # ★ 双攻型默认走「双刃」预设（2026-11-06 补）
        #   "爆攻"/"爆体"/"爆盾"/"高速" 都只有 physical/special 两档，
        #   双攻型精灵会 fallback 到 physical，等于给它配了单攻印 ——
        #   修罗铠甲（技能词条「双攻值之和」）本该吃双刃印，却拿到物攻方案。
        if not preset and style == "dual":
            want = "双刃"
        variants = self.PRESET_MINTS.get(want, {})
        names = (variants.get(style) or variants.get("physical")
                 or variants.get("special") or variants.get("dual") or [])
        view.update({
            "found": True, "name": petname, "pid": pid,
            "style": style,
            "dir_cn": dir_cn,
            "mints": list(names)[:limit],
            "preset": want,
            "note": (
                f"种族 攻{atk}/特攻{spa} → {dir_cn}；"
                f"预设「{want}」；同系列最多装 2 个，所以「该维度最高×2 + 另一系列一个」"
            ),
        })
        return view

    # ────────────────────────────────────────────────────────────
    # 语义层：config_view —— 配置图（图鉴卡片）需要的数据
    #
    # 布局对照手册 app 的「配置计算器」导出图：
    #   左栏：头像 / 额外数值(战队+额外) / 立绘 / 名字 / 套装 / 目镜 / 称号
    #   右栏：基础属性(等级-学习力-天赋-体力上限-性格-特性) / 战斗属性 / 装备刻印
    # 本方法只产出数据，排版交给 HTML 模板。
    # ────────────────────────────────────────────────────────────
    _TEAM_MAX = {"hp": 30, "sp_atk": 15, "atk": 15, "def": 15, "sp_def": 15, "spd": 10}
    # ★ 同时收全称和简称（2026-11-03 补）
    #   之前只认全称，写「攻」「速」「体」会**静默失效**（team_skip 完全不生效），
    #   排查时极难发现 —— 战队合计仍显示满值，只是那一项没被扣掉。
    _CN2KEY = {
        "攻击": "atk", "攻": "atk",
        "防御": "def", "防": "def",
        "特攻": "sp_atk",
        "特防": "sp_def",
        "速度": "spd", "速": "spd",
        "体力": "hp", "体": "hp",
    }

    @classmethod
    def _parse_bonus_text(cls, text: str) -> dict[str, int]:
        """从套装/称号这类自然语言 desc 里抽**面板加成**。

        ★ 不能贪心匹配。晨曦之星战甲原文：
          「背包内所有精灵速度、体力+60，…忽略对手10点速度；…攻击伤害提升10%…」
          贪心匹配会把「忽略对手10点速度」的 10 当成「攻击+10」，
          把「攻击伤害提升10%」也当成「攻击+10」—— 而真正的面板加成只有 速度/体力+60。

        做法：从「+数字」往前扫，收集紧邻的属性名（允许顿号/空格连接），
        扫到非属性名就停。这样「背包内所有精灵速度、体力+60」能正确拿到 速度+60 体力+60，
        而「忽略对手10点速度」因为数字后面是「点」，直接排除。
        """
        out: dict[str, int] = {}
        s = str(text or "")
        if not s:
            return out
        pairs = [("特攻", "sp_atk"), ("特防", "sp_def"), ("攻击", "atk"),
                 ("防御", "def"), ("速度", "spd"), ("体力", "hp")]
        # 数字后面不能是「点/%/回合/次/层/级」
        pat = re.compile(r"\+\s*(\d+)(?!\s*[%点回次层级别])")
        for m in pat.finditer(s):
            num = int(m.group(1))
            # 往前扫属性名
            head = s[:m.start()]
            consumed = 0
            found: list[str] = []
            while True:
                tail = head[:len(head) - consumed]
                hit = None
                for cn, key in pairs:
                    if tail.endswith(cn):
                        hit = (cn, key)
                        break
                if not hit:
                    # 允许跳过顿号/空格
                    tail2 = tail.rstrip("、 \u3000")
                    if tail2 != tail:
                        consumed = len(head) - len(tail2)
                        continue
                    break
                cn, key = hit
                if key not in found:
                    found.append(key)
                consumed += len(cn)
            if found:
                for key in found:
                    out[key] = num
        return out

    def lookup_suit(self, name: str) -> dict:
        """套装：suit → suit_bonus（desc 是文本，逐项数值靠正则解析）。"""
        rows = self._q(
            "select s.id, s.name, b.desc from suit s "
            "left join suit_bonus b on b.id=s.id "
            "where s.name = ? or s.name like ? limit 1", (name, "%" + name + "%")
        )
        if not rows:
            return {}
        rid, rname, desc = rows[0]
        desc = str(desc or "")
        return {"id": rid, "name": rname, "text": desc,
                "vals": self._parse_bonus_text(desc)}

    def lookup_title(self, name: str) -> dict:
        """称号：title_part → title_attr_bonus（六项数值）。"""
        rows = self._q(
            "select t.id, t.name, b.atk, b.def, b.sp_atk, b.sp_def, b.spd, b.hp "
            "from title_part t left join title_attr_bonus b on b.id = t.id "
            "where t.name = ? or t.name like ? order by t.id limit 1",
            (name, "%" + name + "%"),
        )
        if not rows:
            return {}
        r = rows[0]
        vals = {"atk": int(r[2] or 0), "def": int(r[3] or 0),
                "sp_atk": int(r[4] or 0), "sp_def": int(r[5] or 0),
                "spd": int(r[6] or 0), "hp": int(r[7] or 0)}
        return {"id": r[0], "name": r[1], "vals": vals, "total": sum(vals.values())}

    def lookup_eyepiece(self, name: str) -> dict:
        """目镜：equip → bonus_id → equip_bonus_attr（六项数值）。"""
        rows = self._q(
            "select e.id, e.name, b.atk, b.def, b.sp_atk, b.sp_def, b.spd, b.hp "
            "from equip e left join equip_bonus_attr b on b.id = e.bonus_id "
            "where (e.name = ? or e.name like ?) limit 1",
            (name, "%" + name + "%"),
        )
        if not rows:
            return {}
        r = rows[0]
        vals = {"atk": int(r[2] or 0), "def": int(r[3] or 0),
                "sp_atk": int(r[4] or 0), "sp_def": int(r[5] or 0),
                "spd": int(r[6] or 0), "hp": int(r[7] or 0)}
        return {"id": r[0], "name": r[1], "vals": vals, "total": sum(vals.values())}


    def detect_soulmod(self, name: str) -> dict:
        """从魂印原文自动识别派生类型，省得每次手填 soulmod 参数。

        已确认的三种（见 facts）：
          · 「消耗X%的体力上限并提升自身的其他能力值等量的数值」→ pct（天堂极龙兽）
          · 「自身所有能力值变为所有能力值之和的一半」        → average（无为觉者）
          · 「英雄之耀」+ 双攻差                              → atk_diff（无极圣武，层数需外部给）
        识别不出返回 {}。
        """
        out: dict = {}
        try:
            hits = self.find_pets(name)
            if not hits:
                return out
            pid = hits[0][0]
            rows = self._q(
                "select s.desc from petsoulmarklink l "
                "join soulmark s on s.id = l.soulmark_id where l.pet_id = ?", (pid,)
            )
            text = " ".join(str(r[0] or "") for r in rows)
            if not text:
                return out
            import re as _re
            # 总和均分
            if "所有能力值变为所有能力值之和的一半" in text or "能力值之和的一半" in text:
                out["average"] = True
                out["reason"] = "魂印「所有能力值变为总和的一半」"
                return out
            # 体力换属性
            m = _re.search(r"消耗\s*(\d+)\s*%\s*的?体力上限.{0,24}提升", text)
            if m:
                out["pct"] = int(m.group(1)) / 100.0
                out["reason"] = f"魂印「消耗{m.group(1)}%体力上限换属性」"
                return out
            # 双防转化型（玄武）
            m2 = _re.search(r"双防值(?:的)?\s*(\d+)\s*%\s*提升至", text)
            if m2:
                out["double_def_pct"] = int(m2.group(1)) / 100.0
                out["reason"] = f"魂印「双防值{m2.group(1)}%提升至速度外的能力值」"
                return out
            # 英雄之耀（层数需外部指定，这里只标记类型）
            if "英雄之耀" in text:
                out["atk_diff_flag"] = True
                out["reason"] = "魂印含「英雄之耀」（双攻差型，层数需确认）"
                return out
        except Exception:
            pass
        return out

    def attack_orientation(self, pet_id: int) -> dict:
        """判断精灵是物攻型、特攻型还是双攻型 —— **查技能表，不看种族值**。

        返回 {orient, phys_cnt, spec_cnt, phys_max, spec_max, why}

        ★ 为什么不能看种族值：玄武 攻100 特攻95 看着接近，
          但技能表是 物理10条(最高160/150×4) vs 特殊4条(最高130) —— 纯物攻精灵，
          特攻完全是废项。按种族差 5 点根本看不出这个。
        """
        try:
            rows = self._q(
                "select s.power, sc.name from skillinpetorm l "
                "join skill s on s.id = l.skill_id "
                "left join skill_category sc on sc.id = s.category_id "
                "where l.pet_id = ? and s.power > 0", (pet_id,)
            )
        except Exception:
            return {}
        phys = [int(r[0] or 0) for r in rows if r[1] == "物理攻击"]
        spec = [int(r[0] or 0) for r in rows if r[1] == "特殊攻击"]
        if not phys and not spec:
            return {}
        pmax = max(phys) if phys else 0
        smax = max(spec) if spec else 0
        # ★ 判据（2026-11-03 校正）：
        #   物理最高威力 >= 特殊最高威力 AND 物理条数 >= 特殊条数 → 物攻型
        #   反过来 → 特攻型；两边完全相等 → 双攻型
        #   之前用「加权分 ×1.5」太严，玄武（物理9条160 vs 特殊4条130）会被判 mixed。
        # ★ 先查「双攻和」机制（2026-11-03 修罗铠甲实例）
        #   修罗铠甲三个攻击技能都带：
        #     「将自身攻击、特攻值之和作为攻击、特攻值进行伤害计算」
        #   → 它的伤害吃「双攻和」，双刃刻印才是对的。
        #   但它物理技能 2 条(160) vs 特殊 1 条(150)，只按条数会判成物攻精灵。
        dual_mech = ""
        try:
            # ① 技能词条求和（修罗铠甲）
            hints = self._q(
                "select u.info from skillinpetorm l "
                "join skilleffectlink e on e.skill_id = l.skill_id "
                "join skill_effect_in_use u on u.id = e.effect_in_use_id "
                "where l.pet_id = ? and u.info like ?", (pet_id, "%攻击、特攻值之和%")
            )
            if hints:
                dual_mech = "skill"
            else:
                # ② 魂印求和（星辰万象·天启星魂 #4677）
                sm = self._q(
                    "select s.desc from petsoulmarklink l "
                    "join soulmark s on s.id = l.soulmark_id "
                    "where l.pet_id = ? and s.desc like ?",
                    (pet_id, "%双攻值视为二者之和%")
                )
                if sm:
                    dual_mech = "soul"
                else:
                    # ③ 魂印拉平（天启星魂 #3661）
                    #   「攻击和特攻等于其中较高的一项」→ 给单攻印时另一边跟着涨
                    #   facts：天启星魂 #3661 应走单特攻印，不是双刃
                    sm2 = self._q(
                        "select s.desc from petsoulmarklink l "
                        "join soulmark s on s.id = l.soulmark_id "
                        "where l.pet_id = ? and s.desc like ?",
                        (pet_id, "%等于其中较高的一项%")
                    )
                    if sm2:
                        dual_mech = "flatten"
        except Exception:
            pass

        pc, sc_ = len(phys), len(spec)
        if dual_mech == "flatten":
            # 拉平型：拉平后双攻相同，所以「种族高的那项」决定单攻印方向
            _st = self.pet_stats(pet_id) or {}
            atk0 = int(_st.get("atk") or 0)
            spa0 = int(_st.get("sp_atk") or 0)
            if atk0 > spa0:
                orient = "physical"
            elif spa0 > atk0:
                orient = "special"
            else:
                orient = "dual"
        elif dual_mech:
            # 求和机制 → 判双攻（除非只有一种攻击技能）
            if pc and sc_:
                orient = "dual"
            else:
                orient = "physical" if pc else "special"
        elif pmax >= smax and pc >= sc_:
            orient = "physical" if (pmax > smax or pc > sc_) else "mixed"
        elif smax >= pmax and sc_ >= pc:
            orient = "special"
        else:
            orient = "mixed"
        _why = "物理%d条(最高%d) vs 特殊%d条(最高%d)" % (len(phys), pmax, len(spec), smax)
        if dual_mech == "skill":
            _why += "；技能词条含「双攻值之和」（双刃印收益）"
        elif dual_mech == "soul":
            _why += "；魂印「双攻值视为二者之和」（双刃印收益）"
        elif dual_mech == "flatten":
            _why += "；魂印「双攻等于较高的一项」→ 走单攻印，别用双刃"
        return {"orient": orient, "phys_cnt": len(phys), "spec_cnt": len(spec),
                "phys_max": pmax, "spec_max": smax,
                "dual_mech": dual_mech, "why": _why}

    def pick_nature_by_ev(self, pet_id: int, ev: str, fallback: str = "") -> dict:
        """按学习力侧重挑性格。用户没说性格时用。

        返回 {nature, reason, up, down}

        ★ 判据：
          ① 解析学习力，找主刷项
          ② 主刷项与其他项的差 <= 20 → 视为「均衡型」，不硬改，返回 {}
             （双防拉满的精灵加哪个都一样，总和不变）
          ③ 否则选「加主刷项」的性格，减项取该精灵种族最低且没刷的项

        ★ 为什么均衡型不硬改：性格加成作用在「裸值×1.1」，
          而裸值里种族×2 占大头。双防都刷满时，加防或加特防对总和影响相同。
        """
        import re as _re6
        alias = {"攻": "atk", "防": "def", "特攻": "sp_atk", "特防": "sp_def",
                 "速": "spd", "体": "hp"}
        evmap: dict[str, int] = {}
        for piece in str(ev or "").replace("，", ",").split(","):
            if "=" not in piece:
                continue
            kk, _, vv = piece.partition("=")
            kk = alias.get(kk.strip(), kk.strip())
            vv = vv.strip()
            if kk in ("atk", "def", "sp_atk", "sp_def", "spd", "hp") and vv.isdigit():
                evmap[kk] = evmap.get(kk, 0) + int(vv)
        if not evmap:
            return {}
        # ★ 排除 hp：25 种性格里没有「体力+10%」的，top 选到 hp 就永远推不出性格。
        #   实例：sp_def=252,hp=252 → top 若选 hp，会返回空（不换性格），
        #         实际该选 sp_def → 慎重。
        _cand_keys = [k for k in evmap if k != "hp"]
        if not _cand_keys:
            return {}
        top = max(_cand_keys, key=lambda k: evmap[k])
        # ★ others 也要排除 hp：否则 sp_def=252,hp=252 会误判成「并列」
        #   （hp 252 会把 max(others) 顶到 252，和 top 相等 → 误返回 {}）
        others = [v for k, v in evmap.items() if k != top and k != "hp"]
        # ★ 阈值不能用 20：「特防253 防252」差 1 点也该跟着改。
        #   实测玄武：特防253 + 慎重(特防+10%) = 双防合计 1548
        #             特防253 + 顽皮(防+10%)   = 1547
        #   原因是 253/4 = 63.25 的小数被 ×1.1 放大后多跨一格。
        #   只有「当场并列最大」才判均衡（此时加哪个确实一样）。
        if others and evmap[top] == max(others) and len(evmap) > 1:
            return {}                       # 真并列，别动
        if evmap[top] < 100:
            return {}                       # 主刷项也没刷多少，不算侧重

        # 该精灵种族，用来选减项
        try:
            st = self.pet_stats(pet_id) or {}
        except Exception:
            st = {}
        stats = {k: int(st.get(k) or 0) for k in ("atk", "def", "sp_atk", "sp_def", "spd", "hp")}

        # 候选性格：加 top 的所有性格
        cands = [(nm, pair) for nm, pair in self._NATURE_TABLE.items() if pair[0] == top]
        if not cands:
            return {}
        # ★ 减项打分（2026-11-03 用户校正）
        #   ① 废项打折：与主攻类型相反的那项减了不亏
        #      玄武是物攻型（技能表 物理10 vs 特殊4），减特攻代价最低
        #   ② 速度不再硬编码 +40：只有种族 >110 的高速精灵才扣分
        #      玄武速度种族 90、魂印也不加速 → 减速不亏（但也别主动去减）
        #   ③ 减主攻项 = 自杀
        orient = (self.attack_orientation(pet_id) or {}).get("orient", "")

        def down_cost(pair):
            dn = pair[1]
            if dn == top:
                return 9999
            if evmap.get(dn, 0) > 0:
                return 9998                 # 刷了的不能减
            base = float(stats.get(dn, 0))
            # ① 废项打折
            if orient == "physical" and dn == "sp_atk":
                base *= 0.3
            elif orient == "special" and dn == "atk":
                base *= 0.3
            # ② 主攻项不能减
            elif orient == "physical" and dn == "atk":
                base += 999
            elif orient == "special" and dn == "sp_atk":
                base += 999
            # ③ 速度只在高速精灵身上扣分
            if dn == "spd":
                spd_base = stats.get("spd", 0)
                if spd_base > 110:
                    base += 50
                elif spd_base > 95:
                    base += 20
                # 低于 95 的慢速精灵，减速基本无感
            if dn == "hp":
                base += 60                  # 减体力最亏
            return base
        best = min(cands, key=lambda x: down_cost(x[1]))
        nat, pair = best
        if nat == fallback:
            return {}
        _o = self.attack_orientation(pet_id) or {}
        _why = ""
        if _o.get("why"):
            _why = "；减%s（%s）" % (
                self._DIR_CN.get(pair[1], pair[1]), _o["why"])
        return {"nature": nat, "up": pair[0], "down": pair[1],
                "reason": "学习力主刷%s(%d)，选加%s的性格「%s」%s" % (
                    self._DIR_CN.get(top, top), evmap[top],
                    self._DIR_CN.get(top, top), nat, _why)}

    def suggest_team_skip(self, name: str, nature: str = "", ev: Any = 0) -> str:
        """按精灵**种族定位**推断战队该跳过哪一项。

        ★ 判断顺序（2026-11-03 校准，实测反推确认）：
          ① 种族攻/特攻全 0（魂印拉平型，如无为觉者）→ 不跳
          ② |攻-特攻| <= 10 → 双攻型 → **不跳**（六项全点）
             实例：天堂极龙兽 攻110/特攻110，facts 明确写「满战队」，
             它虽然刷了攻击学习力，战队仍六项全点。
          ③ 攻 > 特攻 +10 → 物攻向 → 跳「特攻」（南霜 148/70、无极圣武 125/80）
          ④ 特攻 > 攻 +10 → 特攻向 → 跳「攻击」

        ★ 不看学习力：刷攻击学习力不代表战队要跳特攻
          （天堂极龙兽就是反例，之前按学习力判错过一次）。
        ★ 性格只作辅助：种族判定不出时才用。
        """
        try:
            hits = self.find_pets(name)
            if not hits:
                return ""
            st = self.pet_stats(hits[0][0]) or {}
            atk = int(st.get("atk") or 0)
            spa = int(st.get("sp_atk") or 0)

            # ① 种族全 0
            if not atk and not spa:
                return ""

            # ② 双攻型：差值 <=10 不跳
            if abs(atk - spa) <= 10:
                return ""

            # ③④ 明显偏向
            if atk > spa:
                return "特攻"
            if spa > atk:
                return "攻击"
            return ""
        except Exception:
            return ""

    def check_mints(self, mints: list[str]) -> dict:
        """严格校验刻印名。返回 {ok: [...], bad: [...], hints: [...]}。

        为什么需要：之前 LLM 写错刻印名时只是 warn 一句就继续，
        结果图上少装两个刻印，面板全错。
        """
        ok, bad = [], []
        for mn in (mints or []):
            s = str(mn or "").strip()
            if not s:
                continue
            # ★ 2026-11-08 改 mint_vals：check_mints 是「校验刻印名」，
            #   原来走 lookup_mintmark → 「九天攻」「圣战体」这类简称
            #   直接判成「查不到」，seer_build 的刻印校验就误报。
            info = self.mint_vals(s)
            if info and info.get("vals"):
                ok.append({"input": s, "name": info.get("name") or s, "vals": info["vals"]})
            else:
                bad.append(s)
        hints: list[str] = []
        if bad:
            # 给几个相近候选，方便 LLM 自我纠正
            for b in bad[:3]:
                try:
                    cands = self.mintmark_search(b, limit=3)
                    names = [c.get("name") for c in cands if c.get("name")]
                    if names:
                        hints.append(f"「{b}」查不到，相近的有：{'、'.join(names)}")
                except Exception:
                    pass
        return {"ok": ok, "bad": bad, "hints": hints}

    # ── 刻印系列 / 方向推断 ──────────────────────────────────────
    # 群友常只说系列名（「双威临一龙子」），具体哪一条要按上下文推：
    #   · 已有刻印的数值方向（威临=防65特防65 → 盾）
    #   · 学习力分配（防253 特防252 → 双防向）
    # 推出来的必须标注「这是推理的」，不能冒充用户原话。

    _DIR_CN = {"atk": "物攻", "sp_atk": "特攻", "hp": "体力",
               "def": "物防", "sp_def": "特防", "spd": "速度",
               "dual_atk": "双攻", "mixed": "混合"}

    def mint_class(self, mint_name: str) -> str:
        """查刻印属于哪个系列（如「赤洲龙子系列」「铠甲勇士系列」）。查不到返回 ""。

        ★ 关联路径（一开始只看 mintmark 表，完全漏了这张）：
            mintmark.id
              → universal_mintmark_part.mintmark_id
              → universal_mintmark_part.mintmark_class_id
              → mintmark_class.name
        覆盖：3834 条里 946 条（25%）。剩下的是 type_id=0/1 的老式刻印，
        它们在 ability_/skill_mintmark_part 表里，那两张**没有 class 字段**，真的查不到。
        """
        s = str(mint_name or "").strip()
        if not s:
            return ""
        try:
            rows = self._q(
                "select c.name from mintmark m "
                "join universal_mintmark_part p on p.mintmark_id = m.id "
                "left join mintmark_class c on c.id = p.mintmark_class_id "
                "where m.name = ? and c.name is not null limit 1", (s,)
            )
            return str(rows[0][0]) if rows else ""
        except Exception:
            return ""

    def mint_series_members(self, mint_name: str) -> list[str]:
        """列出与给定刻印**同一系列**的全部刻印名（去重）。

        比按名字前缀猜靠得住：刑天·威临 和 刑天·破岳 名字前缀相同，
        但帝皇·无极 同属铠甲勇士系列却没有共同前缀。
        """
        cls = self.mint_class(mint_name)
        if not cls:
            return []
        try:
            rows = self._q(
                "select distinct m.name from mintmark m "
                "join universal_mintmark_part p on p.mintmark_id = m.id "
                "join mintmark_class c on c.id = p.mintmark_class_id "
                "where c.name = ? order by m.name", (cls,)
            )
            return [r[0] for r in rows]
        except Exception:
            return []

    # 方向词 → 内部方向键。**长的优先匹配**：不先判「特攻」就会把「特攻」吃成「攻」。
    # ★ "MAIN_ATK" = 主攻项（2026-11-08）：玩家写「九天攻」时说的是「主攻那一项」，
    #   不是「物攻那项」。九天/圣战这类系列里物攻版和特攻版是成对的
    #   （九曲之刃 攻63 / 九曲之光 特攻63），必须由精灵流派决定选哪只。
    _DIR_WORDS = (
        ("特攻", "sp_atk"), ("特防", "sp_def"), ("双防", "def"), ("体力", "hp"),
        ("攻", "MAIN_ATK"), ("防", "def"), ("体", "hp"), ("速", "spd"),
    )

    def parse_series_direction(self, text: str) -> tuple[str, str]:
        """把「九天攻 / 圣战体 / V8 特攻」拆成 (系列名, 方向键)。

        玩家口语常这么写：系列名 + 一个方向字。旧实现只认纯系列名（"九天"），
        带方向词就整条查不到。返回 ("","") 表示不是这种写法。
        """
        s = str(text or "").strip()
        if not s or "·" in s:
            return ("", "")
        for word, dkey in self._DIR_WORDS:
            if s.endswith(word) and len(s) > len(word):
                return (s[: -len(word)].strip(), dkey)
        return ("", "")

    def mint_series(self, series: str) -> list[dict]:
        """列出某个刻印系列的全部条目及满级数值。

        series 传「赤洲龙子」「黄金残影」「刑天」这类前缀（不含「·」后缀）。
        desc 原始格式是「防御3/38,特攻7/75」——属性名后面紧跟等级数字。
        """
        s = str(series or "").strip()
        if not s:
            return []
        # ⓪ 先当「系列名」认：MintMarkClass 里有 97 个正式系列
        #    （九天系列 / 巅峰圣战系列 / V8系列 / 赤洲龙子系列 …）
        #    旧实现只按刻印名前缀匹配，传系列名永远返回空。
        cid = None
        try:
            for cand in (s, s[:-2] if s.endswith("系列") else s + "系列"):
                hit = self._manual_q(
                    "mintmark", "select ID from MintMarkClass where ClassName=?", (cand,)
                )
                if hit:
                    cid = int(hit[0][0])
                    break
            if cid is None:
                bare = s[:-2] if s.endswith("系列") else s
                hit = self._manual_q(
                    "mintmark",
                    "select ID, ClassName from MintMarkClass where ClassName like ? limit 1",
                    (f"%{bare}%",),
                )
                if hit:
                    cid = int(hit[0][0])
        except Exception:  # noqa: BLE001
            cid = None
        if cid is not None:
            out = []
            for mid, des, maxattr in self._manual_q(
                "mintmark",
                "select ID, Des, MaxAttriValue from MintMark where MintmarkClass=? order by ID",
                (cid,),
            ):
                vals: dict[str, int] = {}
                try:
                    arr = ast.literal_eval(str(maxattr)) if maxattr else None
                    if isinstance(arr, (list, tuple)) and len(arr) >= 6:
                        vals = dict(zip(
                            ("atk", "def", "sp_atk", "sp_def", "spd", "hp"),
                            [int(x) for x in arr[:6]],
                        ))
                except (ValueError, SyntaxError):
                    pass
                out.append({"id": mid, "name": des, "vals": vals, "suffix": ""})
            return out

        rows = self._q(
            "select id, name, desc from mintmark where name like ? order by name",
            (s + "%",)
        )
        out = []
        import re as _re3
        _KEYMAP = {"攻击": "atk", "防御": "def", "特攻": "sp_atk",
                   "特防": "sp_def", "速度": "spd", "体力": "hp"}
        for mid, nm, desc in rows:
            vals = {}
            # desc 形如 "防御3/38,特攻7/75,..." —— 属性名后面**紧跟等级数字**，
            # partition("/") 会切出 "防御3"，必须先剥掉尾部数字再查表。
            for part in str(desc or "").split(","):
                if "/" not in part:
                    continue
                k, _, v = part.partition("/")
                k = _re3.sub(r"\d+$", "", k.strip())
                v = v.strip()
                key = _KEYMAP.get(k)
                if key and v.isdigit():
                    vals[key] = int(v)
            if vals:
                out.append({"id": mid, "name": nm, "vals": vals,
                            "suffix": nm.split("·")[-1] if "·" in nm else ""})
        return out

    def mint_direction(self, mint_name: str, series: str = "") -> dict:
        """判断一条刻印偏哪个方向。返回 {dir, score, vals, why}。

        ★ 单属性峰值法（2026-11-03 用户校正后定稿）：
          ① 体力 >= 120 → 直接体印（135 档那批，绝对高档）
          ② 否则取 max(攻, 特攻, min(防,特防), 速)，谁是峰值就是哪向
          ③ 攻与特攻差 <=10 且都 >0 → 双攻印

        ★ 为什么双防用 min 而不是合计：
          蒲牢 防38+特防38=76 合计不算低，但它是「两条都不高」的单边攻印；
          狴犴 防60+特防60 才是「两条都高」的真盾。用 min 能区分这两者。

        ★ 前三版为什么废弃：
          · 占自身总和：体力天然占大头 → 蒲牢被判体印
          · 系列内相对：刑天攻普遍低，威临攻30 成系列最高 → 误判物攻
          · 全库归一化：速度分母仅 45，蒲牢 37/45=82% 压过攻 75/95=79% → 误判速度
        """
        # ★ 2026-11-08 改 mint_vals：mint_direction 是「从刻印数值反推它偏哪一项」，
        #   传进来的可能是简称（「九天攻」「圣战体」），lookup_mintmark 认不出，
        #   就会返回 {} 让上层以为「这颗印没方向」。
        info = self.mint_vals(mint_name)
        vals = (info or {}).get("vals") or {}
        if not vals:
            return {}

        def _v(key):
            return int(vals.get(key) or 0)

        a, sa = _v("atk"), _v("sp_atk")
        d, sd = _v("def"), _v("sp_def")
        sp, h = _v("spd"), _v("hp")

        # ① 体力绝对高档
        if h >= 120:
            return {"dir": "hp", "score": h, "vals": vals,
                    "why": "体力 %d（≥120 属高档体印）" % h}

        # ③ 双攻印
        if a > 0 and sa > 0 and abs(a - sa) <= 10 and a >= 50:
            return {"dir": "dual_atk", "score": max(a, sa), "vals": vals,
                    "why": "攻%d 特攻%d 双高" % (a, sa)}

        # ② 单属性峰值
        peak = {
            "atk": a,
            "sp_atk": sa,
            "def": min(d, sd),      # 双防要求两边都不低
            "spd": sp,
        }
        best = max(peak, key=lambda k: peak[k])
        if peak[best] <= 0:
            return {"dir": "mixed", "score": 0, "vals": vals}
        return {"dir": best, "score": peak[best], "vals": vals,
                "why": "%s峰值 %d" % (self._DIR_CN.get(best, best), peak[best])}

    def infer_main_direction(self, mints: list[str], ev: str = "") -> str:
        """从「已给的刻印 + 学习力」推断这套配装的主方向。

        返回 atk / sp_atk / def / hp / spd，推不出返回 ""。
        """
        score = {"atk": 0.0, "sp_atk": 0.0, "def": 0.0, "hp": 0.0, "spd": 0.0}
        # ① 已刻印的方向（权重高，因为它们是用户明确给的）
        for mn in (mints or []):
            s = str(mn or "").strip()
            if not s or "·" not in s:
                continue
            d = self.mint_direction(s)
            if d.get("dir") in score:
                score[d["dir"]] += 3.0
        # ② 学习力
        evmap = {}
        for piece in str(ev or "").replace("，", ",").split(","):
            if "=" not in piece:
                continue
            k, _, v = piece.partition("=")
            k = k.strip()
            v = v.strip()
            alias = {"攻": "atk", "防": "def", "特攻": "sp_atk",
                     "特防": "sp_def", "速": "spd", "体": "hp"}
            k = alias.get(k, k)
            if v.isdigit() and int(v) > 0:
                evmap[k] = evmap.get(k, 0) + int(v)
        for k, v in evmap.items():
            if k == "def":
                score["def"] += v / 100.0 * 1.5   # 防御和特防都算到 def 方向
            elif k == "sp_def":
                score["def"] += v / 100.0 * 1.5
            elif k in score:
                score[k] += v / 100.0 * 1.5
        best = max(score, key=lambda k: score[k])
        return best if score[best] > 0 else ""

    # 不参与「属性最高」命名的基名（与 tools/mint_picker.py 的
    # EXCLUDE_FROM_ATTR_NAMING 保持一致，两边改动要同步）。
    # 「圣战之无限」是适配特殊精灵的双攻印，玩家直接叫「无限」，
    # 不该抢「圣战体」这个名字（三只体都是 113，不排除就会随机给它）。
    _EXCLUDE_FROM_ATTR_NAMING = ("圣战之无限", "圣战之究极")

    def _pick_series_entry(self, items: list[dict], dkey: str, style: str = "") -> dict:
        """从某系列的条目里按「方向」挑一条。

        dkey 取值：
          "MAIN_ATK" 主攻项  —— 按 style 落成 atk / sp_atk（成对系列必须这样选）
          "atk" / "sp_atk" / "hp" / "spd"   单项峰值
          "def"             双防合计（承/重那种「双防高、副属性不同」的写法）

        并列时（成对版：物攻版和特攻版数值完全相同）再按 style 选主攻项，
        否则「九天体」会随机给九霄之钟（物攻44）而不是九霄之灵（特攻44）。
        style 缺失时按物攻处理（旧行为），不会报错。
        """
        def score(it):
            v = it.get("vals") or {}
            if dkey == "def":
                return int(v.get("def") or 0) + int(v.get("sp_def") or 0)
            if dkey == "MAIN_ATK":
                return max(int(v.get("atk") or 0), int(v.get("sp_atk") or 0))
            return int(v.get(dkey) or 0)

        if not items:
            return {}
        # 排除不参与属性命名的基名（无限/究极），全被排除时退回全集
        def _base_of(nm: str) -> str:
            for mk in ("[Ⅰ]", "αS", "α"):
                nm = nm.replace(mk, "")
            return nm.strip()
        _filtered = [x for x in items
                     if _base_of(x.get("name") or "") not in self._EXCLUDE_FROM_ATTR_NAMING]
        if _filtered:
            items = _filtered

        top = max(score(x) for x in items)
        cands = [x for x in items if score(x) == top]

        def tie(it):
            v = it.get("vals") or {}
            a, sa = int(v.get("atk") or 0), int(v.get("sp_atk") or 0)
            if style == "special":
                return -sa
            if style == "physical":
                return -a
            # dual / 未判定：谁的双攻合计高取谁
            return -(a + sa)

        cands.sort(key=tie)
        return cands[0]

    def pick_style_by_stats(self, name: str) -> str:
        """按种族值判流派：dual / physical / special（判不出返回 ""）。

        ★ 用种族值，不要用 attack_orientation —— 后者看技能表，
          南霜技能偏物理会被判 physical，但她种族 攻70/特攻148 是特攻向，
          配装方向按种族值才对。
        """
        try:
            hits = self.find_pets(str(name or ""))
            if not hits:
                return ""
            st = self.pet_stats(hits[0][0]) or {}
            a = int(st.get("atk") or 0)
            sa = int(st.get("sp_atk") or 0)
            if not (a or sa):
                return ""
            if min(a, sa) > 0 and abs(a - sa) <= 10:
                return "dual"
            return "physical" if a >= sa else "special"
        except Exception:
            return ""

    def complete_mint_series(self, mints: list[str], ev: str = "", name: str = "",
                             style: str = "") -> dict:
        """把「只写了系列名 / 系列+方向」的刻印补全成具体条目。

        mints 里若出现「赤洲龙子」「九天攻」这种**不带 · 后缀**的项，
        说明群友只说了系列名或方向词。

        name 传精灵名时按**种族值**判流派，用于在「成对版」里选对那一版：
          九天攻 @特攻精灵 → 九曲之光（不是九曲之刃）
          九天体 @特攻精灵 → 九霄之灵（不是九霄之钟）
        name 留空则按老行为（并列时取物攻那版）。

        返回 {mints: [...], inferred: [(系列, 选中的条目, 依据)], direction, style}
        没有需要补的就返回 inferred=[]，mints 原样。
        """
        out = list(mints or [])
        inferred = []
        # 方向来源优先级：显式 style > 按精灵种族值判 > ""（成对版并列时随机取一侧）
        style = str(style or "").strip()
        if not style and name:
            style = self.pick_style_by_stats(name)
        _STYLE_CN = {"physical": "物攻", "special": "特攻", "dual": "双攻"}

        def _dkey_cn(dk: str) -> str:
            if dk == "MAIN_ATK":
                return _STYLE_CN.get(style, "主攻") + "项"
            return self._DIR_CN.get(dk, dk)

        # 先算方向（用「已经有后缀的刻印 + 学习力」）
        base = [m for m in out if m and "·" in m]
        direction = self.infer_main_direction(base, ev)
        for i, mn in enumerate(out):
            s = str(mn or "").strip()
            if not s or "·" in s:
                continue
            # ★「九天攻 / 圣战体」这种「系列 + 方向词」写法：方向由词面直接给定，
            #   优先于「按学习力/已有刻印推断」——玩家明说了方向就照玩家的。
            _bare, _dkey = self.parse_series_direction(s)
            if _dkey:
                _series = self.mint_series(_bare)
                if _series:
                    pick = self._pick_series_entry(_series, _dkey, style)
                    if pick:
                        out[i] = pick["name"]
                        inferred.append({
                            "series": _bare,
                            "picked": pick["name"],
                            "direction": _dkey,
                            "direction_cn": _dkey_cn(_dkey),
                            "style": style,
                            "reason": (f"按你写的「{s}」直接取的"
                                       f"（该系列{_dkey_cn(_dkey)}最高）"),
                        })
                        continue
            series = self.mint_series(s)
            if not series:
                continue
            if not direction:
                # 推不出方向就留原样，让上层去问
                continue
            # 按方向挑：同系列里该方向数值最高的（并列时按流派选成对版）
            pick = self._pick_series_entry(series, direction, style)
            if not pick:
                continue
            out[i] = pick["name"]
            inferred.append({
                "series": s,
                "picked": pick["name"],
                "direction": direction,
                "direction_cn": _dkey_cn(direction),
                "style": style,
                "reason": f"按「{_dkey_cn(direction)}向」推的",
            })
        return {"mints": out, "inferred": inferred, "direction": direction, "style": style}

    def pet_build(self, name_or_id) -> dict:
        """查精灵专属配装（pet_build 表）。查不到返回 {}。

        ★ 为什么需要：通用 PRESET_MINTS 只按「物攻/特攻」分两套，
          对无极圣武（二角极差）、无为觉者（三310）、玄武（双防向）全给错。
          实例：bot 算「顶配无极」默认套爆攻印「黄金残影·希望×2+修罗·寂灭」，
          要人提醒才改成「瓏·寅勇×2+星辉·相伴」。
        """
        try:
            pid = None
            if isinstance(name_or_id, int):
                pid = name_or_id
            else:
                hits = self.find_pets(str(name_or_id))
                if hits:
                    pid = hits[0][0]
            if not pid:
                return {}
            rows = self._kb.execute(
                "select pet_id, pet_name, mints, suit, title, eye, nature, ev, "
                "team_skip, soulmod, note from pet_build where pet_id = ?", (pid,)).fetchall()
            
            if not rows:
                return {}
            r = rows[0]
            return {
                "pet_id": r[0], "pet_name": r[1],
                "mints": [x.strip() for x in str(r[2] or "").split(",") if x.strip()],
                "suit": r[3] or "", "title": r[4] or "", "eye": r[5] or "",
                "nature": r[6] or "", "ev": r[7] or "",
                "team_skip": r[8] or "", "soulmod": r[9] or "",
                "note": r[10] or "",
            }
        except Exception:
            return {}

    def config_view(
        self,
        name: str,
        *,
        talent: int = 31,
        nature: str = "",
        ev: Any = 0,
        mints: list[str] | None = None,
        suit: str = "",
        title: str = "",
        eye: str = "",
        year_bonus: bool = True,
        team: bool = True,
        hpup: int = 20,
        level: int = 100,
        soulmod_pct: float = 0.0,
        team_skip: str = "",
        soulmod_atk_diff: int = 0,
        soulmod_average: bool = False,
        soulmod_double_def_pct: float = 0.0,
        gems: list[str] | None = None,
    ) -> dict[str, Any]:
        """组装配置图数据（字段对齐手册 app 的配置计算器导出图）。

        ★★ 改这个函数之前，先读 `G:/dsh workspace/配置图与面板计算-交接说明.md`
           里面写了：最终公式（学习力/4 保留小数）、四种魂印派生、
           pet_build 专属配装表、以及四个真踩过的坑。
           公式已经错过四版，别再凭直觉改。

        返回结构：
          pet        名字/ID/属性/立绘 resource_id
          base       基础属性：等级/学习力合计/天赋/体力上限/性格/特性
          extra      额外数值：战队加成 / 额外加成（年费+10 走 year_bonus）
          battle     战斗属性：六项最终值 + 各项学习力
          equipment  套装/目镜/称号（名字 + 逐项加成）
          mintmarks  三个刻印（名字 + 图标 id）
          judgement  empty_hint / warn
        """
        view: dict = {
            "found": False, "pet": {}, "base": {}, "extra": {},
            "battle": [], "equipment": {}, "mintmarks": [],
            "empty_hint": None, "warn": [],
        }
        hits = self.find_pets(name) if hasattr(self, "find_pets") else []
        if not hits:
            view["empty_hint"] = f"找不到精灵：{name}"
            return view
        pid, petname = hits[0]
        stats = self.pet_stats(pid) or {}
        if not stats:
            view["empty_hint"] = f"{petname} 没有种族值数据"
            return view

        # ① 精灵基本信息
        # ★ 2026-11-04 修复：原来调 self.type_name_of()，但该方法从未存在；
        #   因为外面套了 hasattr 守卫，不报错但 types 恒为空 →
        #   配置图卡片上属性栏一直是空白。改用实际存在的 pet_type()。
        type_name = ""
        res_id = None
        try:
            row = self._q("select type_id, resource_id from pet where id=?", (pid,))
            if row:
                res_id = row[0][1]
            type_name = self.pet_type(pid) or ""
        except Exception:
            pass
        view["found"] = True
        view["pet"] = {
            "name": petname, "id": pid, "types": type_name,
            "resource_id": res_id,
        }

        # ② 学习力
        alias = {"攻": "atk", "防": "def", "特攻": "sp_atk", "特防": "sp_def",
                 "速": "spd", "体": "hp", "hp": "hp", "atk": "atk", "def": "def",
                 "sp_atk": "sp_atk", "sp_def": "sp_def", "spd": "spd"}
        ev_by_key: dict[str, int] = {}
        ev_all = 0
        if isinstance(ev, str) and "=" in ev:
            for piece in str(ev).replace("，", ",").split(","):
                piece = piece.strip()
                if not piece or "=" not in piece:
                    continue
                kk, vv = piece.split("=", 1)
                key = alias.get(kk.strip())
                if key and vv.strip().lstrip("-").isdigit():
                    ev_by_key[key] = int(vv)
        else:
            try:
                ev_all = int(ev or 0)
            except (TypeError, ValueError):
                ev_all = 0
        ev_total = sum(ev_by_key.values()) if ev_by_key else ev_all * len(_STAT_KEYS)

        # ③ 基础属性区
        view["base"] = {
            "level": level,
            "ev_total": ev_total,
            "talent": talent,
            "hpup": f"{min(int(hpup or 0), 20)}/20",
            "nature": nature or "中性",
            "ability": "（默认不选）",
        }

        # ④ 额外数值区：战队 +10/+15 + 年费全属性+10
        team_vals = dict(self._TEAM_MAX) if team else {}
        # 战队极差向：允许跳过某项（如「跳特攻」= 特攻不点）
        _skip = {s.strip() for s in str(team_skip or "").replace("，", ",").split(",") if s.strip()}
        _skip_keys = set()
        for _s in _skip:
            _k = self._CN2KEY.get(_s, _s)
            if _k in team_vals:
                _skip_keys.add(_k)
        for _k in _skip_keys:
            team_vals.pop(_k, None)
        year_vals = {k: 10 for k in _STAT_KEYS} if year_bonus else {}
        view["extra"] = {
            "team": [{"key": k, "cn": self._STAT_CN.get(k, k),
                      "now": team_vals.get(k, 0), "max": self._TEAM_MAX.get(k, 0)}
                     for k in _STAT_KEYS],
            "year_bonus": bool(year_bonus),
            "year_vals": [{"key": k, "cn": self._STAT_CN.get(k, k), "val": 10}
                          for k in (_STAT_KEYS if year_bonus else [])],
            "team_total": sum(team_vals.values()),
            "year_total": sum(year_vals.values()),
        }

        # ⑤ 装备区
        def _pack(label: str, nm: str, lookup):
            if not nm:
                return {"label": label, "name": "", "vals": [], "text": ""}
            info = {}
            try:
                info = lookup(nm) or {}
            except Exception:
                info = {}
            vals = info.get("vals") or {}
            items = [{"key": k, "cn": self._STAT_CN.get(k, k), "val": int(vals.get(k, 0) or 0)}
                     for k in _STAT_KEYS if int(vals.get(k, 0) or 0)]
            if not items and info.get("text"):
                view["warn"].append(f"{label}「{nm}」只有文案、没有逐项数值")
            return {"label": label, "name": nm, "vals": items,
                    "id": info.get("id"),
                    "text": str(info.get("text") or "")[:120]}

        view["equipment"] = {
            "suit": _pack("套装", suit, getattr(self, "lookup_suit", lambda x: {})),
            "eye": _pack("目镜", eye, getattr(self, "lookup_eyepiece", lambda x: {})),
            "title": _pack("称号", title, getattr(self, "lookup_title", lambda x: {})),
        }

        # ⑥ 刻印
        mm_list = []
        # ★ 2026-11-08 改 mint_vals（+按精灵流派分流）：
        #   原来走 lookup_mintmark，它认不出「九天攻」「圣战体」这类
        #   「系列+方向」简称，配置图会直接报「刻印查不到」。
        #   而且必须传 style —— 不传就固定给物攻版，
        #   特攻精灵拿到的刻印加成是错的项（攻41 而不是特攻41）。
        _cfg_style = ""
        try:
            _cfg_style = self.pick_style_by_stats(petname)
        except Exception:  # noqa: BLE001
            _cfg_style = ""
        for mn in (mints or [])[:3]:
            info = self.mint_vals(mn, style=_cfg_style)
            _vals = info.get("vals") or {}
            mm_list.append({
                "name": str(info.get("name") or mn),
                "id": info.get("id"),
                "vals": _vals,
                # 逐项（只列非零），模板直接渲染
                "vals_list": [{"key": k, "cn": self._STAT_CN.get(k, k), "val": int(_vals.get(k, 0) or 0)}
                              for k in _STAT_KEYS if int(_vals.get(k, 0) or 0)],
                "found": bool(info),
            })
            if not info:
                view["warn"].append(f"刻印「{mn}」查不到")
        # ⑥.5 刻印绑定的宝石（每个刻印最多一颗）
        #     gems 参数写法：["威力宝石Lv7@星纪", "攻击+1宝石Lv8", "威力宝石Lv6@折服"]
        #     第一段是宝石全名/简称，@ 后面是绑定的技能名
        #     ★必须在刻印对称重排之前算好，并和刻印一起重排，否则宝石会错位
        _gems = []
        for _spec in (gems or [])[:3]:
            _s = str(_spec or "").strip()
            if not _s:
                _gems.append(None)
                continue
            _gname, _, _skill = _s.partition("@")
            _gname = _gname.strip()
            _info = self.lookup_gem(_gname) if hasattr(self, "lookup_gem") else {}
            _gems.append({
                "raw": _s,
                "name": _info.get("name") or _gname,
                "effect": _info.get("desc") or "",
                "skill": _skill.strip(),
                "found": bool(_info),
            })
        view["gems"] = _gems
        # ★ 同名刻印摆两边（对齐手册配置图：A / B / A）
        #   三个刻印里若有两个同名，把它们分到首位和末位，中间放另一个。
        if len(mm_list) == 3:
            # 宝石跟着刻印走：先配对，再一起重排
            for _i, _m in enumerate(mm_list):
                _m["gem"] = _gems[_i] if _i < len(_gems) else None
            names = [m.get("name") or "" for m in mm_list]
            if names[0] == names[1] and names[0] != names[2]:
                mm_list = [mm_list[0], mm_list[2], mm_list[1]]
            elif names[1] == names[2] and names[0] != names[1]:
                mm_list = [mm_list[1], mm_list[0], mm_list[2]]
            elif names[0] == names[2]:
                pass          # 已经对称
            else:
                # 有两个同名但位置不规整，按次数重排成 A/B/A
                from collections import Counter as _Counter
                _cnt = _Counter(names)
                _dup = [n for n, c in _cnt.items() if c >= 2]
                if _dup:
                    _d = _dup[0]
                    _other = [m for m in mm_list if (m.get("name") or "") != _d]
                    _same = [m for m in mm_list if (m.get("name") or "") == _d]
                    if _other and len(_same) >= 2:
                        mm_list = [_same[0], _other[0], _same[1]]
        view["mintmarks"] = mm_list


        # ⑦ 战斗属性：六项最终值 + 各项学习力
        #    stat_view 只算「种族+学习力+刻印+套装/称号/目镜」，
        #    战队加成和年费（全属性+10）是额外两段，要在这里补。
        # ★ stat_view 现在自己也会加战队/年费，这里必须传 False 关掉，
        #   否则同一份加成会被算两遍。
        st = self.stat_view(
            petname, talent=talent, nature=nature, ev=ev,
            mints=list(mints or []), suit=suit, title=title, eye=eye,
            team=False, year_bonus=False, hpup=0,
        )
        battle = []
        _extra_flat = dict(team_vals) if team else {}
        if year_bonus:
            for _k in _STAT_KEYS:
                _extra_flat[_k] = _extra_flat.get(_k, 0) + 10
        # 体力上限药剂：只加体力，最多 20（permanent）
        _hpup = min(int(hpup or 0), 20)
        if _hpup:
            _extra_flat["hp"] = _extra_flat.get("hp", 0) + _hpup
        for r in (st.get("rows") or []):
            ev_v = ev_by_key.get(r["key"], 0) if ev_by_key else ev_all
            _flat = int(_extra_flat.get(r["key"], 0))
            battle.append({
                "key": r["key"], "cn": r["cn"],
                "value": int(r["final"]) + _flat,
                "ev": ev_v,
                "base": r["base"], "mint": r["mint"],
                "overlay": r["overlay"], "flat": _flat,
            })
        # ⑧ 魂印派生：消耗体力上限的 N%，其他五项各加等量
        #    实例·天堂极龙兽「消耗20%的体力上限并提升其他能力值等量数值」
        #    裸 1249/551/382/285/382/415 → 1249×20%=249 → 战内 1000/800/631/534/631/664
        _sm = float(soulmod_pct or 0.0)
        if _sm > 0 and battle:
            _hp_row = next((b for b in battle if b["key"] == "hp"), None)
            if _hp_row:
                _cut = int(_hp_row["value"] * _sm + 1e-9)   # 向下取整
                _hp_row["value"] = _hp_row["value"] - _cut
                for b in battle:
                    if b["key"] != "hp":
                        b["value"] = b["value"] + _cut
                view.setdefault("notes", []).append(
                    f"魂印派生：体力上限 {_hp_row['value'] + _cut} × {_sm:.0%} = {_cut}"
                    f"（体力 −{_cut}，其余五项各 +{_cut}）"
                )
        # ⑧.5 英雄之耀型：双攻差 × 系数 × 层数 → 加到体力/防御/特防/速度
        #    实例·无极圣武「双攻差 724-216=508，508×15%×5层=381，
        #    加到体力/防御/特防/速度，双攻本身不变」
        if soulmod_atk_diff and battle:
            _by = {b["key"]: b for b in battle}
            _d = int(_by["atk"]["value"]) - int(_by["sp_atk"]["value"]) \
                 if ("atk" in _by and "sp_atk" in _by) else 0
            _gain = int(abs(_d) * 0.15 * int(soulmod_atk_diff) + 1e-9)
            if _gain:
                for _k in ("hp", "def", "sp_def", "spd"):
                    if _k in _by:
                        _by[_k]["value"] += _gain
                view.setdefault("notes", []).append(
                    f"英雄之耀 {soulmod_atk_diff} 层：双攻差 {abs(_d)} × 15% × "
                    f"{soulmod_atk_diff} = {_gain}"
                )
                view["notes"].append("__CONT__加到体力 / 防御 / 特防 / 速度，双攻本身不变")
        # ⑧.6 总和均分型：每项变成「六项之和 / 2」
        #    实例·无为觉者「战斗开始时，自身所有能力值变为所有能力值之和的一半」
        #    ★是每项变 S/2（总和 S → 3S），不是「总和变 S/2 再均分」
        if soulmod_average and battle:
            _S = sum(b["value"] for b in battle)
            _half = _S // 2
            for b in battle:
                b["value"] = _half
            view.setdefault("notes", []).append(
                f"魂印·总和均分：六项之和 S={_S} → 每项 = S/2 = {_half}"
            )
            # ★ 不是「总和变 3S」：S 为奇数时 floor(S/2) 会丢 1 点，
            #   真实六项和 = 6 × floor(S/2)，与 3S 可能差 3 点以内。
            view["notes"].append(
                "__CONT__六项各变 %d，六项和 = %d（S 为奇数时 floor 会丢位）"
                % (_half, _half * 6)
            )
        # ⑧.7 双防转化型：自身双防值的 X% 提升至「速度外」的能力值
        #    实例·镇世神岳·玄武 #4995
        #    「战斗开始时，自身双防值的15%提升至速度外的能力值」
        #    ★口径：先算出**含刻印装备加成后**的防御+特防，再 ×15% 向下取整，
        #      然后同量加到 攻/防/特攻/特防/体 五项（速度不加）。
        #    实测（防253 特252 体5，晨曦神话AH顽皮双威临一信仰）：
        #      小计 攻381 防613 特攻243 特防579 速361 体958
        #      双防 613+579=1192 → ×15% = 178.8 → 178
        #      → 559 / 791 / 421 / 757 / 361 / 1135/1136
        if soulmod_double_def_pct and battle:
            _d = next((b["value"] for b in battle if b["key"] == "def"), 0)
            _sd = next((b["value"] for b in battle if b["key"] == "sp_def"), 0)
            _sf = _d + _sd
            _gain = int(_sf * float(soulmod_double_def_pct))
            if _gain > 0:
                for b in battle:
                    if b["key"] != "spd":          # 速度外的能力值
                        b["value"] += _gain
                view.setdefault("notes", []).append(
                    f"魂印·双防转化：双防 {_d}+{_sd}={_sf} × "
                    f"{float(soulmod_double_def_pct)*100:.0f}% = {_gain}"
                )
                view["notes"].append(
                    f"__CONT__同量加到 攻/防/特攻/特防/体 五项，速度不加"
                )
        if _extra_flat:
            view.setdefault("notes", []).insert(0,
                "已叠加：战队加成" + ("＋年费全属性+10" if year_bonus else "") + ("＋体力上限药剂+%d" % _hpup if _hpup else ""))
        view["battle"] = battle
        view["total"] = st.get("total")
        if st.get("missing_mints"):
            view["warn"].append("未找到刻印：" + "、".join(st["missing_mints"]))
        # 六项和按最终「战内面板」算（含战队/年费/药剂/魂印派生）
        if battle:
            view["total"] = sum(b["value"] for b in battle)
        return view

    # 手册 MintMark.Type → 人话（2026-11-09）
    #   ★ 值里不要带括号 —— 输出时外面会套一层「（…）」，否则变括号套括号。
    #   ★ 实测过每类的实际内容，别照名字猜：
    #     Type=0  386 条  微型攻击刻印 / 王者级体力刻印 / 高级全能刻印
    #                      → 老版「能力刻印」，不用升级就有数值，MaxAttriValue 是空的
    #     Type=1  660 条  撞击刻印（先制+1）/ 电光火石刻印（威力+15）
    #                      → 「技能刻印」，EffectDes 写明给哪个技能加什么
    #     Type=3 2788 条  睿智 / 狂战 / 焚世之炎
    #                      → 现行「全能刻印」，有 MaxAttriValue，才谈几角
    #     Type=4    3 条  小碎片 / 中碎片 / 大碎片
    #                      → 「刻印碎片」（合成材料），六项全空、连 EffectDes 都没有
    _MINT_KIND = {
        0: "老版能力刻印，非全能",
        1: "技能刻印",
        3: "全能刻印",
        4: "刻印碎片",
    }

    def mint_vals(self, mint_name: str, style: str = "") -> dict[str, Any]:
        """刻印名 / 简称 → {name, id, vals}（格式与 lookup_mintmark 一致）。

        ★ 2026-11-08 加（bot 自测报的 bug）：
          `lookup_mintmark` **认不出「系列+方向」简称** ——
          `lookup_mintmark("九天攻")` 返回空（它的模糊匹配要求名字里含「九天攻」，
          但真正的名字是「九曲之刃」）。而 `seer_mintmark` 走的是
          `mintmark_manual_search`（它会拆系列+方向），所以同一个简称
          两个工具结果不一致：mintmark 认得、stat 认定「未找到」。

        ★★ 第二次修：加 `style`。
          `mintmark_manual_search` 默认给物攻版，**调用方得告诉它精灵是什么流派**。
          bot 实测「空元行者(special) 和伏尔格安(physical) 都出圣战之渊α(攻41)，
          特攻精灵白给」—— 就是这里没传 style。
          调用方（stat_view / build_view / config_view）应按精灵传
          `pick_style_by_stats(petname)`。
        """
        info = self.lookup_mintmark(mint_name) or {}
        if info.get("vals"):
            return info
        try:
            rows = self.mintmark_manual_search(mint_name, 1, style=style)
        except Exception:  # noqa: BLE001
            rows = []
        if not rows:
            return info or {}
        r = rows[0]
        mx = list(r.get("max") or [])
        ex = list(r.get("extra") or [])
        keys = ("atk", "def", "sp_atk", "sp_def", "spd", "hp")
        return {
            "name": r.get("name"),
            "id": None,
            "source": "manual_search",
            "vals": {k: (int(mx[i]) if i < len(mx) else 0) for i, k in enumerate(keys)},
            "extra": {k: (int(ex[i]) if i < len(ex) else 0) for i, k in enumerate(keys)},
        }

    def stat_view(
        self,
        name: str,
        *,
        talent: int = 31,
        nature: str = "",
        ev: Any = 0,
        mints: list[str] | None = None,
        suit: str = "",
        title: str = "",
        eye: str = "",
        team: bool = True,
        year_bonus: bool = True,
        team_skip: str = "",
        hpup: int = 0,
    ) -> dict[str, Any]:
        """面板计算：种族值 → 满级面板（可叠刻印/套装/称号/目镜）。

        ev 两种写法：
          单个整数            → 六项都刷这么多（一般用 0 或 255）
          "atk=254,hp=148"    → 分项，未指定的项按 0

        返回 rows 每项含 base / ev / mult / value / extra / final，
        judgement 字段：empty_hint / overlay_hint
        """
        view: dict = {
            "found": False, "name": name, "pid": None,
            "rows": [], "empty_hint": None, "overlay_hint": None,
            "mint_names": [], "suit": suit, "title": title, "eye": eye,
        }
        hits = self.find_pets(name) if hasattr(self, "find_pets") else []
        if not hits:
            view["empty_hint"] = f"找不到精灵：{name}"
            return view
        pid, petname = hits[0]
        stats = self.pet_stats(pid) or {}
        if not stats:
            view["empty_hint"] = f"{petname} 没有种族值数据"
            return view
        view["found"] = True
        view["name"] = petname
        view["pid"] = pid

        # ① 学习力写法
        alias = {"攻": "atk", "防": "def", "特攻": "sp_atk", "特防": "sp_def",
                 "速": "spd", "体": "hp", "hp": "hp", "atk": "atk", "def": "def",
                 "sp_atk": "sp_atk", "sp_def": "sp_def", "spd": "spd"}
        ev_by_key: dict[str, int] = {}
        ev_all = 0
        if isinstance(ev, str) and "=" in ev:
            for piece in str(ev).replace("，", ",").split(","):
                piece = piece.strip()
                if not piece or "=" not in piece:
                    continue
                kk, vv = piece.split("=", 1)
                key = alias.get(kk.strip())
                if key and vv.strip().lstrip("-").isdigit():
                    ev_by_key[key] = int(vv)
        else:
            try:
                ev_all = int(ev or 0)
            except (TypeError, ValueError):
                ev_all = 0

        # ② 刻印叠加
        extra = {k: 0 for k in _STAT_KEYS}
        mint_used: list[str] = []
        for mn in (mints or [])[:3]:
            # ★ 2026-11-08 改 mint_vals：lookup_mintmark 认不出「九天攻」「圣战体」
            #   这类「系列+方向」简称，而 seer_mintmark 认得 —— bot 自测发现
            #   同一个简称在 seer_stat 里报「未找到」，在 seer_mintmark 里正常。
            # ★ 并且要传 style：不传就固定给物攻版，
            #   bot 实测「空元行者(特攻向)也出圣战之渊α(攻41)」。
            info = self.mint_vals(mn, style=self.pick_style_by_stats(petname))
            if not (info and info.get("vals")):
                view.setdefault("missing_mints", []).append(mn)
                continue
            mint_used.append(str(info.get("name") or mn))
            for k, v in (info.get("vals") or {}).items():
                if k in extra:
                    extra[k] += int(v or 0)
        view["mint_names"] = mint_used

        # ③ 套装 / 称号 / 目镜（走已有的加成表；查不到就跳过）
        ov: dict[str, int] = {k: 0 for k in _STAT_KEYS}
        hints: list[str] = []
        for kind, label, fn in ((suit, "套装", self.lookup_suit),
                                (eye, "目镜", self.lookup_eyepiece),
                                (title, "称号", self.lookup_title)):
            if not kind:
                continue
            try:
                info = fn(kind) or {}
            except Exception:
                info = {}
            bonus = info.get("vals") or {}
            if bonus:
                for k, v in bonus.items():
                    if k in ov:
                        ov[k] += int(v or 0)
                hints.append(f"{label}「{info.get('name') or kind}」已计入")
            elif info.get("text"):
                hints.append(f"{label}「{kind}」只有文案、没有逐项数值")
            else:
                hints.append(f"{label}「{kind}」查不到")
        if hints:
            view["overlay_hint"] = "；".join(hints)

        # ③.5 战队加成 + 年费（额外两段，直接加，不参与性格修正）
        #   ★ 2026-11-04 修复：team 参数此前只是声明、函数体里从没使用，
        #     导致 stat_view 这条路径（seer.py stat / build）永远少算
        #     战队(攻15 防15 特攻15 特防15 体30 速10) + 年费(全属性+10)。
        #     柯尔霍德晨曦顶配因此显示 649，实际应为 704。
        #   额外加成不吃性格修正，所以放在 _stat_value 之后做加法。
        flat: dict[str, int] = {}
        if team:
            flat = dict(self._TEAM_MAX)
            _skip = {
                s.strip()
                for s in str(team_skip or "").replace("，", ",").split(",")
                if s.strip()
            }
            for _s in _skip:
                _k = self._CN2KEY.get(_s, _s)
                if _k in flat:
                    flat.pop(_k, None)
        if year_bonus:
            for _k in _STAT_KEYS:
                flat[_k] = flat.get(_k, 0) + 10
        # 体力上限药剂：只加体力，单只最多 +20（永久）
        _hpup = min(int(hpup or 0), 20)
        if _hpup:
            flat["hp"] = flat.get("hp", 0) + _hpup
        view["team"] = bool(team)
        view["year_bonus"] = bool(year_bonus)
        view["flat"] = dict(flat)
        view["flat_total"] = sum(flat.values())

        # ④ 逐项算
        rows = []
        for key in _STAT_KEYS:
            base = stats.get(key)
            if base is None:
                base = stats.get(key.replace("sp_", "sp"))
            if base is None:
                continue
            base = int(base)
            mult = 1.0 if key == "hp" else self._nature_mult(nature, key)
            ev_key = ev_by_key.get(key, 0) if ev_by_key else ev_all
            val = self._stat_value(base, talent, ev_key, mult, key == "hp")
            final = val + extra.get(key, 0) + ov.get(key, 0) + flat.get(key, 0)
            rows.append({
                "key": key, "cn": self._STAT_CN.get(key, key),
                "base": base, "ev": ev_key, "mult": mult,
                "value": val, "mint": extra.get(key, 0),
                "overlay": ov.get(key, 0), "flat": flat.get(key, 0),
                "final": final,
            })
        view["rows"] = rows
        view["total"] = sum(r["final"] for r in rows)
        return view

    def lookup_gem(self, name: str) -> dict:
        """按名字查宝石。返回 id / name / level / desc。

        ★ gem 表本身没有效果文本（skill_effect_in_use_id 全为 NULL），
          真正关联在 gemeffectlink(gem_id → skill_effect_in_use_id)。
          只查 gem 表会误判成「库里没有宝石效果」。
        """
        raw = str(name or "").strip()
        if not raw:
            return {}
        rows = self._q(
            "select id, name, level from gem where name = ? or name like ? "
            "order by level desc, id limit 1", (raw, "%" + raw + "%")
        )
        if not rows:
            return {}
        gid, gname, level = rows[0]
        desc = ""
        eff = self._q(
            "select u.info from gemeffectlink l "
            "join skill_effect_in_use u on u.id = l.skill_effect_in_use_id "
            "where l.gem_id = ? limit 1", (gid,)
        )
        if eff:
            desc = str(eff[0][0] or "")
        return {"id": gid, "name": gname, "level": level, "desc": desc}

    def lookup_mintmark(self, name: str) -> dict:
        """按名字查刻印数值（满级六项，**含隐藏加成**）。查不到返回 {}。

        ★ 隐藏数值（2026-11-03 补）：342 条刻印（8.9%）带 ExtraAttriValue，
          平时 desc 里不显示。例：
            雷之霆威  desc 写 攻50 防25 特防25 速30 体90
                     实际   攻53 防28 特防28 速35 体93
          只解析 desc 会把这批（精灵王系列、泰坦系列那批强力印）全算低。

        ★ 关联链（★ max_attr 的 id 是 attr_value_id，不是 mintmark.id，
          所以直接 `where id=?` 查 mintmark_max_attr 永远空）：
            mintmark.id
              → universal_mintmark_part.mintmark_id
              → max_attr_value_id   → mintmark_max_attr.id
              → extra_attr_value_id → mintmark_extra_attr.id

        ★ 取值优先级：
          ① manual_db/mintmark.db 的 MaxAttriValue（数组直读，已含隐藏值）
          ② seerapi 关联链 max_attr + extra_attr
          ③ seerapi desc 解析（不含隐藏值，兜底）
        """
        raw = str(name or "").strip()
        if not raw:
            return {}
        s = raw
        if "·" not in s and "-" not in s:
            # ⓪ 简称 / 系列名 → 全名（aliases.sqlite 的刻印三表，2026-11-06 接上）
            #    例：毛毛 → 45026、梅花 → 42320、青龙 → 40817；
            #    系列名（十年/V10/九霄系列）会展开成成员 id，取第一个。
            try:
                _ids = self.mint_alias_targets(s)
            except Exception:  # noqa: BLE001
                _ids = []
            if _ids:
                row = self._manual_q("mintmark", "select Des from MintMark where ID=?", (int(_ids[0]),))
                if row and row[0][0]:
                    s = str(row[0][0])
            # ① 简称补全：直接精确名优先，其次模糊
            hit = self._q("select id, name from mintmark where name = ? limit 1", (s,))
            if not hit:
                hit = self._q(
                    "select id, name from mintmark where name like ? "
                    "order by length(name), id limit 1", ("%" + s + "%",)
                )
            if hit:
                s = hit[0][1]
        head = self._q("select id, name from mintmark where name = ? order by id limit 1", (s,))
        if not head:
            return {}
        mid, full = head[0]

        # ① manual_db/mintmark.db：MaxAttriValue 已经是含隐藏加成的数组
        #
        # 2026-11-04（四号鱼）：这里原来没有 close —— 所有 return 路径都直接返回，
        # 每查一次刻印就漏一个 manual_db/mintmark.db 的只读连接。
        # Windows 下文件句柄会一直涨，最后 manual_db 自己也更新不了（被自己占住）。
        _mn = self._open_manual("mintmark")
        if _mn is not None:
            try:
                rows = _mn.execute(
                    "select MaxAttriValue, ExtraAttriValue, Hide "
                    "from MintMark where Des = ? order by dbid limit 1", (full,)
                ).fetchall()
                if rows:
                    import json as _json
                    arr = _json.loads(rows[0][0] or "[]")
                    if isinstance(arr, list) and len(arr) >= 6:
                        vals = dict(zip(("atk", "def", "sp_atk", "sp_def", "spd", "hp"),
                                        [int(x or 0) for x in arr[:6]]))
                        extra = _json.loads(rows[0][1] or "[]")
                        return {"id": mid, "name": full, "vals": vals,
                                "extra": extra if isinstance(extra, list) else [],
                                "source": "manual"}
            except Exception as exc:
                logger.debug("[kb] lookup_mintmark 手册源读取失败（%s），回落到 seerapi", exc)
            finally:
                _mn.close()

        # ② seerapi 完整关联链
        try:
            part = self._q(
                "select max_attr_value_id, extra_attr_value_id from universal_mintmark_part "
                "where mintmark_id = ? order by id limit 1", (mid,)
            )
            if part:
                max_id, extra_id = part[0][0], part[0][1]
                vals, extra = {}, []
                if max_id:
                    row = self._q(
                        "select atk, def, sp_atk, sp_def, spd, hp from mintmark_max_attr "
                        "where id = ?", (max_id,)
                    )
                    if row:
                        vals = dict(zip(("atk", "def", "sp_atk", "sp_def", "spd", "hp"),
                                        [int(x or 0) for x in row[0]]))
                if extra_id:
                    row = self._q(
                        "select atk, def, sp_atk, sp_def, spd, hp from mintmark_extra_attr "
                        "where id = ?", (extra_id,)
                    )
                    if row:
                        extra = [int(x or 0) for x in row[0]]
                        for kk, vv in zip(("atk", "def", "sp_atk", "sp_def", "spd", "hp"), extra):
                            if vals:
                                vals[kk] = vals.get(kk, 0) + vv
                if vals:
                    return {"id": mid, "name": full, "vals": vals,
                            "extra": extra, "source": "seerapi"}
        except Exception:
            pass

        # ③ desc 解析兜底（不含隐藏加成）
        info = self._q("select desc from mintmark where id = ?", (mid,))
        if not info:
            return {}
        import re as _re5
        KEYMAP = {"攻击": "atk", "防御": "def", "特攻": "sp_atk",
                  "特防": "sp_def", "速度": "spd", "体力": "hp"}
        vals = {}
        for part in str(info[0][0] or "").split(","):
            if "/" not in part:
                continue
            k, _, v = part.partition("/")
            k = _re5.sub(r"\d+$", "", k.strip())
            v = v.strip()
            key = KEYMAP.get(k)
            if key and v.isdigit():
                vals[key] = int(v)
        if not vals:
            return {}
        return {"id": mid, "name": full, "vals": vals, "extra": [], "source": "desc"}


    def pet_stats(self, pet_id: int) -> dict[str, Any]:
        """种族值：有觉醒/进阶形态时优先返回觉醒后数值。"""
        both = self.pet_stats_both(pet_id)
        return both.get("awakened") or both.get("base") or {}

    def pet_stats_both(self, pet_id: int) -> dict[str, dict[str, Any]]:
        """同时取觉醒前/觉醒后两套种族值。"""
        out: dict[str, dict[str, Any]] = {}
        base = self._q(
            "select atk, def, sp_atk, sp_def, spd, hp, percent from pet_base_stats where id=?", (pet_id,)
        )
        # ① 手册优先（2026-11-06）：pets.db 的 New* 字段就是觉醒后种族值，
        #    全库 28 只非空，和 seerapi.pet_advance 的 28 条完全对应，
        #    且手册更新更贴近游戏内手册。seerapi 作为回退。
        mrow = self._manual_pet_row(pet_id)
        if mrow and len(mrow) >= 19:
            _, _n, _t, m_atk, m_spatk, m_def, m_spdef, m_spd, m_hp = mrow[:9]
            n_atk, n_def, n_spatk, n_spdef, n_spd, n_hp = mrow[13:19]
            if any(v for v in (m_atk, m_spatk, m_def, m_spdef, m_spd, m_hp)):
                out["base"] = {
                    "atk": m_atk, "def": m_def, "sp_atk": m_spatk,
                    "sp_def": m_spdef, "spd": m_spd, "hp": m_hp, "percent": 0,
                }
            if any(v for v in (n_atk, n_def, n_spatk, n_spdef, n_spd, n_hp)):
                out["awakened"] = {
                    "atk": n_atk, "def": n_def, "sp_atk": n_spatk,
                    "sp_def": n_spdef, "spd": n_spd, "hp": n_hp, "percent": 0,
                }
            if out:
                return out
        # ② seerapi 回退
        if base:
            r = base[0]
            d = {k: r[i] for i, k in enumerate(_STAT_KEYS)}
            d["percent"] = r[6]
            out["base"] = d
        adv = self._q(
            """select a.atk, a.def, a.sp_atk, a.sp_def, a.spd, a.hp, a.percent
               from pet_advance pa join pet_advance_base_stats a on a.id = pa.id
               where pa.pet_id = ?""",
            (pet_id,),
        )
        if adv:
            r = adv[0]
            d = {k: r[i] for i, k in enumerate(_STAT_KEYS)}
            d["percent"] = r[6]
            out["awakened"] = d
        return out

    def pet_intro(self, pet_id: int) -> str:
        rows = self._q("select introduction from pet_encyclopedia_entry where id=?", (pet_id,))
        return (rows[0][0] or "") if rows else ""

    def _has_col(self, table: str, col: str) -> bool:
        """该表是否有这一列（兼容旧版 seerapi：缺列时不能硬写进 SQL）。"""
        try:
            return any(str(r[1]) == col for r in self._q(f"pragma table_info({table})"))
        except Exception:
            return False

    def pet_soulmarks(self, pet_id: int) -> list[str]:
        """魂印文本；有觉醒形态时标注【觉醒前】/【觉醒后】，觉醒后优先。

        **兼容旧版 seerapi**：老库的 soulmark 表可能没有 is_adv 列，
        直接写 coalesce(s.is_adv,0) 会让整条 SQL 报 no such column，
        结果连觉醒前魂印都查不到（实测旧库返回空列表）。
        这里按「有 is_adv → 用 is_adv／没有 → 用 pet_advance 表反推」逐级降级。
        """
        # ① 基础魂印（只依赖必然存在的列）
        rows = self._q(
            "select s.id, s.desc from petsoulmarklink l "
            "join soulmark s on s.id = l.soulmark_id where l.pet_id=?",
            (pet_id,),
        )
        rows = [(int(i), d) for i, d in rows if d]
        if not rows:
            return []
        if len(rows) == 1:
            return [rows[0][1]]

        # ② 判定「哪些是觉醒后」：优先 is_adv，其次 pet_advance，最后按长度粗排
        adv_ids: set[int] = set()
        if self._has_col("soulmark", "is_adv"):
            for sid, flag in self._q(
                "select id, coalesce(is_adv,0) from soulmark where id in (%s)"
                % ",".join("?" * len(rows)), tuple(i for i, _ in rows)
            ):
                if flag:
                    adv_ids.add(int(sid))
        if not adv_ids and self._has_col("pet_advance", "soulmark_id"):
            try:
                for (sid,) in self._q(
                    "select soulmark_id from pet_advance where pet_id=?", (pet_id,)
                ):
                    if sid is not None:
                        adv_ids.add(int(sid))
            except Exception:
                pass
        # 多个魂印但「没有任何一个标 is_adv」时，通常不是觉醒关系，
        # 而是同一精灵的多版本文案（如倪克斯 #4329 的 #1555/#1735）。
        # 这种情况**不要**乱标觉醒前/后，直接按原顺序列出并注明。
        if not adv_ids and len(rows) > 1 and self._has_col("soulmark", "is_adv"):
            got_any_flag = False
            for sid, flag in self._q(
                "select id, coalesce(is_adv,0) from soulmark where id in (%s)"
                % ",".join("?" * len(rows)), tuple(i for i, _ in rows)
            ):
                if flag:
                    got_any_flag = True
                    break
            if not got_any_flag:
                # 都是非觉醒：并列列出，不加觉醒标注
                return ["（本库该精灵有多版魂印文案，均非觉醒形态，按库内顺序列出）"] + [
                    d for _, d in rows
                ]

        guess = False
        if not adv_ids and len(rows) > 1:
            # ③ 最后兜底：多数觉醒后描述更长（实测 24/27≈89%，3 只反例），
            # 所以这里只作「猜测」并显式标注，不让它冒充确定结论。
            adv_ids = {max(rows, key=lambda x: len(x[1]))[0]}
            guess = True

        out = []
        for sid, d in rows:
            if sid in adv_ids:
                out.append(("【觉醒后】" if not guess else "【觉醒后·推测】") + d)
            else:
                out.append(("【觉醒前】" if not guess else "【觉醒前·推测】") + d)
        out.sort(key=lambda x: 0 if "觉醒后" in x[:8] else 1)  # 觉醒后优先
        if guess:
            out.insert(0, "（本库缺少觉醒标记，以下按描述长度推测，准确率约 89%，仅供参考）")
        return out

    def soulmark_effect_search(self, keyword: str, limit: int = 5) -> list[dict[str, str]]:
        """按效果关键词搜魂印正文，回答「哪些精灵的魂印写了这个效果」。

        为什么单独要这个方法：像「非真实伤害减少」这类效果散落在
        soulmark 表（魂印）里，而 effect_lookup 只查专属效果表，
        根本搜不到魂印——数据在库里，检索面却没覆盖。
        """
        kw = str(keyword or "").strip()
        if not kw:
            return []
        kws = self._expand_keyword(kw)
        try:
            cond = " or ".join("s.desc like ?" for _ in kws)
            args = [f"%{x}%" for x in kws] + [max(int(limit) * 4, 12)]
            rows = self._q(
                """select pe.name, pe.id, s.desc, coalesce(s.is_adv, 0)
                   from soulmark s
                   join petsoulmarklink l on l.soulmark_id = s.id
                   join pet pe on pe.id = l.pet_id
                   where """
                + cond
                + """ order by coalesce(s.is_adv, 0) desc limit ?""",
                tuple(args),
            )
        except Exception:
            return []
        out: list[dict[str, str]] = []
        seen: set[int] = set()
        for pname, pid, desc, adv in rows:
            try:
                pid = int(pid)
            except Exception:
                continue
            if pid in seen:
                continue
            d = self._strip_html(desc or "")
            if not d:
                continue
            hit_kw = next((x for x in kws if x in d), kw)
            segs = [x for x in re.split(r"[|｜]", d) if hit_kw in x]
            base = segs[0].strip() if segs else d
            if len(base) > 160:
                idx = base.find(hit_kw)
                start = max(0, idx - 50) if idx >= 0 else 0
                base = ("…" if start else "") + base[start: start + 150]
            seen.add(pid)
            out.append({
                "pet": str(pname or ""),
                "pet_id": str(pid),
                "snippet": base,
                "tier": "觉醒后" if adv else "",
                "via": "" if hit_kw == kws[0] else hit_kw,
            })
            if len(out) >= int(limit):
                break
        return out

    @staticmethod
    def _strip_html(text: str) -> str:
        import re as _re

        t = _re.sub(r"<[^>]+>", "", str(text or ""))
        return t.replace("&nbsp;", " ").strip()

    def pet_soulmark_forms(self, pet_id: int) -> list[dict[str, str]]:
        """魂印形态：觉醒前 / 觉醒后 / 强化 / 羁绊强化，全部按 display 表区分。"""
        rows = self._q(
            """select s.desc, coalesce(s.pve_effective, 0), d.display_kind
               from pet_soulmark_display d
               join soulmark s on s.id = d.soulmark_id
               where d.pet_id=?
               order by case d.display_kind
                          when 'partner_upgrade' then 0
                          when 'intensified' then 1
                          when 'advance' then 2
                          else 3 end, d.display_order""",
            (pet_id,),
        )
        label = {
            "base": "觉醒前", "advance": "觉醒后",
            "intensified": "强化版", "partner_upgrade": "羁绊强化",
        }
        out = []
        for desc, pve, kind in rows:
            if not desc:
                continue
            out.append({
                "form": label.get(str(kind), str(kind)),
                "desc": str(desc),
                "pve": "PVE 有效" if pve else "PVE 无效",
            })
        if out:
            for i, item in enumerate(out):
                item["primary"] = "1" if i == 0 else ""
            return out
        # 回退：display 表没有记录时按 is_adv 判断
        legacy = self._q(
            """select s.desc, coalesce(s.is_adv, 0), coalesce(s.pve_effective, 0)
               from petsoulmarklink l join soulmark s on s.id = l.soulmark_id
               where l.pet_id=? order by coalesce(s.is_adv, 0) desc""",
            (pet_id,),
        )
        legacy = [x for x in legacy if x[0]]
        if not legacy:
            return []
        # ★ 只有一版魂印时不要标「觉醒前/后」——那会暗示还有另一版。
        #   实例：#3311 圣光灵神 的觉醒只换了种族值和技能组，魂印 638 一动没动
        #   （28 只觉醒精灵里唯一一只魂印不变的）。旧逻辑看 soulmark.is_adv=1
        #   就标成「觉醒后」，让人以为存在两个版本，和玩家认知冲突。
        if len(legacy) == 1:
            d, _adv, pve = legacy[0]
            return [{
                "form": "当前",
                "desc": str(d),
                "pve": "PVE 有效" if pve else "PVE 无效",
            }]
        return [
            {"form": "觉醒后" if adv else "觉醒前", "desc": str(d),
             "pve": "PVE 有效" if pve else "PVE 无效"}
            for d, adv, pve in legacy if d
        ]

    def pet_partner_upgrade(self, pet_id: int) -> list[dict[str, str]]:
        """精灵羁绊（伙伴）带来的魂印强化：强化前 / 强化后。"""
        rows = self._q(
            """select u.before_description, u.after_description, g.name, g.required_pet_count
               from pet_partner_upgrade u
               left join pet_partner_group g on g.group_id = u.group_id
               where u.pet_id=?""",
            (pet_id,),
        )
        out = []
        for before, after, gname, need in rows:
            out.append({
                "group": str(gname or ""),
                "need": str(need or ""),
                "before": self._strip_html(before),
                "after": self._strip_html(after),
            })
        return out

    @staticmethod
    def _is_cjk(ch: str) -> bool:
        return bool(ch) and "\u4e00" <= ch <= "\u9fff"

    def _name_standalone(self, name: str, text: str) -> bool:
        """判断 name 是否在 text 里以「独立词」形式出现过。

        中文没有词边界，上游 SeerAPI 用 text_exact_name 规则按字面把专属名词
        挂到精灵身上，就会把「已损失体力值」里的「失体」当成名词「失体」。

        按字长分层（2026-11-03 定稿）：
          · **≥3 字 → 直接算独立词。** 长词几乎不可能是更长词的片段；
            实测「强制执行」（4字）在萨芙凯特/无为觉者/圣光莫妮卡/教皇 四处
            都是「并强制执行命中效果」这种正常用法，用几何规则会全部误杀。
          · **2 字 → 几何校验**：前后都是汉字 → 判为片段（不是独立词）。
            例：「已损失体力值」里的「失体」、「无限战意」末尾的「战意」。
            边界侧（before/after 为空）按汉字处理，宁可漏也不错挂。
          · **1 字 → 由 pet_effects 的「只信 direct_pet_glossary」策略处理**，
            不走这里（「星盘转动1刻」的量词「刻」无法用几何规则区分）。
        """
        if not name or not text:
            return False
        if len(name) >= 3:
            return True
        i = 0
        while True:
            i = text.find(name, i)
            if i < 0:
                return False
            before = text[i - 1] if i > 0 else ""
            after = text[i + len(name)] if i + len(name) < len(text) else ""
            b_cjk = self._is_cjk(before) if before else False
            a_cjk = self._is_cjk(after) if after else False
            if not b_cjk and not a_cjk:
                return True
            i += 1

    def pet_effects(self, pet_id: int) -> list[dict[str, str]]:
        """精灵专属名词（魂印/技能里提到的「弥沦之镜」「神途之熠」这类）。

        ⚠️ 2026-11-06 修复：seerapi 已经把 pet_special_effect（带 primary_rule 的
        宽表、靠名字子串匹配）换成了 petglossaryentrylink + glossary_entry 的
        精确关联。**旧表在新库里根本不存在**，_q 吞掉异常后返回空 ——
        于是所有专属名词查询静默失效，「神途之熠」「神魂之器」全查不到，
        pet_terms() 的 defined 永远是空列表。

        新表是精确关联（一个 pet_id 直接指向 glossary_entry_id），不存在
        旧表那种「已损失体力值」→「失体」的子串误挂，所以不需要独立词校验。
        旧表逻辑保留在 _pet_effects_legacy 里，兼容还没升级的库。
        """
        # ① 手册优先（2026-11-06）：effectDes 643 条 vs seerapi 598 条，
        #    关联在 monster 字段（510 单值 + 5 条逗号分隔）
        mrows = self._manual_q(
            "effectDes",
            """select id, kinddes, desc from effectDes
               where ',' || replace(monster, ' ', '') || ',' like ?
               order by id""",
            (f"%,{pet_id},%",),
        )
        if mrows:
            return [
                {"name": r[1], "desc": (r[2] or "").strip(), "rule": "manual"}
                for r in mrows
                if r[1]
            ]
        # ② seerapi 回退
        rows = self._q(
            """select g.name, g.desc from petglossaryentrylink l
               join glossary_entry g on g.id = l.glossary_entry_id
               where l.pet_id = ? order by g.id""",
            (pet_id,),
        )
        if rows:
            return [
                {"name": r[0], "desc": (r[1] or "").strip(), "rule": "direct_pet_glossary"}
                for r in rows
                if r[0]
            ]
        return self._pet_effects_legacy(pet_id)

    def _pet_effects_legacy(self, pet_id: int) -> list[dict[str, str]]:
        """旧版 seerapi 的 pet_special_effect 路径（含子串误挂校验）。"""
        rows = self._q(
            """select name, description, coalesce(primary_rule, '') from pet_special_effect
               where pet_id=? order by sort_id""",
            (pet_id,),
        )
        if not rows:
            return []
        try:
            marks_list = list(self.pet_soulmarks(pet_id) or [])
            # 专属名词不一定只写在魂印里，很多是写在技能效果里的，
            # 所以把该精灵所有技能的效果文本也纳入校验，避免误删。
            try:
                skill_ids = [r[0] for r in self._q(
                    "select skill_id from skillinpetorm where pet_id=?", (pet_id,))]
                for effs in self.skill_effects_map(skill_ids).values():
                    marks_list.extend(effs)
            except Exception:
                pass
        except Exception:
            marks_list = []
        out: list[dict[str, str]] = []
        for name, desc, rule in rows:
            if not name:
                continue
            if rule != "direct_pet_glossary":
                # 单字词（刻/辛/幕/烛）：text_exact_name 完全不可靠。
                # 实例「刻」：「星盘转动1刻」里的「1刻」被当成专属名词「刻」，
                # 前[1]后[，] 前后都不是汉字，几何校验放过了它 —— 但那是量词。
                # 这 4 个单字词在库里都有 direct_pet_glossary 的真主人，
                # 丢掉 text 版本不会丢信息，所以单字词只信 direct。
                if len(name) < 2:
                    continue
                if marks_list and not any(self._name_standalone(name, t) for t in marks_list):
                    continue
            out.append({"name": name, "desc": (desc or "").strip(), "rule": rule})
        return out

    def mentioned_effects(self, pet_id: int, text: str) -> list[dict[str, str]]:
        """魂印/技能原文里提到的专属效果，自动展开定义。"""
        text = text or ""
        return [e for e in self.pet_effects(pet_id) if e["name"] and e["name"] in text]

    def term_lookup_any(self, keyword: str) -> dict[str, Any]:
        """先按「专属名词/术语」精确找，再退回精灵名。"""
        kw = (keyword or "").strip()
        if not kw:
            return {}
        # 1) 专属名词（2026-11-06 修：旧表 pet_special_effect 已被
        #    petglossaryentrylink + glossary_entry 取代，查旧表永远返回空）
        rows = self._q(
            """select g.name, pe.name, g.desc from petglossaryentrylink l
               join glossary_entry g on g.id = l.glossary_entry_id
               left join pet pe on pe.id = l.pet_id
               where g.name = ? limit 3""",
            (kw,),
        )
        hits = [{"name": r[0], "pet": r[1], "desc": (r[2] or "").strip()} for r in rows]
        if hits:
            return {"kind": "effect", "items": hits}
        # 2) 术语库
        rows = self._q(
            "select name, desc from glossary_entry where name=? limit 3", (kw,)
        )
        if rows:
            return {"kind": "glossary",
                    "items": [{"pet": "", "name": r[0], "desc": (r[1] or "").strip()} for r in rows]}
        # 3) 模糊
        rows = self._q(
            """select g.name, pe.name, g.desc from petglossaryentrylink l
               join glossary_entry g on g.id = l.glossary_entry_id
               left join pet pe on pe.id = l.pet_id
               where g.name like ? limit 3""",
            (f"%{kw}%",),
        )
        if rows:
            return {"kind": "effect_fuzzy",
                    "items": [{"name": r[0], "pet": r[1], "desc": (r[2] or "").strip()} for r in rows]}
        rows = self._q("select name, desc from glossary_entry where name like ? limit 3", (f"%{kw}%",))
        if rows:
            return {"kind": "glossary_fuzzy",
                    "items": [{"pet": "", "name": r[0], "desc": (r[1] or "").strip()} for r in rows]}
        # 4) 官方词条查不到 → 转玩家确认过的 facts（换源，不是换关键词）
        #    2026-11-02：「本次在场」这类术语 effectDes 没有独立词条，
        #    只有 kb.facts 里写清了判定（连续在场回合算、切走清零重算）。
        #    只在 glossary 里换关键词打转会直接漏掉。
        facts = self._facts_by_keyword(kw, limit=3)
        if facts:
            return {"kind": "facts",
                    "items": [{"pet": "", "name": t, "desc": (c or "").strip()}
                              for t, c in facts]}
        return {}

    def _facts_by_keyword(self, kw: str, limit: int = 3) -> list[tuple[str, str]]:
        """在 kb.facts 里按 topic/正文搜（官方词条缺失时的兜底源）。"""
        k = (kw or "").strip()
        if not k or len(k) < 2:
            return []
        try:
            rows = self._kb.execute(
                """select topic, content from facts
                   where topic like ? or content like ?
                   order by (case when topic like ? then 0 else 1 end), updated_at desc
                   limit ?""",
                (f"%{k}%", f"%{k}%", f"%{k}%", int(limit)),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [(str(r[0] or ""), str(r[1] or "")) for r in rows]


    def icon_png_for_name(self, name: str) -> tuple[int, bytes] | None:
        """按名词名取「战斗中显示的专属图标」（effectIcon/{id}.png）。"""
        kw = (name or "").strip()
        if not kw:
            return None
        ids: list[int] = []
        row = self._q("select id from glossary_entry where name=?", (kw,))
        if row:
            ids.append(int(row[0][0]))
        # 2026-11-06：pet_special_effect 已被 petglossaryentrylink 取代，
        # 旧查询返空不影响结果（glossary_entry 那条已覆盖同名名词）。
        rows = self._q(
            """select l.glossary_entry_id from petglossaryentrylink l
               join glossary_entry g on g.id = l.glossary_entry_id
               where g.name = ? limit 5""",
            (kw,),
        )
        ids.extend(int(r[0]) for r in rows)
        for eid in dict.fromkeys(ids):
            hit = self._q(
                """select soulmark_id, icon_png from soulmark_icon
                   where icon_png_available=1 and icon_png is not null
                     and icon_asset_url like ? limit 1""",
                (f"%effectIcon/{eid}.png%",),
            )
            if hit:
                blob = hit[0][1]
                if isinstance(blob, (bytes, bytearray)):
                    return int(eid), bytes(blob)
        return None

    def soulmark_icon_png(self, pet_id: int) -> tuple[int, bytes] | None:
        """取该精灵魂印图标的 PNG 原始字节（库里存的是 blob）。"""
        rows = self._q(
            """select soulmark_id, icon_png from soulmark_icon
               where pet_id=? and icon_png_available=1 and icon_png is not null
               order by soulmark_id desc limit 1""",
            (pet_id,),
        )
        if not rows:
            return None
        smid, blob = rows[0]
        data = blob if isinstance(blob, (bytes, bytearray)) else None
        if not data:
            return None
        return int(smid), bytes(data)

    def pet_terms(self, pet_id: int) -> dict[str, list[dict[str, str]]]:
        """某精灵的全部专属名词：已收录的 + 文中提到但库中没有定义的。"""
        defined = self.pet_effects(pet_id)
        defined_names = {d["name"] for d in defined}

        texts: list[str] = []
        texts.extend(self.pet_soulmarks(pet_id))
        for sk in self.pet_skills(pet_id, 20):
            if sk.get("info"):
                texts.append(sk["info"])
        blob = " ".join(texts)

        import re as _re
        noisy = _re.compile(r"(结束|开始|触发|减少|提升|增加|附加|状态|技能|属性|伤害|能力|体力|对手|自身|命中|威力|先制|回合|效果)")

        stop = {"吸取", "反弹", "执行", "达到", "登场", "己方", "然后", "护盾", "害怕",
                "全属性", "弱点", "命中", "闪避", "免疫", "强化", "弱化", "异常"}

        mentioned: list[dict[str, str]] = []
        if blob:
            rows = self._q("select name, desc from glossary_entry where length(name)>=2 and length(name)<=8")
            for name, desc in rows:
                if name in defined_names or not name:
                    continue
                if noisy.search(name):
                    continue
                if name in stop:
                    continue
                if name in blob:
                    mentioned.append({"name": name, "desc": (desc or "").strip()})
                    if len(mentioned) >= 8:
                        break
        return {"defined": defined, "mentioned": mentioned}

    # 同一个机制在手册里常有多种写法，按字面搜会漏。玩家确认（2026-10-28）：
    # 「免疫秒杀」在库里至少三种表述，光搜这个短语只能查到琉梦和玄武。
    _EFFECT_SYNONYMS = {
        "免疫秒杀": (
            "免疫秒杀",                                   # 琉梦「希望」、玄武魂印
            "秒杀效果改为",                               # 咤克斯：把秒杀转成魔王咒怨
            "不会被对方以伤害、恢复以外的形式改变",        # 阿克希亚「霜域血装」
            "免疫下次受到的秒杀",
            "免疫一击必杀",
        ),
        "秒杀": ("秒杀", "瞬杀"),
        "免疫异常": ("免疫异常", "免疫受到的异常", "免疫异常状态"),
        "锁伤": ("锁伤", "伤害不超过", "最高不超过"),
    }

    @classmethod
    def _expand_keyword(cls, kw: str) -> list[str]:
        """把查询词扩成同义说法列表；没有同义组就原样返回。"""
        kw = str(kw or "").strip()
        if not kw:
            return []
        for head, variants in cls._EFFECT_SYNONYMS.items():
            if kw == head or kw in head or head in kw:
                return list(dict.fromkeys([kw, *variants]))
        return [kw]

    def effect_lookup(self, name: str, limit: int = 5) -> list[dict[str, str]]:
        """按效果名/描述反查：哪只精灵的专属效果。

        支持同义扩展：搜「免疫秒杀」会一并搜「秒杀效果改为…」
        「体力不会被对方以伤害、恢复以外的形式改变」等等价表述。
        """
        kws = self._expand_keyword(name)
        out: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for kw in kws:
            rows = self._q(
                """select g.name, pe.name, g.desc from petglossaryentrylink l
                   join glossary_entry g on g.id = l.glossary_entry_id
                   left join pet pe on pe.id = l.pet_id
                   where g.name like ? or g.desc like ? limit ?""",
                (f"%{kw}%", f"%{kw}%", limit),
            )
            for pname, ename, desc in rows:
                key = (str(pname), str(ename))
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "effect": ename,
                    "pet": pname,
                    "desc": (desc or "").strip(),
                    "via": "" if kw == kws[0] else kw,
                })
        if not out:
            for kw in kws:
                rows = self._q(
                    "select name, desc from glossary_entry where name like ? or desc like ? limit ?",
                    (f"%{kw}%", f"%{kw}%", limit),
                )
                for ename, desc in rows:
                    key = ("", str(ename))
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "effect": ename, "pet": "", "desc": (desc or "").strip(),
                        "via": "" if kw == kws[0] else kw,
                    })
        return out[:limit]

    # ────────────────────────────────────────────────────────────
    # 语义层：pet_view —— bot 和 CLI 共用的唯一判定源
    #
    # 背景（2026-11-03）：pet 这个功能原来两边各写一份渲染，
    # 判定逻辑（形态选择、同名警告、数据不全警告）被复制成两份，
    # 改一处就漏一处。实测已经分叉过两次：
    #   · CLI 加了〔同名多形态〕提示，bot 侧没有 → bot 照样选错形态
    #   · bot 加了「完全同名的是老形态」警告，CLI 侧没有 → DSH 看不到
    #
    # 分层的原则：
    #   ① 数据层   pet_stats / pet_skills …        共用（本来就在 db.py）
    #   ② 语义层   pet_view                        共用（本方法）
    #   ③ 呈现层   cmd_pet / seer_pet              各自排版，只管详略和符号
    #
    # 所以「判定」只在②写一次，不会再分叉；
    # 而「列几条技能、用什么符号」属于③，两边各管各的，
    # 漏了只是不好看，不会给出矛盾答案。
    # ────────────────────────────────────────────────────────────
    def pet_view(self, name: str, limit: int = 20) -> dict[str, Any] | None:
        """精灵全景的语义层：只产结构化数据，不做任何排版。

        返回里以 `_` 结尾命名的字段（form_hint / exact_warn / alias_note /
        sparse_warn …）是**必须被两边呈现**的判定结果，
        呈现层若漏渲染它们，selftest 应当报警（见 cmd_selftest 的契约检查）。
        """
        from typing import Any as _Any  # 局部别名，避免与外部签名冲突

        hits = self.find_pets(name, limit=limit)
        if not hits:
            return None

        pid, pname = hits[0]
        key = str(name or "").strip()

        view: dict = {
            "pid": pid,
            "name": pname,
            "query": key,
            "candidates": [(i, n) for i, n in hits],
            "types": self.pet_types(pid) or [],
            "stats": None,
            "stats_base": None,
            "stats_label_awakened": False,
            "soulmark_forms": [],
            "soulmark_bonus": [],
            "soulmark_refs": [],
            "effects": [],
            "skills": [],
            "skills_grouped": {},
            "intro": "",
            "moveset": None,
            "aliases": [],
            "pet_rules": [],
            # ── 判定字段（两边必须渲染）──
            "form_hint": None,
            "exact_warn": None,
            "alias_note": None,
            "alias_chain_hint": None,
            "other_aliases_hint": None,
            "sparse_warn": None,
        }

        # ① 俗称命中 → 进化链提示
        alias_hit = self.alias_lookup(key)
        if alias_hit and alias_hit == pid:
            view["alias_chain_hint"] = (
                f"〔进化链提示〕玩家说的「{key}」属于这条进化链，"
                f"最新形态是「{pname}」(#{pid})，以下按最新形态作答。"
            )

        # ② 外号备注（社区梗来历 / 多义候选）
        try:
            row = self._kb.execute(
                "select note from pet_alias where alias=?", (key,)
            ).fetchone()
        except Exception:
            row = None
        if row and row[0]:
            view["alias_note"] = f"〔外号备注〕{row[0]}"

        # ③ 同只的其他叫法
        try:
            others_alias = [a for a in (self.alias_of(pid) or []) if a != key]
        except Exception:
            others_alias = []
        view["aliases"] = self.alias_of(pid) or []
        if others_alias:
            view["other_aliases_hint"] = f"〔同只的其他叫法〕{'、'.join(others_alias[:8])}"

        # ④ 同名多形态：默认取种族和最高的，并点出「完全同名的是老形态」
        if len(hits) > 1:
            others = "、".join(f"{n}(#{i})" for i, n in hits[1:6])
            view["form_hint"] = (
                f"〔同名多形态〕「{key}」共匹配 {len(hits)} 个形态，"
                f"已按种族和最高取「{pname}」(#{pid})；其它候选：{others}"
                + ("…" if len(hits) > 6 else "")
            )
            exact = [(i, n) for i, n in hits if n == key]
            if exact and exact[0][0] != pid:
                view["exact_warn"] = (
                    f"〔注意〕名字完全同名的「{exact[0][1]}」是 #{exact[0][0]}"
                    f"（老形态，种族和较低），不是默认这个；"
                    f"PVP/配装语境一般用上面的最新形态。若玩家指的就是老的，"
                    f"用 pet {exact[0][1]} 或 ID 精确查。"
                )

        # ⑤ 种族值（含觉醒前后）
        both = self.pet_stats_both(pid) or {}
        view["stats"] = both.get("awakened") or both.get("base") or None
        view["stats_label_awakened"] = "awakened" in both
        if "awakened" in both and "base" in both:
            view["stats_base"] = both["base"]

        # ⑥ 魂印：多版本 + 羁绊强化 + 提到的专属效果
        marks = self.pet_soulmarks(pid) or []
        try:
            view["soulmark_forms"] = self.pet_soulmark_forms(pid) or []
        except Exception:
            view["soulmark_forms"] = []
        try:
            view["soulmark_bonus"] = self.pet_partner_upgrade(pid) or []
        except Exception:
            view["soulmark_bonus"] = []
        refs = []
        for m in marks:
            try:
                for eff in self.mentioned_effects(pid, m) or []:
                    refs.append({"name": eff.get("name"), "desc": eff.get("desc")})
            except Exception:
                pass
        view["soulmark_refs"] = refs

        # ⑦ 专属效果
        try:
            view["effects"] = self.pet_effects(pid) or []
        except Exception:
            view["effects"] = []
        # 去重（2026-11-06）：soulmark_refs 是「魂印文本里提到的专属名词」，
        # 而这些通常就是该精灵自己的专属名词 —— 两段会一字不差地重复显示。
        # 实例：SP 的「神魂之器」、柯尔霍德的「寂杀之魄/肃霜之禁」、神盖的 5 个「道」。
        _eff_names = {str(x.get("name")) for x in (view["effects"] or []) if x.get("name")}
        if _eff_names and view.get("soulmark_refs"):
            view["soulmark_refs"] = [
                x for x in view["soulmark_refs"]
                if str(x.get("name")) not in _eff_names
            ]

        # ⑧ 技能：全量 + 分组（详略交给呈现层）
        view["skills"] = self.pet_skills(pid, limit=40) or []
        try:
            view["skills_grouped"] = self.pet_skills_grouped(pid) or {}
        except Exception:
            view["skills_grouped"] = {}

        # ⑨ 图鉴 / 定型配招 / 玩家已确认规则
        view["intro"] = self.pet_intro(pid) or ""
        try:
            view["moveset"] = self.get_moveset(pid) or None
        except Exception:
            view["moveset"] = None
        try:
            view["pet_rules"] = self.facts_for_pet(pname) or []
        except Exception:
            view["pet_rules"] = []

        # ⑩ 数据不全警告（缺魂印/技能/属性之一 → 禁止编造细节）
        if (not marks) or (not view["skills"]) or (not view["types"]):
            view["sparse_warn"] = (
                "〔数据不全·回答纪律〕这只精灵本地库缺魂印/技能/属性之一，"
                "禁止编造具体数值、倍率、概率、判定顺序；"
                "只允许说「我库里对这只资料不全」，最多给一句方向性看法，"
                "并注明是个人看法、可能过时；需要细节就请玩家补充或确认。"
            )

        return view


    def _manual_pet_skills(self, pet_id: int, limit: int = 40) -> list[tuple]:
        """手册回退：LearnableMoves/ExtraMoves 给 ID，详情去 seerapi.skill 取。

        返回元组顺序与 pet_skills 里那条 SQL 完全一致，好让上层代码不用改。
        """
        mrow = self._manual_pet_row(pet_id)
        if not mrow:
            return []
        base_pairs = self._manual_move_ids(mrow[10]) if len(mrow) > 10 else []
        extra_pairs = self._manual_move_ids(mrow[11]) if len(mrow) > 11 else []
        # SpExtraMoves 不在 _manual_pet_row 的字段列表里（会打乱 New* 的索引），单独补一次。
        # 3311 就是靠这条才凑齐 31 个技能（29 + 1 + 1）。
        try:
            sp = self._manual_q(
                "pets", "select SpExtraMoves from pets where ID=? and isBook=1", (int(pet_id),)
            )
            if sp and sp[0][0]:
                extra_pairs += self._manual_move_ids(sp[0][0])
        except Exception:  # noqa: BLE001
            pass
        if not base_pairs and not extra_pairs:
            return []

        lv: dict[int, int] = {}
        fifth: set[int] = set()
        for sid, L in base_pairs:
            lv.setdefault(sid, L)
        for sid, L in extra_pairs:
            lv.setdefault(sid, L)
            fifth.add(sid)

        ids = list(lv.keys())[:150]
        ph = ",".join("?" * len(ids))
        rows = self._q(
            f"""select s.id, s.name, s.power, s.max_pp, s.accuracy, s.info,
                       e.name, s.priority, s.category_id
                from skill s
                left join element_type_combination e on e.id = s.type_id
                where s.id in ({ph})""",
            tuple(ids),
        )
        out = []
        for sid, name, power, pp, acc, info, type_name, priority, cat in rows:
            out.append(
                (name, power, pp, acc, info, type_name,
                 1 if sid in fifth else 0, priority, cat, lv.get(sid, 0))
            )
        # 与 pet_skills 的排序口径对齐：第五 → 属性技/先手 → 威力
        out.sort(key=lambda r: (-r[6], r[9] == 0, 0 if r[8] == 4 else (1 if (r[7] or 0) > 0 else 2),
                                -(r[7] or 0), -(r[1] or 0)))
        return out[:limit]

    def pet_skills(self, pet_id: int, limit: int = 40) -> list[dict[str, Any]]:
        """按「实战携带模板」排序：第五 → 先手 → 高威力攻击 → 属性技。

        ★ 为什么不能按威力排序（2026-11-02 踩过两次）：
          属性技威力恒为 0，按 `power desc` 会全部排到最末尾，再被 limit 截掉。
          而强化/免控/次免/吸血/弹控这些关键机制全在属性技里 ——
          柯尔霍德的「寒渊之殇」（弹控 + 下2回合攻击伤害额外+100%）就是这么被漏掉的，
          导致伤害被低估一倍以上。

        常规精灵的携带模板是「第五 + 两攻击 + 两属性」：
          两攻击 = 先手技能 + 高威力/功能性大招
          两属性 = 强化/免控/次免/吸血 的组合
        """
        # ① 手册优先（2026-11-06）：moves 是技能超集（27421 vs 27414），
        #    且 skills 列表来自 pets.LearnableMoves，与 seerapi.skillinpetorm 数量一致
        rows = self._manual_pet_skills(pet_id, limit)
        if not rows:
            # ② seerapi 回退
            rows = self._q(
                """select s.name, s.power, s.max_pp, s.accuracy, s.info, e.name,
                          l.is_fifth, s.priority, s.category_id, l.learning_level
                   from skillinpetorm l
                   join skill s on s.id=l.skill_id
                   left join element_type_combination e on e.id=s.type_id
                   where l.pet_id=?
                   order by
                     l.is_fifth desc,
                     l.learning_level=0 desc,
                     case when s.category_id=4 then 3
                          when s.priority>0 then 1
                          else 2 end,
                     s.priority desc,
                     s.power desc,
                     s.max_pp asc
                   limit ?""",
                (pet_id, limit),
            )
        return [
            {
                "name": r[0],
                "power": r[1],
                "pp": r[2],
                "accuracy": r[3],
                "info": (r[4] or "").strip(),
                # combination 表里双属性写作「远古 龙」，统一成「远古/龙」
                "type": (r[5] or "").replace(" ", "/").strip(),
                "fifth": bool(r[6]),
                "priority": r[7],
                "is_attr": (r[8] == 4),
                "level": r[9],  # 0 = 靠道具/兑换开启的后期补强技能
            }
            for r in rows
        ]

    # 技能词条的「固有 / 命中」标注藏在 analyze_info 的图标里（玩家确认，2026-10-28）：
    #   iconInherent = 固有（技能用出来就生效，不需要命中）
    #   iconHit      = 命中（必须命中才触发；被闪避/免伤/封属挡掉就不生效）
    #   iconComp     = 补充说明（条件类，如「对手处于能力提升状态时先制额外+1」）
    _EFFECT_LABELS = (
        ("iconInherent", "固有"),
        ("iconComp", "补充"),
        ("iconHit", "命中"),
    )

    @classmethod
    def _effect_label(cls, analyze_info: str) -> str:
        """从 analyze_info 的图标判断这条效果是固有还是命中。"""
        head = str(analyze_info or "")[:160]
        for key, label in cls._EFFECT_LABELS:
            if key in head:
                return label
        return ""

    def skill_effects(self, skill_id: int) -> list[str]:
        """技能效果原文（含具体数值），每条前面标〔固有〕或〔命中〕。

        注意：skill.info 有时是叙事化的模糊描述（"全属性都提升了"），
        真正的数值在 skill_effect_in_use.info 里（"全属性+1…"），必须走这张表。
        固有/命中的判定看 analyze_info 里的 sprite 图标，不要靠文案猜。
        """
        rows = self._q(
            """select u.info, u.analyze_info from skilleffectlink l
               join skill_effect_in_use u on u.id = l.effect_in_use_id
               where l.skill_id = ? order by l.effect_in_use_id""",
            (skill_id,),
        )
        out: list[str] = []
        for text, ai in rows:
            text = (text or "").strip()
            if not text:
                continue
            label = self._effect_label(ai)
            item = f"〔{label}〕{text}" if label else text
            if item not in out:
                out.append(item)
        return out

    def skill_effects_map(self, skill_ids: list[int]) -> dict[int, list[str]]:
        """一次查完多个技能的效果，避免逐个技能开连接。"""
        ids = [int(x) for x in skill_ids if x]
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = self._q(
            f"""select l.skill_id, u.info, u.analyze_info from skilleffectlink l
                join skill_effect_in_use u on u.id = l.effect_in_use_id
                where l.skill_id in ({marks}) order by l.skill_id, l.effect_in_use_id""",
            tuple(ids),
        )
        out: dict[int, list[str]] = {}
        for sid, info, ai in rows:
            text = (info or "").strip()
            if not text:
                continue
            label = self._effect_label(ai)
            item = f"〔{label}〕{text}" if label else text
            if item not in out.setdefault(sid, []):
                out[sid].append(item)
        return out

    def skill_detail(self, skill_id: int) -> dict[str, Any]:
        row = self._q(
            "select name, power, max_pp, accuracy, info, priority from skill where id=?", (skill_id,)
        )
        if not row:
            return {}
        r = row[0]
        return {
            "name": r[0],
            "power": r[1] or 0,
            "pp": r[2],
            "accuracy": r[3],
            "info": (r[4] or "").strip(),
            "priority": r[5],
            "effects": self.skill_effects(skill_id),
        }

    def pet_skills_grouped(self, pet_id: int) -> dict[str, list[dict[str, Any]]]:
        """按「配招习惯」分组，而不是按威力排序。

        赛尔号的实际规则：
        * 第五技能新旧只能带一个，一般都带**新**第五（有 advance_id 的那条）；
        * 本次进阶新增的技能（advance_id 非空）优先；
        * 其余技能看需求（控制/续航/先制），威力不是排序依据。
        """
        rows = self._q(
            """select s.id, s.name, s.power, s.advance_id, l.is_fifth, l.is_advanced,
                      s.info, l.learning_level, s.max_pp, s.priority, s.category_id,
                      (select count(distinct x.pet_id) from skillinpetorm x
                       where x.skill_id = s.id) as owners
               from skillinpetorm l join skill s on s.id=l.skill_id
               where l.pet_id=?""",
            (pet_id,),
        )
        def item(r):
            return {
                "id": r[0], "name": r[1], "power": r[2] or 0, "advance": r[3],
                "fifth": bool(r[4]), "advanced": bool(r[5]),
                "info": (r[6] or "").strip(), "level": r[7] or 0,
                "pp": r[8],
                # ★ 2026-11-05：必须带 priority，否则 LLM 看不出哪条是先手。
                #   实例：克雷弗德的「协议I·激」先制+3、威力90，因为 info 为空
                #   且输出不带 priority，被当成普通技能，bot 答成「就协议II·限一个先手」。
                "priority": r[9] or 0,
                # ★ category_id: 1=物理攻击 2=特殊攻击 4=属性技能
                #   判断「是不是攻击技能」必须用它，不能用 power>0——
                #   变威力技能（如沧岚某些技能）power 显示为 0，但确实是攻击技能。
                "category_id": r[10] or 0,
                # ★ 2026-11-06：owner 数 —— 1 = 独占（该精灵专属），>1 = 系列共用。
                #   玩家规则「只看独占技能组」：骁勇战魂/斗气护体这种英卡洛斯系列
                #   共用的技能（3 只都有）不该当候补，会误导配招。
                "owners": r[11] or 1,
            }

        effects_map = self.skill_effects_map([r[0] for r in rows])
        items = []
        for r in rows:
            it = item(r)
            it["effects"] = effects_map.get(r[0], [])
            items.append(it)
        fifths = [x for x in items if x["fifth"]]
        # 新第五：有 advance_id 的优先 → 学级高（后出的/觉醒后）→ 技能 id 大
        #
        # ★ 2026-11-08 补 level（阴灵魂姬踩出来的）：
        #   她有三条 is_fifth=1，学级不同：
        #     冥·十刀弑魂 #31391 学级76  ← 手册 ExtraMoves，真正要带的
        #     瑰·极天阴谕 #38462 学级71  ← 手册 ShowExtraMoves，转化产物
        #     瑰·伏心命朽 #38463 学级71  ← 同上
        #   原来只按 (advance, id) 排序 → id 最大的 38463 胜出，
        #   bot 于是推荐「瑰·伏心命朽」这个玩家根本带不了的转化产物，
        #   而 CLI（走手册 ExtraMoves）显示的是「冥·十刀弑魂」—— 两边打架。
        #   加上 level 后 76 > 71，十刀弑魂正确胜出。
        fifths.sort(key=lambda x: (1 if x["advance"] else 0, x["level"], x["id"]), reverse=True)
        new_fifth = fifths[0] if fifths else None
        old_fifth = fifths[1:] if len(fifths) > 1 else []

        new_skills = [x for x in items if not x["fifth"] and x["advance"]]
        rest = [x for x in items if not x["fifth"] and not x["advance"]]
        # ★ Lv0 技能单独拆出来（2026-11-03）
        #   Lv0 = 靠道具/兑换开启的，不是升级学的，基本都是后期补强
        #   （王·联盟审判 威350、魔·黑天暗罚 威350、零时诳语 改魂印…）
        #   这些技能如果混在 legacy 里按等级倒序排，永远沉在列表最末尾，注意不到。
        #
        # ★ 2026-11-06 修正：Lv0 必须**不看 advance**，从全部技能里取。
        #   原写法 lv0 = [x for x in rest ...] 只从「无 advance」里取，
        #   导致「觉醒给的 Lv0 技能」全被分进 new_skills，lv0 组永远是空的。
        #   实例：圣光莫妮卡 #3711 觉醒给的新先手/新属性/新大招全是
        #   Lv0+进阶（万籁悦性 威90 / 繁嚣此寂 / 王·鸾歌余音 威150），
        #   圣光格劳瑞 #4004 同理（神驰万钧 威85 / 辉荣不泯 / 王·天冕奉世）。
        #   觉醒的规律就是「新第五 + 新先手 + 新属性 + 新大招，替换 4 个，
        #   旧技能只留一个可选」，这些新技能几乎都是 Lv0 或第五。
        lv0 = [x for x in items if not x["fifth"] and x["level"] == 0]
        lv0_ids = {x["id"] for x in lv0}
        new_skills = [x for x in new_skills if x["id"] not in lv0_ids]
        legacy = [x for x in rest if x["level"] != 0]
        # 旧技能按学习等级倒序（越晚学越常用），不按威力
        legacy.sort(key=lambda x: (-x["level"], -x["id"]))
        # Lv0 也按 id 倒序（后出的补强优先）
        lv0.sort(key=lambda x: -x["id"])
        return {
            "fifth_new": [new_fifth] if new_fifth else [],
            "fifth_old": old_fifth,
            "new_skills": new_skills,
            "lv0": lv0,
            "legacy": legacy,
        }

    def gem_search(self, keyword: str, limit: int = 10) -> list[dict[str, str]]:
        """查宝石效果。

        关键：gem 表本身的 skill_effect_in_use_id 全是 NULL，
        真正的关联在 gemeffectlink（gem_id → skill_effect_in_use_id），
        不走这张表就会以为「库里没有宝石效果」。
        """
        kw = str(keyword or "").strip()
        if not kw:
            return []
        rows = self._q(
            """select g.name, g.level, gc.name, u.info, u.args
               from gem g
               left join gemeffectlink l on l.gem_id = g.id
               left join skill_effect_in_use u on u.id = l.skill_effect_in_use_id
               left join gem_category gc on gc.id = g.category_id
               where g.name like ? or u.info like ?
               order by g.category_id, g.level
               limit ?""",
            (f"%{kw}%", f"%{kw}%", limit),
        )
        return [
            {
                "gem": r[0],
                "level": r[1],
                "category": r[2] or "",
                "effect": (r[3] or "").strip(),
                "args": r[4] or "",
            }
            for r in rows
        ]

    # ---- 赛尔专门词自动注释 -------------------------------------------------
    # 魂印/技能原文里大量使用「之后/然后/以/未触发/达到/首次/当回合…」这类词，
    # 它们的赛尔语义与日常中文差别很大（例：之后=持续存在而非「接下来」；
    # 然后=无因果独立结算；以=因果关联，前不成立则后不执行）。模型若按日常
    # 语感读，结论会反。这里在返回原文时把命中的词连同官方解释一起给出。
    _ANNOTATE_KINDS = {4: "语法", 5: "时点", 6: "通用效果"}
    # 仅这些词值得提示（避免把「己方」「吸取」这类不易误解的词也堆进来）
    _ANNOTATE_WATCH = (
        # 多字词：语义与日常中文差得远，且不会误匹配
        "之后", "然后", "执行", "未触发", "达到", "当回合", "首次", "保留",
        "改变", "反馈", "视为", "响应", "同时", "依次", "额外行动",
        "战斗阶段结束时", "回合结束后", "回合开始时", "受到攻击后",
        "被击败", "击败", "登场时", "致死伤害", "致死攻击伤害",
        "秒杀", "本系加成", "放逐", "支援", "危机感", "转化", "先制",
        "必定命中", "回合类效果", "致死伤害",
        # 注意：不含「固定伤害/百分比伤害/真实伤害」——这三个词义自明，
        # 解释它们只会把输出撑长（实测每条白占约 40 字）。
    )
    # 单字词（以/起/承/转/合）极易被子串误命中（「合」会撞到「回合结束后」），
    # 因此不进自动注释，只在文末给一句总提醒。
    _ANNOTATE_HINT = (
        "【读法提醒】赛尔语法里「之后」＝从该时点起持续存在（未说明结束则持续到下场），"
        "**不是「接下来」**；「然后」＝与前文同一时点、**无因果**独立结算，"
        "前文成败不影响后文；而「**以**」是**因果关联词**——前文未触发则后文也不执行。"
        "「首次」默认指**每场战斗**首次，不是每回合。原文若含这些词且结论关键，"
        "先 glossary_search 查词条再下判断。"
    )

    def annotate_terms(self, text: str, max_n: int = 10) -> list[str]:
        """扫描原文，返回命中的赛尔专门词解释（语法/时点/通用效果）。"""
        if not text:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for w in self._ANNOTATE_WATCH:
            if w in seen or w not in text:
                continue
            row = None
            for kind in (4, 5, 6):
                try:
                    row = self._q(
                        "select name, desc from glossary_entry where name=? and kind=? limit 1",
                        (w, kind),
                    )
                except Exception:
                    row = None
                if row:
                    break
            if not row:
                continue
            seen.add(w)
            label = self._ANNOTATE_KINDS.get(kind, "术语")
            desc = (row[0][1] or "").strip().replace("\n", " ")
            if len(desc) > 150:
                desc = desc[:150] + "…"
            out.append(f"·【{row[0][0]}】({label}) {desc}")
            if len(out) >= max_n:
                break
        # 白名单只覆盖常见词；再扫一遍全量词条补漏（按名字长度降序，长词优先，
        # 避免「之后」这种短词把「之后的效果」吃掉）。只取语法/时点/通用名词三类。
        if len(out) < max_n:
            try:
                extra = self.glossary_terms_in(
                    text, min_len=2, max_n=max_n * 3, only_kinds={4, 5, 6}
                )
            except Exception:
                extra = []
            for it in extra:
                nm = str(it.get("name") or "")
                if not nm or nm in seen:
                    continue
                seen.add(nm)
                desc = str(it.get("desc") or "").strip().replace("\n", " ")
                if len(desc) > 150:
                    desc = desc[:150] + "…"
                out.append(f"·【{nm}】({self._ANNOTATE_KINDS.get(it.get('kind'), '术语')}) {desc}")
                if len(out) >= max_n:
                    break
        out.append(self._ANNOTATE_HINT)
        return out


    # ---- 功能标签：按精灵查覆盖 / 按标签查精灵内 ---------------------------------
    def _pet_skill_ids(self, pet_name: str) -> tuple[int, str, list[int]]:
        """解析精灵名 → (pet_id, 正式名, skill_id 列表)。找不到返回 (0, "", [])。"""
        hits = self.find_pets(pet_name, 1)
        if not hits:
            return 0, "", []
        pid, pname = hits[0]
        try:
            rows = self._q("select skill_id from skillinpetorm where pet_id=?", (pid,))
        except Exception:
            rows = []
        return pid, pname, [int(r[0]) for r in rows]

    def _pet_soulmark_ids(self, pid: int) -> list[int]:
        try:
            rows = self._q("select soulmark_id from petsoulmarklink where pet_id=?", (pid,))
        except Exception:
            return []
        return [int(r[0]) for r in rows]

    # ────────────────────────────────────────────────────────────
    # 语义层：tag_view —— 功能标签的唯一判定源
    #
    # bot 的 seer_tag 支持三种模式（全库反查 / 精灵覆盖 / 精灵×标签），
    # CLI 的 cmd_tags 只有「精灵覆盖」一种。现在统一到 tag_view，
    # CLI 补上另外两种，两边的模式判定和空结果提示都只在这里写一次。
    # ────────────────────────────────────────────────────────────
    def tag_view(self, tag: str = "", pet: str = "") -> dict[str, Any]:
        """功能标签的语义层。

        mode:
          "intersect"  pet + tag  → 这只精灵身上带该标签的技能/魂印
          "coverage"   pet 只有    → 精灵的功能覆盖总览（含空白项）
          "reverse"    tag 只有    → 全库反查
          "none"       都没有      → 返回可用标签提示

        判定字段：empty_hint（任何空结果/用法提示）、missing（精灵的空白标签项）
        """
        tg = str(tag or "").strip()
        pt = str(pet or "").strip()
        view: dict = {
            "mode": "none",
            "tag": tg,
            "pet": pt,
            "pname": None, "pid": None,
            "rows": [],
            "coverage": None,
            "total": 0,
            "empty_hint": None,
            "missing": [],
        }

        # ③ 精灵 × 标签
        if pt and tg:
            view["mode"] = "intersect"
            try:
                pname, rows = self.tag_in_pet(tg, pt)
            except Exception:
                pname, rows = None, []
            if not pname:
                view["empty_hint"] = f"未收录精灵「{pt}」。"
                return view
            view["pname"] = pname
            view["rows"] = [{"kind": k, "name": n} for k, n in (rows or [])]
            if not view["rows"]:
                view["empty_hint"] = f"【{pname}】没有带「{tg}」标签的技能或魂印。"
            return view

        # ② 只给精灵：功能覆盖
        if pt:
            view["mode"] = "coverage"
            try:
                cov = self.tag_coverage(pt)
            except Exception:
                cov = None
            if not cov:
                view["empty_hint"] = f"未收录精灵「{pt}」。"
                return view
            view["pname"] = cov.get("pet")
            view["pid"] = cov.get("id")
            view["coverage"] = cov
            view["missing"] = list(cov.get("missing") or [])
            if not (cov.get("skills") or cov.get("soulmarks")):
                view["empty_hint"] = "这只精灵的技能/魂印在 tags 表里没有标签记录。"
            return view

        # ① 只给标签：全库反查
        if tg:
            view["mode"] = "reverse"
            try:
                total, rows = self.tag_search(tg, 15)
            except Exception:
                total, rows = 0, []
            view["total"] = int(total or 0)
            view["rows"] = [{"kind": k, "name": n, "extra": x} for k, n, *rest in
                            [(r[0], r[1], r[2] if len(r) > 2 else None) for r in (rows or [])]
                            for x in [rest[0] if rest else None]]
            if not view["total"]:
                try:
                    common = "、".join(t for t, _ in self.tag_stats()[:10])
                except Exception:
                    common = ""
                view["empty_hint"] = f"没有标签「{tg}」。常用标签：{common}"
            return view

        # 都没有
        try:
            common = "、".join(t for t, _ in self.tag_stats()[:12])
        except Exception:
            common = ""
        view["empty_hint"] = (
            f"请给 tag 或 pet。常用标签：{common}；也可以只给 pet 查它的功能覆盖。"
        )
        return view

    def tag_coverage(self, pet_name: str) -> dict[str, Any] | None:
        """某精灵技能池＋魂印的功能标签覆盖。

        返回 {"pet": 名称, "id": pid, "skills": {tag: [技能名…]}, "soulmarks": {tag: [魂印#id…]},
              "missing": [19 种标签里完全没覆盖的]}
        这是配招/定位分析最快的入口：一眼看出这只精灵在哪些功能上有覆盖、哪项是空白。
        """
        pid, pname, sids = self._pet_skill_ids(pet_name)
        if not pid:
            return None
        out: dict[str, Any] = {"pet": pname, "id": pid, "skills": {}, "soulmarks": {}}
        if sids:
            q = ",".join("?" * len(sids))
            try:
                for tag, nm in self._kb.execute(
                    f"select tag, name from tags where kind='skill' and ref_id in ({q})", sids
                ):
                    out["skills"].setdefault(str(tag), []).append(str(nm or ""))
            except Exception:
                pass
        smids = self._pet_soulmark_ids(pid)
        if smids:
            q = ",".join("?" * len(smids))
            try:
                for tag, ref in self._kb.execute(
                    f"select tag, ref_id from tags where kind='soulmark' and ref_id in ({q})", smids
                ):
                    out["soulmarks"].setdefault(str(tag), []).append(f"魂印#{ref}")
            except Exception:
                pass
        try:
            all_tags = [t for t, _ in self.tag_stats()]
        except Exception:
            all_tags = []
        out["missing"] = [t for t in all_tags if t not in out["skills"] and t not in out["soulmarks"]]
        return out

    def tag_in_pet(self, tag: str, pet_name: str, limit: int = 30):
        """某精灵身上带指定标签的技能/魂印。返回 (精灵名, [(kind, name)])。"""
        pid, pname, sids = self._pet_skill_ids(pet_name)
        if not pid:
            return "", []
        rows: list[tuple[str, str]] = []
        if sids:
            q = ",".join("?" * len(sids))
            try:
                for nm in self._kb.execute(
                    f"select name from tags where kind='skill' and tag like ? and ref_id in ({q})",
                    [f"%{tag}%"] + sids,
                ):
                    rows.append(("技能", str(nm[0] or "")))
            except Exception:
                pass
        smids = self._pet_soulmark_ids(pid)
        if smids:
            q = ",".join("?" * len(smids))
            try:
                for ref in self._kb.execute(
                    f"select ref_id from tags where kind='soulmark' and tag like ? and ref_id in ({q})",
                    [f"%{tag}%"] + smids,
                ):
                    rows.append(("魂印", f"魂印#{ref[0]}"))
            except Exception:
                pass
        return pname, rows[:limit]


    # ---- 精灵王识别 ---------------------------------------------------------
    # 判据（玩家口径，2026-10-29）：
    #   **主判据 = 第五技能是否带「攻击时造成的伤害不会出现微弱（克制关系为微弱时都变成普通）」**
    #   有 → 就是精灵王；没有 → 不是。
    #   **辅助 = 本名是否在 king_roster（已核定的 63 条名册）里**，用来给出身份
    #   （原生王 / 继承者 / 合体）以及提示"该属性有几支血脉"。
    # 特例：**普通系**对全属性都是 ×1.0、本来就没有微弱，策划会删掉这条效果
    #   （例：欧德奈瑞#3340 带、非凡·欧德内里#3807 不带），所以普通系只能靠名册确认。
    KING_STRONG = ("不会出现微弱", "克制关系为微弱时都变成普通")
    KING_EFFECT_ID = 760   # 权威代码：skill_effect_type.id=760 ＝「攻击时造成的伤害不会出现微弱」

    def _fifth_has_no_weak(self, pet_id: int) -> tuple[bool, str]:
        """第五技能是否带「伤害不会出现微弱」。返回 (是否, 技能名)。"""
        try:
            rows = self._q(
                "select s.id, s.name from skillinpetorm l"
                " join skill s on s.id=l.skill_id where l.pet_id=? and l.is_fifth=1",
                (pet_id,),
            )
        except Exception:
            return False, ""
        for sid, sname in rows:
            try:
                effs = self._q(
                    "select u.info from skilleffectlink k"
                    " join skill_effect_in_use u on u.id=k.effect_in_use_id where k.skill_id=?",
                    (sid,),
                )
            except Exception:
                continue
            if any(any(w in str(e[0] or "") for w in self.KING_STRONG) for e in effs):
                return True, str(sname or "")
        return False, ""

    def king_status(self, pet_id: int) -> dict[str, Any]:
        """判断是否精灵王。主判据看第五的「不微弱」，名册只用来确认身份。

        返回 {"is_king", "role", "why", "known", "types"}
          known=True 表示本名已在 king_roster（身份已核定）。
        """
        out: dict[str, Any] = {"is_king": False, "role": "", "why": [], "known": False, "types": []}
        try:
            out["types"] = list(self.pet_types(pet_id) or [])
        except Exception:
            pass
        strong, sname = self._fifth_has_no_weak(pet_id)
        role = ""
        try:
            row = self._kb.execute(
                "select role, note from king_roster where pet_id=?", (pet_id,)
            ).fetchone()
            if row:
                role = str(row[0] or "")
                out["known"] = True
        except Exception:
            pass

        if role:
            out["role"] = role
        if strong:
            out["is_king"] = True
            out["why"].append(f"第五「{sname}」带「伤害不会出现微弱」")
        is_normal = any(t == "普通" for t in out["types"])
        if out["is_king"]:
            if not role:
                out["role"] = "精灵王系（新形态·名册未收录，血脉待确认）"
                out["why"].append("→ 判据成立但名册没有，属新出现的精灵王，可告知玩家并建议补录")
            return out
        if role:
            # 名册有但第五没这条：普通系特例或该形态数据缺失
            out["is_king"] = True
            out["why"].append(
                "名册已核定，但第五未带「不微弱」"
                + ("（普通系特例：本身无微弱关系，策划会删）" if is_normal else "（该形态可能数据缺失）")
            )
            return out
        out["why"].append("第五不带「伤害不会出现微弱」，名册也没有 → 不是精灵王")
        if is_normal:
            out["why"].append("（普通系注意：不能只靠「不微弱」判断，需查名册）")
        return out

    # ---- 属性克制系数（官方公式：单/双属性四种组合） --------------------------
    # ---- 克制问句自动解析：把「A打B几倍」直接算好，不依赖模型调工具 ----------
    # 常规属性（不含「王」）：「王」在玩家话里高频出现在「X王」俗称里
    # （龙王/火王/电王…），直接纳入会误判，所以走下面的兜底。
    TYPE_NAMES = ("草", "水", "火", "飞行", "电", "机械", "地面", "普通", "冰", "超能",
                  "战斗", "光", "暗影", "神秘", "龙", "圣灵", "次元", "远古",
                  "邪灵", "自然", "混沌", "神灵", "轮回", "虫", "虚空")
    # 「王」是真实属性（element_type 221，重生之翼/王·萨格罗斯/始皇帝·嬴政 等 6 只），
    # 有完整克制表（王→战斗·暗影·次元·邪灵 ×2；地面·神秘·自然→王 ×2）。
    # 规则：先用常规属性解析，**某侧解析为空**时才启用「王」兜底，
    # 这样「龙王/火王」不会被拆成「龙+王」，「王系/王打XX」仍能算。
    TYPE_FALLBACK = ("王",)
    # 算覆盖度时要从两侧剥离的"非属性字"：疑问词、量词、助词、套话
    _TYPE_WORDS = ("怎么样", "多少倍", "是几倍", "几倍", "一下", "请问", "属性", "技能",
                   "精灵", "攻击", "克制", "多少", "怎么", "什么", "如何", "倍", "系",
                   "打", "克", "碰", "遇", "的", "是", "了", "呢", "吗", "啊", "嘛",
                   "问", "算", "？", "?", "，", ",", "。", "、")
    # 注意：**不能**把「能」放进剥离表——「超能」会被剥成「超」，覆盖率归零，
    # 导致「王系打超能」这类问句被误判为非属性问句。

    def _greedy_types(self, s: str, fallback: bool = False) -> list[str]:
        """从一段文字里贪心匹配属性名（长名优先，跳过多余字）。

        fallback=True 时额外把「王」纳入候选（只在常规属性解析为空时使用）。
        """
        pool = list(self.TYPE_NAMES) + (list(self.TYPE_FALLBACK) if fallback else [])
        names = sorted(pool, key=len, reverse=True)
        out, i = [], 0
        while i < len(s):
            hit = None
            for n in names:
                if s.startswith(n, i):
                    hit = n
                    break
            if hit:
                if hit not in out:
                    out.append(hit)
                i += len(hit)
            else:
                i += 1
        return out

    def parse_type_query(self, msg: str) -> tuple[list[str], list[str]] | None:
        """从「自然圣灵打电几倍」这类问句里解析出 (攻击方属性, 防守方属性)。

        只在句子里出现「打/攻击/克/碰/遇」等动词时工作，失败返回 None。
        """
        txt = str(msg or "")
        if not txt:
            return None
        for sep in ("打", "攻击", "克制", "克", "碰", "遇"):
            if sep not in txt:
                continue
            left, _, right = txt.partition(sep)
            atk = self._greedy_types(left)[-2:]     # 取靠近动词的那 1~2 个
            dfd = self._greedy_types(right)[:2]
            if not atk:                              # 该侧没有常规属性 → 试「王」
                atk = self._greedy_types(left, fallback=True)[-2:]
            if not dfd:
                dfd = self._greedy_types(right, fallback=True)[:2]
            if atk and dfd:
                return atk, dfd
        return None

    def type_query_answer(self, msg: str) -> str:
        """给「A打B几倍」直接产出结论文本；不是克制问句时返回空串。

        前置排除：句中若出现**精灵名/俗称**（古龙、龙王、火王…），
        这些名字里的字正好是属性名单字（龙/王/火），强行解析会得出错误结论
        （例：「古龙打龙王」会被解成 龙→龙/王）。此时返回空串，
        交给模型用 seer_type_chart / seer_pet 查真实对象。
        """
        parsed = self.parse_type_query(msg)
        if not parsed:
            return ""
        # 用「覆盖度」判断这一侧到底是属性写法还是精灵名：
        #   战斗龙 → [战斗,龙]   = 3/3 = 100% → 属性写法
        #   冰暗影 → [冰,暗影]   = 3/3 = 100% → 属性写法
        #   古龙   → [龙]        = 1/2 =  50% → 有非属性字 → 是精灵名
        #   （「王」已从 TYPE_NAMES 剔除，所以「龙王」只剩 [龙] = 50% → 精灵名）
        txt = str(msg or "")
        for _sep in ("打", "攻击", "克制", "克", "碰", "遇"):
            if _sep in txt:
                _l, _, _r = txt.partition(_sep)
                break
        else:
            return ""
        def _cov(side, types):
            for _w in sorted(self._TYPE_WORDS, key=len, reverse=True):
                side = side.replace(_w, "")
            if not side:
                return 1.0
            used = 0
            tmp = side
            for t in sorted(types, key=len, reverse=True):
                if t in tmp:
                    used += len(t)
                    tmp = tmp.replace(t, "", 1)
            return used / len(side)
        if _cov(_l, parsed[0]) < 0.8 or _cov(_r, parsed[1]) < 0.8:
            return ""   # 这一侧掺了非属性字 → 当精灵名处理，交给工具查
        atk, dfd = parsed
        try:
            detail = self.type_multiplier_detail(atk, dfd, "seer", 0.0)
        except Exception:
            return ""
        mult = detail.get("mult")
        if mult is None:
            return ""
        pairs = detail.get("pairs") or []
        lines = [
            f"【本条是克制计算·已由系统按官方公式算好，直接采信，不要凭记忆改口】",
            f"{'/'.join(atk)} → {'/'.join(dfd)} = **×{mult}**"
            + ("（完全免疫）" if mult == 0 else ""),
            "拆解：" + "｜".join(f"{a}→{d} ×{m:g}" for a, d, m in pairs),
        ]
        vals = [m for _, _, m in pairs]
        if len(vals) == 1:
            lines.append("单属性技能 → 单属性：查表直接取用。")
        elif len(vals) == 2:
            if vals[0] == 2 and vals[1] == 2:
                lines.append("**双2特例：两项都是 ×2 → 相加得 ×4（不除以2）**。"
                             "这是最容易被凭记忆答成 ×2 的地方。")
            elif 0 in vals:
                lines.append(f"含免疫(×0) → 相加 ÷4 = ×{mult}")
            else:
                lines.append(f"一般情况 → 相加 ÷2 = ×{mult}")
        else:
            cs = detail.get("contribs") or []
            lines.append(
                "双属性技能 → 双属性：拆**防守方**，各自按「双→单」算出后再平均"
                + ("（各系结算：" + "、".join(f"×{c}" for c in cs) + "）" if cs else "")
            )
        if detail.get("missing"):
            lines.append("⚠ 库中缺这些关系：" + "、".join(detail["missing"]))
        return "\n".join(lines)


    def type_mult(self, atk_ids, def_ids) -> dict[str, Any]:
        """按官方公式计算克制系数。atk_ids / def_ids：属性 id 列表（1 或 2 个）。

        规则（务必区分四种组合，别混）：
          单→单：查表直接用
          双→单：拆**攻击方**，两系数组合（双2得a+b；含0得(a+b)/4；其他(a+b)/2）
          单→双：拆**防守方**，同样三分类
          双→双：拆**防守方**，分别算出「双→单」的系数1、系数2，最后 (c1+c2)/2（不再分类）
        返回 {"mult", "steps", "atk", "def"}
        """
        def raw(s, t):
            r = self._q(
                "select multiple from elementtyperelationorm where source_id=? and target_id=?",
                (s, t),
            )
            return float(r[0][0]) if r else None

        def combine(a, b, tag=""):
            """两系数三分类：双2→a+b；含0→(a+b)/4；其他→(a+b)/2"""
            if a is None or b is None:
                return None, f"{tag}查表缺关系"
            if a == 2 and b == 2:
                return a + b, f"{tag}双2 → ({a:g}+{b:g})÷1={a + b:g}"
            if a == 0 or b == 0:
                return (a + b) / 4, f"{tag}含0 → ({a:g}+{b:g})÷4={(a + b) / 4:g}"
            return (a + b) / 2, f"{tag}其他 → ({a:g}+{b:g})÷2={(a + b) / 2:g}"

        atk = [int(x) for x in (atk_ids or []) if x is not None]
        dfn = [int(x) for x in (def_ids or []) if x is not None]
        out: dict[str, Any] = {"mult": None, "steps": [], "atk": atk, "def": dfn}
        if not atk or not dfn:
            out["steps"].append("属性为空，无法计算")
            return out

        # 单 → 单
        if len(atk) == 1 and len(dfn) == 1:
            v = raw(atk[0], dfn[0])
            out["mult"] = v
            out["steps"].append(f"单→单：直接查表 = {v}")
            return out

        # 双 → 单（拆攻击方）
        if len(atk) == 2 and len(dfn) == 1:
            v, why = combine(raw(atk[0], dfn[0]), raw(atk[1], dfn[0]), "双→单 ")
            out["mult"] = v
            out["steps"].append(why)
            return out

        # 单 → 双（拆防守方，仍是三分类，不是平均！）
        if len(atk) == 1 and len(dfn) == 2:
            v, why = combine(raw(atk[0], dfn[0]), raw(atk[0], dfn[1]), "单→双 ")
            out["mult"] = v
            out["steps"].append(why)
            return out

        # 双 → 双（拆防守方，先各算「双→单」，再平均，不再分类）
        c1, w1 = combine(raw(atk[0], dfn[0]), raw(atk[1], dfn[0]), f"打{dfn[0]} ")
        c2, w2 = combine(raw(atk[0], dfn[1]), raw(atk[1], dfn[1]), f"打{dfn[1]} ")
        out["steps"].extend([w1, w2])
        if c1 is None or c2 is None:
            out["steps"].append("存在查表缺失，无法给出最终系数")
            return out
        out["mult"] = (c1 + c2) / 2
        out["steps"].append(
            f"双→双 → ({c1:g}+{c2:g})÷2 = {out['mult']:g}（拆防守方，不再分类）"
        )
        return out

    # ---- 最优系别（官方 #489）--------------------------------------------
    # 规则来源：kb.facts「最优系别·候选集合到底怎么构成」等 3 条（2026-10-31 玩家核定）
    #
    # 候选集合：
    #   攻击方视角 = 对手在场精灵 + 对方上只在场精灵，每只展开成「组合 + 各子系别」
    #                → 双对手共 6 个候选，取克制系数**最高**
    #   受击方视角 = 自身系别的「组合 + 各子系别」= 3 个候选，取**最低**
    #                （受击方向不把对方上个精灵纳入候选）
    #
    # 关键：组合本身必须进候选。单系攻击方有 98 例「组合倍率 > max(子系别)」，
    #       例：虚空→神秘/战斗：组合 4.0，子系别 (2.0, 2.0)。
    #       漏掉组合会导致攻击方选不到最大值、受击方漏掉该避开的值。
    def type_expand_candidates(self, types: list[str]) -> list[list[str]]:
        """把一组系别展开成候选：组合本身 + 各子系别。

        输入 ['神秘','战斗'] → [['神秘/战斗'], ['神秘'], ['战斗']]
        输入 ['神秘']       → [['神秘']]
        """
        ts = [t for t in (types or []) if t]
        if not ts:
            return []
        out: list[list[str]] = []
        if len(ts) >= 2:
            out.append(list(ts[:2]))          # 组合本身
            for t in ts[:2]:
                out.append([t])                # 各子系别
        else:
            out.append([ts[0]])
        return out

    def optimal_type_mult(
        self,
        atk_types: list[str],
        groups: list[list[str]],
        pick: str = "max",
    ) -> dict[str, Any]:
        """最优系别克制计算。

        Args:
            atk_types: 攻击方（技能）系别，如 ['自然','圣灵']
            groups:    候选来源，每个元素是一组系别（会展开成组合+子系别），
                       如 [['机械','草'], ['混沌','战斗']] → 6 个候选
            pick:      'max'（攻击方视角）/ 'min'（受击方视角）

        返回 {"mult", "pick", "candidates":[{types,mult}], "chosen", "steps"}
        """
        atk = [str(x) for x in (atk_types or []) if x]
        if not atk or not groups:
            return {"mult": None, "pick": pick, "candidates": [], "chosen": None,
                    "steps": ["参数为空"]}
        cands: list[dict[str, Any]] = []
        for g in groups:
            for cand in self.type_expand_candidates(list(g)):
                ids = [self._type_id(c) for c in cand]
                if any(i is None for i in ids):
                    continue
                atk_ids = [self._type_id(a) for a in atk]
                if any(i is None for i in atk_ids):
                    continue
                r = self.type_mult(atk_ids, ids)
                if r.get("mult") is None:
                    continue
                cands.append({"types": "/".join(cand), "mult": r["mult"]})
        if not cands:
            return {"mult": None, "pick": pick, "candidates": [], "chosen": None,
                    "steps": ["候选取值为空"]}
        key = (lambda c: c["mult"]) if pick == "max" else (lambda c: -c["mult"])
        chosen = sorted(cands, key=key, reverse=True)[0] if pick == "max" else sorted(cands, key=lambda c: c["mult"])[0]
        steps = [f"{'/'.join(atk)} 视角，候选 {len(cands)} 个，取{'最高' if pick=='max' else '最低'}："]
        steps += [f"  {c['types']} → ×{c['mult']:g}" for c in cands]
        steps.append(f"选定 {chosen['types']} → ×{chosen['mult']:g}")
        return {"mult": chosen["mult"], "pick": pick, "candidates": cands,
                "chosen": chosen["types"], "steps": steps}

    def _type_id(self, name: str):
        """系别名（含双属性写法「A/B」「A·B」）→ element_type_combination/element_type 的 id。"""
        nm = str(name or "").strip()
        if not nm:
            return None
        for sep in ("/", "·", "／"):
            if sep in nm:
                parts = [p.strip() for p in nm.split(sep) if p.strip()]
                if len(parts) == 2:
                    row = self._q(
                        "select id from element_type_combination where name=?",
                        (f"{parts[0]}/{parts[1]}",),
                    ) or self._q(
                        "select id from element_type_combination where name=?",
                        (f"{parts[1]}/{parts[0]}",),
                    )
                    if row:
                        return int(row[0][0])
                nm = parts[0] if parts else nm
        row = self._q("select id from element_type where name=?", (nm,))
        return int(row[0][0]) if row else None

    # ---- 术语自动识别：从文本里找出官方词条（不依赖模型自觉） ----------------
    _KIND_LABEL = {
        1: "专属名词·归一化描述（范围/主体可能与具体魂印原文有出入，判某只精灵时以魂印原文为准）",
        2: "异常状态",
        3: "伤害类型",
        4: "语法定义",
        5: "时点定义",
        6: "官方通用效果名词",
    }
    _GLOSSARY_SKIP = {"待补"}

    def glossary_terms_in(self, text: str, *, min_len: int = 2,
                          max_n: int = 8, only_kinds=None) -> list[dict]:
        """扫描一段文本，返回其中出现的官方词条定义。

        - **跳过单字词条**（刻/辛/幕/烛/以/组/起/承/转/合 等，子串误匹配极严重）
        - **跳过占位词条**「待补」
        - 按名字长度降序匹配，避免短词先吃掉长词
        - only_kinds: 只保留这些 kind（如 {4,5,6} 只看语法/时点/通用名词）
        """
        txt = str(text or "")
        if not txt:
            return []
        try:
            rows = self._q(
                "select id,name,desc,kind from glossary_entry "
                "where length(name)>=? order by length(name) desc",
                (int(min_len),),
            )
        except Exception:
            return []
        out: list[dict] = []
        used: set[str] = set()
        for gid, name, desc, kind in rows:
            name = str(name or "")
            if not name or name in self._GLOSSARY_SKIP or name in used:
                continue
            if only_kinds is not None and kind not in only_kinds:
                continue
            if name not in txt:
                continue
            used.add(name)
            out.append({
                "id": gid, "name": name, "kind": kind,
                "kind_label": self._KIND_LABEL.get(kind, f"kind{kind}"),
                "desc": str(desc or ""),
            })
            if len(out) >= max_n:
                break
        return out


    def term_hits(self, text: str, max_n: int = 4) -> list[dict[str, Any]]:
        """文本里出现的基础术语 → [{name, desc, src}]。

        ★ 2026-11-04 重建：main.py 的 enforce_local_source 一直在调
          `self.kb.term_hits(msg, 4)`，但这个方法从来没被定义过
          （全仓库 + 100 多个备份都搜不到 `def term_hits`）。
          因为外层 try 只 catch + logger.debug，异常被完全吞掉，
          结果从这一行往后的所有注入（基础概念、精灵王判定、
          以及整段「数据源纪律」40 多行）**全部静默失效**。

        这里基于 glossary_terms_in 实现：它已经做了单字跳过、占位跳过、
        长词优先匹配，直接复用即可。src 用词条类别标注来源。
        """
        try:
            terms = self.glossary_terms_in(str(text or ""), max_n=max_n)
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for t in terms or []:
            name = str(t.get("name") or "").strip()
            if not name:
                continue
            out.append({
                "name": name,
                "desc": str(t.get("desc") or "").strip(),
                "src": str(t.get("kind_label") or "官方词条"),
            })
        return out

    def skill_type_name(self, type_id) -> str:
        """技能属性名：手册 skillTypes → element_type_combination → element_type。

        注意：skill.type_id 引用的是 **element_type_combination**（139 条，含双属性），
        不是 element_type（26 条单属性）。早前误 join element_type，
        导致约 32% 的技能查不到属性、前端显示「未知」。
        """
        if type_id is None:
            return ""
        try:
            tid = int(type_id)
        except (TypeError, ValueError):
            return ""
        rows = self._manual_q("skillTypes", "select cn, is_dou from skillTypes where id=?", (tid,))
        if rows and rows[0][0]:
            return str(rows[0][0]).replace(" ", "/").strip()
        rows = self._q("select name from element_type_combination where id=?", (tid,))
        if rows and rows[0][0]:
            return str(rows[0][0]).replace(" ", "/").strip()
        rows = self._q("select name from element_type where id=?", (tid,))
        return str(rows[0][0]) if rows else ""

    # ────────────────────────────────────────────────────────────
    # 语义层：skill_view —— 技能详情的唯一判定源
    #
    # 两边原来的差异：
    #   · bot 显示攻击次数(atk_num)、暴击率(crit_rate)，CLI 没有
    #   · CLI 有「这是精灵不是技能」的引导，bot 没有（它只报未收录）
    #   · bot 另外还会叠玩家规则/术语标注/词条块（那是 bot 的增强层，留在呈现层）
    # 判定（尤其是「查不到时该说什么」）统一到这里。
    # ────────────────────────────────────────────────────────────
    @staticmethod
    def _extract_combo_hint(effects: list, info: str = "") -> dict[str, Any] | None:
        """从技能效果里抽连击信息。

        为什么需要：连击技能的次数经常带修正，直接用裸数字会错。
        实例（冰王·凝寒止喧）：「连续攻击对手90次，对手每有50点肃霜之禁额外攻击10次，
        自身处于能力提升状态时效果翻倍」——
        按 90 算 vs 按 180 算差一倍，而 90 就写在句首，最容易被直接抄用。

        返回：
          base     句首那个裸次数（如 90）
          fixed    是否「固定次数无修正」（true 时可直接用 base）
          text     相关原文（让人看修正条件）
          warn     提示语（有修正时必须渲染）
        """
        texts = [str(x) for x in (effects or []) if x]
        if info:
            texts.append(str(info))
        found = None
        pat = re.compile(
            r"(连续攻击[^0-9]{0,6}\d+\s*次|\d+\s*[~～\-]\s*\d+\s*次攻击|做\s*\d+\s*次攻击|攻击\s*\d+\s*次)"
        )
        for t in texts:
            m = pat.search(t)
            if m:
                found = t
                break
        if not found:
            return None

        nums = re.findall(r"\d+", found)
        base = int(nums[0]) if nums else None
        # 是否带修正：出现「额外」「翻倍」「每有」「提升状态」「能力」这类
        mods = [w for w in ("额外", "翻倍", "每有", "每层", "提升状态", "能力下降", "视为") if w in found]
        fixed = (not mods) and ("~" not in found and "～" not in found and "-" not in found[:40])
        return {
            "base": base,
            "fixed": fixed,
            "text": found[:160],
            "warn": None if fixed else (
                f"连击次数带修正（{'、'.join(mods) or '区间'}），**不能直接用 {base}**。"
                "要按场景代入：额外攻击/每 N 点加几次/强化时翻倍都要算进去。"
            ),
        }

    def skill_referenced(self, skill_name: str) -> list[dict]:
        """该技能「引用」到的其他技能（复合技能展开）。

        2026-11-06 加。赛尔有一类技能的效果是「拥有别的技能的效果」，形如：

        A 列表式 —— 明确列出技能名
           #神诏之诀别「每获得1层神途之熠，此技能拥有辟邪斩、圣世锋芒斩、
             神圣领域、诛魔杀阵、光辉审判、王凌盛世、神诏之救赎中前1个技能的效果」
        B 动态式 —— 只写机制名，要自己找来源
           #超越式·变革律「此技能拥有本次在场期间自身生效过的所有构造效果」
             构造来自 协议I·激 / II·限 / III·维 / IV·解

        不做这步的话，模型只能看到一句「拥有 A、B、C 中前 N 个」，然后要去
        逐个调 seer_skill —— 7 个技能就是 7 次调用，实测极易漏查或只答前半句。
        """
        name = (skill_name or "").strip()
        if not name:
            return []
        r = self._q("select id from skill where name=? limit 1", (name,))
        if not r:
            return []
        skill_id = int(r[0][0])

        # 该技能的全部效果文本（引用清单就写在里面）
        # ⚠️ 不要把技能名自己放进来：「光辉审判」的名字里含「审判」，
        #    放进去会把自己引用成「审判」（实测误伤）。
        texts: list[str] = []
        try:
            for row in self._q(
                """select se.info from skilleffectlink l
                   join skill_effect_in_use se on se.id = l.effect_in_use_id
                   where l.skill_id = ?""",
                (skill_id,),
            ):
                if row[0]:
                    texts.append(str(row[0]))
        except sqlite3.Error:
            pass
        blob = " ".join(texts)

        # 拥有该技能的精灵（引用只在这些精灵自己的技能表里找，范围小、误判低）
        pet_ids = [
            int(x[0]) for x in self._q(
                "select distinct pet_id from skillinpetorm where skill_id=?", (skill_id,)
            )
        ]
        if not pet_ids:
            return []
        ph = ",".join("?" * len(pet_ids))
        rows = self._q(
            f"""select distinct s.id, s.name, s.power, s.priority, s.category_id, s.info
                from skillinpetorm l join skill s on s.id = l.skill_id
                where l.pet_id in ({ph}) and s.id != ?""",
            tuple(pet_ids) + (skill_id,),
        )

        out: list[dict] = []
        seen: set[str] = set()

        # A：技能名直接出现在效果文本里
        # ⚠️ 中文没有词边界，短名会被长名带中：
        #    「光辉审判」里的「审判」→ 把「审判」也当成被引用技能（实测误伤）。
        #    所以先收集全部命中，再把「被更长命中名包含」的短名剔掉。
        a_hits: list[tuple] = []
        for sid, sname, power, prio, cat, sinfo in rows:
            if not sname or len(sname) < 2:
                continue
            if sname in blob:
                a_hits.append((sid, sname, power, prio, cat))
        hit_names = [h[1] for h in a_hits]
        for sid, sname, power, prio, cat in a_hits:
            if sname in seen:
                continue
            if any(sname != other and sname in other for other in hit_names):
                continue          # 是别的更长技能名的子串，跳过
            seen.add(sname)
            out.append({
                "id": sid, "name": sname, "power": power,
                "priority": prio, "category_id": cat, "via": "按名",
            })

        # B：动态式「构造效果」—— 找同精灵里带「构造」的技能效果
        if "构造" in blob:
            for sid, sname, power, prio, cat, sinfo in rows:
                if not sname or sname in seen:
                    continue
                etext = str(sinfo or "")
                if "构造" not in etext:
                    try:
                        eff = self._q(
                            """select se.info from skilleffectlink l
                               join skill_effect_in_use se on se.id = l.effect_in_use_id
                               where l.skill_id = ?""",
                            (sid,),
                        )
                        etext = " ".join(str(x[0]) for x in eff if x[0])
                    except sqlite3.Error:
                        etext = ""
                if "构造" in etext:
                    seen.add(sname)
                    out.append({
                        "id": sid, "name": sname, "power": power,
                        "priority": prio, "category_id": cat, "via": "构造",
                    })
        return out

    def skill_view(self, name: str) -> dict[str, Any]:
        """技能详情的语义层。

        判定字段：empty_hint（查不到时的提示，会区分「其实是精灵名」这种情况）
        """
        info = self.skill_info(name) or {}
        view: dict = {
            "found": bool(info),
            "name": info.get("name"),
            "power": info.get("power"),
            "category": ({1: "物理", 2: "特殊", 4: "属性"}
                         .get(info.get("category_id")) or info.get("skill_type") or "?"),
            "type": info.get("type") or "?",
            "pp": info.get("pp"),
            "accuracy": info.get("accuracy"),
            "priority": info.get("priority", 0),
            "must_hit": bool(info.get("must_hit")),
            "atk_num": info.get("atk_num"),
            "crit_rate": info.get("crit_rate"),
            "effects": list(info.get("effects") or []),
            "info": info.get("info") or "",
            "referenced": [],
            "empty_hint": None,
            "combo_hint": None,
        }
        if view["found"]:
            view["combo_hint"] = self._extract_combo_hint(
                (view.get("effects") or []), view.get("info") or ""
            )
            # 复合技能：把「拥有别的技能的效果」那批技能挖出来一起给
            try:
                view["referenced"] = self.skill_referenced(name)
            except Exception:  # noqa: BLE001
                view["referenced"] = []
            return view

        # 查不到：区分「其实是精灵名」和「根本没有」
        try:
            hits = self.find_pets(name, limit=3)
        except Exception:
            hits = []
        if hits:
            view["empty_hint"] = (
                f"「{name}」是精灵不是技能。要看它的资料用：pet {hits[0][1]}"
            )
        else:
            view["empty_hint"] = (
                f"未收录技能「{name}」。可向玩家确认技能全名后再查，"
                "或先把效果记进规则库。"
            )
        return view

    # ────────────────────────────────────────────────────────────
    # 语义层：dmg_view —— 伤害计算的唯一判定源
    #
    # 2026-11-03 合并 bot(seer_damage 31 参数) 与 CLI(cmd_dmg 13 参数) 时的取舍：
    #   · 合并等价参数：add_pct+general_add_pct、reduce_pct+general_reduce_pct、
    #     flat_damage+flat_segments、percent_damage+percent_segments、
    #     crit_reduce_pct+flat_reduce+percent_reduce → resist
    #   · extra_add_pct+extra_add_segments  → extra_add（逗号分隔，连乘）
    #   · random_factor → random（档位语义，防「0.85」重演）
    #   · 新增 power_boost：威力加成必须单独一条通道，否则会绕开
    #     「变威力技能吃不到单属性加成」这条规则
    # 31 → 25 个参数。
    #
    # 增伤/减伤归类（玩家确认）：
    #   强袭/精神  → 额外增伤（二者互斥，合并一个乘区）→ extra_add
    #   傲视/技能额外+100% → 额外增伤 → extra_add（逗号续接）
    #   强能/绝命/爆伤（二代宝石） → 通用增伤 → general_add
    #   坚伤 → 额外减伤 → extra_reduce（多个连乘）
    #   冰霜等威力+N% → power_boost
    # ────────────────────────────────────────────────────────────
    DMG_FLOAT_MIN = 217           # 浮动下限分子（分母固定 255）
    DMG_FLOAT_MAX = 255
    DMG_FLOAT_LEVELS = 39         # 217~255 共 39 个离散档，每档 1/39

    # ══════════════════════════════════════════════════════════════
    # 先手权判定（2026-11-10）
    #
    # 规则（kb.facts 原文）：
    #   P4 先手权判定 = 必先 > 先制等级 > 速度
    #   · 必先：技能/效果写明"必定先手"的，压过一切
    #   · 先制等级：高者先手；等级相同才比速度
    #   · 速度：高者先手
    #   · 双方都相同 → 随机（挑战方略优，但实测有随机）
    #
    # ⚠ 容易错的：先制等级是**每回合**的，Buff 只管"下 N 回合"。
    #   例：丧月之夜给「下2回合先制+2」，那 T2/T3 有、T4 就没了。
    #   所以每次判定都要重新算双方当前的先制等级，不能沿用上一回合。
    # ══════════════════════════════════════════════════════════════

    def speed_order(self, left_name: str = "左方", left_spd: int = 0,
                    left_pri: int = 0, left_must_first: bool = False,
                    right_name: str = "右方", right_spd: int = 0,
                    right_pri: int = 0, right_must_first: bool = False,
                    left_rank: int = 0, right_rank: int = 0,
                    tie_winner: str = "") -> dict:
        """判定本回合谁先手。必先等级 > 必先 > 先制等级 > 速度。

        返回 {first, reason, layer, left, right}
          layer ∈ {"必先等级", "必先同级", "必先", "先制", "速度", "相同"}

        ★ 2026-11-10：必先从布尔升级成【等级】（kb.first_move_rank）。
           依据「必先种类与优先级」表：11 级，rank 1 最高、11 最低。
           同 rank 时按规则③——由房主（开房方）先手，用 tie_winner 指定。

        口径（表下 6 条规则，已录 kb.first_move_rule）：
          ① 必先无视双方任何先制与速度
          ② 11 级优先级，数字越小越优先
          ③ 同级 → 房主先手（tie_winner）
          ④ 嗑药/切换等弃权操作不令必先生效
          ⑤ 次数必先仅在出手时消耗（被控可「生效但不消耗」）
          ⑥ 「竭血残蝶/白帝托孤/钢铁熔炉」是次数必先；
             「天幕威仪/始祖之光」可令属性技能必先

        兼容：双方都 must_first 且都没给 rank 时，退回先制/速度比较（原行为）。
        """
        def _i(v):
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0

        lspd, rspd = _i(left_spd), _i(right_spd)
        lpri, rpri = _i(left_pri), _i(right_pri)
        lrank, rrank = _i(left_rank), _i(right_rank)
        # rank>0 视为必先；布尔 must_first 也视为必先（但无等级）
        lf = bool(left_must_first) or lrank > 0
        rf = bool(right_must_first) or rrank > 0
        base = {
            "left": {"name": left_name, "spd": lspd, "pri": lpri,
                     "must_first": lf, "rank": lrank},
            "right": {"name": right_name, "spd": rspd, "pri": rpri,
                      "must_first": rf, "rank": rrank},
        }

        # ① 必先（含等级判定）
        if lf and not rf:
            extra = f"（必先 rank {lrank}）" if lrank > 0 else ""
            return {**base, "first": "left", "layer": "必先",
                    "reason": f"{left_name} 有「必定先手」{extra}，压过一切"}
        if rf and not lf:
            extra = f"（必先 rank {rrank}）" if rrank > 0 else ""
            return {**base, "first": "right", "layer": "必先",
                    "reason": f"{right_name} 有「必定先手」{extra}，压过一切"}
        if lf and rf and (lrank > 0 or rrank > 0):
            # 双方都必先，且至少一方有等级
            if lrank > 0 and rrank > 0:
                if lrank != rrank:
                    who = "left" if lrank < rrank else "right"
                    nm = left_name if who == "left" else right_name
                    return {**base, "first": who, "layer": "必先等级",
                            "reason": f"双方必先：rank {lrank} vs {rrank}，"
                                      f"数字小的优先 → {nm} 先手"}
                tw = str(tie_winner or "").strip().lower()
                specified = tw in ("left", "l", "right", "r", "左", "右")
                who = "right" if tw in ("right", "r", "右") else "left"
                nm = left_name if who == "left" else right_name
                if specified:
                    reason = (f"双方必先同为 rank {lrank} → 按规则③由【房主】先手，"
                              f"房主指定为 {nm} → {nm} 先手")
                else:
                    reason = (f"双方必先同为 rank {lrank} → 按规则③应由【房主】先手，"
                              f"但没说明谁是房主；暂按「左方=房主」判给 {nm}，"
                              f"房主若在另一边结果就反过来")
                return {**base, "first": who, "layer": "必先同级",
                        "tie_specified": specified, "reason": reason}
            # 一方有明确等级、另一方只有布尔必先 → 有等级的赢（布尔视为未知等级）
            who = "left" if lrank > 0 else "right"
            nm = left_name if who == "left" else right_name
            rk = lrank if who == "left" else rrank
            return {**base, "first": who, "layer": "必先等级",
                    "reason": f"{nm} 有明确必先等级 rank {rk}，对方只确认「必先」无等级"}
        # 双方必先但都没有等级 → 退回先制/速度（原行为）

        # ② 先制等级
        if lpri != rpri:
            who = "left" if lpri > rpri else "right"
            nm = left_name if who == "left" else right_name
            return {**base, "first": who, "layer": "先制",
                    "reason": f"先制等级 {lpri} vs {rpri}，{nm} 更高"}

        # ③ 速度
        if lspd != rspd:
            who = "left" if lspd > rspd else "right"
            nm = left_name if who == "left" else right_name
            return {**base, "first": who, "layer": "速度",
                    "reason": f"先制等级都是 {lpri}，比速度 {lspd} vs {rspd}，{nm} 更快"}

        # ④ 全同
        return {**base, "first": "random", "layer": "相同",
                "reason": f"先制 {lpri}、速度 {lspd} 都相同 → 随机决定（挑战方略占优）"}

    # ══════════════════════════════════════════════════════════════
    # 必先等级查询（2026-11-10，数据源：用户提供的「必先种类与优先级」表）
    #
    # 22 条实例 + 11 级 + 6 条规则，全部在 kb：
    #   first_move_rank(rank, pet, move, effect_id, scope, kind, note, desc)
    #   first_move_rule(rid, rule)
    #
    # ★ effect_id 是【手册 manual_db/skill_effect.db】的 id，
    #   **不是** seerapi 的 skill_effect_in_use.id —— 两套编号完全不通。
    #   拿 seerapi 去对会全错位（实测 20 条无一命中）。
    # ══════════════════════════════════════════════════════════════

    def first_move_of_pet(self, name: str = "") -> dict:
        """列出某只精灵所有「必定先手」类技能，并尽量给出等级。

        ★ 2026-11-10：解决「表里没有、但同代码复用」的问题。
          表只人工收录了 22 条（精灵+技能），但同一个效果模板被多个技能共用：
            effect_id 1310 → 夜洛乌泽(混沌布莱克) / 神羽逆卷(擎空·凡尔斯)
                            / 无情魔瞳斩(混沌伊洛维奇) / 狂雷奔袭(暴乱·格劳瑞)
          所以只要拿到技能的 effect_id，就能反查「这招和表里那条是同一个代码」，
          等级直接继承，不用逐条人工录。

        ★ 关键对应关系（今天纠正过一次）：
            seerapi.skill_effect_in_use.effect_id  ↔  手册 skill_effect.id
          两边范围都是 1~2621。**不是** skill_effect_in_use.id（那是实例号，
          和手册 id 撞号但含义不同，按它去比会全错位）。

        返回 {"ok", "pet_id", "pet", "items":[{move, skill_id, effect_id, rank,
              kind, note, via}]}
          via ∈ {"template"=模板命中表里的 effect_id, "text"=文本判为必先但等级未收录}
        """
        kw = (name or "").strip()
        pid = None
        if kw:
            pid = self.alias_lookup(kw)
            if not pid:
                cand = self.find_pets(kw, limit=1)
                if cand:
                    pid = cand[0][0]
        if not pid:
            return {"ok": False, "pet_id": 0, "pet": kw, "items": []}

        prow = self._q("select name from pet where id=?", (pid,))
        pname = prow[0][0] if prow else kw

        # 表里已知的模板 → rank
        known = {}
        for r in self._kb.execute(
            "select effect_id, rank, kind, note from first_move_rank where effect_id is not null"
        ):
            known.setdefault(int(r[0]), (r[1], r[2], r[3]))

        rows = self._q(
            """select s.id, s.name, u.effect_id, u.info
               from skillinpetorm l
               join skill s on s.id = l.skill_id
               left join skilleffectlink sl on sl.skill_id = s.id
               left join skill_effect_in_use u on u.id = sl.effect_in_use_id
               where l.pet_id = ?""",
            (pid,),
        )

        items = []
        seen = set()
        for sid, sname, eid, info in rows:
            txt = (info or "")
            hit_tpl = int(eid) in known if eid is not None else False
            hit_txt = ("必定先手" in txt) or ("必定先出手" in txt)
            if not hit_tpl and not hit_txt:
                continue
            key = (sid, eid)
            if key in seen:
                continue
            seen.add(key)
            if hit_tpl:
                rk, kind, note = known[int(eid)]
                items.append({"move": sname, "skill_id": sid, "effect_id": int(eid),
                              "rank": rk, "kind": kind, "note": note, "via": "template"})
            else:
                items.append({"move": sname, "skill_id": sid,
                              "effect_id": int(eid) if eid is not None else None,
                              "rank": 0, "kind": "", "note": "",
                              "via": "text"})
        # 有等级的排前面，同等级按技能名
        items.sort(key=lambda x: (x["rank"] == 0, x["rank"], x["move"]))
        return {"ok": bool(items), "pet_id": pid, "pet": pname, "items": items}

    def first_move_resolve(self, name: str = "", move: str = "",
                           explicit_rank: int = 0) -> dict:
        """给 seer_speed 用：把「精灵名/外号/技能名」直接解析成必先等级。

        ★ 2026-11-10（鱼片指出设计问题后改的）：
          原来要先调 seer_first_move 查 rank、再手动传给 seer_speed，
          等于让模型在两次调用之间搬运一个数字——查到了忘传、或者填错侧，
          都会静默变成「按速度判」，而速度恰好可能给出相反答案。
          现在改成 speed 内部自己解析：名字进来，等级出来。

        优先级：explicit_rank（调用方明确给）> 技能名 > 精灵名/外号。

        返回：
          rank        0=没有必先；>0 为 1~11
          matched_by  "explicit" / "move" / "pet" / ""
          item        命中的那条记录（可能为 None）
          candidates  同名多条时的候选（供提示「你这只精灵有多个必先技能」）
          hint        给人看的说明，直接塞进输出
        """
        def _i(v):
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0

        rk = _i(explicit_rank)
        if rk > 0:
            return {"rank": rk, "matched_by": "explicit", "item": None,
                    "candidates": [], "hint": f"调用方指定 rank {rk}"}

        mv = (move or "").strip()
        nm = (name or "").strip()

        def _rows_by_move(term, exact=False):
            if not term:
                return []
            op = "=" if exact else "like"
            val = term if exact else f"%{term}%"
            return self._kb.execute(
                f"""select rank, pet, move, effect_id, scope, kind, note, desc
                    from first_move_rank where move {op} ? order by rank""",
                (val,),
            ).fetchall()

        def _rows_by_pet(term):
            if not term:
                return []
            return self._kb.execute(
                """select rank, pet, move, effect_id, scope, kind, note, desc
                   from first_move_rank where pet like ? or ? like '%'||pet||'%'
                   order by rank""",
                (f"%{term}%", term),
            ).fetchall()

        def _pack(rows, by):
            items = [
                {"rank": r[0], "pet": r[1], "move": r[2], "effect_id": r[3],
                 "scope": r[4], "kind": r[5], "note": r[6], "desc": r[7]}
                for r in rows
            ]
            best = items[0]
            hint = f"{best['pet']}·{best['move']} → rank {best['rank']}（{best['kind']}必先）"
            if len(items) > 1:
                others = "、".join(f"{i['pet']}·{i['move']}(rank {i['rank']})"
                                   for i in items[1:4])
                hint += f"；另有同关键词 {len(items) - 1} 条：{others}"
            return {"rank": best["rank"], "matched_by": by, "item": best,
                    "candidates": items, "hint": hint}

        # ① 按技能名
        rows = _rows_by_move(mv, exact=True) or _rows_by_move(mv)
        if rows:
            return _pack(rows, "move")

        # ② 按精灵名（含外号）
        rows = _rows_by_pet(nm)
        if not rows and nm:
            target = nm
            pid = self.alias_lookup(nm)
            if pid:
                r = self._q("select name from pet where id=?", (pid,))
                if r:
                    target = r[0][0]
            else:
                cand = self.find_pets(nm, limit=1)
                if cand:
                    target = cand[0][1]
            if target != nm:
                rows = _rows_by_pet(target)
        if rows:
            return _pack(rows, "pet")

        # ③ 表里没有 → 扫这只精灵的技能：复用表内模板的继承等级，纯文本判定的给最低档
        if nm:
            fm = self.first_move_of_pet(nm)
            tmpl = [x for x in fm.get("items", []) if x["via"] == "template" and x["rank"]]
            if tmpl:
                b = tmpl[0]
                hint = (f"{fm['pet']}·{b['move']} → rank {b['rank']}"
                        f"（{b['kind']}必先；本招与表内同名效果共用模板 {b['effect_id']}）")
                item = {"rank": b["rank"], "pet": fm["pet"], "move": b["move"],
                        "effect_id": b["effect_id"], "scope": "技能",
                        "kind": b["kind"], "note": b["note"], "desc": ""}
                return {"rank": b["rank"], "matched_by": "template", "item": item,
                        "candidates": fm["items"], "hint": hint}
            txt = [x for x in fm.get("items", []) if x["via"] == "text"]
            if txt:
                b = txt[0]
                hint = (f"{fm['pet']}·{b['move']} 按效果文本判定为「必定先手」，"
                        f"但等级未收录 → 按最低档 rank 99 处理")
                item = {"rank": 0, "pet": fm["pet"], "move": b["move"],
                        "effect_id": b["effect_id"], "scope": "技能",
                        "kind": "", "note": "", "desc": ""}
                return {"rank": 99, "matched_by": "text", "item": item,
                        "candidates": fm["items"], "hint": hint}

        return {"rank": 0, "matched_by": "", "item": None,
                "candidates": [], "hint": ""}

    def first_move_lookup(self, keyword: str = "") -> dict:
        """按精灵名/外号/技能名/描述关键词查必先等级。

        ★ 2026-11-10：支持外号——表里存的是正式名（光之惩戒·英卡洛斯），
          而群里问的是「光螳螂」「表姐」。所以查不到时自动过一遍别名解析：
            ① pet_alias 精确（表姐 → #3512）
            ② find_pets 模糊取第一个候选
          解析到了就用正式名重查一次。
          实测不解析会直接漏：查「螳螂」「表姐」都是 0 条。

        返回 {"ok", "keyword", "resolved", "items":[{...}], "rules":[...]}
        """
        kw = (keyword or "").strip()

        def _query(term: str):
            return self._kb.execute(
                """select rank, pet, move, effect_id, scope, kind, note, desc
                   from first_move_rank
                   where ? = '' or pet like ? or move like ? or desc like ?
                   order by rank, pet""",
                (term, f"%{term}%", f"%{term}%", f"%{term}%"),
            ).fetchall()

        rows = _query(kw)
        resolved = ""
        if not rows and kw:
            # 外号 → 正式名
            pid = self.alias_lookup(kw)
            name = ""
            if pid:
                r = self._q("select name from pet where id=?", (pid,))
                name = r[0][0] if r else ""
            else:
                cand = self.find_pets(kw, limit=1)
                if cand:
                    pid, name = cand[0][0], cand[0][1]
            if name:
                rows = _query(name)
                if rows:
                    resolved = name

        items = [
            {"rank": r[0], "pet": r[1], "move": r[2], "effect_id": r[3],
             "scope": r[4], "kind": r[5], "note": r[6], "desc": r[7]}
            for r in rows
        ]
        rules = [r[0] for r in self._kb.execute(
            "select rule from first_move_rule order by rid"
        )]
        return {"ok": bool(items), "keyword": kw, "resolved": resolved,
                "items": items, "rules": rules}

    # ══════════════════════════════════════════════════════════════
    # 百分比伤害 / 吸取 口径固化（2026-11-10）
    #
    # 为什么单独做：`_parse_segments` 只吃 "0.333,0.333" 这种逗号分隔的 float，
    # 玩家和模型说的「1/3 粉」「吸一口六分之一」它都不认；而 `dmg_view` 有 20+
    # 参数，LLM 填不准。推演场景实际只需要【比例 → 实扣多少 → 回多少】这一小块。
    #
    # 口径全部来自 kb.facts 的原文（不是猜的）：
    #   · 粉/吸取按【对手】最大体力算，不是施法者的
    #   · 都要 ×(1-抗性)，满抗性 35% → ×0.65
    #   · 吸取 = 扣对手等量 + 自己回等量，回血量也按折算后的算
    #   · 比例吸取属于百分比伤害 → 会被「免疫百分比伤害」挡掉
    #   · 真伤不吃抗性、不吃减伤
    # ══════════════════════════════════════════════════════════════

    RATIO_WORDS = {
        "三分之一": "1/3", "三分一": "1/3", "1/3": "1/3",
        "六分之一": "1/6", "六分一": "1/6", "1/6": "1/6",
        "四分之一": "1/4", "四分之一": "1/4", "1/4": "1/4",
        "二分之一": "1/2", "一半": "1/2", "半血": "1/2", "1/2": "1/2",
        "五分一": "1/5", "1/5": "1/5",
        "八分之一": "1/8", "1/8": "1/8",
    }

    @staticmethod
    def parse_ratio(raw) -> float | None:
        """把「1/3」「三分之一」「33%」「0.3333」都解析成 0.3333。

        返回 None 表示认不出来（调用方给提示，别默默当 0）。
        """
        s = str(raw or "").strip().replace("％", "%")
        if not s:
            return None
        for w, r in SeerKnowledge.RATIO_WORDS.items():
            if s == w:
                s = r
                break
        if "/" in s:
            a, _, b = s.partition("/")
            try:
                a, b = float(a), float(b)
                return a / b if b else None
            except ValueError:
                return None
        if s.endswith("%"):
            try:
                return float(s[:-1]) / 100.0
            except ValueError:
                return None
        try:
            v = float(s)
        except ValueError:
            return None
        # >1 当百分数（"33"→0.33）
        return v / 100.0 if v > 1 else v

    def pct_calc(self, target_max_hp: int, ratio="1/3", segments: int = 1,
                 resist: int = 35, kind: str = "pct",
                 caster_cur_hp: int = 0, caster_max_hp: int = 0) -> dict:
        """算百分比伤害 / 吸取 / 固定伤害 / 真实伤害的实际数值。

        取整口径（kb.facts 原文，别合并两步）：
          ① 量 = floor(最大体力 × 系数 × 翻倍系数)
          ② 实扣 = 量 − floor(量 × 抗性)
        例：H=1000 的 1/3 → floor(333.33)=333 → 333−floor(116.55)=333−116=217
            跳过①直接拿 333.33 过抗会得 216，差 1 点。

        Args:
            target_max_hp: **对手**最大体力（粉和吸取都按这个算，不是施法者的）。
            ratio: 比例，吃「1/3」「三分之一」「33%」「0.33」四种写法。
                   kind="true" 时按【绝对值点数】解释（如 "50" = 50 点真伤）。
            segments: 几段。神罡 + 魂印同时点触发就是 2 段。
            resist: 抗性百分比，满抗性 35。真伤忽略。
            kind: pct=百分比 / drain=吸取(扣对手+回自己) / fixed=固定伤害 / true=真实伤害
            caster_cur_hp / caster_max_hp: ★ **只用来判「吸取时是否低于半血翻倍」**，
                **不参与计算基数**。基数永远是 target_max_hp（对手最大体力）。
                技能写「吸取对手最大体力1/3」→ 基数是对手**最大**体力，
                跟对手当前剩多少血无关。

        返回 {ok, ratio, per_segment, segments, total_dealt, healed, doubled, detail, base_from}
        """
        import math as _m

        try:
            hp = int(target_max_hp or 0)
        except (TypeError, ValueError):
            hp = 0
        if hp <= 0:
            return {"ok": False, "error": "要给对手最大体力（target_max_hp > 0）"}

        kd = str(kind or "pct").strip().lower()
        n = max(1, int(segments or 1))
        try:
            rz = max(0, min(100, int(resist if resist is not None else 35)))
        except (TypeError, ValueError):
            rz = 35
        if kd == "true":
            rz = 0

        # ① 先定每一段的「原始量」
        #
        #   支持三种写法：
        #     ratio="1/3"            + segments=2      → 两段同比例
        #     ratio="1/3,1/6"                          → 两段不同比例（神罡 1/3 + 魂印 1/6）
        #     ratio="1/3" + segments=1 & kind=drain    → 单段吸取
        #
        #   ★ 多段必须**逐段各自取整、各自过抗**，不能合并后过一次抗。
        #     facts 原文：三段各 2 点固伤过 35% → 每段 floor(0.7)=0 → 各 2 → 合计 6；
        #     合并成 6 点则 floor(2.1)=2 → 只剩 4。
        #     1/3 粉两段：分别 217+217=434；合并 433。
        raw_segs: list[float] = []
        if kd in ("true", "fixed"):
            # 绝对值点数（"2"=2点），不能走 parse_ratio（会把 "2" 当 2%）
            try:
                amt_point = int(float(str(ratio).strip()))
            except (TypeError, ValueError):
                return {"ok": False,
                        "error": f"{kd} 要给点数（如 2 或 50），收到 {ratio!r}"}
            raw_segs = [float(amt_point)] * n
            ratio_desc = f"{amt_point}点"
        else:
            parts = [x.strip() for x in str(ratio).replace("，", ",").split(",") if x.strip()]
            vals: list[float] = []
            for part in parts:
                rv = self.parse_ratio(part)
                if rv is None:
                    return {"ok": False,
                            "error": f"比例「{part}」没认出来，可写 1/3、三分之一、33%、0.33"}
                vals.append(rv)
            if not vals:
                return {"ok": False, "error": "比例不能为空"}
            # 逗号给了几段就按几段；只给一个就复制 segments 份
            if len(vals) > 1:
                raw_segs = [hp * v for v in vals]
                n = len(vals)
            else:
                raw_segs = [hp * vals[0]] * n
            ratio_desc = ",".join(parts)

        # 吸取：先判半血 → 翻倍作用在每段「量」上，再 floor
        doubled = False
        note = ""
        if kd == "drain":
            try:
                cur = int(caster_cur_hp or 0)
                mx = int(caster_max_hp or 0)
            except (TypeError, ValueError):
                cur = mx = 0
            if cur > 0 and mx > 0 and cur < mx / 2:
                doubled = True
                raw_segs = [x * 2 for x in raw_segs]
                note = f"施法者 {cur}/{mx} = {cur/mx:.0%} < 50% → 吸取翻倍（每段量先×2 再取整）"
            elif cur > 0 and mx > 0:
                note = f"施法者 {cur}/{mx} = {cur/mx:.0%} ≥ 50% → 不翻倍"

        # ② 逐段：量 floor → 抗性 floor → 相减
        ############################################################
        # ★ 2026-11-10 修正浮点误差：抗性扣除必须用【整数运算】
        #
        #   原来写 `int(math.floor(amt * (rz / 100.0)))`，
        #   但 360 * 0.35 在浮点下是 125.99999999999999 → floor 得 125，
        #   而正确值是 360*35//100 = 126。差 1 点。
        #
        #   赛尔游戏里是整数运算，所以这里必须 `amt * rz // 100`。
        #   实测出处：战刀掉命斩那条 facts 写「360 过抗 → 234」（=360-126），
        #   用浮点会算出 235，与实测口径差 1。
        ############################################################
        detail: list[dict] = []
        total = 0
        for raw in raw_segs:
            amt = int(_m.floor(raw))
            cut = amt * rz // 100          # 整数运算，不用浮点
            per = amt - cut
            total += per
            detail.append({"raw": round(raw, 2), "amount": amt,
                           "resist_cut": cut, "actual": per})

        # ★ 2026-11-10（鱼片纠正）：吸取的**恢复量不过抗**。
        #
        #   扣对手的那部分走粉伤通道，要过抗性（上面 detail 里的 actual）；
        #   但**自己回的血等于吸取的量本身**，不折算。
        #
        #   例：吸收对手 1165 最大体力的 1/3
        #       量 = floor(388.33) = 388
        #       扣对手 = 388 − floor(388×35%) = 388 − 135 = 253
        #       自己回 = 388（不是 253）
        #
        #   依据：[王·荒合归心]「附加对手最大体力1/3百分比伤害，未击败则恢复自身等量体力」
        #         ——「等量」等于 1/3 那个量，不是过抗后的实际扣血。
        heal_amount = sum(d["amount"] for d in detail) if kd == "drain" else 0

        return {
            "ok": True,
            "kind": kd,
            "ratio": ratio_desc,
            "target_max_hp": hp,
            # ★ 明确写清基数来源，防止把「对手当前血量」当成基数
            "base_from": f"对手最大体力 {hp}（不是当前血量，也不是施法者体力）",
            "segments": n,
            "detail": detail,
            "per_segment": detail[0]["actual"] if detail else 0,
            "amount": detail[0]["amount"] if detail else 0,
            "resist_cut": detail[0]["resist_cut"] if detail else 0,
            "raw": detail[0]["raw"] if detail else 0,
            "total_dealt": total,
            "healed": heal_amount,
            "heal_note": ("恢复量取吸取的量本身（不过抗）" if kd == "drain" and heal_amount != total else ""),
            "resist": rz,
            "doubled": doubled,
            "note": note,
        }


    @staticmethod
    def _parse_segments_checked(raw: Any, *, percent_mode: bool = False):
        """解析逗号分隔的分段系数，并把「认不出的段」原样带出来。

        返回 (values, bad)：
            values  解析成功的 float 列表（坏段不会混进来）
            bad     认不出的原始片段（空列表 = 全部认出来了）

        ★ 为什么要有 checked 版（2026-11-10 七号鱼）：
          原实现是 `except ValueError: continue`，认不出的段**静默丢掉**。
          于是 percent="1/3" → 列表为空 → 粉伤按 0 算，还照常返回 ok=True，
          群里就念出「百分比伤害 0」。**静默失败比报错危险得多。**

          本函数只负责「如实上报」，要不要报错由调用方决定。

          percent_mode=True 时额外吃「1/3」「三分之一」「33%」这类写法
          （走 parse_ratio）。flat 这类「点数」通道不要开，否则 "1/2"
          会被当成 0.5 点而不是报错。
        """
        s = str(raw if raw is not None else "").strip().replace("，", ",")
        if not s:
            return [], []
        vals: list[float] = []
        bad: list[str] = []
        for part in s.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                vals.append(float(part))
                continue
            except ValueError:
                pass
            if percent_mode:
                rv = SeerKnowledge.parse_ratio(part)
                if rv is not None:
                    vals.append(rv)
                    continue
            bad.append(part)
        return vals, bad

    @staticmethod
    def _parse_segments(raw: str, *, percent_mode: bool = False) -> list[float]:
        """旧签名兼容层：只要数值，丢弃错误信息。

        新代码请用 `_parse_segments_checked`。这个包装留着是防止
        还有只想要列表、不在乎坏段的地方。
        """
        vals, _ = SeerKnowledge._parse_segments_checked(raw, percent_mode=percent_mode)
        return vals

    @staticmethod
    def _norm_pct(v: float) -> float:
        """宽容处理「20」「0.2」「20%」三种写法 → 都变 0.2。"""
        try:
            x = float(v)
        except (TypeError, ValueError):
            return 0.0
        if abs(x) > 1.0:
            return x / 100.0
        return x

    def _resolve_random(self, random: Any) -> tuple[float | None, str]:
        """把 random 参数解析成 (random_factor or None, 说明文字)。

        None 表示「要区间」（交给 combo_damage 用 217/255~255/255 算两端）。
        """
        if isinstance(random, (int, float)) and not isinstance(random, bool):
            n = int(random)
            if self.DMG_FLOAT_MIN <= n <= self.DMG_FLOAT_MAX:
                return n / 255.0, f"浮动取第 {n} 档（{n}/255）"
            # 兼容 0~1 的小数写法
            if 0.0 < float(random) <= 1.0:
                return float(random), f"浮动 ×{float(random):.4f}（非整数档，注意 39 档口径）"
            return None, "浮动参数超出范围，按区间算"

        key = str(random or "").strip().lower()
        # ★ 数字字符串（CLI 传进来的 "236"）要能识别成档位
        if key.isdigit():
            n = int(key)
            if self.DMG_FLOAT_MIN <= n <= self.DMG_FLOAT_MAX:
                return n / 255.0, f"浮动取第 {n} 档（{n}/255）"
            return None, f"浮动档位 {n} 超出 217~255，按区间算"
        if key in ("", "range", "all", "区间"):
            return None, "浮动按 217~255 全区间（39 档）"
        if key == "min":
            return self.DMG_FLOAT_MIN / 255.0, "浮动取下限 217/255"
        if key == "max":
            return self.DMG_FLOAT_MAX / 255.0, "浮动取上限 255/255（1/39 概率）"
        if key in ("avg", "mid", "average"):
            mid = (self.DMG_FLOAT_MIN + self.DMG_FLOAT_MAX) // 2
            return mid / 255.0, f"浮动取中位第 {mid} 档（{mid}/255）"
        return None, f"浮动参数「{random}」没认出来，按区间算"

    def _resolve_resist(self, resist: Any) -> tuple[float, float, float, list[str]]:
        """抗性档位 → (暴击抗, 固伤抗, 百分比抗, 认不出的片段)。

        ★ 2026-11-10：原来认不出的写法会 fallback 成满抗 0.35 ——
          也就是传 resist="abc" 被静默当成「满抗性」算。现在把坏片段带出去，
          由 dmg_view 统一报错。
        """
        if isinstance(resist, (int, float)) and not isinstance(resist, bool):
            v = self._norm_pct(float(resist))
            return v, v, v, []
        key = str(resist or "").strip().lower()
        if key in ("", "full", "满", "满抗"):
            return 0.35, 0.35, 0.35, []
        if key in ("none", "no", "无", "零"):
            return 0.0, 0.0, 0.0, []
        parts, bad = self._parse_segments_checked(str(resist), percent_mode=True)
        if bad:
            # 有认不出的就先不猜，把坏片段交给调用方报错
            return 0.35, 0.35, 0.35, bad
        if len(parts) >= 3:
            return (self._norm_pct(parts[0]), self._norm_pct(parts[1]),
                    self._norm_pct(parts[2]), [])
        if len(parts) == 1:
            v = self._norm_pct(parts[0])
            return v, v, v, []
        return 0.35, 0.35, 0.35, []

    def dmg_view(
        self,
        *,
        power: int,
        atk: int,
        defense: int,
        level: int = 100,
        attack_type: str = "",
        defend_type: str = "",
        type_mult: float = 0.0,
        type_mult_cap: float = 0.0,
        stab: float = 1.0,
        crit: float = 1.0,
        other: float = 1.0,
        random: Any = "range",
        power_boost: float = 0.0,
        power_flat: float = 0.0,
        variable_power: bool = False,
        general_add: float = 0.0,
        extra_add: str = "",
        general_reduce: float = 0.0,
        extra_reduce: str = "",
        resist: Any = "full",
        flat: str = "",
        percent: str = "",
        true_damage: float = 0.0,
        attr_damage: float = 0.0,
        attr_type: str = "",
        atk_stage: int = 0,
        defense_stage: int = 0,
        hits: int = 1,
        target_hp: float = 0.0,
    ) -> dict[str, Any]:
        """伤害计算的语义层。返回结构化结果，不做排版。

        判定字段（两边都必须渲染）：
          float_hint   —— 浮动口径说明（39 档，不是连续区间）
          slot_hints   —— 各增伤/减伤来源被归到哪个乘区的说明
          empty_hint   —— 参数不合法时的提示
        """
        import math as _m
        try:
            from . import formula as _f
        except Exception:
            import formula as _f  # type: ignore

        view: dict = {
            "ok": False,
            "empty_hint": None,
            "float_hint": None,
            "slot_hints": [],
            "result": None,
            "inputs": {},
        }

        if power is None or atk is None or defense is None:
            view["empty_hint"] = "参数不全：power / atk / defense 三个必须给。"
            return view
        if defense <= 0:
            view["empty_hint"] = "defense 必须大于 0。"
            return view
        # 2026-11-04（四号鱼）：负威力和 0 连击原来会静默算出结果 ——
        # power=-50 被 max(1.0, ...) 夹成 1 威力，最后报「2 点伤害」；
        # hits=-3 被当成 1 段，报的还是正常数值。群里问「这招打多少」时这种数字最误导。
        if float(power) < 0:
            view["empty_hint"] = f"power 不能为负（收到 {power}）。威力最低按 0 算。"
            return view
        if int(hits or 1) < 1:
            view["empty_hint"] = f"hits 至少为 1（收到 {hits}）。"
            return view

        # ① 威力加成（改威力，不改总伤）
        # ★ 变威力技能不吃任何威力层加成（2026-11-04）
        #   依据 facts「变威力·暴击为何无视双防强化」里的覆盖清单：
        #     烧伤面板威力减半、**威力宝石**、…、**特性单属性增伤**
        #   所以这一类技能要把 power_boost 和 power_flat 一起清零，
        #   不能只关其中一个。
        _pb = float(power_boost or 0.0)
        _pf = float(power_flat or 0.0)
        if variable_power and (_pb or _pf):
            view["slot_hints"].append(
                "变威力技能：威力层加成一律不生效"
                f"（已忽略 特性单属性加成 {_pb:+.0%}、威力宝石 +{_pf:.0f}）"
            )
            _pb = 0.0
            _pf = 0.0
        # 百分比先乘，固定值后加 —— 威力宝石原文「不受其他威力提升/下降效果加成」
        pw_pct = float(power) * (1.0 + self._norm_pct(_pb))
        pw = max(1.0, pw_pct + _pf)
        if _pb:
            view["slot_hints"].append(
                f"威力加成 {_pb:+.0%} → 威力 {power}→{pw_pct:.0f}（改的是威力，不是总伤）"
            )
        if _pf:
            view["slot_hints"].append(
                f"威力宝石 +{_pf:.0f} 点 → 威力合计 {pw:.0f}"
                "（固定值，不吃其他威力加成/下降）"
            )

        # ② 浮动
        rf, float_desc = self._resolve_random(random)
        view["float_hint"] = (
            float_desc + "；(217~255)/255 共 39 个离散档，每档 1/39≈2.56%，"
            "不是 [0.85,1.0] 连续区间"
        )

        # ③ 增伤/减伤解析
        extra_add_list, extra_add_bad = self._parse_segments_checked(extra_add, percent_mode=True)
        extra_red_list, extra_red_bad = self._parse_segments_checked(extra_reduce, percent_mode=True)
        gen_add = self._norm_pct(general_add)
        gen_red = self._norm_pct(general_reduce)

        # 额外乘区连乘
        extra_add_factor = 1.0
        for x in extra_add_list:
            extra_add_factor *= (1.0 + self._norm_pct(x))
        extra_red_kept = 1.0
        for x in extra_red_list:
            extra_red_kept *= (1.0 - self._norm_pct(x))

        if extra_add_list:
            view["slot_hints"].append(
                "额外增伤（连乘）：" + " × ".join(f"{self._norm_pct(x):.0%}" for x in extra_add_list)
                + f" → ×{extra_add_factor:.4f}"
            )
        if gen_add:
            view["slot_hints"].append(f"通用增伤（相加）：+{gen_add:.0%} → ×{1+gen_add:.4f}")
        if extra_red_list:
            view["slot_hints"].append(
                "额外减伤（连乘）：" + " × ".join(f"{self._norm_pct(x):.0%}" for x in extra_red_list)
                + f" → 保留 {extra_red_kept:.4f}"
            )
        if gen_red:
            view["slot_hints"].append(f"通用减伤（相加）：-{gen_red:.0%} → 保留 {1-gen_red:.4f}")

        # 总增伤/减伤（通用相加，额外已连乘进因子）
        total_add = (1.0 + gen_add) * extra_add_factor
        # ★ total_red 是「减伤比例」不是「保留比例」：
        #   总减伤 = 1 - (1-通用) × Π(1-额外)
        #   之前写成 (1-gen_red)*extra_red_kept，无减伤时得 1.0，导致 ×(1-1)=0 全打光
        total_red = 1.0 - (1.0 - gen_red) * extra_red_kept

        # ④ 等级换算
        stage_mult = getattr(_f, "stage_mult", None)
        atk_real = float(atk)
        def_real = float(defense)
        if stage_mult:
            if atk_stage:
                atk_real = atk * stage_mult(int(atk_stage))
                view["slot_hints"].append(f"攻击等级 {atk_stage:+d} → 面板 {atk}→{atk_real:.0f}")
            if defense_stage:
                def_real = defense * stage_mult(int(defense_stage))
                view["slot_hints"].append(f"防御等级 {defense_stage:+d} → 面板 {defense}→{def_real:.0f}")

        # ④.5 克制倍率：显式给了 type_mult 就用它，否则用攻击/防守属性算
        tm = float(type_mult or 0.0)
        if tm <= 0:
            if attack_type and defend_type:
                try:
                    tv = self.type_view(attack_type, defend_type)
                    tm = float(tv.get("mult") or 1.0)
                    view["slot_hints"].append(
                        f"克制 {attack_type}→{defend_type} = ×{tm}"
                        + (f"（{tv.get('rule_hint')}）" if tv.get("rule_hint") else "")
                    )
                    if tv.get("missing_hint"):
                        view["slot_hints"].append(tv["missing_hint"])
                except Exception:
                    tm = 1.0
            else:
                tm = 1.0
        else:
            view["slot_hints"].append(f"克制倍率（显式给定）= ×{tm}")

        # ④.6 属性伤害的克制倍率（attr_mult）
        attr_mult = 1.0
        _at = str(attr_type or attack_type or "").strip()
        if attr_damage and _at and defend_type:
            try:
                attr_mult = float(self.type_view(_at, defend_type).get("mult") or 1.0)
                view["slot_hints"].append(f"属性伤害克制 {_at}→{defend_type} = ×{attr_mult}")
            except Exception:
                attr_mult = 1.0

        # ⑤ 抗性三件套
        cr, fr, pr, resist_bad = self._resolve_resist(resist)

        # ⑥ 分类型伤害解析
        flat_list, flat_bad = self._parse_segments_checked(flat)
        pct_list, pct_bad = self._parse_segments_checked(percent, percent_mode=True)

        # ★ 2026-11-10：百分比通道的系数是「目标最大体力的倍数」。
        #
        #   阈值这里踩过一次坑：最初写「正常上限 1.0（100% 体力）」，是错的。
        #   一层的「固定伤害、百分比伤害提升 N%」就能把它顶上去，实例如下：
        #
        #     帝录天锋 #4903 魂印#2090 + 敕赦千铭（先制+3）+ 追命宝石Ⅴ
        #       魂印：体力<1/2 时「攻击伤害为 0 时吸取对手 100% 体力」
        #       敕赦千铭：2回合内自身造成的固伤、百分比伤害 +30%
        #       追命Ⅴ：攻击伤害为 0 时吸取对手最大体力 20%
        #       → 单段 130% / 26%，逐段过 35% 抗后合计 101.4%（H=1165 时 101.5%）
        #       → 两层增粉（魂印登场效果那条也叠）则到 124.8%
        #
        #   所以这里不设「理论上限」，只设一个「明显是写法错误」的门槛：
        #   5.0 倍体力。真实的极端套路到不了这个量级，但想写 33% 却传了 "33"
        #   会被拦住（原来会算出 floor(1165×33)=38445 这种离谱值）。
        _pct_over = [x for x in pct_list if abs(x) > 5.0]
        if _pct_over:
            pct_bad = list(pct_bad) + [
                f"{x:g}（超过 5 倍体力，是不是想写 {x * 100:g}% 或 {x / 100:g}）"
                for x in _pct_over
            ]
            pct_list = [x for x in pct_list if abs(x) <= 5.0]

        # ★ 认不出的写法一律停下来报错，不再静默当 0 —— 参数写法由调用方负责，
        #   但「没听懂」这件事必须说出来，否则群里会念出「粉伤 0」这种假结论。
        _seg_bad: list[str] = []
        for _label, _bad_parts, _hint in (
            ("extra_add", extra_add_bad, "额外增伤写小数，如 0.2 或 0.2,0.15"),
            ("extra_reduce", extra_red_bad, "额外减伤写小数，如 0.1"),
            ("flat", flat_bad, "固定伤害写点数，如 5 或 1,1,1"),
            ("percent", pct_bad, "百分比写系数，如 0.125 / 1/3 / 33%"),
            ("resist", resist_bad, "抗性写 full / none / 0.35，或 0.35,0.2,0.1"),
        ):
            if _bad_parts:
                _seg_bad.append(f"{_label}：「{'、'.join(_bad_parts)}」认不出（{_hint}）")
        if _seg_bad:
            view["empty_hint"] = (
                "参数里有认不出的写法，已停下不猜 —— " + "；".join(_seg_bad)
            )
            return view

        # ⑦ 算
        try:
            _h = max(1, int(hits or 1))
            if _h > 1:
                # ── 连击专用链（facts「冰王·强一先手」确认的顺序）──
                #   base → 本系/克制 → floor → 浮动 → floor → 连击 → 增伤 → 减伤
                #   所以 combo_damage 这里只算到「× 连击」，增伤传 0，外面再乘。
                cr_ = _f.combo_damage(
                    int(level), int(round(pw)), int(round(atk_real)), int(round(def_real)),
                    stab=float(stab), type_mult=tm, crit=float(crit),
                    crit_reduce_pct=cr,
                    general_add=0.0,
                    extra_add_pct=0.0,
                    hit_count=_h,
                    reduce_total=0.0,
                    random_factor=rf,
                )
                _single_lo = int(cr_.get("single_min") or 0)
                _single_hi = int(cr_.get("single_max") or 0)
                _hit_lo = int(cr_.get("total_min") or 0)
                _hit_hi = int(cr_.get("total_max") or 0)
                # ② 乘增伤（通用相加 × 额外连乘）
                _add_lo = _m.floor(_hit_lo * total_add)
                _add_hi = _m.floor(_hit_hi * total_add)
                # ③ 乘减伤
                _fin_lo = _m.floor(_add_lo * (1.0 - min(total_red, 1.0)))
                _fin_hi = _m.floor(_add_hi * (1.0 - min(total_red, 1.0)))

                r = {
                    "attack_channel": _fin_lo,
                    "attack_channel_max": _fin_hi,
                    "single_channel": _single_lo,
                    "single_channel_max": _single_hi,
                    "hit_count": _h,
                    "before_add_min": _hit_lo,
                    "before_add_max": _hit_hi,
                    "flat_channel": 0, "percent_channel": 0,
                    "true_channel": int(true_damage or 0),
                    "attr_channel": 0,
                    "total_reduce": round(total_red, 3),
                }
                # 分类型伤害另算（不随连击翻倍）
                _ex = _f.settle(
                    int(level), int(round(pw)), int(round(atk_real)), int(round(def_real)),
                    type_mult=tm, type_mult_cap=float(type_mult_cap or 0.0),
                    stab=float(stab), crit=float(crit),
                    random_factor=(rf if rf is not None else 1.0),
                    general_add_pct=gen_add,
                    extra_add_pct=(extra_add_list[0] if extra_add_list else 0.0),
                    extra_add_more=(extra_add_list[1:] if len(extra_add_list) > 1 else None),
                    general_reduce_pct=gen_red,
                    extra_reduce_pct=(extra_red_list[0] if extra_red_list else 0.0),
                    extra_reduce_more=(extra_red_list[1:] if len(extra_red_list) > 1 else None),
                    crit_reduce_pct=cr,
                    flat_damage=(flat_list[0] if len(flat_list) == 1 else 0.0),
                    flat_segments=(flat_list if len(flat_list) > 1 else None),
                    flat_reduce=fr,
                    percent_damage=(pct_list[0] if len(pct_list) == 1 else 0.0),
                    percent_segments=(pct_list if len(pct_list) > 1 else None),
                    percent_reduce=pr,
                    true_damage=0.0,
                    attr_damage=float(attr_damage or 0.0),
                    attr_mult=attr_mult,
                    other=float(other or 1.0),
                    target_hp=float(target_hp or 0.0),
                )
                r["flat_channel"] = _ex.get("flat_channel", 0)
                r["percent_channel"] = _ex.get("percent_channel", 0)
                r["attr_channel"] = _ex.get("attr_channel", 0)
                r["total"] = (_fin_lo + int(r["flat_channel"]) + int(r["percent_channel"])
                              + int(r["true_channel"]) + int(r["attr_channel"]))
                r["total_max_all"] = (_fin_hi + int(r["flat_channel"]) + int(r["percent_channel"])
                                      + int(r["true_channel"]) + int(r["attr_channel"]))
                view["slot_hints"].append(
                    f"连击 ×{_h}：单段 {_single_lo}~{_single_hi} → ×{_h} = {_hit_lo}~{_hit_hi}"
                    f"（增伤在连击之后乘）→ 增伤后 {_add_lo}~{_add_hi} → 减伤后 {_fin_lo}~{_fin_hi}"
                )
            else:
                # ── 单段：走 settle 的全通道 ──
                #
                # ★ 2026-11-08 修（bot 自测报的 bug）：
                #   range 模式原来写 `random_factor=(rf if rf is not None else 1.0)`，
                #   rf=None 被当成 1.0 = 255/255 满浮动 —— **区间丢了，只出上限**。
                #   bot 反馈「random 传 range 只给单值，要传 min、max 才区分得开」。
                #   现在 rf=None 时分别按 217/255 和 255/255 各算一次，
                #   把下限放进 *_min 字段，并给一个 float_range。
                def _settle_one(_rf_val):
                    return _f.settle(
                        int(level), int(round(pw)), int(round(atk_real)), int(round(def_real)),
                        type_mult=tm,
                        type_mult_cap=float(type_mult_cap or 0.0),
                        stab=float(stab),
                        crit=float(crit),
                        random_factor=_rf_val,
                        general_add_pct=gen_add,
                        extra_add_pct=(extra_add_list[0] if extra_add_list else 0.0),
                        extra_add_more=(extra_add_list[1:] if len(extra_add_list) > 1 else None),
                        general_reduce_pct=gen_red,
                        extra_reduce_pct=(extra_red_list[0] if extra_red_list else 0.0),
                        extra_reduce_more=(extra_red_list[1:] if len(extra_red_list) > 1 else None),
                        crit_reduce_pct=cr,
                        flat_damage=(flat_list[0] if len(flat_list) == 1 else 0.0),
                        flat_segments=(flat_list if len(flat_list) > 1 else None),
                        flat_reduce=fr,
                        percent_damage=(pct_list[0] if len(pct_list) == 1 else 0.0),
                        percent_segments=(pct_list if len(pct_list) > 1 else None),
                        percent_reduce=pr,
                        true_damage=float(true_damage or 0.0),
                        attr_damage=float(attr_damage or 0.0),
                        attr_mult=attr_mult,
                        other=float(other or 1.0),
                        target_hp=float(target_hp or 0.0),
                    )

                if rf is None:
                    _lo = _settle_one(self.DMG_FLOAT_MIN / 255.0)
                    _hi = _settle_one(1.0)
                    r = dict(_hi)
                    r["total_min"] = _lo.get("total")
                    r["attack_channel_min"] = _lo.get("attack_channel")
                    r["float_range"] = [_lo.get("total"), _hi.get("total")]
                    r["float_range_pct"] = [
                        round(self.DMG_FLOAT_MIN / 255.0, 4), 1.0]
                else:
                    r = _settle_one(rf)
        except Exception as exc:
            view["empty_hint"] = f"计算失败：{exc}"
            return view

        view["ok"] = True
        view["result"] = r
        view["inputs"] = {
            "level": level, "power": power, "power_real": pw, "atk": atk, "defense": defense,
            "atk_real": atk_real, "def_real": def_real,
            "stab": stab, "crit": crit, "other": other, "hits": hits,
            "general_add": gen_add, "extra_add_factor": extra_add_factor,
            "general_reduce": gen_red, "extra_reduce_keep": extra_red_kept,
            "total_add": total_add, "total_red": total_red, "type_mult": tm,
            "type_mult_cap": type_mult_cap,
            "resist": (cr, fr, pr),
            "flat": flat_list, "percent": pct_list,
            "true_damage": true_damage, "attr_damage": attr_damage, "attr_type": attr_type,
            "target_hp": target_hp,
        }
        return view

    def skill_info(self, name: str, pet_id: int | None = None) -> dict[str, Any] | None:
        """按技能名查详情。

        ★ 2026-11-06 修重名 bug：传入 pet_id 时按「该精灵拥有的那个同名技能」精确查。
        原来只按名字查 + order by power desc limit 1，遇到重名会认错：
          「虚妄幻境」有 3 个 —— #23973(幻世帝天)、#26274(艾夏拉)、#31786(魔钰 威85)
          爱夏拉的那个是 #26274（弹控+受击睡眠+闪避，威力0），
          但按威力排序会固定取到魔钰那个 85 威力的版本，
          导致把「弹控」说成「吸强化+免疫1次异常」，配招判断全错。
        """
        base_cols = """select s.name, s.power, s.max_pp, s.accuracy, s.info, e.name, s.priority, s.id,
                      s.crit_rate, s.atk_num, s.must_hit, s.category_id, s.type_id"""
        nm = (name or "").strip()
        rows = []
        if pet_id:
            rows = self._q(
                base_cols
                + """ from skillinpetorm l join skill s on s.id=l.skill_id
                      left join element_type_combination e on e.id=s.type_id
                      where l.pet_id=? and s.name=? limit 1""",
                (pet_id, nm),
            )
        if not rows:
            rows = self._q(
                base_cols
                + """ from skill s left join element_type_combination e on e.id=s.type_id
                      where s.name=? order by s.power desc limit 1""",
                (nm,),
            )
        if not rows:
            rows = self._q(
                base_cols
                + """ from skill s left join element_type_combination e on e.id=s.type_id
                      where s.name like ? order by s.power desc limit 1""",
                (f"%{nm}%",),
            )
        if not rows:
            return None
        r = rows[0]
        return {
            "name": r[0],
            "power": r[1],
            "pp": r[2],
            "accuracy": r[3],
            "info": (r[4] or "").strip(),
            "type": (r[5] or "").replace(" ", "/").strip(),
            "priority": r[6],
            "effects": self.skill_effects(r[7]),
            "crit_rate": r[8],
            "atk_num": r[9],
            "must_hit": bool(r[10]),
            "category_id": r[11],
            "type_id": r[12],
            "skill_type": self.skill_type_name(r[12]),
        }

    _STATUS_CAT_RE = __import__("re").compile(r"^([\u4e00-\u9fa5]{2,6}类)异常状态")

    def pool_data_version(self) -> str:
        """pvp.db 的数据版本。

        2026-11-14 加：模型曾经在没有任何日期依据时自己编了个「10-30 那期」，
        而实际 pvp.db 的 ban_* 档期字段是空的。这里给出真实可追溯的版本号：
        优先取 manual_sync 落盘的 `.version` 旁注，没有就退到文件 mtime。
        游戏内「巅峰圣战精灵池」页面顶部显示的就是这个时间戳
        （实测 2026-11-13 19:00:33 对应上游文件名 pvp_20261113190033.db）。
        """
        import datetime as _dt
        try:
            path = os.path.join(self.manual_db_dir(), "pvp.db")
        except Exception:
            return ""
        if not os.path.isfile(path):
            return ""
        # ① 优先：manual_sync 落的上游文件名（pvp_20261113190033.db → 2026-11-13 19:00:33）
        try:
            vpath = path + ".version"
            if os.path.isfile(vpath):
                raw = open(vpath, encoding="utf-8").read().strip()
                m = re.search(r"_(\d{8})(\d{6})", raw)
                if m:
                    d, tm = m.group(1), m.group(2)
                    return f"{d[:4]}-{d[4:6]}-{d[6:8]} {tm[:2]}:{tm[2:4]}"
        except Exception:
            pass
        # ② 兜底：本地同步时间
        try:
            return _dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ""

    def _pool_rows(self) -> list[tuple[str, list[str], str]]:
        """读手册同步下来的 pvp.db，返回 [(池名, 精灵名列表, 截止时间)]。"""
        import json as _json

        rows = self._manual_q("pvp", "select name, pet, time from pvp")
        if not rows:
            return []
        names = {}
        for pid, pname in self._q("select id, name from pet"):
            names[int(pid)] = str(pname)
        if not names:
            for pid, pname in self._manual_q("pets", "select ID, Name from pets"):
                names[int(pid)] = str(pname)
        out = []
        for key, pets, deadline in rows:
            try:
                ids = _json.loads(str(pets).replace("'", '"'))
            except Exception:
                ids = []
            out.append((str(key), [names.get(int(i), f"#{i}") for i in ids], str(deadline or "")))
        return out

    @staticmethod
    def _pool_label(key: str) -> str:
        # 与手册「巅峰圣战精灵池」标签保持一致
        if key == "ban_0":
            return "禁止池（限0）"
        if key == "ban_2":
            return "限制池（限2）"
        if key == "ban_3":
            return "准限制池（限3）"
        if key.startswith("cost_"):
            return f"大师竞技点 {key.split('_')[1]} 点"
        if key.startswith("expert_"):
            return "专家禁用池"
        return key

    def rank_ban_list(self) -> list[dict]:
        """竞技模式限制池（按投票期次）。"""
        import json as _json
        import time as _time

        names = {}
        for pid, pname in self._q("select id, name from pet"):
            names[int(pid)] = str(pname)
        rows = self._kb.execute(
            "select period, kind, quota, start_ts, end_ts, pets, ranklimit1, ranklimit2 "
            "from rank_ban order by start_ts desc"
        ).fetchall()
        now = _time.time()
        out = []
        for period, kind, quota, t1, t2, pets, rl1, rl2 in rows:
            try:
                ids = _json.loads(pets or "[]")
            except Exception:
                ids = []
            out.append({
                "period": str(period),
                "kind": str(kind),
                "quota": int(quota or 0),
                "start": float(t1 or 0),
                "end": float(t2 or 0),
                "active": bool(t1 and t2 and float(t1) <= now <= float(t2)),
                "pets": [names.get(int(i), f"#{i}") for i in ids],
                "ranklimit": int(rl1 or 0),
            })
        return out

    POOL_ALIASES = {
        "限0": "ban_0", "限零": "ban_0", "禁用0": "ban_0",
        "限2": "ban_2", "限二": "ban_2", "禁用2": "ban_2",
        "限3": "ban_3", "限三": "ban_3", "禁用3": "ban_3",
        "专家": "expert_0",
        "0费": "cost_0", "6费": "cost_6", "10费": "cost_10",
        "20费": "cost_20", "35费": "cost_35",
    }

    def pool_match(self, arg: str) -> dict | None:
        """把「限二 / 限三 / 35 / 专家」这类说法解析成池子。"""
        kw = (arg or "").strip()
        if not kw:
            return None
        pools = self.pool_list()
        key = self.POOL_ALIASES.get(kw)
        if key:
            for p in pools:
                if p["key"] == key:
                    return p
        for p in pools:
            if kw in p["label"] or kw in p["key"]:
                return p
        return None

    def pool_list(self) -> list[dict]:
        return [
            {"key": k, "label": self._pool_label(k), "pets": pets, "deadline": d}
            for k, pets, d in self._pool_rows()
        ]

    # ────────────────────────────────────────────────────────────
    # 语义层：pool_view —— 巅峰池的唯一判定源
    #
    # 两边原来的能力互补：
    #   · CLI 只能「按精灵查它在哪些池」
    #   · bot 还能「列所有池」「查某池里有哪些精灵」
    # 现在统一到 pool_view，CLI 补上后两种。
    # ────────────────────────────────────────────────────────────
    def pool_view(self, pool: str = "", pet: str = "") -> dict[str, Any]:
        """巅峰池的语义层。

        mode:
          "pet"   给了 pet       → 这只精灵在哪些池
          "match" 给了 pool      → 这个池里有哪些精灵
          "list"  都没给         → 列出所有池
        判定字段：empty_hint
        """
        pt = str(pet or "").strip()
        pl = str(pool or "").strip()
        view: dict = {
            "mode": "list", "pet": pt, "pool": pl,
            "hits": [], "pools": [], "matched": None,
            "empty_hint": None,
            # 2026-11-14：真实数据版本，避免模型自行编造档期
            "version": self.pool_data_version(),
        }

        if pt:
            view["mode"] = "pet"
            try:
                hits = self.pool_search(pt) or []
            except Exception:
                hits = []
            view["hits"] = list(hits)
            if not hits:
                view["empty_hint"] = f"「{pt}」当前不在任何池子里。"
            return view

        try:
            pools = self.pool_list() or []
        except Exception:
            pools = []
        view["pools"] = list(pools)
        if not pools:
            view["empty_hint"] = "还没有池子数据。"
            return view

        if not pl:
            view["mode"] = "list"
            return view

        view["mode"] = "match"
        try:
            m = self.pool_match(pl)
        except Exception:
            m = None
        view["matched"] = m
        if not m:
            view["empty_hint"] = (
                f"没有叫「{pl}」的池子。可选：" + "、".join(x["label"] for x in pools[:12])
            )
        return view

    def pool_search(self, pet_name: str) -> list[dict]:
        kw = (pet_name or "").strip()
        if not kw:
            return []
        return [p for p in self.pool_list() if any(kw in name for name in p["pets"])]

    def status_structured(self, category: str = "", limit: int = 80) -> list[dict[str, str]]:
        """结构化异常状态：分类走关联表、抗性走 resistance_category，比正则准。"""
        rows = self._q(
            """select b.id, b.name, b.desc, coalesce(r.name,'无') as resist
               from battle_effect b
               left join resistance_category r on r.id = b.resistance_id
               order by b.id"""
        )
        links: dict[int, list[str]] = {}
        for eid, tname in self._q(
            """select l.battle_effect_id, t.name from battleeffectcategorylink l
               join battle_effect_type t on t.id = l.type_id"""
        ):
            links.setdefault(int(eid), []).append(str(tname))

        out: list[dict[str, str]] = []
        for eid, name, desc, resist in rows:
            cats = links.get(int(eid), [])
            cat_text = "/".join(cats) if cats else "未分类"
            if category and not any(category in c for c in cats):
                continue
            out.append({
                "id": str(eid), "name": str(name), "category": cat_text,
                "resist": str(resist), "desc": (desc or "").strip(),
                "icon": str(eid),          # abnormal/{id}.png
            })
            if len(out) >= limit:
                break
        return out

    def manual_db_dir(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(self._kb_path)), "manual_db")

    def _manual_q(self, name: str, sql: str, args: tuple = ()) -> list[tuple]:
        """查询手册同步下来的数据库（manual_db/{name}.db）。"""
        path = os.path.join(self.manual_db_dir(), f"{name}.db")
        if not os.path.isfile(path):
            return []
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        except sqlite3.Error:
            return []
        try:
            return con.execute(sql, args).fetchall()
        except sqlite3.Error:
            return []
        finally:
            con.close()

    # ── 手册 pets.db 回退（2026-11-06 加）──────────────────────────────
    # 手册源（火の辞典）比 seerapi 快。实例：圣光灵神SP(#13311) 属于
    # 「同序号双形态」——手册 pets.db 已有完整种族值/技能表/介绍，
    # 而 seerapi 的 pet 表还没这一行（只有魂印和名词挂在 13311 下）。
    # 这里一律**只做加法**：seerapi 查得到就原样返回，查空才回退手册，
    # 保证现有精灵的查询路径一个字都不变。

    def _manual_pet_row(self, pet_id: int) -> tuple:
        """手册 pets.db 取一只（只要书页条目 isBook=1）。

        字段顺序（改 SQL 时必须和 _manual_pet_stats 里的解包顺序保持一致）：
            0 ID / 1 Name / 2 Type / 3 Atk / 4 SpAtk / 5 Def / 6 SpDef / 7 Spd / 8 HP
            9 des / 10 LearnableMoves / 11 ExtraMoves / 12 PetClass
            13 NewAtk / 14 NewDef / 15 NewSpAtk / 16 NewSpDef / 17 NewSpd / 18 NewHP
            （New* 是觉醒后种族值，全库只有 28 只非空，和 seerapi.pet_advance 的 28 条对应）
        """
        rows = self._manual_q(
            "pets",
            """select ID, Name, Type, Atk, SpAtk, Def, SpDef, Spd, HP,
                      des, LearnableMoves, ExtraMoves, PetClass,
                      NewAtk, NewDef, NewSpAtk, NewSpDef, NewSpd, NewHP
               from pets where ID=? and isBook=1 limit 1""",
            (int(pet_id),),
        )
        return tuple(rows[0]) if rows else ()

    def manual_find_pets(self, name: str, limit: int = 5) -> list[tuple[int, str]]:
        """手册里按名字找精灵，按种族和降序（与 seerapi 侧「同名取最强」一致）。"""
        name = (name or "").strip()
        if not name:
            return []
        rows = self._manual_q(
            "pets",
            """select ID, Name from pets
               where isBook=1 and Name like ?
               order by (coalesce(Atk,0)+coalesce(SpAtk,0)+coalesce(Def,0)
                        +coalesce(SpDef,0)+coalesce(Spd,0)+coalesce(HP,0)) desc
               limit ?""",
            (f"%{name}%", int(limit)),
        )
        return [(int(r[0]), r[1]) for r in rows]

    @staticmethod
    def _manual_move_ids(*blobs) -> list[tuple[int, int]]:
        """把手册的 LearnableMoves / ExtraMoves 摊平成 [(技能ID, 学习等级)]。

        手册存的是 Python dict 字面量（单引号），用 literal_eval 而不是 json.loads。
        """
        out: list[tuple[int, int]] = []
        seen: set[int] = set()
        for blob in blobs:
            if not blob:
                continue
            data = blob
            if isinstance(blob, str):
                try:
                    data = ast.literal_eval(blob)
                except (ValueError, SyntaxError):
                    continue
            if not isinstance(data, dict):
                continue
            for key in ("Move", "AdvMove", "SpMove"):
                for item in data.get(key) or []:
                    if not isinstance(item, dict):
                        continue
                    try:
                        sid = int(item.get("ID"))
                    except (TypeError, ValueError):
                        continue
                    if sid in seen:
                        continue
                    seen.add(sid)
                    try:
                        lv = int(item.get("LearningLv") or 0)
                    except (TypeError, ValueError):
                        lv = 0
                    out.append((sid, lv))
        return out

    # ── 手册跨库关联（2026-11-06）──────────────────────────────────────
    # manual_db 是 15 个彼此独立的 sqlite 文件，原先只能一个一个查，
    # 没法像 seerapi 那样 JOIN。但关联字段其实都在：
    #     effectDes.monster    → 精灵 id（多为单值，少量逗号分隔，如 '4321,4400'）
    #     mintmark.MonsterID   → 专属精灵 id
    #     petEffectIcon.id     → 精灵 id（⚠️ 只覆盖 id≥2724 的 1078 只）
    #     pets.LearnableMoves  → 技能 id 列表（Python dict 字面量，需应用层解析）
    #     pvp.pet              → 池内精灵 id 列表
    # 这里用 ATTACH 把它们拼成一个连接，就能写跨库 JOIN 了。

    _MANUAL_BUNDLE_DEFAULT = ("pets", "moves", "effectDes", "mintmark", "effectIcon", "petEffectIcon", "te_xing")

    def manual_bundle(self, *names: str):
        """把若干手册库 ATTACH 到同一个内存连接，返回可用跨库 JOIN 的连接。

        用完必须 close()——它持有 6 个文件句柄。
        """
        con = sqlite3.connect(":memory:", timeout=3)
        for n in names or self._MANUAL_BUNDLE_DEFAULT:
            p = os.path.join(self.manual_db_dir(), f"{n}.db")
            if not os.path.isfile(p):
                continue
            try:
                con.execute(f'ATTACH DATABASE ? AS "{n}"', (p,))
            except sqlite3.Error:
                continue
        return con

    def manual_pet_bundle(self, pet_id: int) -> dict[str, Any]:
        """手册里一只精灵的全部关联：名词 / 专属刻印 / 技能 id / 魂印文本。"""
        out: dict[str, Any] = {"pet": None, "terms": [], "mintmarks": [], "skills": [], "soulmarks": [], "soulmark": ""}
        con = self.manual_bundle()
        try:
            row = con.execute(
                "select ID, Name, Type, PetClass from pets.pets where ID=? and isBook=1", (pet_id,)
            ).fetchone()
            if row:
                out["pet"] = {"id": row[0], "name": row[1], "type": row[2], "pet_class": row[3]}
            # 名词：monster 可能是 '3311' 或 '4321,4400'
            for r in con.execute(
                """select id, kinddes, desc, kind from effectDes.effectDes
                   where ',' || replace(monster, ' ', '') || ',' like ?""",
                (f"%,{pet_id},%",),
            ):
                out["terms"].append({"id": r[0], "name": r[1], "desc": (r[2] or "").strip(), "kind": r[3]})
            # 专属刻印
            for r in con.execute(
                """select ID, Des, MaxAttriValue from mintmark.MintMark
                   where ',' || replace(replace(MonsterID, '[', ''), ']', '') || ',' like ?""",
                (f"%,{pet_id},%",),
            ):
                out["mintmarks"].append({"id": r[0], "name": r[1], "max_attr": r[2]})
            # 魂印：effectIcon.tips 才是全文主库（2162 条，全覆盖），
            # petEffectIcon 只是「带图标的子集」（1200 条，容易误判成缺数据）。
            # ⚠️ pet_id 是数组格式 '[3311]' / '[3311,3329]'，只能用 LIKE。
            try:
                rows = con.execute(
                    """select id, tips from effectIcon.effectIcon
                       where ',' || replace(replace(pet_id, '[', ''), ']', '') || ',' like ?
                       order by id""",
                    (f"%,{pet_id},%",),
                ).fetchall()
            except sqlite3.Error:
                rows = []
            if rows:
                out["soulmarks"] = [
                    {"id": r[0], "text": (r[1] or "").strip()} for r in rows
                ]
                out["soulmark"] = "|".join(x["text"] for x in out["soulmarks"])
            # 技能：JSON 在 pets 里，用 Python 解析后去 moves 查
            r = con.execute(
                "select LearnableMoves, ExtraMoves, SpExtraMoves from pets.pets where ID=?", (pet_id,)
            ).fetchone()
            if r:
                pairs = self._manual_move_ids(r[0], r[1], r[2])
                ids = [sid for sid, _ in pairs]
                if ids:
                    ph = ",".join("?" * len(ids))
                    rows = con.execute(
                        f"select id, name, power, category, priority from moves.moves where id in ({ph})",
                        tuple(ids),
                    ).fetchall()
                    lv = dict(pairs)
                    for m in rows:
                        out["skills"].append(
                            {"id": m[0], "name": m[1], "power": m[2], "category": m[3],
                             "priority": m[4], "level": lv.get(m[0], 0)}
                        )
        except sqlite3.Error:
            pass
        finally:
            con.close()
        return out

    def manual_status_list(self, category: str = "", limit: int = 80) -> list[dict[str, str]]:
        """用手册数据库里的 effectDes（kind=2）列异常状态，比 seerapi 新。"""
        rows = self._manual_q(
            "effectDes",
            "select id, kinddes, desc, icon from effectDes where kind=2 order by id",
        )
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        known = ("控制类", "弱化类", "附属类", "限制类", "增益类")
        for gid, name, desc, icon in rows:
            text = (desc or "").strip()
            if not name or name in seen:
                continue
            if "异常状态" not in text:
                continue
            seen.add(name)
            cats = [c for c in known if c in text]
            cat_text = "/".join(cats) if cats else "未分类"
            if category and not any(category in c for c in cats):
                continue
            out.append({
                "id": str(gid), "name": name, "category": cat_text,
                "desc": text, "icon": str(icon if icon is not None else ""),
            })
            if len(out) >= limit:
                break
        return out

    def status_icon_path(self, name: str) -> str:
        """异常状态的本地图标路径（需先同步/下载）。"""
        hit = [x for x in self.status_list(limit=200) if x["name"] == name]
        if not hit:
            return ""
        icon = str(hit[0].get("icon") or "")
        if not icon.isdigit():
            return ""
        path = os.path.join(self.manual_db_dir(), "icons", f"abnormal_{icon}.png")
        return path if os.path.isfile(path) else ""

    def status_icon_ids(self) -> list[int]:
        """所有异常状态的图标编号，用于批量预下载。"""
        ids = []
        for x in self.manual_status_list(limit=200):
            icon = str(x.get("icon") or "")
            if icon.isdigit():
                ids.append(int(icon))
        return sorted(set(ids))

    def status_list(self, category: str = "", limit: int = 60) -> list[dict[str, str]]:
        """异常状态清单：取 kind=2 全部条目，解析描述里的所有分类（可多分类）。"""
        structured = self.status_structured(category, limit)
        if structured:
            return structured
        manual = self.manual_status_list(category, limit)
        if manual:
            return manual
        import re as _re
        rows = self._q(
            "select id, name, desc from glossary_entry where kind=2 order by id"
        )
        out: list[dict[str, str]] = []
        for gid, name, desc in rows:
            text = (desc or "").strip()
            if "异常状态" not in text:
                continue          # 护盾/护罩这类不是异常状态
            known = ("控制类", "弱化类", "附属类", "限制类", "增益类")
            cats = [c for c in known if c in text]
            cat_text = "/".join(cats) if cats else "未分类"
            if category and not any(category in c for c in cats):
                continue
            out.append({"id": str(gid), "name": name, "category": cat_text, "desc": text})
            if len(out) >= limit:
                break
        return out

    def term(self, keyword: str) -> list[dict[str, str]]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        out: list[dict[str, str]] = []
        for name, desc, kind in self._q(
            "select name, desc, kind from glossary_entry where name like ? or desc like ? limit 6",
            (f"%{keyword}%", f"%{keyword}%"),
        ):
            out.append({"name": name, "desc": desc or "", "kind": f"术语{kind}"})
        for (desc,) in self._q("select desc from soulmark where desc like ? limit 4", (f"%{keyword}%",)):
            out.append({"name": f"魂印效果（含「{keyword}」）", "desc": desc or "", "kind": "魂印"})
        return out

    TYPE_SEPS = ("/", "、", ",", "，", "+", "|", "·", " ")

    def parse_type_list(self, raw: str) -> list[str]:
        """把「火/草」「火、草」「次元龙」这类写法拆成属性列表。"""
        text = str(raw or "").strip()
        if not text:
            return []
        for sep in self.TYPE_SEPS:
            if sep in text:
                return [x.strip() for x in text.split(sep) if x.strip()][:2]
        if text in self._type_names():
            return [text]
        # 无分隔符的双属性写法（如「次元龙」）：按已知属性名切分
        names = sorted(self._type_names(), key=len, reverse=True)
        parts: list[str] = []
        rest = text
        while rest and len(parts) < 2:
            for n in names:
                if rest.startswith(n):
                    parts.append(n)
                    rest = rest[len(n):]
                    break
            else:
                break
        if parts and not rest:
            return parts
        return [text]

    def _type_names(self) -> set[str]:
        cached = getattr(self, "_type_name_cache", None)
        if cached is None:
            cached = {str(r[0]) for r in self._q("select name from element_type")}
            self._type_name_cache = cached
        return cached

    def _raw_mult(self, attack: str, defend: str) -> float | None:
        rows = self._q(
            """select r.multiple from elementtyperelationorm r
               join element_type a on a.id=r.source_id
               join element_type d on d.id=r.target_id
               where a.name=? and d.name=?""",
            (attack.strip(), defend.strip()),
        )
        return float(rows[0][0]) if rows else None

    # ────────────────────────────────────────────────────────────
    # 语义层：type_view —— 属性克制的唯一判定源
    #
    # 原来 bot 的 seer_type_chart 和 CLI 的 cmd_type 各写一份拆解/合成规则说明，
    # 而且 bot 那份走配置 dual_type_mode（曾经可能是 multiply），
    # 两边口径可以不一致。现在倍率、拆解、合成规则、缺失警告都只在这里产。
    # ────────────────────────────────────────────────────────────
    def type_view(self, attack: str, defend: str, mode: str = "seer") -> dict[str, Any]:
        """属性克制的语义层：只产结构化数据，不做排版。

        返回里 rule_hint / missing_hint 是**必须被两边呈现**的判定字段。
        """
        atk = self.parse_type_list(attack)
        dfd = self.parse_type_list(defend)
        if not atk or not dfd:
            return {
                "error": "属性名解析失败",
                "available": self.type_names(),
                "atk": atk, "dfd": dfd, "mult": None,
                "feel": None, "pairs": [], "rule_hint": None, "missing_hint": None,
            }

        d = self.type_multiplier_detail(atk, dfd, mode=mode)
        mult = d.get("mult")

        view: dict = {
            "error": d.get("error"),
            "atk": atk,
            "dfd": dfd,
            "mult": mult,
            "feel": None,
            "pairs": d.get("pairs") or [],
            "contribs": d.get("contribs") or [],
            "rule_hint": None,
            "missing_hint": None,
        }

        if mult is not None:
            if mult == 0:
                view["feel"] = "完全免疫"
            elif mult > 1:
                view["feel"] = "克制"
            elif mult < 1:
                view["feel"] = "被抵抗"
            else:
                view["feel"] = "无克制关系"

        # 缺失关系警告
        if d.get("missing"):
            view["missing_hint"] = (
                "⚠ 库中缺少这些关系：" + "、".join(d["missing"])
                + "（已按 ×1.0 占位，不能当成确定结论。"
                "先说「库里没有收录这条关系」，再按属性常识给推断值并标明是推断，"
                "不要把占位的 ×1.0 说成真实倍率）"
            )

        # 合成规则说明（判定字段·两边必须渲染）
        pairs = view["pairs"]
        if len(pairs) > 1 and mult is not None:
            vals = [m for _, _, m in pairs]
            if len(dfd) == 1:
                if len(vals) == 2:
                    if vals[0] == 2 and vals[1] == 2:
                        view["rule_hint"] = (
                            "双2特例：两项都是 ×2 → 相加得 ×4（不除以2）。"
                            "★ 双系防守里每一侧内部也要走这条，别把 2.0+2.0 平均成 2.0"
                        )
                    elif 0 in vals:
                        view["rule_hint"] = f"含免疫(×0)：两项相加后 ÷4 = ×{mult}"
                    else:
                        view["rule_hint"] = (
                            f"其他情形：两项相加后 ÷2 = ×{mult}"
                        )
            else:
                # 双系防守：每侧内部先按三联规则算，再对两侧取平均
                sides = []
                for side in dfd:
                    sv = [m for _, y, m in pairs if y == side]
                    if len(sv) == 2:
                        a, b = sv
                        if a == 2 and b == 2:
                            sides.append((side, 4.0, "×2+×2=×4"))
                        elif 0 in sv:
                            sides.append((side, (a + b) / 4, f"(×{a}+×{b})÷4"))
                        else:
                            sides.append((side, (a + b) / 2, f"(×{a}+×{b})÷2"))
                    elif len(sv) == 1:
                        sides.append((side, sv[0], f"×{sv[0]}"))
                detail = "；".join(f"{s} 侧 {r} = ×{v}" for s, v, r in sides)
                view["rule_hint"] = (
                    "双系防守：每侧的两位攻方先按三联规则算出该侧系数，再对各侧取平均"
                    + ("（" + detail + "）" if detail else "")
                    + f" → ×{mult}"
                )
        elif len(pairs) == 1 and mult is not None:
            view["rule_hint"] = f"单系对单系：直接取表值 ×{mult}"

        return view

    def type_multiplier_detail(
        self,
        attack_types: list[str],
        defend_types: list[str],
        mode: str = "seer",
        cap: float = 0.0,
    ) -> dict[str, Any]:
        """双属性克制。

        ★ 默认 mode 必须是 "seer"（2026-11-03 改）：
          "multiply" 是简单相乘，会把双→双算成 0.5×0.5=0.25 之类的错值；
          赛尔官方用的是见「双2特例 / 含0除4 / 其余平均」那一套。
          之前默认是 multiply，靠配置 dual_type_mode=seer 兜着，
          但只要有人不传 mode 就会静默退回错误算法 —— 这种默认值是陷阱。
        """
        atk = [t for t in attack_types if t][:2]
        dfd = [t for t in defend_types if t][:2]
        # 攻击侧也允许 2 个：赛尔存在双属性技能（例：阅世 = 次元·龙）

        pairs: list[tuple[str, str, float]] = []
        missing: list[str] = []
        for a in atk:
            for d in dfd:
                m = self._raw_mult(a, d)
                if m is None:
                    # 库里没有这条关系：不要默认 1.0，标记出来让上层显式说明
                    missing.append(f"{a}→{d}")
                    m = 1.0
                pairs.append((a, d, m))
        if not pairs:
            return {"mult": None, "pairs": [], "mode": mode, "missing": missing}
        values = [p[2] for p in pairs]
        if mode == "seer" and len(atk) >= 2 and len(dfd) >= 2:
            # 双属性技能 × 双属性防守：
            # 每个技能系各自按「单系 vs 双防守」结算，含 ×0（免疫）的系别直接记 0，
            # 再把两个系别的结果取平均。
            # 例：次元·龙 → 冰/暗影：次元含免疫→0；龙=(2+1)/2=1.5 → (0+1.5)/2 = ×0.75
            # 正确算法：**拆防守方**（不能拆攻击方），
            # 每个防守系算出「双→单」的系数（双2得4 / 含0除4 / 其他除2），
            # 最后对这些系数取平均。
            # 旧实现遍历攻击方、且含0时"整系记0"，会把
            #   战斗·龙 → 冰/暗影 算成 1.375（应为 2.375）
            contribs = []
            for d in dfd:
                vals = [m for _, y, m in pairs if y == d]
                if not vals:
                    continue
                if len(vals) == 1:
                    contribs.append(vals[0])
                elif len(vals) == 2:
                    v1, v2 = vals[0], vals[1]
                    if v1 == 2 and v2 == 2:
                        contribs.append(4.0)
                    elif 0 in vals:
                        contribs.append((v1 + v2) / 4)
                    else:
                        contribs.append((v1 + v2) / 2)
                else:
                    contribs.append(sum(vals) / len(vals))
            mult = sum(contribs) / len(contribs) if contribs else 1.0
            return {
                "mult": round(mult, 3),
                "raw": round(mult, 3),
                "pairs": pairs,
                "mode": "seer-dual-skill",
                "cap": cap,
                "missing": missing,
                "contribs": [round(c, 3) for c in contribs],
            }
        if mode == "seer":
            # 完全复刻「伤害计算器1.1」的 SX_KZ 规则
            n = len(values)
            if n == 1:
                mult = values[0]
            elif n == 2:
                if values[0] == 2 and values[1] == 2:
                    mult = 4.0
                elif 0 in values:
                    mult = sum(values) / 4
                else:
                    mult = sum(values) / 2
            else:
                mult = sum(values) / 4 + sum(0.5 for v in values if v == 2)
        elif mode == "max":
            mult = max(values)
        elif mode == "average":
            mult = sum(values) / len(values)
        else:
            mult = 1.0
            for v in values:
                mult *= v
        raw_mult = mult
        if cap and cap > 0:
            mult = min(mult, cap)
        return {
            "mult": round(mult, 3),
            "raw": round(raw_mult, 3),
            "pairs": pairs,
            "mode": mode,
            "cap": cap,
            "missing": missing,
            "incomplete": bool(missing),
        }

    def type_multiplier(self, attack_type: str, defend_type: str) -> float | None:
        """支持单属性或双属性（如「火/草」）。"""
        detail = self.type_multiplier_detail(
            self.parse_type_list(attack_type), self.parse_type_list(defend_type)
        )
        return detail["mult"]

    def type_names(self) -> list[str]:
        return [r[0] for r in self._q("select name from element_type order by id")]

    # ---------------- 定型配招 ----------------

    def set_moveset(self, pet_id: int, skills: list[str], note: str = "", detail: str = "") -> None:
        import json as _json
        self._backup_kb("set-moveset")
        self._kb.execute(
            """insert into movesets(pet_id, skills, note, detail, updated_at) values(?,?,?,?,?)
               on conflict(pet_id) do update set skills=excluded.skills,
                 note=excluded.note, detail=excluded.detail, updated_at=excluded.updated_at""",
            (pet_id, _json.dumps(skills, ensure_ascii=False), note, detail, time.time()),
        )
        self._kb.commit()

    def get_moveset_detail(self, pet_id: int) -> str:
        row = self._kb.execute("select detail from movesets where pet_id=?", (pet_id,)).fetchone()
        return (row[0] or "") if row else ""

    def get_moveset(self, pet_id: int) -> list[str]:
        import json as _json
        row = self._kb.execute("select skills from movesets where pet_id=?", (pet_id,)).fetchone()
        if not row:
            return []
        try:
            return _json.loads(row[0])
        except Exception:
            return []

    def clear_moveset(self, pet_id: int) -> bool:
        self._backup_kb("clear-moveset")
        cur = self._kb.execute("delete from movesets where pet_id=?", (pet_id,))
        self._kb.commit()
        return cur.rowcount > 0

    # ---------------- 效果标签（借鉴「火の辞典」的 tag 体系） ----------------

    TAG_RULES: tuple[tuple[str, str], ...] = (
        ("先制", r"先制"),
        ("增伤", r"(造成的)?(攻击)?伤害(额外)?(提升|提高|增加)"),
        ("减伤", r"受到的?(攻击)?伤害(额外)?(减少|降低)"),
        ("致命一击", r"致命一击|暴击"),
        ("免伤", r"免疫(受到的)?(攻击)?伤害|抵挡.{0,6}攻击"),
        ("免疫异常", r"免疫.{0,8}(异常|控制|弱化)"),
        ("施加异常", r"令对手(陷入|进入|处于).{0,10}(异常|中毒|烧伤|冻伤|麻痹|害怕|睡眠|冰封|诅咒|感染|焚烬)"),
        ("附加伤害", r"附加.{0,8}(伤害|固定伤害|百分比伤害)"),
        ("固定伤害", r"固定伤害"),
        ("百分比伤害", r"百分比伤害"),
        ("真实伤害", r"真实伤害"),
        ("恢复", r"(恢复|回复|吸取).{0,8}(体力|HP)"),
        ("闪避", r"闪避"),
        ("强化", r"全属性\+|能力(等级)?提升|强化"),
        ("弱化", r"能力(等级)?下降|弱化"),
        ("反伤", r"反弹"),
        ("穿防", r"无视.{0,4}(防御|护盾)"),
        ("回PP", r"恢复.{0,6}PP"),
        ("清PP", r"(减少|消除|清空).{0,6}PP"),
    )

    def build_effect_tags(self, force: bool = False) -> int:
        """扫描技能效果与魂印文本，按关键词打标签，写入 tags 表。

        2026-11-04 修复（四号鱼）：旧实现是「先 delete from tags，再去 seerapi 生成行」，
        一旦 seerapi 不可读（`_q()` 返回空）或中途异常，tags 会被清空却照样写
        tags_built=1 —— 标签功能永久静默失效，且不会自愈。
        现在改成：先把行全部生成完，为空就拒绝动数据；删和插放在同一事务，失败回滚。
        """
        import re as _re

        with self._lock:
            self._kb.executescript(
                """
                CREATE TABLE IF NOT EXISTS tags (
                    kind TEXT NOT NULL,
                    ref_id INTEGER NOT NULL,
                    name TEXT,
                    tag TEXT NOT NULL,
                    PRIMARY KEY (kind, ref_id, tag)
                );
                CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
                """
            )
            done = self._kb.execute("select value from meta where key='tags_built'").fetchone()
            if done and not force:
                return 0

        compiled = [(_re.compile(pat), tag) for tag, pat in self.TAG_RULES]
        rows: list[tuple[str, int, str, str]] = []

        # 技能：skilleffectlink → skill_effect_in_use.info
        for sid, sname, info in self._q(
            """select distinct s.id, s.name, u.info
               from skilleffectlink l
               join skill s on s.id = l.skill_id
               join skill_effect_in_use u on u.id = l.effect_in_use_id"""
        ):
            text = str(info or "")
            for rx, tag in compiled:
                if rx.search(text):
                    rows.append(("skill", int(sid), str(sname or ""), tag))

        # 魂印：soulmark.desc
        for smid, desc in self._q("select id, desc from soulmark"):
            text = str(desc or "")
            for rx, tag in compiled:
                if rx.search(text):
                    rows.append(("soulmark", int(smid), "", tag))

        if not rows:
            logger.warning(
                "[kb] build_effect_tags 生成 0 条标签（seerapi 可能读不到），保留原有 tags 不动"
            )
            return 0

        with self._lock:
            old_count = self._kb.execute("select count(*) from tags").fetchone()[0]
            if old_count and not force and len(rows) < old_count * 0.5:
                logger.warning(
                    "[kb] build_effect_tags 新结果 %d 条不足现有 %d 条的一半，疑上游没读全，已跳过（force=True 可强制重建）",
                    len(rows),
                    old_count,
                )
                return 0
            self._backup_kb("tags-rebuild")
            try:
                self._kb.execute("delete from tags")
                self._kb.executemany(
                    "insert or ignore into tags(kind, ref_id, name, tag) values(?,?,?,?)", rows
                )
                self._kb.execute(
                    "insert into meta(key, value) values('tags_built', '1') "
                    "on conflict(key) do update set value='1'"
                )
                self._kb.commit()
            except Exception:
                self._kb.rollback()
                logger.warning("[kb] build_effect_tags 写入失败，已回滚，原 tags 保留")
                raise
        return len(rows)

    def tag_search(self, tag: str, limit: int = 20) -> tuple[int, list[tuple[str, str, str]]]:
        """按标签反查技能/魂印。返回 (总数, [(kind, name, 说明起点)])。

        2026-11-04（四号鱼）：空串会走 `like '%%'` 命中整张 tags 表（实测 38959 条），
        bot 侧一旦漏传参数就会把全表刷出来。空串直接返回空结果。
        """
        tag = (tag or "").strip()
        if not tag:
            return 0, []
        with self._lock:
            total = self._kb.execute("select count(*) from tags where tag like ?", (f"%{tag}%",)).fetchone()[0]
            rows = self._kb.execute(
                """select kind, name, ref_id from tags where tag like ?
                   order by kind, ref_id limit ?""",
                (f"%{tag}%", limit),
            ).fetchall()
        return total, [(r[0], r[1] or f"#{r[2]}", r[2]) for r in rows]

    def tag_stats(self) -> list[tuple[str, int]]:
        with self._lock:
            return self._kb.execute(
                "select tag, count(*) from tags group by tag order by 2 desc"
            ).fetchall()

    # ---------------- 问答记忆 ----------------

    def remember(self, topic: str, content: str, source: str = "chat") -> None:
        """写入/覆盖一条 facts。

        ★ 2026-11-10 上限 600 → 1200（鱼片定的）。
          原因：`content[:600]` 是**静默截断**——切掉的内容永久丢失，
          库里存的跟完整条目长得一模一样，读取方（bot）无从察觉。
          实测 667 字进去只剩 600 字，尾部的关键结论直接消失且无任何标记。
          现在的处理：
            · ≤1200 → 原样写入
            · >1200  → 截断，但【在结尾留标记】并打 warning，
                       让读取方知道这条不完整、该去问人
        """
        topic = (topic or "").strip()[:60]
        raw = (content or "").strip()
        LIMIT = 1200
        if len(raw) > LIMIT:
            logger.warning(
                "[facts] 主题 %r 正文 %d 字，超过上限 %d，已截断",
                topic, len(raw), LIMIT,
            )
            raw = raw[:LIMIT] + f"…（原文 {len(raw)} 字，已截断）"
        content = raw
        if not topic or not content:
            return
        self._backup_kb("remember")
        self._kb.execute(
            """insert into facts(topic, content, source, updated_at) values(?,?,?,?)
               on conflict(topic) do update set content=excluded.content,
                 source=excluded.source, updated_at=excluded.updated_at""",
            (topic, content, source, time.time()),
        )
        self._kb.commit()

    def fact_exists_by_content(self, content_prefix: str) -> bool:
        """检查是否已有内容相似的 fact（用于写入前去重）。"""
        row = self._kb.execute(
            "select 1 from facts where content like ? limit 1",
            (f"%{content_prefix}%",),
        ).fetchone()
        return row is not None

    def recall(self, topic: str, limit: int = 5) -> list[tuple[str, str]]:
        """按关键词召回 facts。

        ★ 2026-11-08 修：原来只 `order by updated_at desc` —— 谁最近改过谁排第一。
          后果很具体：刚补过「空元行者·技能代指对照（定稿）」里"强化=告命诗途"，
          然后群里问「灵巢强化位放什么」，recall("强化") 第一条就是空元那条，
          bot 会把空元的代称套到别的精灵上。

          现在改成**两段排序**：
            ① topic 里含关键词的优先（问「空元」才把空元条目顶上来）
            ② 同档内再按 updated_at 倒序（新知识优先）
          纯在 content 里出现的（比如通用词命中某条正文）排到后面。
        """
        topic = (topic or "").strip()
        if not topic:
            return []
        like = f"%{topic}%"
        rows = self._kb.execute(
            "select topic, content from facts where topic like ? or content like ? "
            "order by (case when topic like ? then 0 else 1 end), updated_at desc "
            "limit ?",
            (like, like, like, limit),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    # ---------------- 学习状态 ----------------

    def mark_seen(self, key: str, kind: str) -> int:
        now = time.time()
        row = self._kb.execute("select count from seen where key=?", (key,)).fetchone()
        if row:
            self._kb.execute("update seen set count=count+1, last_at=? where key=?", (now, key))
            n = row[0] + 1
        else:
            self._kb.execute(
                "insert into seen(key, kind, count, first_at, last_at) values(?,?,1,?,?)",
                (key, kind, now, now),
            )
            n = 1
        self._kb.commit()
        return n

    def get_summary(self, key: str) -> tuple[str, str] | None:
        row = self._kb.execute("select summary, src_hash from summaries where key=?", (key,)).fetchone()
        return (row[0], row[1] or "") if row else None

    def put_summary(self, key: str, kind: str, src_hash: str, summary: str) -> None:
        self._kb.execute(
            """insert into summaries(key, kind, src_hash, summary, updated_at) values(?,?,?,?,?)
               on conflict(key) do update set src_hash=excluded.src_hash,
                 summary=excluded.summary, updated_at=excluded.updated_at""",
            (key, kind, src_hash, summary, time.time()),
        )
        self._kb.commit()

    def delete_summary(self, key: str) -> None:
        self._kb.execute("delete from summaries where key=?", (key,))
        self._kb.commit()

    def add_pending(self, key: str, question: str) -> None:
        self._kb.execute(
            """insert into pending(key, question, created_at, resolved) values(?,?,?,0)
               on conflict(key) do update set question=excluded.question, resolved=0""",
            (key, question, time.time()),
        )
        self._kb.commit()

    def list_pending(self, limit: int = 10) -> list[tuple[str, str]]:
        return self._kb.execute(
            "select key, question from pending where resolved=0 order by created_at desc limit ?", (limit,)
        ).fetchall()

    def resolve_pending(self, key: str) -> None:
        rows = self._kb.execute(
            "select key from pending where key like ? and resolved=0", (f"%{key}%",)
        ).fetchall()
        if len(rows) > 5:
            logger.info("[kb] resolve_pending('%s') 一次命中 %d 条待确认项", key, len(rows))
        self._backup_kb("resolve-pending")
        self._kb.execute("update pending set resolved=1 where key like ?", (f"%{key}%",))
        self._kb.commit()

    def delete_facts(self, keyword: str) -> list[str]:
        """按关键词删除已记录规则，返回被删的 topic 列表。

        2026-11-04 加护栏（四号鱼）：kb 是自建数据、没有第二份来源，LIKE 模糊删很容易误伤。
        关键词短于 DELETE_FACTS_MIN_KEYWORD 且命中超过 DELETE_FACTS_MAX_HITS 时拒绝执行，
        调用方应换成更精确的关键词。
        """
        kw = (keyword or "").strip()
        if not kw:
            return []
        rows = self._kb.execute(
            "select topic from facts where topic like ? or content like ?", (f"%{kw}%", f"%{kw}%")
        ).fetchall()
        topics = [r[0] for r in rows]
        if not topics:
            return []
        if len(kw) < DELETE_FACTS_MIN_KEYWORD and len(topics) > DELETE_FACTS_MAX_HITS:
            logger.warning(
                "[kb] delete_facts 拒绝执行：关键词 %r 太短且命中 %d 条（阈值 %d），命中示例：%s",
                kw,
                len(topics),
                DELETE_FACTS_MAX_HITS,
                "、".join(topics[:5]),
            )
            return []
        logger.info("[kb] delete_facts(%r) 将删除 %d 条：%s", kw, len(topics), "、".join(topics[:8]))
        self._backup_kb("delete-facts")
        self._kb.execute("delete from facts where topic like ? or content like ?", (f"%{kw}%", f"%{kw}%"))
        self._kb.commit()
        return topics


    # ────────────────────────────────────────────────────────────
    # 语义层：facts_view —— 玩家已确认规则的唯一判定源
    #
    # bot 的 seer_rules 除了搜 facts 还会附「解析节点清单」(rules.md)，
    # CLI 的 cmd_facts 只搜 facts —— 两边信息量不一样。
    # 现在两者都从这里取，CLI 可以选择不渲染 rules_text，但判定不会再分叉。
    # ────────────────────────────────────────────────────────────
    def facts_view(self, keyword: str = "", limit: int = 5) -> dict[str, Any]:
        """玩家已确认规则的语义层。

        判定字段：empty_hint（没搜到时的说明）、rules_text（解析节点清单原文）。
        """
        kw = str(keyword or "").strip()
        hits = self.facts_search(kw, limit) if kw else []
        view: dict = {
            "keyword": kw,
            "hits": [{"topic": t, "content": c} for t, c in (hits or [])],
            "empty_hint": None,
            "rules_text": "",
        }
        if kw and not view["hits"]:
            view["empty_hint"] = (
                f"没有匹配「{kw}」的 facts。"
                "提示：试试更短的关键词，或 seer.py topics 看全部主题。"
            )
        # 解析节点清单：插件目录下的 rules.md
        import os as _os
        path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "rules.md")
        try:
            with open(path, encoding="utf-8") as f:
                view["rules_text"] = f.read()
        except OSError:
            view["rules_text"] = ""
        return view

    def fact_topics(self) -> list[str]:
        """返回 facts 里出现过的精灵/主题前缀（topic 第一个「·」之前的部分），按出现次数降序。"""
        try:
            rows = self._kb.execute(
                "select topic from facts"
            ).fetchall()
        except Exception:
            return []
        from collections import Counter
        c = Counter()
        for (topic,) in rows:
            t = str(topic or "")
            head = t.split("·")[0].strip()
            if head:
                c[head] += 1
        return [k for k, _ in c.most_common()]




    # ---------------- 魂印：手册 effectIcon 优先，SeerAPI 兜底 ----------------
    def _open_manual(self, name: str):
        """打开 manual_db/<name>.db（只读）。"""
        cands = []
        try:
            base = os.path.dirname(os.path.abspath(getattr(self, "_kb_path", "") or ""))
            cands.append(os.path.join(base, "manual_db", f"{name}.db"))
        except Exception:
            pass
        for c in cands:
            if c and os.path.isfile(c):
                try:
                    return sqlite3.connect(f"file:{c}?mode=ro", uri=True)
                except Exception:
                    continue
        return None

    @staticmethod
    def _manual_icon_rows(conn, where: str, args: tuple, limit: int = 8) -> list[dict]:
        """从手册 effectIcon 里取魂印条目。"""
        try:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT id, tips, come, pet_id, specific_id, intensify, is_adv, kind "
                f"FROM effectIcon WHERE {where} AND tips IS NOT NULL AND tips != '' "
                "ORDER BY id LIMIT ?",
                args + (int(limit),),
            ).fetchall()
        except Exception:
            return []
        out = []
        for r in rows:
            out.append(
                {
                    "id": r[0],
                    "desc": r[1] or "",
                    "come": r[2] or "",
                    "pet_id": r[3] or "",
                    "specific_id": r[4] or "",
                    "intensify": r[5],
                    "is_adv": r[6],
                    "kind": r[7],
                }
            )
        return out

    def soulmark_manual_by_pet(self, pid: int, limit: int = 6) -> list[dict]:
        """按精灵 id 取手册魂印（pet_id 是形如 '[2479]' 的数组文本）。"""
        conn = self._open_manual("effectIcon")
        if conn is None:
            return []
        try:
            rows = self._manual_icon_rows(
                conn, "(pet_id LIKE ? OR specific_id LIKE ?)", (f"%{pid}%", f"%{pid}%"), limit
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return rows

    def soulmark_manual_search(self, keyword: str, limit: int = 6) -> list[dict]:
        """按关键字/来源取手册魂印。"""
        kw = str(keyword or "").strip()
        if not kw:
            return []
        conn = self._open_manual("effectIcon")
        if conn is None:
            return []
        try:
            rows = self._manual_icon_rows(
                conn, "(tips LIKE ? OR come LIKE ?)", (f"%{kw}%", f"%{kw}%"), limit
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return rows

    # ---------------- 刻印（mintmark）检索 ----------------
    _MINT_NOISE = ("之", "的", "专属", "全能", "刻印", "α", "β", "alpha", "beta", "·", " ")

    @staticmethod
    def _mint_norm(name: str) -> str:
        """刻印名归一：α/β 与「a/阿尔法/进阶/升级」等写法统一后再去掉虚词。

        目标：用户打不出 α 时，用 a、阿尔法、进阶版、升级版 都能匹配到同一条。
        """
        t = str(name or "").strip()
        # 1) 先统一修饰词写法
        for src, dst in (
            ("阿尔法", "alpha"), ("ａ", "alpha"),
            ("贝塔", "beta"),
            ("进阶版", "alpha"), ("进阶", "alpha"),
            ("升级版", "alpha"), ("升级", "alpha"),
        ):
            t = t.replace(src, dst)
        # 2) 中文后面挂单个 a/A 也当 alpha
        t = re.sub(r"(?<=[\u4e00-\u9fff])[aA]$", "alpha", t)
        # 3) 去掉虚词与版本修饰（alpha/beta 也当修饰，不参与匹配）
        for w in SeerKnowledge._MINT_NOISE:
            t = t.replace(w, "")
        t = t.replace("alpha", "").replace("beta", "")
        return t.lower()

    @staticmethod
    def _mint_total(desc: str) -> int:
        """取每个属性的满级值（斜杠后数字）求和，作为刻印总数值。"""
        total = 0
        for part in str(desc or "").replace("，", ",").split(","):
            part = part.strip()
            if "/" in part:
                m = re.search(r"\d+", part.rsplit("/", 1)[-1])
                if m:
                    total += int(m.group())
            else:
                m = re.search(r"\+?(\d+)", part)
                if m:
                    total += int(m.group(1))
        return total


    # ---------------- 刻印：手册 MintMark（含隐藏/额外数值） ----------------
    _MINT_ATTRS = ("攻击", "防御", "特攻", "特防", "速度", "体力")

    @classmethod
    def _parse_attr_arr(cls, text) -> list[int]:
        """把 '[4, 2, 4, 2, 0, 10]' 解析成 6 个整数（攻/防/特攻/特防/速/体）。"""
        try:
            import json as _j
            arr = _j.loads(text) if isinstance(text, str) else list(text or [])
            vals = [int(x) for x in arr]
        except Exception:
            return [0] * 6
        while len(vals) < 6:
            vals.append(0)
        return vals[:6]

    @classmethod
    def _fmt_attrs(cls, vals: list[int], min_value: int = 1) -> str:
        parts = [f"{n}{v}" for n, v in zip(cls._MINT_ATTRS, vals) if v >= min_value]
        return "，".join(parts) if parts else "（无）"

    def mintmark_manual_search(self, keyword: str, limit: int = 5,
                               style: str = "") -> list[dict]:
        """查手册 MintMark：给出 基础/满级/额外(隐藏) 三段 + 含隐藏的合计。

        ★ 2026-11-08 修：原来只有 `Des LIKE %kw%` + `_mint_norm` 归一化。
          查「九天体」「圣战体」这种**系列+方向**的简称必然 0 条
          （没有任何刻印叫「九天体」），于是静默掉到 SeerAPI 兜底、
          只拿到 desc（不含隐藏值），群里就会看到
          「8/82 是初始值和满级值……隐藏值还得换个写法再问一次」。

          现在先做一次简称解析（九天体 → 九霄之钟；无限 → 圣战之无限α），
          再去查手册。解析失败才走原来的字面匹配。
        """
        kw = str(keyword or "").strip()
        if not kw:
            return []

        # ⓪ 简称 → 全名，两级：
        #   ① complete_mint_series —— 处理「系列+方向」（九天体 / 圣战攻 / v8体）
        #   ② lookup_mintmark      —— 处理单只别名（青龙 / 梅花）和精确名
        names = [kw]
        try:
            # style="" → 单查刻印没有精灵上下文，统一先给物攻版（行为可预期），
            #            另一版由 mint_paired_version 在输出里列出来。
            # style="special" 等 → 调用方知道精灵是谁，按它的流派分流。
            #   ★ 2026-11-08：bot 实测「空元行者和伏尔格安都出圣战之渊α（物攻版），
            #     特攻精灵直接白给」—— 因为这里原先硬编码 physical。
            fill = self.complete_mint_series([kw], "", "", style or "physical")
            got = [x for x in (fill.get("mints") or []) if x and x != kw]
            names = got + names
        except Exception:  # noqa: BLE001
            pass
        try:
            hint = self.lookup_mintmark(kw) or {}
            full = str(hint.get("name") or "").strip()
            if full and full != kw and full not in names:
                names.insert(0, full)
        except Exception:  # noqa: BLE001
            pass

        conn = self._open_manual("mintmark")
        if conn is None:
            return []
        rows = []
        try:
            cur = conn.cursor()
            # ⓪-a 几角筛选（2026-11-09 加，P7）
            #
            #   玩家说「五角/四角/三角/二角」= 这颗刻印给了六项里的几项。
            #   之前 bot 完全没有这个概念，问「五角刻印」直接返回 0 条，
            #   而 CLI 的 mint_picker 早就有（corner 字段 + --corner 参数）。
            #   这里补上：关键词命中角数词就全表筛一遍。
            #
            #   ⚠ 返回条数由 limit 控制，五角刻印上千条，调用方要自己截。
            _CW = {"六角": 6, "五角": 5, "四角": 4, "三角": 3,
                   "二角": 2, "两角": 2, "一角": 1}
            _cw = None
            for _w, _n in _CW.items():
                if _w in kw:
                    _cw = _n
                    break
            if _cw is not None and not rows:
                try:
                    _allr = cur.execute(
                        "SELECT Des, BaseAttriValue, MaxAttriValue, ExtraAttriValue, Hide, Grade, Rarity, AttriValue, Type, EffectDes "
                        "FROM MintMark"
                    ).fetchall()
                    def _nz(r):
                        # ★ 2026-11-09：MaxAttriValue 为空的老式小刻印
                        #   （微型攻击刻印 / 小型防御刻印…，共 1049 条）
                        #   会算成「0 角」，其实它们是**一角**（只给一项）。
                        #   MaxAttriValue 空时退回 AttriValue。
                        v = self._parse_attr_arr(r[2])
                        if not any(v):
                            v = self._parse_attr_arr(r[7])
                        return v
                    #   ★ 2026-11-09（鱼片指出）：**只对全能刻印算几角**。
                    #     Type=0 是老版「能力刻印」（微型攻击刻印那种，不用升级就有数值）
                    #     Type=1/4 是「技能刻印」（撞击刻印那种，给某个技能加效果）
                    #     两类都不是全能刻印，谈几角会污染结果。
                    rows = [r for r in _allr
                            if r[8] == 3 and sum(1 for x in _nz(r) if x) == _cw]
                    rows.sort(key=lambda r: -sum(_nz(r)))
                except Exception:  # noqa: BLE001
                    pass
            # ⓪ 系列别名（2026-11-08）
            #
            #   玩家按「联动作品」叫系列，官方按「图案/主题」命名，名字对不上：
            #     奥特曼联动 → 官方「光与暗系列」（class 85）
            #     铁甲小宝联动 → 卡布达专属刻印（不属于任何系列）
            #
            #   ★ 别名一律读 aliases overlay 的 mintmark_class_aliases 表，
            #     **不要在代码里硬编码**（我第一版写了 MINT_CLASS_ALIAS 常量，
            #     那样加新联动就得改代码，而且实例里跑的和工作区两份会漂）。
            #   加别名：往 overlay 的 mintmark_class_aliases 插 (外号, class_id)。
            #
            #   ★ 只在**精确等于某个系列别名**时生效 —— 带方向词的（奥特曼攻）
            #     走 ④ 那条分支，那里会先拆方向再映射。
            if not rows:
                try:
                    _alias_cid = None
                    for (_cid,) in self._alias_q(
                        "select target_id from mintmark_class_aliases where name=?", (kw,)
                    ):
                        _alias_cid = int(_cid)
                        break
                    if _alias_cid:
                        rows = cur.execute(
                            "SELECT Des, BaseAttriValue, MaxAttriValue, ExtraAttriValue, Hide, Grade, Rarity, AttriValue, Type, EffectDes "
                            "FROM MintMark WHERE MintmarkClass = ? ORDER BY ID LIMIT ?",
                            (_alias_cid, int(limit) * 4),
                        ).fetchall()
                except Exception:  # noqa: BLE001
                    pass
            # ⓪-b 系列名直接命中（2026-11-08）
            #
            #   ★ 必须排在 ① 名字匹配之前。反例：「和平星」——
            #     名字 LIKE '%和平星%' 本来能命中 13 条（和平星-双鱼座/白羊座/…），
            #     但上游 lookup_mintmark 的模糊匹配会先把「和平星」解析成
            #     「和平星-双鱼座」塞进 names 第一位，于是只精确命中 1 条。
            #     系列名是**集合**语义，比单条名优先。
            if not rows:
                try:
                    _cid0 = cur.execute(
                        "select ID from MintMarkClass where ClassName like ? limit 1",
                        (f"%{kw}%",),
                    ).fetchone()
                    if _cid0:
                        rows = cur.execute(
                            "SELECT Des, BaseAttriValue, MaxAttriValue, ExtraAttriValue, Hide, Grade, Rarity, AttriValue, Type, EffectDes "
                            "FROM MintMark WHERE MintmarkClass = ? ORDER BY ID LIMIT ?",
                            (_cid0[0], int(limit) * 4),
                        ).fetchall()
                except Exception:  # noqa: BLE001
                    pass
            # ⓪-c 「系列别名 + 方向词」（2026-11-08）
            #
            #   实例：「卡布达攻」—— 卡布达是铁甲小宝联动主角，玩家用它代指
            #   **整个和平星系列**（13 条），方向词再筛攻最高的。
            #
            #   ★ 必须排在 ① 名字匹配之前：否则 lookup_mintmark("卡布达攻")
            #     会先命中「卡布达超级形态专属全能刻印」塞进 names，
            #     ① 就只返回那一条专属印了。
            if not rows:
                _DIRS = (("特攻", 2), ("物攻", 0), ("主攻", -1),
                         ("攻", -1), ("体", 5), ("速", 4), ("防", 1), ("特防", 3))
                for _w, _ix in _DIRS:
                    if not (kw.endswith(_w) and len(kw) > len(_w)):
                        continue
                    _h = kw[: -len(_w)]
                    # ★ 只查**别名表**（玩家外号：卡布达/铁甲小宝/奥特曼），
                    #   **不查 ClassName**。
                    #   原因：官方系列名（九天/圣战/和平星）有自己的处理路径 ——
                    #     「九天攻」→ 单条 九曲之刃（配装只能选一个）
                    #     「和平星攻」→ ④ 的名字片段匹配本来就能全中 13 条
                    #   这里要是把 ClassName 也算进来，「九天攻」会被升级成
                    #   整个九天系列 8 条，等于把配装场景搞坏了。
                    _cid5 = None
                    try:
                        for (_c6,) in self._alias_q(
                            "select target_id from mintmark_class_aliases where name=?", (_h,)
                        ):
                            _cid5 = int(_c6)
                            break
                    except Exception:  # noqa: BLE001
                        pass
                    if _cid5:
                        _cand5 = cur.execute(
                            "SELECT Des, BaseAttriValue, MaxAttriValue, ExtraAttriValue, Hide, Grade, Rarity, AttriValue, Type, EffectDes "
                            "FROM MintMark WHERE MintmarkClass = ? LIMIT 60",
                            (_cid5,),
                        ).fetchall()
                        # ★ 2026-11-08：有 style 时**先按流派筛一遍再排序**。
                        #   不筛的后果（实测）：光与暗系列「奥特曼攻」
                        #   物攻向也拿到「大地神迹」(攻0/特攻72)，
                        #   因为按 max(atk,sp_atk) 排序时它和「辉光永恒」(攻72) 并列、
                        #   取了排前面的那个。特攻版把物攻版的位置抢了。
                        if _cand5 and style:
                            _want_atk = style != "special"

                            def _keep(_r):
                                _m = self._parse_attr_arr(_r[2])
                                _a = _m[0] if len(_m) > 0 else 0
                                _sa = _m[2] if len(_m) > 2 else 0
                                return (_a > 0 and _sa == 0) if _want_atk else (_sa > 0 and _a == 0)
                            _filtered = [r for r in _cand5 if _keep(r)]
                            if _filtered:
                                _cand5 = _filtered
                        if _cand5:
                            def _dv(_r):
                                _m = self._parse_attr_arr(_r[2])
                                if _ix == -1:
                                    return max(_m[0] if len(_m) > 0 else 0,
                                               _m[2] if len(_m) > 2 else 0)
                                return _m[_ix] if len(_m) > _ix else 0
                            rows = sorted(_cand5, key=_dv, reverse=True)
                    break
            # ① 按候选名逐个 LIKE（先全名后原名）
            if not rows:
                for nm in names:
                    rows = cur.execute(
                        "SELECT Des, BaseAttriValue, MaxAttriValue, ExtraAttriValue, Hide, Grade, Rarity, AttriValue, Type, EffectDes "
                        "FROM MintMark WHERE Des LIKE ? LIMIT ?",
                        (f"%{nm}%", int(limit) * 4),
                    ).fetchall()
                    if rows:
                        break
            # ② 归一化兜底（去「之/的/专属/全能」等虚词后全表比对）
            if not rows:
                for nm in names:
                    nkw = self._mint_norm(nm)
                    if not nkw:
                        continue
                    for r in cur.execute(
                        "SELECT Des, BaseAttriValue, MaxAttriValue, ExtraAttriValue, Hide, Grade, Rarity FROM MintMark"
                    ):
                        nn = self._mint_norm(r[0])
                        if nn == nkw or nkw in nn:
                            rows.append(r)
                            if len(rows) >= int(limit) * 4:
                                break
                    if rows:
                        break
            # ③ 系列名兜底（2026-11-08 加）
            #
            #    刻印名通常**不含系列名**，所以按名字 LIKE 永远查不到：
            #      光与暗系列（class 85）的 6 条成员叫
            #      大地神迹 / 辉光永恒 / 深渊瞥视 / 恶念缚魂 / 超限审判 / 时空秩守
            #      → 按「光与暗」搜名字必然 0 条，bot 只能回「查不到」。
            #    但手册 MintMarkClass 表里明明有这个系列，MintMark.MintmarkClass 也
            #    指向它。所以最后一环直接按系列名反查成员。
            #
            #    另注：奥特曼那 3 条（迪迦/赛罗/贝利亚专属）的 MintmarkClass = 0，
            #    它们**不属于任何系列**，只能按「奥特曼」搜名字命中，别指望系列查法。
            if not rows:
                try:
                    # 外号映射已在 ⓪ 处理过（精确匹配），这里就是原词
                    _cls_kw = kw
                    cid = cur.execute(
                        "select ID from MintMarkClass where ClassName like ? limit 1",
                        (f"%{_cls_kw}%",),
                    ).fetchone()
                    if cid:
                        rows = cur.execute(
                            "SELECT Des, BaseAttriValue, MaxAttriValue, ExtraAttriValue, Hide, Grade, Rarity, AttriValue, Type, EffectDes "
                            "FROM MintMark WHERE MintmarkClass = ? ORDER BY ID LIMIT ?",
                            (cid[0], int(limit) * 4),
                        ).fetchall()
                except Exception:  # noqa: BLE001
                    pass
            # ④ 「名字片段 + 方向词」兜底（2026-11-08 加）
            #
            #    实例：「奥特曼攻」—— 3 个奥特曼刻印（迪迦/赛罗/贝利亚专属）
            #    的 MintmarkClass 全是 0（不属于任何系列），③ 系列兜底也无效；
            #    但按名字 LIKE '%奥特曼%' 能命中 3 条，再用「攻」按主攻项挑最高的。
            #
            #    方向词按长度优先匹配（「特攻」必须先于「攻」），避免把「特攻」
            #    吃成「攻」。索引对应 MaxAttriValue 六项：atk,def,sp_atk,sp_def,spd,hp
            if not rows:
                _DIR_TAIL = (
                    ("特攻", 2), ("物攻", 0), ("主攻", -1),
                    ("攻", -1), ("体", 5), ("速", 4), ("防", 1), ("特防", 3),
                )
                for _word, _idx in _DIR_TAIL:
                    if not (kw.endswith(_word) and len(kw) > len(_word)):
                        continue
                    _head = kw[: -len(_word)]
                    # ★ head 先查「系列别名」（overlay 的 mintmark_class_aliases）：
                    #   玩家说「奥特曼攻」，官方系列叫「光与暗系列」、成员名里也不含
                    #   「奥特曼」，直接按名字搜只会命中迪迦/赛罗/贝利亚那 3 条专属印
                    #   （玩家根本不考虑它们）。
                    _alias_cid2 = None
                    try:
                        for (_cid4,) in self._alias_q(
                            "select target_id from mintmark_class_aliases where name=?",
                            (_head,),
                        ):
                            _alias_cid2 = int(_cid4)
                            break
                    except Exception:  # noqa: BLE001
                        pass
                    _cand = []
                    if _alias_cid2:
                        _cand = cur.execute(
                            "SELECT Des, BaseAttriValue, MaxAttriValue, ExtraAttriValue, Hide, Grade, Rarity, AttriValue, Type, EffectDes "
                            "FROM MintMark WHERE MintmarkClass = ? LIMIT 60",
                            (_alias_cid2,),
                        ).fetchall()
                    # ① 系列名（奥特曼→光与暗 / 君子→四君子）
                    if not _cand:
                        try:
                            _cid3 = cur.execute(
                                "select ID from MintMarkClass where ClassName like ? limit 1",
                                (f"%{_head}%",),
                            ).fetchone()
                            if _cid3:
                                _cand = cur.execute(
                                    "SELECT Des, BaseAttriValue, MaxAttriValue, ExtraAttriValue, Hide, Grade, Rarity, AttriValue, Type, EffectDes "
                                    "FROM MintMark WHERE MintmarkClass = ? LIMIT 60",
                                    (_cid3[0],),
                                ).fetchall()
                        except Exception:  # noqa: BLE001
                            _cand = []
                    # ② 系列没命中，再按名字片段
                    if not _cand:
                        _cand = cur.execute(
                            "SELECT Des, BaseAttriValue, MaxAttriValue, ExtraAttriValue, Hide, Grade, Rarity, AttriValue, Type, EffectDes "
                            "FROM MintMark WHERE Des LIKE ? LIMIT 60",
                            (f"%{_head}%",),
                        ).fetchall()
                    if _cand:
                        def _dir_val(_r):
                            _mx = self._parse_attr_arr(_r[2])
                            if _idx == -1:      # 「攻」= 主攻项（物攻/特攻取大）
                                return max(_mx[0] if len(_mx) > 0 else 0,
                                           _mx[2] if len(_mx) > 2 else 0)
                            return _mx[_idx] if len(_mx) > _idx else 0
                        _cand = sorted(_cand, key=_dir_val, reverse=True)
                        rows = _cand
                    break
        finally:
            try:
                conn.close()
            except Exception:
                pass

        out = []
        # ★ 2026-11-08 去重：同名字可能有多条 id ——
        #   实例：灵籁·伴夏 有 42235 和 45026 两条、数值完全一样，
        #   查「毛毛」会重复列出两遍。按名字去重，只保留先出现的那条。
        _seen_names: set[str] = set()
        for r in rows:
            if len(out) >= int(limit):
                break
            if r[0] in _seen_names:
                continue
            _seen_names.add(r[0])
            base = self._parse_attr_arr(r[1])
            mx = self._parse_attr_arr(r[2])
            extra = self._parse_attr_arr(r[3])
            # ★ 2026-11-09：MaxAttriValue 为空的老式刻印（初级全能/烈焰纹章/
            #   微型体力刻印这些，共上千条）要退回 AttriValue 取数值，
            #   否则「六角」查出来合计是 0，看着自相矛盾。
            #   这类刻印没有成长空间，AttriValue 就是它的最终值。
            if not any(mx):
                mx = self._parse_attr_arr(r[7])
            # MaxAttriValue 已含隐藏值：可见满级 = 满级 - 隐藏
            total_all = sum(mx)
            total_visible = total_all - sum(extra)
            out.append(
                {
                    "name": r[0],
                    "base": base,
                    "max": mx,
                    "extra": extra,
                    "hide": r[4],
                    "grade": r[5],
                    "total": total_visible,
                    "total_with_extra": total_all,
                    # ★ 2026-11-09（P7）：几角 = 六项里有几项有值。
                    #   玩家说「五角/四角/三角/二角」就是按这个分的：
                    #     六角 = 六项全有（全库仅 2 条：谱尼专属全能刻印B、
                    #            星辰万象·天启星魂专属全能刻印）
                    #     五角 = 绝大多数 PVP 刻印（2426 条）
                    #     二角 = 只给两项（无极圣武那种「二角极差」）
                    #   ★ 2026-11-09（鱼片指出）：**只有全能刻印才谈几角**。
                    #     Type=3 → 全能刻印（有 MaxAttriValue，谈到几角）
                    #     Type=0 → 老版「能力刻印」（不用升级就有数值，不是全能）
                    #     Type=1/4 → 「技能刻印」（给某个技能加效果，没有属性值）
                    #   后两类 corner 一律给 None，免得被误当成「一角 / 零角」。
                    "corner": (sum(1 for x in mx if x) if r[8] == 3 else None),
                    "is_universal": r[8] == 3,
                    # ★ 2026-11-09（P7）：刻印大类。玩家口中的「几角」只对全能刻印成立，
                    #   但有几个**名字里带「全能」却不是全能**的老刻印
                    #   （高级全能刻印 / 全国巅峰联赛专属全能刻印 / 王者级均衡刻印…），
                    #   它们是 Type=0 的老版「能力刻印」—— 不用升级就有数值。
                    #   输出时要明说，免得群友按名字误会。
                    "kind": self._MINT_KIND.get(r[8], ""),
                    # ★ 2026-11-09：技能刻印（Type=1）的属性值全是 0，
                    #   真正的信息在 EffectDes（「先制+1」「威力+15」「固定伤害+50」）。
                    #   不返回这个字段，查技能刻印等于查了个空壳。
                    "effect": (r[9] or "").strip() if len(r) > 9 else "",
                }
            )
        out.sort(key=lambda x: (-x["total_with_extra"], len(x["name"])))
        return out

    def mint_paired_version(self, name: str) -> str:
        """找同系列的「成对版」：攻/特攻互换，其余四项完全相同。

        九天 / 圣战 / V8 这些系列里，物攻版和特攻版是成对设计的：
            九曲之刃 攻63  ↔  九曲之光 特攻63
            九霄之钟 体116 ↔  九霄之灵 体116
            圣战之渊α ↔ 圣战之鸿α
            圣战之锋α[Ⅰ] ↔ 圣战之舞α[Ⅰ]
            V8-05（体·物攻）↔ V8-06（体·特攻）

        ★ 为什么需要：`seer_mintmark` 是**单查刻印**，没有精灵上下文，
          只能给一版（默认物攻）。不提示另一版的话，特攻精灵的玩家照抄
          就会少一大截面板 —— 「九天体」抄成九霄之钟，特攻精灵该拿九霄之灵。

        返回成对版的名字；没有配对（如赤洲龙子、黄金残影那种单一定向）返回 ""。
        """
        nm = str(name or "").strip()
        if not nm:
            return ""
        target = self.lookup_mintmark(nm) or {}
        v1 = target.get("vals") or {}
        if not v1:
            return ""
        a1, s1 = int(v1.get("atk") or 0), int(v1.get("sp_atk") or 0)
        if a1 and s1:            # 双攻印（如圣战之无限、帝皇·无极），没有单边配对
            return ""
        if not a1 and not s1:    # 纯防/纯速/纯体印，方向不靠攻区分
            return ""
        rest1 = tuple(int(v1.get(k) or 0) for k in ("def", "sp_def", "spd", "hp"))
        main1 = max(a1, s1)
        try:
            siblings = self.mint_series_members(nm) or []
        except Exception:  # noqa: BLE001
            return ""

        # ★ 不能要求「其余四项完全相同」——同系列里存在**不对称成对**：
        #   拿瓦·熔岩 防32 特攻47 特防32 速34 体135
        #   拿瓦·焚阳 防33 攻46  特防33 速33 体135   ← 每项差 1
        #   所以改成「方向相反 + 数值够接近」，取差异最小的那个。
        TOL = 5          # 四项 + 主攻项的总容差；完全对称的 diff=0，拿瓦是 4
        best = None      # (diff, name)
        for other in siblings:
            if other == nm:
                continue
            o = self.lookup_mintmark(other) or {}
            v2 = o.get("vals") or {}
            if not v2:
                continue
            a2, s2 = int(v2.get("atk") or 0), int(v2.get("sp_atk") or 0)
            # 方向必须相反：我物攻它特攻，或我特攻它物攻
            if not ((a1 > 0 and s1 == 0 and a2 == 0 and s2 > 0)
                    or (s1 > 0 and a1 == 0 and a2 > 0 and s2 == 0)):
                continue
            rest2 = tuple(int(v2.get(k) or 0) for k in ("def", "sp_def", "spd", "hp"))
            diff = sum(abs(rest1[i] - rest2[i]) for i in range(4))
            diff += abs(main1 - max(a2, s2))       # 主攻项本身也可能差（47 vs 46）
            if best is None or diff < best[0]:
                best = (diff, other)
        if best is not None and best[0] <= TOL:
            return best[1]
        return ""

    def mintmark_search(self, keyword: str, limit: int = 6) -> list[dict]:
        """按刻印名检索：忽略「之/的/专属/全能/刻印」等虚词，支持包含匹配与候选返回。"""
        kw = str(keyword or "").strip()
        if not kw:
            return []
        conn = None
        rows = []
        try:
            conn = self._open_seer_raw()
            if conn is None:
                return []
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT id, name, desc, rarity_id FROM mintmark WHERE name LIKE ? LIMIT ?",
                (f"%{kw}%", int(limit) * 3),
            ).fetchall()
            if not rows:
                allrows = cur.execute(
                    "SELECT id, name, desc, rarity_id FROM mintmark"
                ).fetchall()
                nkw = self._mint_norm(kw)
                scored = []
                for r in allrows:
                    nn = self._mint_norm(r[1])
                    if not nn or not nkw:
                        continue
                    if nn == nkw:
                        scored.append((0, r))
                    elif nkw in nn:
                        scored.append((1, r))
                    elif len(nkw) >= 2 and nn.startswith(nkw[:2]):
                        scored.append((2, r))
                scored.sort(key=lambda x: x[0])
                rows = [r for _, r in scored[: int(limit) * 3]]
        except Exception:
            rows = []
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        out = [
            {
                "id": r[0],
                "name": r[1],
                "desc": r[2],
                "rarity_id": r[3],
                "total": self._mint_total(r[2]),
            }
            for r in rows[: int(limit)]
        ]
        # 总和高的优先；同分按名字短的优先（通常是基础版）
        out.sort(key=lambda x: (-x["total"], len(x["name"])))
        return out

    def _open_seer_raw(self):
        """打开 seerapi.sqlite（只读），文件被覆盖时不长期持有。"""
        cands = [getattr(self, "seer_path", "") or ""]
        try:
            base = os.path.dirname(os.path.abspath(getattr(self, "_kb_path", "") or ""))
            cands.append(os.path.join(base, "..", "astrbot_plugin_seer_info", "seerapi.sqlite"))
        except Exception:
            pass
        cands.append(
        )
        for c in cands:
            if not c:
                continue
            path = os.path.abspath(c)
            if os.path.isfile(path):
                try:
                    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                except Exception:
                    continue
        return None

    # ---------------- 宝石等级表（附加件，走 seerapi） ----------------

    def gem_view(self, keyword: str = "") -> dict:
        """宝石逐级数值（36 类 / 245 条）。

        ★ 为什么不写进 facts：宝石是**结构化可枚举数据**。
          写死进文本必然过时（而且一次能列 245 行，facts 装不下）。
          这类东西就该做成工具查；facts 只放「怎么理解、怎么用」。

        数据链：gem(245) → gemeffectlink → skill_effect_in_use(52896)
               类别在 gem_category(36)
        ⚠️ 工作区 `_manual_all/gems.db` 是旧副本，CLI 的 `ls gem` 也不读它 ——
           真实数据在 seerapi，别去翻那个文件。

        keyword：
          ""            → 只返回类别目录（36 类 + 每类几级），不吐 245 条明细
          "威力" / "真实伤害" / "强能" → 返回该类逐级数值
        """
        kw = str(keyword or "").strip()
        try:
            cats = {r[0]: r[1] for r in self._q("select id, name from gem_category")}
            rows = self._q(
                """select g.id, g.name, g.level, g.category_id,
                          (select u.info from gemeffectlink l
                             join skill_effect_in_use u on u.id = l.skill_effect_in_use_id
                            where l.gem_id = g.id limit 1)
                   from gem g order by g.category_id, g.level"""
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[kb] gem_view 查询失败：%s", exc)
            return {"error": f"{type(exc).__name__}: {exc}", "categories": [], "matched": 0}

        if not rows:
            return {"error": "gem 表读不到数据（seerapi 可能缺表）", "categories": [], "matched": 0}

        bycat: dict[str, list[dict]] = {}
        for _gid, gname, level, cat, info in rows:
            cname = cats.get(cat, f"类别{cat}")
            bycat.setdefault(cname, []).append({
                "name": re.sub(r"\[/?[^\]]*\]", "", str(gname or "")).strip(),
                "level": int(level or 0),
                "info": re.sub(r"\[/?[^\]]*\]", "", str(info or "")).replace("|", " / ").strip(),
            })

        if not kw:
            return {
                "mode": "catalog",
                "total_categories": len(bycat),
                "total_items": len(rows),
                "catalog": [{"category": c, "levels": len(v),
                             "range": f"{v[0]['name']} → {v[-1]['name']}"}
                            for c, v in bycat.items()],
                "matched": 0,
                "hint": "传关键词查具体数值，如「威力」「真实伤害」「强能」。"
                        "库里的正式名可能比俗称长（真伤宝石 → 真实伤害宝石）。",
            }

        hit = {c: v for c, v in bycat.items()
               if kw in c or any(kw in it["name"] for it in v)}
        if not hit:
            near = [c for c in bycat if any(ch in c for ch in kw)]
            return {
                "mode": "search", "keyword": kw, "categories": [], "matched": 0,
                "hint": f"没有匹配「{kw}」的宝石。"
                        + (f"相近类别：{'、'.join(near[:5])}。" if near else "")
                        + "俗称和正式名对不上时换个写法（真伤宝石 → 真实伤害宝石）。",
            }
        items = list(hit.items())[:3]
        return {
            "mode": "search", "keyword": kw,
            "matched": len(hit),
            "truncated": len(hit) > 3,
            "categories": [{"category": c, "levels": v} for c, v in items],
        }

    # ---------------- 专栏文档检索（doc_rules / doc_concepts / articles） ----------------
    def doc_rules_search(self, keyword: str, limit: int = 5) -> list[dict]:
        """在 doc_rules 里按条件/结论模糊检索，返回 [{title, condition, conclusion}]。"""
        kw = str(keyword or "").strip()
        if not kw:
            return []
        like = f"%{kw}%"
        try:
            rows = self._kb.execute(
                "select title, condition, conclusion from doc_rules "
                "where condition like ? or conclusion like ? or title like ? "
                "order by (case when title like ? then 0 else 1 end), id limit ?",
                (like, like, like, like, int(limit)),
            ).fetchall()
        except Exception:
            return []
        return [
            {"title": t or "", "condition": c or "", "conclusion": d or ""}
            for t, c, d in rows
        ]

    def doc_concepts_search(self, keyword: str, limit: int = 3) -> list[dict]:
        """在 doc_concepts 里按术语/定义模糊检索，返回 [{title, term, definition}]。"""
        kw = str(keyword or "").strip()
        if not kw:
            return []
        like = f"%{kw}%"
        try:
            rows = self._kb.execute(
                "select title, term, definition from doc_concepts "
                "where term like ? or definition like ? or title like ? "
                "order by (case when term = ? then 0 when term like ? then 1 else 2 end), id limit ?",
                (like, like, like, kw, like, int(limit)),
            ).fetchall()
        except Exception:
            return []
        return [
            {"title": t or "", "term": m or "", "definition": d or ""}
            for t, m, d in rows
        ]

    def doc_search(self, keyword: str, limit: int = 2) -> list[dict]:
        """在 articles 里按标题/正文模糊检索，返回 [{title, snippet}]。

        ★ 2026-11-08 修：原 SQL 写的是 `select title, content from articles`，
          但 articles 表的正文字段叫 **text**（title/url/words/text/fetched_at），
          没有 content 列 → OperationalError 被 except 吞掉 → 永远返回 []。
          表现是「专栏原文明明在库里，bot 却一篇都翻不到」，而且不报错。
          字段名以 `pragma table_info(articles)` 为准，别再凭印象写。
        """
        kw = str(keyword or "").strip()
        if not kw:
            return []
        like = f"%{kw}%"
        try:
            rows = self._kb.execute(
                "select title, text from articles where title like ? or text like ? limit ?",
                (like, like, int(limit)),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[kb] articles 检索失败：%s", exc)
            return []
        out = []
        for t, c in rows:
            text = str(c or "")
            pos = text.find(kw)
            snippet = text[max(0, pos - 60) : pos + 160] if pos >= 0 else text[:200]
            out.append({"title": t or "", "snippet": snippet})
        return out


    # ---------------- 按聊天原文召回 facts（跨会话通用） ----------------

    # 常见同音/形近错字 → 正确精灵名（玩家打错也能召回）
    TYPO_MAP = {
        "龙啖": "龙琰", "龙淡": "龙琰", "龙炎": "龙琰",
        "柯尔霍得": "柯尔霍德", "空元行着": "空元行者",
        "帝皇之玉": "帝皇之御", "帝皇之越": "帝皇之钺",
        "詹刀": "战刀", "詹螳螂": "战螳螂",
    }

    # 战刀/龙琰这类技能常用单字代称，允许单字参与召回
    _ONE_CHAR_SKILLS = {"饕", "染", "醒", "惘", "临", "枷", "葬", "损", "逝"}

    # 低频概念词：即使主题和原话没有任何字面重合，也允许它把主题「捞」出来。
    # 只放库里出现次数少的词（每个词命中的主题数 ≤ 10），
    # 像「魂印」「判定」这种高频词不能放进来，否则一次查询会把半张表都拉出来。
    _SEED_KEYWORDS = (
        "先制", "时点", "未触发", "秒杀", "反弹", "属性伤害",
        "粉伤", "真伤", "固伤", "红伤", "减伤", "增伤",
    )

    # 召回加词：原话里出现、且该条 fact 的 topic/正文也出现时给分。
    # 存在的理由：facts 的打分原本只看 topic 的分段，正文里的机制词不计分，
    # 于是同分时退回「按 updated_at 倒序」，新写入的 fact 会把真正相关的挤掉
    # （实测 10 个真实提问只召回 3 个，失败时永远返回最新那 3 条）。
    # 这里只放「足够具体、不会一次捞半张表」的机制词。
    _RECALL_KEYWORDS = (
        "被控", "免控", "回血", "回满", "月亮", "盔", "消逝", "阵亡", "存活",
        "逆命", "荒古", "衍化", "臣服", "暴击", "必暴", "连击", "先制",
        "宝石", "刻印", "配招", "摆位", "魂印", "第五", "大招",
        "固定伤害", "百分比伤害", "真实伤害", "属性伤害", "能力等级",
        "视为", "无视", "反噬", "致命一击", "体力上限",
        "瞬杀", "秒杀", "古王", "古龙", "合体", "进化", "同名", "实体",
        # 具体装备/通道词：能定位到规则，又不会一次捞半张表
        "护盾", "护罩", "抗性", "属性技能", "回合盔", "转化", "掉命", "炸盾",
        "分层", "提炼",
        # 计算纪律相关：问「怎么算/能不能手推」时要能落到纪律条目上
        "手推", "反推", "实算", "脚本", "斩杀线", "阈值",
        # 碎片机制的俗称：玩家常只说机制名（尸骸/咒骸），不带精灵名
        "尸骸", "咒骸", "魂契", "攒镜", "弥沦",
    )

    CHAT_ALIASES = {
        "表哥": ("帝皇之钺", "帝皇之戎"),
        "表姐": ("帝皇之御", "帝皇之戎"),
        "表哥形态": ("帝皇之钺", "帝皇之戎"),
        "表姐形态": ("帝皇之御", "帝皇之戎"),
    }


    # ---------------- 原话里识别精灵（用于自动附卡片） ----------------
    def pets_in_text(self, text: str, limit: int = 2, min_len: int = 2) -> list[tuple[int, str]]:
        """在玩家原话里找出现的精灵名（长的优先，避免「星礼」吃掉「霞冠·星礼」）。

        除库里全名外，也认 pet_alias 里玩家确认过的俗称（战刀/光惩/表哥…），
        并按 TYPO_MAP 先纠一次错（龙啖/龙淡 → 龙琰）。
        """
        msg = str(text or "")[:400]
        if not msg:
            return []
        msg = self._apply_typo(msg)
        try:
            rows = self._q("select id, name from pet")
        except Exception:
            return []
        name_by_id = {int(pid): nm for pid, nm in rows if nm}
        scored: list[tuple[int, int, str, str]] = []
        for pid, nm in rows:
            if not nm:
                continue
            if len(nm) >= min_len and nm in msg:
                scored.append((len(nm), int(pid), nm, nm))
                continue
            # 名字头部（「战之狂热·英卡洛斯」→「战之狂热」）也算提到这只精灵，
            # 玩家常说简称而不带形态后缀
            head = nm.split("·")[0].strip()
            if len(head) >= 3 and head in msg:
                scored.append((len(head), int(pid), nm, head))
        try:
            aliases = self._kb.execute("select alias, pet_id from pet_alias").fetchall()
        except Exception:
            aliases = []
        for alias, pid in aliases:
            alias = str(alias or "")
            if len(alias) < min_len or alias not in msg:
                continue
            # 纯数字别名（如「48」=冰王）要求数字边界：
            # 「148攻」「480防」里的 48 不算提到该精灵，否则会污染伤害计算参数。
            if alias.isdigit():
                _pos = 0
                _ok = False
                while True:
                    _i = msg.find(alias, _pos)
                    if _i < 0:
                        break
                    _before = msg[_i - 1] if _i > 0 else ""
                    _after = msg[_i + len(alias)] if _i + len(alias) < len(msg) else ""
                    if not _before.isdigit() and not _after.isdigit():
                        _ok = True
                        break
                    _pos = _i + 1
                if not _ok:
                    continue
            try:
                pid = int(pid)
            except Exception:
                continue
            nm = name_by_id.get(pid)
            if nm:
                scored.append((len(alias), pid, nm, alias))
        scored.sort(key=lambda x: (-x[0], msg.find(x[3])))
        out: list[tuple[int, str]] = []
        seen_ids: set[int] = set()
        used_names: list[str] = []
        used_tokens: list[str] = []
        for _, pid, nm, token in scored:
            if pid in seen_ids:
                continue
            if any(nm in u for u in used_names):
                continue
            # 「螳螂」只出现在更长的「战螳螂」里面时不再单独出一张卡；
            # 但「螳螂和战螳螂谁强」这种明确并列的，挖掉长词后还剩就在这里保留。
            rest = msg
            for t in used_tokens:
                rest = rest.replace(t, "\u0000")
            if token not in rest:
                continue
            out.append((pid, nm))
            seen_ids.add(pid)
            used_names.append(nm)
            used_tokens.append(token)
            if len(out) >= limit:
                break
        return out

    def pet_card(self, pet_id: int) -> str:
        """一段精简卡片：属性 / 种族值 / 魂印首句 / 技能名，供注入上下文。"""
        c = {"atk": "攻击", "def": "防御", "sp_atk": "特攻", "sp_def": "特防", "spd": "速度", "hp": "体力"}
        both = self.pet_stats_both(pet_id)
        stats = both.get("awakened") or both.get("base") or {}
        parts = [f"属性：{self.pet_type(pet_id) or '未知'}"]
        if stats:
            parts.append("种族值：" + "　".join(f"{c[k]} {stats.get(k)}" for k in c))
        try:
            forms = self.pet_soulmark_forms(pet_id)
        except Exception:
            forms = []
        if forms:
            f = forms[0]
            parts.append(f"魂印（{f.get('form')}）：{str(f.get('desc'))[:150]}")
        try:
            g = self.pet_skills_grouped(pet_id)
            names = [x["name"] for x in (
            g["fifth_new"] + g["fifth_old"] + g["new_skills"]
            + g.get("lv0", []) + g["legacy"])][:10]
        except Exception:
            names = []
        if names:
            parts.append("技能名：" + "、".join(names))
        return "｜".join(parts)

    def facts_search(self, keyword: str, limit: int = 5) -> list[tuple[str, str]]:
        """按关键词搜 facts（主题+正文），支持多个词，按命中数排序。"""
        kw = str(keyword or "").strip()
        if not kw:
            return []
        parts = [x for x in re.split(r"[\s，,、/；;|]+", kw) if len(x) >= 1]
        if not parts:
            return []
        try:
            rows = self._kb.execute(
                "select topic, content from facts order by updated_at desc"
            ).fetchall()
        except Exception:
            return []
        scored = []
        for topic, content in rows:
            t, c = str(topic or ""), str(content or "")
            hit = 0
            for w in parts:
                if t.startswith(w):
                    hit += 8
                elif w in t:
                    hit += 3
                if w in c:
                    hit += 1
            if hit:
                scored.append((hit, t, c))
        # 分高优先；同分时主题更短（多为「定义」类）优先
        scored.sort(key=lambda x: (-x[0], len(x[1])))
        return [(t, c) for _, t, c in scored[: int(limit)]]

    def facts_for_chat(self, msg: str, limit: int = 6) -> list[tuple[str, str]]:
        """从玩家这句话里召回相关已确认规则。

        topic 的任意「·」分段出现在原话里就算命中；别名（表哥/表姐等）额外扩展到对应精灵。
        """
        text = str(msg or "")[:400]
        if not text:
            return []
        text = self._apply_typo(text)
        try:
            rows = self._kb.execute(
                "select topic, content from facts order by updated_at desc"
            ).fetchall()
        except Exception:
            return []

        keys = set()
        for alias, targets in self.CHAT_ALIASES.items():
            if alias in text:
                keys.add(alias)
                keys.update(targets)
        # 原话里提到的精灵（认俗称/错字/名字头部），用于把「战刀·…」这类主题
        # 和玩家嘴里的「战之狂热」「战螳螂」对上——靠精灵身份而不是字面巧合。
        pet_prefixes: set[str] = set()
        try:
            hit_pets = self.pets_in_text(text, limit=4)
        except Exception:
            hit_pets = []
        for pid, pname in hit_pets:
            pet_prefixes.add(pname)
            pet_prefixes.add(str(pname).split("·")[0])
            try:
                pet_prefixes.update(self.alias_of(pid) or [])
            except Exception:
                pass
        scored = []
        for topic, content in rows:
            t = str(topic or "")
            segs = [x.strip() for x in re.split(r"[·、与和（）()：:\-—/ ]+", t) if x.strip()]
            hit = 0
            for seg in segs:
                if (len(seg) >= 2 or seg in self._ONE_CHAR_SKILLS) and seg in text:
                    hit += 2
                for k in keys:
                    if k and k in seg:
                        hit += 3
            for k in keys:
                if k and k not in t and k in str(content or ""):
                    hit += 1
            # 精灵身份匹配：topic 以该精灵的正式名/俗称开头时强加分。
            # 原代码算了 pet_prefixes 却没参与打分，导致玩家说「战之狂热」
            # 召不回「战刀·…」这类用俗称命名的规则。
            # 权重高于机制词：知识库变厚后，通用机制词（如「护盾」「增伤」）
            # 会把具体精灵的规则挤出前几名——身份命中比词面命中更可靠。
            if pet_prefixes:
                for pf in pet_prefixes:
                    if pf and (t.startswith(pf) or any(seg.startswith(pf) for seg in segs)):
                        hit += 7
                        break
            # 机制词命中：原话里有、topic 也有的优先，只有正文有的给低分。
            # 这一层是为了让「topic 不含用户问法、但正文讲到了」的规则也能被捞出来。
            # 词越长越具体，加权越高（「属性伤害」比「抗性」更能定位到规则），
            # 避免「属性伤害过不过抗性」这种双词提问被泛化的「抗性」条目挤掉。
            ctext = str(content or "")
            for kw in self._RECALL_KEYWORDS:
                if kw in text:
                    w = 4 if len(kw) >= 4 else 3
                    if kw in t:
                        hit += w
                    elif kw in ctext:
                        hit += w - 1
            # 近似字兜底：等长窗口差一个字（如 龙啖≈龙琰）。
            # 只对 3 字以上的词启用——中文两字词差一字基本是反义词
            # （属性/抗性、增伤/减伤、护盾/护罩、固伤/真伤），2 字放开会制造大量假阳性。
            if not hit:
                for seg in segs:
                    L = len(seg)
                    if L < 3:
                        continue
                    for i in range(0, max(0, len(text) - L + 1)):
                        win = text[i : i + L]
                        if win == seg:
                            continue
                        if sum(1 for a, b in zip(win, seg) if a != b) == 1:
                            hit += 2
                            break
                    if hit:
                        break
            # 主题与玩家问法的字面重合（3 字滑窗），解决「未触发的定义」这种问法
            if not hit and len(t) >= 3:
                grams = {t[i : i + 3] for i in range(len(t) - 2)}
                g_hit = sum(1 for g in grams if g in text)
                if g_hit:
                    hit += min(g_hit * 2, 6)
            # 关键词加权：原文提到的技能词（染/醒/古咒…）出现在主题里 → 优先。
            # 这些词高频，必须有其他命中才加权，否则会冲爆结果。
            if hit:
                for kw in ("染", "醒", "古咒", "枷", "魂印", "抗性", "克制",
                           "保底", "护盾", "护罩", "第五", "翻倍", "锁切",
                           "掉命", "掉命斩", "圣战祝福", "280", "战刀", "饕", "血色"):
                    if kw in text and kw in t:
                        hit += 3
            # 概念词种子：低频概念词即使没有任何字面重合，也能把主题捞出来。
            # 解决「先制等级怎么结算」召不回「先制结算-告命修正」这类换词就失联的问题。
            if not hit:
                for kw in self._SEED_KEYWORDS:
                    if kw in text and kw in t:
                        hit += 3
            # 精灵身份兜底：主题讲的就是原话里那只精灵（俗称也算），
            # 但只用主题首段比对，避免同精灵的十几条一起炸出来；
            # 再用正文里的概念词把「这一只的哪条」排出来。
            if not hit:
                head = segs[0] if segs else ""
                if head and head in pet_prefixes:
                    ctext = str(content or "")
                    extra = sum(
                        1 for kw in self._SEED_KEYWORDS + (
                            "护盾", "护罩", "抗性", "克制", "保底", "第五",
                            "魂印", "掉命", "圣战祝福", "饕", "血色",
                        )
                        if kw in text and kw in ctext
                    )
                    hit += 2 + min(extra, 3)
            if hit >= 2:
                scored.append((hit, t, str(content or "")))
        if not scored:
            return []
        scored.sort(key=lambda x: -x[0])
        return [(t, c) for _, t, c in scored[: int(limit)]]

    def facts_for_pet(self, pet_name: str, limit: int = 6) -> list[tuple[str, str]]:
        """取与某只精灵相关的已确认规则（支持简称：空元行者 → 空元）。

        tier 判定在 Python 里做：只有 topic 以该名字开头、或某个「·」分段以它开头，
        才算「这只精灵的规则」；仅正文提到的降级为「提及」，最多带 1 条。
        """
        name = (pet_name or "").strip()
        if not name:
            return []
        # 别名只取「完整名 + 3 字简称」，**不要 2 字前缀**：
        # 「圣光灵神SP」截成「圣光」会把「圣光莫妮卡」的规则全认领过来（实测串味）。
        # 3 字仍保留，是为了「空元行者 → 空元」这种真简称能用；
        # 而「圣光灵」不会误中「圣光莫妮卡」。
        aliases = [name]
        if len(name) >= 4:
            aliases.append(name[:3])
        aliases = [a for a in dict.fromkeys(aliases) if a]

        conds, args = [], []
        for a in aliases:
            conds.append("(topic like ? or content like ?)")
            args += [f"%{a}%", f"%{a}%"]
        rows = self._kb.execute(
            f"select topic, content from facts where {' or '.join(conds)} order by updated_at desc",
            tuple(args),
        ).fetchall()

        def owned(topic: str) -> bool:
            t = str(topic or "")
            for seg in [t] + t.split("·"):
                for a in aliases:
                    if seg.startswith(a):
                        return True
            return False

        primary = [(t, c) for t, c in rows if owned(t)]
        others = [(t, c) for t, c in rows if not owned(t)]
        out = primary[:limit]
        if others and len(out) < limit:
            t, c = others[0]
            out.append((f"〔提及·不是本条规则的主体〕{t}", c))
        return out

    def _facts_search_legacy(self, keywords: list[str], limit: int = 8) -> list[tuple[str, str]]:
        """按多个关键词（精灵名、技能名、机制词）联合搜已确认规则。"""
        words = [w.strip() for w in keywords if w and w.strip()]
        if not words:
            return []
        conds = []
        args: list[str] = []
        for w in words:
            conds.append("(topic like ? or content like ?)")
            args.extend([f"%{w}%", f"%{w}%"])
        sql = (
            "select topic, content from facts where " + " or ".join(conds)
            + " order by updated_at desc limit ?"
        )
        args.append(str(limit))
        rows = self._kb.execute(sql, tuple(args)).fetchall()
        seen, out = set(), []
        for t, c in rows:
            if t in seen:
                continue
            seen.add(t)
            out.append((t, c))
        return out

    def resolve_pending_by_pet(self, pet_name: str) -> int:
        name = (pet_name or "").strip()
        if not name:
            return 0
        cur = self._kb.execute(
            "update pending set resolved=1 where resolved=0 and key like ?", (f"%{name}%",)
        )
        self._kb.commit()
        return cur.rowcount

    def fact_count(self) -> int:
        return self._kb.execute("select count(*) from facts").fetchone()[0]

    def close(self) -> None:
        self._kb.close()
