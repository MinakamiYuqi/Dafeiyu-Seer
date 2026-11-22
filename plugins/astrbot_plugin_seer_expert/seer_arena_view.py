# -*- coding: utf-8 -*-
"""巅峰数据看板：**只搬运本地整理好的数据，不做统计计算**。

监控进程（`_re/xm/arena_monitor.py`）负责采集与聚合，物化两份东西到 plugin_data：

```
arena_board.json   榜单看板：覆盖率 / 套装汇总 / 套装实战分布 / 精灵×套装榜
arena.db           原始库：只用来查「某个米米号近 N 天的日战绩」
```

本模块的职责只有三件：
  ① 读 json / 查那张日表
  ② 把结果塞进模板
  ③ Playwright 截图成 PNG

**口径提醒（对外必须说清）**：数字是「携带口径」——一场赢，当时携带的精灵各记 1 场 1 胜，
所以各精灵的场次**不能相加**；平局按 0.5 胜计。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_TPL_DIR = os.path.join(os.path.dirname(__file__), "templates", "arena_card")
_TPL_FILE = "template.html.j2"


def board_path(data_dir: str) -> Path:
    return Path(data_dir) / "arena_board.json"


def db_path(data_dir: str) -> Path:
    return Path(data_dir) / "arena.db"


def load_board(data_dir: str) -> dict | None:
    """读榜单看板。文件不存在（监控没跑过）时返回 None，让调用方给提示。"""
    p = board_path(data_dir)
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    d["_age_min"] = round((os.path.getmtime(p) and
                           (__import__("time").time() - os.path.getmtime(p)) / 60) or 0, 1)
    return d


def load_heartbeat(data_dir: str) -> dict | None:
    """读监控心跳（`arena_heartbeat.json`）。没有就返回 None。

    ★ 2026-11-21 加：心跳停了 = 监控挂了。以前只能靠人翻日志时间才发现。
    返回里带 `age_min`（距今多少分钟），调用方自己决定多少分钟算"挂了"。
    """
    p = Path(data_dir) / "arena_heartbeat.json"
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        d["age_min"] = round((__import__("time").time() - float(d.get("ts") or 0)) / 60, 1)
    except (TypeError, ValueError):
        d["age_min"] = None
    return d


def _parse_hm(s: str) -> tuple[int, int]:
    """'11:00' → (11, 0)。"""
    h, m = str(s).split(":")
    return int(h), int(m)


def _day_windows(day, raw) -> list[tuple[datetime, datetime]]:
    """把作息表里的一组 "HH:MM" 区间套到指定日期上。"""
    out = []
    for a, b in raw or []:
        ah, am = _parse_hm(a)
        bh, bm = _parse_hm(b)
        out.append((datetime(day.year, day.month, day.day, ah, am),
                    datetime(day.year, day.month, day.day, bh, bm)))
    return out


def monitor_state(hb: dict | None, now: datetime | None = None) -> dict:
    """判断「监控现在该不该是新鲜的」，并算出下一次开盘时间。

    ★ 2026-11-23 十四号鱼加。原来 bot 只看「心跳 > 15 分钟」就报「监控可能挂了」，
      不知道竞技几点关门 —— 23:30 收盘后、每天 10:55 起采前，任何一次问榜都会
      白报一次警（2026-11-22 23:08 被群友当场指出「竞技 23:30 就关门了，白报了」）。
      作息表由监控写进心跳（`schedule` 字段），这里只读不抄 —— 窗口表只有一份真相。

    Args:
        hb: `load_heartbeat()` 的返回。心跳里带 `schedule` 才判得了。
        now: 当前时间（测试用）；None 取系统时间。

    Returns:
        dict：
          should_be_fresh  True  = 现在本该每 3 分钟刷一次 → 心跳停了就是真故障
                           False = 竞技关门/未开盘，心跳不动是正常作息
                           None  = 心跳里没有作息表（旧版监控写的），别下结论
          age_min          心跳距今多少分钟
          in_window        是否落在「该有新鲜数据」的时段（含开盘前基线、封账 3 分钟）
          window_end       当前窗口收盘时刻 'HH:MM'
          next_open        下一次开始采集的时刻 'MM-DD HH:MM'
          next_open_hm     同上，只取 'HH:MM'
    """
    now = now or datetime.now()
    age = (hb or {}).get("age_min")
    # 数据时刻优先用 last_collect（监控重启时它还是上次采集时刻）；
    # 老心跳没有这个字段才退回 iso。
    last_iso = (hb or {}).get("last_collect") or (hb or {}).get("iso") or ""
    data_age = age
    if last_iso:
        try:
            _d = (now - datetime.strptime(last_iso, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
            # 解析结果比 0 还早说明有心跳/时钟不一致，信监控自己报的 age_min
            data_age = round(_d if _d >= 0 else (age or 0), 1)
        except ValueError:
            data_age = age
    sched = (hb or {}).get("schedule") or None
    if not sched:
        return {"should_be_fresh": None, "age_min": age, "data_age_min": data_age,
                "last_collect": last_iso, "in_window": None,
                "window_end": "", "next_open": "", "next_open_hm": ""}
    lead = int(sched.get("lead_min") or 0)
    seal = int(sched.get("seal_min") or 0)
    in_win = False
    cur_end = None
    last_end = None
    nxt = None
    # 从昨天扫到后天：昨天为了「刚过零点时还能说出上一个窗口几点收的」，
    # 后天为了「周末最后一窗结束后，下一次开盘在后天（周一）」。
    for delta_days in (-1, 0, 1, 2):
        day = (now + timedelta(days=delta_days)).date()
        raw = sched.get("weekend") if day.weekday() >= 5 else sched.get("weekday")
        for start, end in _day_windows(day, raw):
            lead_at = start - timedelta(minutes=lead)
            close_at = end + timedelta(minutes=seal)
            if lead_at <= now < close_at:
                in_win = True
                cur_end = end
            elif now >= close_at:
                last_end = end
            elif now < lead_at and nxt is None:
                nxt = lead_at
    # 关门后 8 小时内还说得出「几点收的」；再往前就是「不在开放时段」了，
    # 免得早上 8 点回一句「已关门（昨天 23:30 收）」显得莫名其妙。
    if cur_end is None and last_end is not None and now - last_end <= timedelta(hours=8):
        cur_end = last_end
    return {
        "should_be_fresh": in_win,
        "age_min": age,
        "data_age_min": data_age,
        "last_collect": last_iso,
        "in_window": in_win,
        "window_end": cur_end.strftime("%H:%M") if cur_end else "",
        "next_open": nxt.strftime("%m-%d %H:%M") if nxt else "",
        "next_open_hm": nxt.strftime("%H:%M") if nxt else "",
    }


def prune_pngs(data_dir: str, pattern: str = "arena_player_*.png",
               keep_days: int = 7, keep_max: int = 200) -> int:
    """清理「发完就没用」的临时图，返回删掉的张数。

    ★ 2026-11-23（十四号鱼）：`_arena_send` 每发一次图就在 plugin_data 落一张，
      看板那三张是固定名（每次覆盖、不涨），**玩家战绩图是 `arena_player_<uid>.png`，
      一人一张、只增不减** —— 实测已经 8 张 2.8 MB，攒一年能到几千张、几百 MB，
      比数据库还大。而它们发完就没用了。

    两条规则，先保时间再保数量：
      · 超过 keep_days 天没被重画的，删
      · 剩下的如果还超过 keep_max 张，按 mtime 从旧到新删到 keep_max

    Args:
        data_dir: 本插件的 plugin_data 目录。
        pattern: 文件名通配。默认只清玩家图 —— 看板图是固定名，覆盖写，不用清。
        keep_days: 保留最近多少天。
        keep_max: 无论多新，最多留多少张（防一天之内刷几百个号也堆爆）。

    Returns:
        实际删除的文件数。删不掉（被占用等）就跳过，不抛异常。
    """
    p = Path(data_dir)
    if not p.is_dir():
        return 0
    files = [f for f in p.glob(pattern) if f.is_file()]
    if not files:
        return 0
    cutoff = datetime.now().timestamp() - keep_days * 86400
    removed = 0
    alive = []
    for f in files:
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
            else:
                alive.append(f)
        except OSError:
            continue
    if len(alive) > keep_max:
        alive.sort(key=lambda x: x.stat().st_mtime)
        for f in alive[:len(alive) - keep_max]:
            try:
                f.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def player_rows(data_dir: str, uid: int, days: int = 7) -> list[dict]:
    """某个米米号近 N 天的日战绩。只读 daily 表，不做任何推算。"""
    p = db_path(data_dir)
    if not p.is_file():
        return []
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select date, fight_end - fight_start, win_end - win_start"
            " from daily where uid = ? order by date desc limit ?",
            (int(uid), int(days))).fetchall()
        draws = dict(con.execute(
            "select date(ts, 'unixepoch', 'localtime') d, count(*) from matches"
            " where uid = ? and kind = 'draw' group by d", (int(uid),)).fetchall())
    finally:
        con.close()
    out = []
    for date, f, w in rows:
        f = int(f or 0)
        w = int(w or 0)
        d = int(draws.get(date, 0))
        out.append({"date": date, "matches": f, "wins": w, "draws": d,
                    "rate": round((w + 0.5 * d) / f * 100, 1) if f else None})
    return out


async def _render(ctx: dict, width: int = 980, timeout_ms: int = 25000) -> bytes:
    """模板 → HTML → Playwright 截图（与 render_card 同一套做法）。"""
    import jinja2
    from playwright.async_api import async_playwright

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TPL_DIR), autoescape=True)
    html = env.get_template(_TPL_FILE).render(**ctx)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            bctx = await browser.new_context(
                viewport={"width": width, "height": 800}, device_scale_factor=2)
            page = await bctx.new_page()
            await page.set_content(html, wait_until="networkidle", timeout=timeout_ms)
            await page.wait_for_timeout(300)
            el = await page.query_selector(".wrap")
            data = await el.screenshot() if el else await page.screenshot(full_page=True)
        finally:
            await browser.close()
    return data


async def render_board(board: dict, kind: str = "pets", top: int = 20,
                       suit: str = "") -> bytes:
    """榜单看板出图。kind: pets（按套装分区的精灵榜）/ suits / dist。

    suit: 只出这一个套装（按名字包含匹配，例：「晨曦」）。空 = 全部套装连成长图。
    """
    pets: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    if kind == "pets":
        names = board.get("pet_names") or {}

        def _nm(p: dict) -> dict:
            r = dict(p)
            r["name"] = names.get(str(p.get("monster_id")), f"ID {p.get('monster_id')}")
            r["rate"] = r.get("rate") or 0
            r["score"] = r.get("score") or 0
            r["pick"] = r.get("pick") or 0
            return r

        pets = [_nm(p) for p in (board.get("pets") or [])[:top]]
        # ★ 2026-11-21：按套装分区（照小苍星成品图）。各组条数在数据层已经截好，
        #   这里**不要再按 top 截** —— top=20 会把晨曦 94 只砍成 20 只。
        want = (suit or "").strip()
        for g in (board.get("pets_by_suit") or []):
            sname = g.get("suit") or "未识别"
            if want and want not in sname:
                continue
            groups.append({"suit": sname,
                           "count": len(g.get("rows") or []) or (g.get("count") or 0),
                           "matches": g.get("matches") or 0,
                           "rows": [_nm(p) for p in (g.get("rows") or [])]})
        if want and not groups:      # 指定的套装没数据 → 别出空白图
            groups = []
    return await _render({
        "mode": kind,
        "title": {"pets": "精灵 × 套装 战绩榜",
                  "suits": "套装战绩汇总",
                  "dist": "套装实战分布"}.get(kind, "巅峰看板"),
        "generated_at": board.get("generated_at", "-"),
        "coverage": board.get("coverage") or {},
        "suits": board.get("suits") or [],
        "dist": board.get("suit_dist") or [],
        "pets": pets,
        "groups": groups,
        "suit_filter": (suit or "").strip(),
        "uid": None,
        "rows": [],
    })


def _pet_names_in_db(data_dir: str, ids) -> dict[int, str]:
    """精灵 id → 名字。读 seerapi.sqlite（在兄弟插件目录里），取不到就返回空 —— 不编名字。"""
    ids = [int(i) for i in ids if i]
    if not ids:
        return {}
    cands = [
        Path(data_dir).parent / "astrbot_plugin_seer_info" / "seerapi.sqlite",
        Path(data_dir) / "seerapi.sqlite",
    ]
    for p in cands:
        if not p.is_file():
            continue
        try:
            con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
            try:
                q = ",".join("?" * len(ids))
                got = dict(con.execute(f"select id, name from pet where id in ({q})", ids))
            finally:
                con.close()
            if got:
                return got
        except sqlite3.Error:
            continue
    return {}


def player_loadouts(data_dir: str, uid: int, days: int = 7,
                    max_pets: int = 16) -> list[dict]:
    """某个米米号近 N 天**用什么打的**：按套装分组，每组带精灵明细（携带口径）。

    ★ 只有归因成功（exact / batch）的对局才进 carry —— unknown 那几局不知道当时带的
      哪 12 只，所以这里的场次合计可能**小于**日战绩里的场次，属正常，不是漏记。

    返回 [{suit, matches, wins, draws, rate, pets:[{id, name, matches, wins, rate}]}]
    """
    p = db_path(data_dir)
    if not p.is_file():
        return []
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        since = int(__import__("time").time()) - int(days) * 86400
        rows = con.execute(
            "select suit_id, suit_name, monster_id, sum(matches), sum(wins),"
            " coalesce(sum(draws),0) from carry where uid = ? and ts >= ?"
            " group by suit_id, suit_name, monster_id"
            " order by sum(matches) desc", (int(uid), since)).fetchall()
    finally:
        con.close()
    if not rows:
        return []
    names = _pet_names_in_db(data_dir, {r[2] for r in rows})

    by_suit: dict = {}
    for sid, sname, mid, m, w, d in rows:
        m = int(m or 0)
        w = int(w or 0)
        d = int(d or 0)
        # 套装这一层用「该套装下最多的那只精灵的场次」代表真实局数：
        # 携带口径下同轮携带的 12 只场次相同，直接 sum 会变成 12 倍。
        g = by_suit.setdefault(sid, {"suit": sname or f"id={sid}", "matches": 0,
                                     "wins": 0, "draws": 0, "pets": []})
        g["matches"] = max(g["matches"], m)
        g["wins"] = max(g["wins"], w)
        g["draws"] = max(g["draws"], d)
        g["pets"].append({"id": mid, "name": names.get(mid, f"ID {mid}"),
                          "matches": m, "wins": w,
                          "rate": round((w + 0.5 * d) / m * 100, 1) if m else None})
    out = []
    for g in by_suit.values():
        g["pets"].sort(key=lambda x: (-(x["matches"] or 0), -(x["rate"] or 0)))
        g["pets"] = g["pets"][:max_pets]
        g["rate"] = (round((g["wins"] + 0.5 * g["draws"]) / g["matches"] * 100, 1)
                     if g["matches"] else None)
        out.append(g)
    out.sort(key=lambda g: -(g["matches"] or 0))
    return out


async def render_player(uid: int, rows: list[dict],
                        loadouts: list[dict] | None = None) -> bytes:
    """单个米米号近 N 天日战绩 + 阵容，出图。"""
    tot_m = sum(r["matches"] for r in rows)
    tot_w = sum(r["wins"] for r in rows)
    tot_d = sum(r["draws"] for r in rows)
    return await _render({
        "mode": "player",
        "title": f"{uid} 近期战绩",
        "generated_at": "",
        "coverage": {},
        "suits": [],
        "dist": [],
        "pets": [],
        "groups": [],
        "loadouts": loadouts or [],
        "uid": uid,
        "rows": rows,
        "totals": {"matches": tot_m, "wins": tot_w, "draws": tot_d,
                   "rate": round((tot_w + 0.5 * tot_d) / tot_m * 100, 1) if tot_m else None},
    })
