# -*- coding: utf-8 -*-
"""巅峰圣战投票榜渲染：票数列表 → Jinja2 → Playwright 截图。

和 render_pool_card.py / render_tier_list.py 同一套架构
（aiohttp 下载 → base64 内嵌 → jinja2 → playwright 截图），
模板在 templates/vote_card/。

数据来源：headless 的 cmd_vote（游戏协议 cmd 4481 GET_DAILY_RANK_INFO）
    key=191 → 限制池（限2），取前 20
    key=192 → 准限制池（限3），取前 30
    响应 = u32 count + count × (u32 精灵id + i32 票数 + 16B 占位)
    ★ 那个 16 字节服务端不填（全零），所以榜项是**精灵**不是玩家。

2026-11-14 实测：
    20261030 期 → key=191 有数据（20 条），key=192 空
    20261106 期 → key=192 有数据（30 条），key=191 空
    → **限2 和限3 是交替投票的**，看一期只能看到一个池子。
"""
from __future__ import annotations

import base64
import logging
import os
import time as _time

import aiohttp

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_TPL_DIR = os.path.join(_HERE, "templates", "vote_card")
_TPL_FILE = "template.html.j2"

# 头像源（和 service.py 的 PetHeadImageGetter 一致）
_HEAD_SRC = [
    "https://newseer.61.com/web/monster/head/{}.png",
    "https://cnb.cool/SeerAPI/seer-unity-assets/-/git/raw/main/"
    "newseer/assets/art/ui/assets/pet/head/{}.png",
    "https://raw.githubusercontent.com/SeerAPI/seer-unity-assets/refs/heads/main/"
    "newseer/assets/art/ui/assets/pet/head/{}.png",
]

# 池子标签
VOTE_LABELS = {191: "限制池（限2）", 192: "准限制池（限3）"}


def _data_uri(raw: bytes, mime: str = "image/png") -> str:
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())


async def _fetch_head(session: aiohttp.ClientSession, rid) -> bytes:
    if not rid:
        return b""
    for tmpl in _HEAD_SRC:
        try:
            async with session.get(tmpl.format(rid), timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status == 200:
                    data = await r.read()
                    if data and len(data) > 200:
                        return data
        except Exception:
            continue
    return b""


def _pick_scale(total: int) -> float:
    if total <= 12:
        return 2.0
    if total <= 24:
        return 1.8
    return 1.5


def _fmt_sub_key(sub_key: int) -> str:
    """20261030 → 2026-10-30"""
    s = str(sub_key)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s or "未标注"


async def render_vote_card(
    key: int,
    rows: list[dict],
    *,
    sub_key: int = 0,
    lookup,
    title: str = "",
    note: str = "",
    viewport_width: int = 820,
    timeout_ms: int = 30000,
) -> bytes:
    """画一张投票榜。

    参数
    ----
    key : 191（限2）或 192（限3）
    rows : headless cmd_vote 的输出，每项 {"id", "name", "score"}
    sub_key : 期次（20261030 这种），只用于标题显示
    lookup : callable(pid) -> dict | None，返回 {"name", "resource_id"}
    note : 底部说明（例如「本期只投限2，限3 在上一期」）
    """
    import jinja2
    from playwright.async_api import async_playwright

    label = VOTE_LABELS.get(key, f"key={key}")
    ordered = sorted(rows or [], key=lambda r: -(r.get("score") or 0))
    top = max((r.get("score") or 0) for r in ordered) if ordered else 0

    items = []
    for i, r in enumerate(ordered, 1):
        pid = r.get("id")
        meta = None
        try:
            meta = lookup(pid) if pid is not None else None
        except Exception:
            meta = None
        meta = meta or {}
        rid = int(meta.get("resource_id") or 0) or (int(pid) if pid else 0)
        score = int(r.get("score") or 0)
        items.append({
            "rank": i,
            "name": meta.get("name") or r.get("name") or (f"#{rid}" if rid else "?"),
            "score": score,
            "pct": round(score * 100.0 / top, 1) if top else 0,
            "img": "",
            "rid": rid,
        })

    async with aiohttp.ClientSession() as s:
        for it in items:
            raw = await _fetch_head(s, it["rid"])
            it["img"] = _data_uri(raw) if raw else ""

    ctx = {
        "title": title or f"巅峰圣战投票 · {label}",
        "subtitle": f"{_fmt_sub_key(sub_key)} 期｜共 {len(items)} 项",
        "note": note or "",
        "rows": items,
        "footer_left": "游戏内日榜 (cmd 4481) · 玩家投票结果",
        "footer_right": _time.strftime("%Y-%m-%d %H:%M"),
    }

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TPL_DIR), autoescape=False)
    html = env.get_template(_TPL_FILE).render(**ctx)

    scale = _pick_scale(len(items))
    logger.info("[vote_card] key=%s：%d 项 → DPR %.1f", key, len(items), scale)

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
            shot_kw = {"type": "jpeg", "quality": 90}
            data = (await el.screenshot(**shot_kw)) if el \
                else (await page.screenshot(full_page=True, **shot_kw))
        finally:
            await browser.close()

    logger.info("[vote_card] 出图 %d KB", len(data) // 1024)
    return data


async def render_vote_card_to_file(key: int, rows: list[dict], *, out_path: str, **kw) -> str:
    data = await render_vote_card(key, rows, **kw)
    with open(out_path, "wb") as f:
        f.write(data)
    return out_path
