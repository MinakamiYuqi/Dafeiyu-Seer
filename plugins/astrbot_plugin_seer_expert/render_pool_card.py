# -*- coding: utf-8 -*-
"""巅峰圣战精灵池渲染：池子数据 → Jinja2 → Playwright 截图。

和 render_tier_list.py / render_card.py / render_roster_card.py 同一套架构
（aiohttp 下载 → base64 内嵌 → jinja2 → playwright 截图），
模板在 templates/pool_card/。

和 tier_list 的区别：
  · tier_list 贴**立绘 body**（长条，一格要看清是谁）
  · 这里贴**头像 head**（方形，和游戏内「巅峰圣战精灵池」页面一致）

数据源：manual_db/pvp.db 的 pvp 表。
  name = ban_0 / ban_2 / ban_3 / expert_0 / cost_35 / cost_20 / cost_10 / cost_6 / cost_0
  pet  = JSON 数组的精灵 id
  time = 档期（只有 cost_* 有值）

2026-11-14 实测：这份数据和游戏内页面顶部「数据版本 2026-11-13 19:00:33」完全对应，
所以它是当前版本，不是旧数据。seerapi 的 peak_pool 那套反而是上一档。
"""
from __future__ import annotations

import base64
import logging
import os
import time as _time

import aiohttp

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_TPL_DIR = os.path.join(_HERE, "templates", "pool_card")
_TPL_FILE = "template.html.j2"

# 头像源：官方 CDN 优先，其次两个镜像（和 service.py 的 PetHeadImageGetter 一致）
_HEAD_SRC = [
    "https://newseer.61.com/web/monster/head/{}.png",
    "https://cnb.cool/SeerAPI/seer-unity-assets/-/git/raw/main/"
    "newseer/assets/art/ui/assets/pet/head/{}.png",
    "https://raw.githubusercontent.com/SeerAPI/seer-unity-assets/refs/heads/main/"
    "newseer/assets/art/ui/assets/pet/head/{}.png",
]

# 三种模式，对应游戏内那三个 tab
# key: 模式名；label: 标题；sub: 副标题；blocks: [(池名, 显示名, 角标, 角标底色)]
POOL_MODES: dict[str, dict] = {
    "arena": {
        "label": "竞技限制池",
        "sub": "禁止池 / 限制池 / 准限制池",
        "blocks": [
            ("ban_0", "禁止池（限0）", "0", "#c9302c"),
            ("ban_2", "限制池（限2）", "2", "#d97b28"),
            ("ban_3", "准限制池（限3）", "3", "#d9a528"),
        ],
    },
    "expert": {
        "label": "专家禁用池",
        "sub": "专家模式",
        "blocks": [
            ("expert_0", "专家禁用池", "禁", "#5b5b64"),
        ],
    },
    "master": {
        "label": "大师竞技点",
        "sub": "按编队消耗点数分档",
        "blocks": [
            ("cost_35", "35 点", "35", "#8d4b8d"),
            ("cost_20", "20 点", "20", "#8d4b8d"),
            ("cost_10", "10 点", "10", "#8d4b8d"),
            ("cost_6", "6 点", "6", "#8d4b8d"),
            ("cost_0", "0 点", "0", "#8d4b8d"),
        ],
    },
}

# 用户/模型可能写的说法 → 模式
MODE_ALIAS: dict[str, str] = {
    "竞技": "arena", "限制": "arena", "巅峰": "arena", "巅峰池": "arena",
    "竞技池": "arena", "竞技限制池": "arena", "限制池": "arena",
    "专家": "expert", "专家池": "expert", "专家禁用池": "expert", "禁用": "expert",
    "大师": "master", "大师池": "master", "大师竞技点": "master", "点池": "master",
    "arena": "arena", "expert": "expert", "master": "master",
}


def normalize_mode(value: str) -> str | None:
    """把「竞技/限制/巅峰池/专家/大师」这类说法规范成 arena/expert/master。"""
    if not value:
        return None
    s = str(value).strip()
    if s in MODE_ALIAS:
        return MODE_ALIAS[s]
    s2 = s.replace(" ", "").replace("　", "")
    if s2 in MODE_ALIAS:
        return MODE_ALIAS[s2]
    # 容错：包含关系（「看看竞技模式的池子」）
    for k, v in MODE_ALIAS.items():
        if len(k) >= 2 and k in s2:
            return v
    return None


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
    """按精灵总数降 DPR。

    2026-11-13 tier_list 实测：50 只 DPR 2.0 时 1960px / 3.2 MB 太大。
    2026-11-14 本渲染器实测：master 126 只 DPR 1.3 时 1170x4515 / 1.6 MB，
    QQ 发送偏大，所以 >80 只再压到 1.1（高度降到 ~3800，肉眼仍清晰）。
    """
    if total <= 12:
        return 2.0
    if total <= 24:
        return 1.8
    if total <= 40:
        return 1.5
    if total <= 80:
        return 1.3
    return 1.1


def _pick_quality(total: int) -> int:
    """JPEG 质量。大图再降一点，因为文件大头来自像素量而非细节。"""
    if total <= 40:
        return 92
    if total <= 80:
        return 88
    return 82


async def render_pool_card(
    mode: str,
    *,
    groups: dict[str, list[dict]],
    lookup,
    version: str = "",
    viewport_width: int = 900,
    timeout_ms: int = 30000,
) -> bytes:
    """把池子数据画成一张图。

    参数
    ----
    mode : "arena" / "expert" / "master"
    groups : {池名: [{"id": int, "name": str}, ...]}
        池名要和 POOL_MODES 里的对齐（ban_0 / expert_0 / cost_35 …）。
        缺的池子会画成「这一档没有精灵」。
    lookup : callable(pid) -> dict | None
        返回 {"name": str, "resource_id": int}，用来补名字和头像 id。
    version : 数据版本（例如 "2026-11-13 19:00"），打在副标题下面。
    """
    import jinja2
    from playwright.async_api import async_playwright

    spec = POOL_MODES.get(mode) or POOL_MODES["arena"]

    # ── 逐档整理 + 查 resource_id ──
    blocks = []
    for pool_key, label, tag, tag_color in spec["blocks"]:
        items = []
        for p in (groups.get(pool_key) or []):
            pid = p.get("id")
            meta = None
            try:
                meta = lookup(pid) if pid is not None else None
            except Exception:
                meta = None
            meta = meta or {}
            rid = int(meta.get("resource_id") or 0) or (int(pid) if pid else 0)
            items.append({
                "name": meta.get("name") or p.get("name") or (f"#{rid}" if rid else "?"),
                "rid": rid,
                "no": str(pid) if pid is not None else "",
                "img": "",
            })
        blocks.append({
            "label": label,
            "tag": tag,
            "tag_color": tag_color,
            "pets": items,
        })

    # ── 下载头像 ──
    async with aiohttp.ClientSession() as s:
        for b in blocks:
            for p in b["pets"]:
                raw = await _fetch_head(s, p["rid"])
                p["img"] = _data_uri(raw) if raw else ""

    total = sum(len(b["pets"]) for b in blocks)
    ctx = {
        "title": spec["label"],
        "subtitle": f"{spec['sub']}｜数据版本 {version}" if version else spec["sub"],
        "blocks": blocks,
        "footer_left": version and f"手册 pvp.db · {version}" or "手册 pvp.db",
        "footer_right": _time.strftime("%Y-%m-%d %H:%M"),
    }

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TPL_DIR), autoescape=False)
    html = env.get_template(_TPL_FILE).render(**ctx)

    scale = _pick_scale(total)
    logger.info("[pool_card] %s：%d 只 / %d 档 → DPR %.1f / Q%d",
                mode, total, len(blocks), scale, _pick_quality(total))

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
            # JPEG：整块不透明底，PNG 的 alpha 通道纯属浪费
            shot_kw = {"type": "jpeg", "quality": _pick_quality(total)}
            data = (await el.screenshot(**shot_kw)) if el \
                else (await page.screenshot(full_page=True, **shot_kw))
        finally:
            await browser.close()

    logger.info("[pool_card] 出图 %d KB", len(data) // 1024)
    return data


async def render_pool_card_to_file(mode: str, *, out_path: str, **kw) -> str:
    """落盘。内容是 JPEG，out_path 请用 .jpg。"""
    data = await render_pool_card(mode, **kw)
    with open(out_path, "wb") as f:
        f.write(data)
    return out_path
