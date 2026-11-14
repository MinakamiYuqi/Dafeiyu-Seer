# -*- coding: utf-8 -*-
"""夯拉榜渲染：分级表 → Jinja2 → Playwright 截图。

和 render_card.py / render_roster_card.py 同一套架构
（aiohttp 下载 → base64 内嵌 → jinja2 → playwright 截图），
模板在 templates/tier_list/。

和那两个的区别：这里贴的是**立绘（body）**不是头像（head）。
榜单一格要看清是谁，头像太小认不出。

素材源同 render_card.py：
    https://newseer.61.com/web/monster/body/{resource_id}.png
    cnb.cool / raw.githubusercontent 两个镜像兜底
"""
from __future__ import annotations

import base64
import logging
import os
import time as _time

import aiohttp

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_TPL_DIR = os.path.join(_HERE, "templates", "tier_list")
_TPL_FILE = "template.html.j2"

# 立绘源：官方 CDN 优先，其次两个镜像
_BODY_SRC = [
    "https://newseer.61.com/web/monster/body/{}.png",
    "https://cnb.cool/SeerAPI/seer-unity-assets/-/git/raw/main/"
    "newseer/assets/art/ui/assets/pet/body/{}.png",
    "https://raw.githubusercontent.com/SeerAPI/seer-unity-assets/refs/heads/main/"
    "newseer/assets/art/ui/assets/pet/body/{}.png",
]

# 分级：从夯到拉，颜色照截图那套（红 / 金 / 黄 / 米白 / 灰）
TIERS = [
    ("夯",     "#e23b2e", "#ffffff"),
    ("顶级",   "#f0b429", "#3a2a06"),
    ("人上人", "#f5e542", "#3a2a06"),
    ("NPC",    "#e8e4d8", "#5a5a4a"),
    ("拉完了", "#9a9a92", "#ffffff"),
]
TIER_LOOKUP = {name: (color, text) for name, color, text in TIERS}

# 允许的别名（用户/模型可能写别的说法）
TIER_ALIAS = {
    "神": "夯", "幻神": "夯", "t0": "夯", "T0": "夯", "s": "夯", "S": "夯",
    "顶级": "顶级", "t1": "顶级", "T1": "顶级", "a": "顶级", "A": "顶级",
    "人上人": "人上人", "t2": "人上人", "T2": "人上人", "b": "人上人", "B": "人上人",
    "npc": "NPC", "NPC": "NPC", "普通": "NPC", "t3": "NPC", "T3": "NPC",
    "拉": "拉完了", "拉完了": "拉完了", "下水道": "拉完了", "t4": "拉完了", "T4": "拉完了",
}


def normalize_tier(name: str) -> str | None:
    """把一个分级名规范成 TIERS 里的标准写法；不认识返回 None。"""
    if not name:
        return None
    s = str(name).strip()
    if s in TIER_LOOKUP:
        return s
    if s in TIER_ALIAS:
        return TIER_ALIAS[s]
    # 容错：去空格、全角转半角后重试
    s2 = s.replace(" ", "").replace("　", "")
    if s2 in TIER_LOOKUP:
        return s2
    if s2 in TIER_ALIAS:
        return TIER_ALIAS[s2]
    return None


def _data_uri(raw: bytes, mime: str = "image/png") -> str:
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())


async def _fetch_body(session: aiohttp.ClientSession, rid) -> bytes:
    if not rid:
        return b""
    for tmpl in _BODY_SRC:
        try:
            async with session.get(tmpl.format(rid),
                                   timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    b = await r.read()
                    if len(b) > 300:
                        return b
        except Exception:
            continue
    return b""


def _pick_scale(total: int) -> float:
    """按精灵总数选 device_scale_factor。

    格子尺寸固定 —— 精灵多了它会自己换行、整图变长，观感是好的；
    真正的问题是文件体积：DPR=2 时 50 只就是 1960x3246 / 3.2 MB，
    QQ 发送容易失败。所以精灵越多，采样倍率越低（文字仍清晰）。
    """
    if total <= 12:
        return 2.0
    if total <= 24:
        return 1.8
    if total <= 40:
        return 1.5
    return 1.3


async def render_tier_list(
    rows: list[dict],
    *,
    lookup,
    title: str = "从夯到拉",
    subtitle: str = "",
    footer_left: str = "",
    footer_right: str = "",
    viewport_width: int = 900,
    timeout_ms: int = 25000,
) -> bytes:
    """把分级表渲染成 PNG。

    参数
    ----
    rows : list[dict]
        [{"tier": "夯", "pets": [{"name": "无极圣武", "id": 4903}, ...]}, ...]
        tier 可以是标准名或别名；顺序按传入顺序，不认识的 tier 会被跳过。
        pets 里每项只要有 name；有 id 时会用它查 resource_id。
    lookup : callable(pid_or_name) -> dict
        需要返回 {"name": str, "resource_id": int}。
        传进来的 key 可能是 id 也可能名字 —— 调用方自己判断。
        **允许返回 None**（查不到就画占位）。
    title / subtitle : 标题与副标题

    尺寸：格子大小固定（每只都看得清名字），**按精灵总数降 device_scale_factor**。
    2026-11-13 实测：50 只时 1960x3246 / 3.2 MB，QQ 发送有风险；
    降到 DPR 1.4 后 1372x2272 / 约 1.1 MB，观感没差多少。
    """
    import jinja2
    from playwright.async_api import async_playwright

    # ── 规范化分级行（保持顺序，去重，跳过不认识的） ──
    merged: dict[str, list] = {}
    order: list[str] = []
    skipped: list[str] = []
    for row in rows or []:
        raw_tier = (row or {}).get("tier") or ""
        tier = normalize_tier(raw_tier)
        if tier is None:
            if str(raw_tier).strip():
                skipped.append(str(raw_tier).strip())
            continue
        if tier not in merged:
            merged[tier] = []
            order.append(tier)
        for p in (row.get("pets") or []):
            if isinstance(p, str):
                merged[tier].append({"name": p, "id": None})
            elif isinstance(p, dict):
                merged[tier].append({
                    "name": str(p.get("name") or p.get("id") or "").strip(),
                    "id": p.get("id"),
                })

    # 没写的分级也补空行（保持「从夯到拉」的完整观感）
    if not order:
        order = [name for name, _, _ in TIERS]
    else:
        full = [name for name, _, _ in TIERS]
        order = [t for t in full if t in merged] + [t for t in order if t not in full]

    # ── 查 resource_id ──
    resolved: dict[str, list[dict]] = {}
    for tier in order:
        items = []
        for p in merged.get(tier, []):
            meta = None
            key = p["id"] if p["id"] is not None else p["name"]
            try:
                meta = lookup(key) if key else None
            except Exception:
                meta = None
            meta = meta or {}
            rid = int(meta.get("resource_id") or 0) or (int(p["id"]) if p["id"] else 0)
            items.append({
                "name": meta.get("name") or p["name"] or f"#{rid or '?'}",
                "rid": rid,
                "img": "",
            })
        resolved[tier] = items

    # ── 下载立绘 ──
    async with aiohttp.ClientSession() as s:
        for tier in order:
            for p in resolved[tier]:
                raw = await _fetch_body(s, p["rid"])
                p["img"] = _data_uri(raw) if raw else ""

    ctx_rows = []
    for tier in order:
        color, text = TIER_LOOKUP.get(tier, ("#cccccc", "#333333"))
        ctx_rows.append({
            "label": tier,
            "color": color,
            "text": text,
            "pets": resolved[tier],
        })

    ctx = {
        "title": title,
        "subtitle": subtitle,
        "rows": ctx_rows,
        "footer_left": footer_left or _time.strftime("%Y-%m-%d %H:%M"),
        "footer_right": footer_right,
        "skipped": skipped,
    }

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TPL_DIR), autoescape=False)
    html = env.get_template(_TPL_FILE).render(**ctx)

    total = sum(len(v) for v in resolved.values())
    scale = _pick_scale(total)
    logger.info("[tier_list] %d 只 / %d 档 → DPR %.1f", total, len(order), scale)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            bctx = await browser.new_context(
                viewport={"width": viewport_width, "height": 800},
                device_scale_factor=scale,
            )
            page = await bctx.new_page()
            await page.set_content(html, wait_until="networkidle", timeout=timeout_ms)
            await page.wait_for_timeout(400)
            el = await page.query_selector(".wrap")
            # 用 JPEG 而不是 PNG：榜单是整块不透明底，PNG 里那堆 alpha 通道纯属浪费。
            # 实测 50 只：PNG 1582 KB → JPEG(92) 约 400 KB，肉眼分辨不出差别。
            shot_kw = {"type": "jpeg", "quality": 92}
            data = (await el.screenshot(**shot_kw)) if el \
                else (await page.screenshot(full_page=True, **shot_kw))
        finally:
            await browser.close()
    logger.info("[tier_list] 出图 %d KB", len(data) // 1024)
    return data


async def render_tier_list_to_file(rows, *, lookup, out_path: str, **kw) -> str:
    """落盘。注意：内容是 JPEG，out_path 请用 .jpg（PNG 后缀名会骗人）。"""
    data = await render_tier_list(rows, lookup=lookup, **kw)
    with open(out_path, "wb") as f:
        f.write(data)
    return out_path
