# -*- coding: utf-8 -*-
"""阵容图渲染：roster（41635 结果）→ Jinja2 → Playwright 截图。

和 render_card.py 同一套架构（aiohttp 下载 → base64 内嵌 → jinja2 → playwright 截图），
但用的是 templates/roster_card/。

数据来源：`_re/lineup_followup/headless_roster.py` 输出的 JSON（cmdId 41635）
头像规则：
    · 穿皮肤 → pet_skin.resource_id（如 3311 穿 508 → 1400508）
    · 没皮肤 / 皮肤不存在 / skinId >= 100000 → pet.resource_id
"""
from __future__ import annotations

import base64
import os
import time as _time

import aiohttp

_HERE = os.path.dirname(os.path.abspath(__file__))
_TPL_DIR = os.path.join(_HERE, "templates", "roster_card")
_TPL_FILE = "template.html.j2"

# 素材源：官方 CDN 优先，其次两个 GitHub 镜像
_HEAD_SRC = [
    "https://newseer.61.com/web/monster/head/{}.png",
    "https://cnb.cool/SeerAPI/seer-unity-assets/-/git/raw/main/newseer/assets/art/ui/assets/pet/head/{}.png",
    "https://raw.githubusercontent.com/SeerAPI/seer-unity-assets/refs/heads/main/newseer/assets/art/ui/assets/pet/head/{}.png",
]

# useFlag 语义（来自客户端反编译：OtherPetInfoManager）
FLAG_ATTACK = (1, 2)      # 1=出战 2=首发
FLAG_STANDBY = 7          # 7=待命

# 属性色（照抄游戏那条属性条的主色；组合属性取主属性）
TYPE_COLORS = {
    1: ("#6fbf4a", "#a8e07a"),    # 草
    2: ("#4a9ee8", "#8cc4f5"),    # 水
    3: ("#e8543f", "#f58a70"),    # 火
    4: ("#8fc7f0", "#c4e2f8"),    # 飞行
    5: ("#f5d84a", "#fae98c"),    # 电
    6: ("#9aa3b5", "#c8cedb"),    # 机械
    7: ("#c8934a", "#e0b87a"),    # 地面
    8: ("#b8b8c0", "#d8d8de"),    # 普通
    9: ("#6fd0e8", "#a8e5f5"),    # 冰
    10: ("#b46ce0", "#d3a4f0"),   # 超能
    11: ("#e07a3c", "#f0a870"),   # 战斗
    12: ("#f0d060", "#f8e8a0"),   # 光
    13: ("#6a4a8f", "#9a7ab8"),   # 暗影
    14: ("#9b6fd6", "#c0a0e8"),   # 神秘
    15: ("#7a5fd6", "#a890e8"),   # 龙
    16: ("#e8d48a", "#f5e8b8"),   # 圣灵
    17: ("#c06fd6", "#dda0e8"),   # 次元
    18: ("#c9a44a", "#e0c47a"),   # 远古
    19: ("#a03050", "#c87090"),   # 邪灵
    20: ("#7ac06a", "#a8d89a"),   # 自然
}
DEFAULT_TYPE_COLOR = ("#8f9cbf", "#c0c8dd")


def type_color(type_id: int | None, primary_id: int | None = None) -> tuple[str, str]:
    """属性 → (主色, 浅色)。组合属性用 primary_id，查不到用灰。"""
    for k in (primary_id, type_id):
        if k and int(k) in TYPE_COLORS:
            return TYPE_COLORS[int(k)]
    return DEFAULT_TYPE_COLOR


def _data_uri(raw: bytes, mime: str = "image/png") -> str:
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())


async def _fetch_head(session: aiohttp.ClientSession, rid) -> bytes:
    if not rid:
        return b""
    for tmpl in _HEAD_SRC:
        try:
            async with session.get(tmpl.format(rid),
                                   timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    b = await r.read()
                    if len(b) > 200:
                        return b
        except Exception:
            continue
    return b""


def _head_resource(pet: dict, pet_row: dict | None, skin_row: dict | None) -> int:
    """决定用哪张头像。

    皮肤优先；皮肤表里查不到 或 skinId>=100000（无皮肤标记）→ 回退本体。
    """
    skin_id = int(pet.get("skinId") or 0)
    if skin_id and skin_id < 100000 and skin_row and skin_row.get("resource_id"):
        return int(skin_row["resource_id"])
    if pet_row and pet_row.get("resource_id"):
        return int(pet_row["resource_id"])
    return int(pet.get("id") or 0)


async def render_roster_card(roster: dict, *, lookup, target=None, viewport_width: int = 560,
                             timeout_ms: int = 20000) -> bytes:
    """把 headless_roster.py 的 JSON 渲染成 PNG。

    参数
    ----
    roster : dict
        {"target": 米米号, "pets": [...], "source": "game-tcp", ...}
    lookup : callable(pid) -> dict
        需要返回 {"name": str, "resource_id": int, "skills": [技能名...],
                  "skin": {"resource_id": int} | None}
        由调用方接自己的数据库（seerapi）。
    """
    import jinja2
    from playwright.async_api import async_playwright

    pets = []
    for p in roster.get("pets") or []:
        pid = int(p.get("id") or 0)
        meta = lookup(pid) or {}
        skin_id = int(p.get("skinId") or 0)
        skin_row = meta.get("skin") if skin_id and skin_id < 100000 else None
        rid = _head_resource(p, {"resource_id": meta.get("resource_id") or pid}, skin_row)
        c_main, c_light = type_color(meta.get("type_id"), meta.get("primary_id"))
        pets.append({
            "id": pid,
            "name": meta.get("name") or f"#{pid}",
            "level": p.get("level") or 0,
            "useFlag": p.get("useFlag") or 0,
            "skinId": skin_id,
            "headRid": rid,
            "baseRid": int(meta.get("resource_id") or pid),
            "altRids": list((meta.get("skin") or {}).get("alt_resource_ids") or []),
            "head": "",
            "type_color": c_main,
            "type_color_light": c_light,
            "catchTime": p.get("catchTime") or 0,
        })

    # ── 下载头像 ──
    async with aiohttp.ClientSession() as s:
        for p in pets:
            raw = await _fetch_head(s, p["headRid"])
            # 三级回退：皮肤自己的 → 同名皮肤（别的机体）→ 本体
            # 依据：部分皮肤头像上游没做（星光艾欧「星际领航员」1400840 缺失，
            # 但老机体 3437 的同名皮肤 1400812 存在）。
            for alt in p.get("altRids") or []:
                if raw:
                    break
                if alt != p["headRid"]:
                    raw = await _fetch_head(s, alt)
            if not raw and p["headRid"] != p["baseRid"]:
                raw = await _fetch_head(s, p["baseRid"])
            p["head"] = _data_uri(raw) if raw else ""

    attack = [p for p in pets if p["useFlag"] in FLAG_ATTACK]
    standby = [p for p in pets if p["useFlag"] == FLAG_STANDBY]
    attack.sort(key=lambda x: (0 if x["useFlag"] == 2 else 1, x["catchTime"]))
    standby.sort(key=lambda x: x["catchTime"])

    ctx = {
        "target": target or roster.get("target"),
        "nick": roster.get("nick") or "",
        "time": _time.strftime("%Y-%m-%d %H:%M", _time.localtime(
            int(roster.get("queriedAt") or _time.time()))),
        "attack": attack,
        "standby": standby,
    }

    # ── 渲染 ──
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TPL_DIR), autoescape=False)
    html = env.get_template(_TPL_FILE).render(**ctx)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            bctx = await browser.new_context(
                viewport={"width": viewport_width, "height": 600},
                device_scale_factor=2,
            )
            page = await bctx.new_page()
            await page.set_content(html, wait_until="networkidle", timeout=timeout_ms)
            await page.wait_for_timeout(400)
            el = await page.query_selector(".wrap")
            data = await el.screenshot() if el else await page.screenshot(full_page=True)
        finally:
            await browser.close()
    return data


async def render_roster_to_file(roster: dict, *, lookup, out_path: str,
                                viewport_width: int = 560) -> str:
    data = await render_roster_card(roster, lookup=lookup, viewport_width=viewport_width)
    with open(out_path, "wb") as f:
        f.write(data)
    return out_path
