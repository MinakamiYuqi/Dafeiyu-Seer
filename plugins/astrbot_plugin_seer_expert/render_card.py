# -*- coding: utf-8 -*-
"""配置图渲染：config_view → Jinja2 → Playwright 截图。

自包含实现（不依赖 seer_info 插件），依赖：jinja2 / playwright / Pillow / aiohttp。
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import tempfile

import aiohttp

_HERE = os.path.dirname(os.path.abspath(__file__))
_TPL_DIR = os.path.join(_HERE, "templates", "config_card")
_TPL_FILE = "template.html.j2"
_MASCOT = os.path.join(_TPL_DIR, "mascot.png")

# 素材源（和 seer_info 的 GetImage 同源）
_SRC = {
    "body": [
        "https://newseer.61.com/web/monster/body/{}.png",
        "https://cnb.cool/SeerAPI/seer-unity-assets/-/git/raw/main/newseer/assets/art/ui/assets/pet/body/{}.png",
        "https://raw.githubusercontent.com/SeerAPI/seer-unity-assets/refs/heads/main/newseer/assets/art/ui/assets/pet/body/{}.png",
    ],
    "head": [
        "https://newseer.61.com/web/monster/head/{}.png",
        "https://cnb.cool/SeerAPI/seer-unity-assets/-/git/raw/main/newseer/assets/art/ui/assets/pet/head/{}.png",
    ],
    "mint": [
        "https://newseer.61.com/web/countermark/icon/{}.png",
        "https://cnb.cool/SeerAPI/seer-unity-assets/-/git/raw/main/newseer/assets/art/ui/assets/countermark/icon/{}.png",
    ],
    "suit": [
        "https://cnb.cool/SeerAPI/seer-unity-assets/-/git/raw/main/newseer/assets/art/ui/assets/item/cloth/suiticon/{}.png",
        "https://raw.githubusercontent.com/SeerAPI/seer-unity-assets/refs/heads/main/newseer/assets/art/ui/assets/item/cloth/suiticon/{}.png",
    ],
    "eye": [
        "https://cnb.cool/SeerAPI/seer-unity-assets/-/git/raw/main/newseer/assets/art/ui/assets/item/cloth/prev/{}.png",
        "https://raw.githubusercontent.com/SeerAPI/seer-unity-assets/refs/heads/main/newseer/assets/art/ui/assets/item/cloth/prev/{}.png",
    ],
    "title": [
        "https://cnb.cool/SeerAPI/seer-unity-assets/-/git/raw/main/newseer/assets/art/ui/assets/achieve/title/{}.png",
        "https://raw.githubusercontent.com/SeerAPI/seer-unity-assets/refs/heads/main/newseer/assets/art/ui/assets/achieve/title/{}.png",
    ],
}


def _data_uri(raw: bytes, mime: str = "image/png") -> str:
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())


async def _fetch(session: aiohttp.ClientSession, kind: str, arg) -> bytes:
    if arg is None or arg == "":
        return b""
    for tmpl in _SRC.get(kind, []):
        url = tmpl.format(arg)
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    b = await r.read()
                    if len(b) > 100:
                        return b
        except Exception:
            continue
    return b""


def _flip(raw: bytes) -> bytes:
    """立绘水平翻转（对齐手册：官方立绘朝右，手册朝左）。"""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).transpose(Image.FLIP_LEFT_RIGHT)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return raw


async def render_config_card(view: dict, *, viewport_width: int = 1040,
                             timeout_ms: int = 20000) -> bytes:
    """把 config_view() 的结果渲染成 PNG。返回图片字节。

    view 需要由 db.SeerKnowledge.config_view() 产出。
    """
    import jinja2
    from playwright.async_api import async_playwright

    pet = view.get("pet") or {}
    res_id = pet.get("resource_id") or pet.get("id")

    # ① 下载图片
    async with aiohttp.ClientSession() as s:
        head_b = await _fetch(s, "head", res_id)
        body_b = await _fetch(s, "body", res_id)
        pet["head_img"] = _data_uri(head_b) if head_b else ""
        pet["body_img"] = _data_uri(_flip(body_b)) if body_b else ""

        for mm in view.get("mintmarks") or []:
            raw = await _fetch(s, "mint", mm.get("id"))
            mm["icon"] = _data_uri(raw) if raw else ""

        eq = view.get("equipment") or {}
        for blk, kind in ((eq.get("suit"), "suit"), (eq.get("eye"), "eye"),
                          (eq.get("title"), "title")):
            if not blk:
                continue
            raw = await _fetch(s, kind, blk.get("id"))
            blk["icon"] = _data_uri(raw) if raw else ""
            txt = str(blk.get("text") or "")
            blk["short"] = (txt[:60] + "…") if len(txt) > 60 else txt

    # ② 组装上下文
    notes_all = view.get("notes") or []
    ctx = {
        "pet": pet,
        "base": view.get("base") or {},
        "extra": view.get("extra") or {},
        "battle": view.get("battle") or [],
        "mintmarks": view.get("mintmarks") or [],
        "equipment_list": [eq.get("suit") or {}, eq.get("eye") or {}, eq.get("title") or {}],
        "total": view.get("total"),
        "warn": view.get("warn") or [],
        "notes": [n for n in notes_all if not n.startswith("已叠加")],
        "extra_line": next((n for n in notes_all if n.startswith("已叠加")), ""),
        "mascot": _data_uri(open(_MASCOT, "rb").read()) if os.path.isfile(_MASCOT) else "",
    }

    # ③ 渲染 HTML
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_TPL_DIR), autoescape=False
    )
    html = env.get_template(_TPL_FILE).render(**ctx)

    # ④ Playwright 截图
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            bctx = await browser.new_context(
                viewport={"width": viewport_width, "height": 800},
                device_scale_factor=2,
            )
            page = await bctx.new_page()
            await page.set_content(html, wait_until="networkidle", timeout=timeout_ms)
            await page.wait_for_timeout(500)
            el = await page.query_selector(".wrap")
            if el:
                data = await el.screenshot()
            else:
                data = await page.screenshot(full_page=True)
        finally:
            await browser.close()
    return data


async def render_to_tempfile(view: dict, *, viewport_width: int = 1040) -> str:
    """渲染并写到临时文件，返回路径（给 bot 发图用）。"""
    data = await render_config_card(view, viewport_width=viewport_width)
    fd, path = tempfile.mkstemp(prefix="seer_config_", suffix=".png")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(data)
    return path
