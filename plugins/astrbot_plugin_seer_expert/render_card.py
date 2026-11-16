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

# 本地图标根目录（plugin_data/astrbot_plugin_seer_info/icon_assets）
# ★ 2026-11-16 鱼片指出：插件不该塞固定实例路径，一律走环境/框架给的目录。
def _icon_dirs() -> tuple[str, ...]:
    """按优先级给出候选图标根目录，**不含任何硬编码实例路径**。

    ① 环境变量 SEER_ICON_DIR —— 测试 / 手动指定
    ② AstrBot 的 plugin_data（和 expert 自己拿 data_dir 用的是同一个 API）
    ③ 插件目录上溯到 plugin_data（工作目录不同时的兜底）
    """
    out: list[str] = []
    env = os.environ.get("SEER_ICON_DIR", "").strip()
    if env:
        out.append(env)
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        out.append(os.path.join(get_astrbot_plugin_data_path(),
                                "astrbot_plugin_seer_info", "icon_assets"))
    except Exception:  # noqa: BLE001
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    out.append(os.path.join(here, "..", "..", "plugin_data",
                            "astrbot_plugin_seer_info", "icon_assets"))
    return tuple(dict.fromkeys(out))  # 去重保序


_ICON_DIRS: tuple[str, ...] = _icon_dirs()


def set_icon_root(root: str) -> None:
    """由插件在启动时注入真实的 icon_assets 目录（从 seerapi.sqlite 的位置反推）。

    不注入也不会崩，只是本地缓存命中率低（会退到联网取图）。
    """
    global _ICON_DIRS
    r = str(root or "").strip()
    if r:
        _ICON_DIRS = (r,) + tuple(d for d in _ICON_DIRS if d != r)

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
    # 2026-11-16：宝石图标（countermark/gem/{gem.id}.png），本地已存 245 张
    "gem": [
        "https://raw.githubusercontent.com/SeerAPI/seer-unity-assets/main/newseer/assets/art/ui/assets/countermark/gem/{}.png",
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


# 一个 kind 可能落在多个子目录里，按顺序试，先命中的赢。
#   element：高清组（element_hd，250~320px）在前，40×40 小图兜底。
#   ★ 2026-11-16 补：原来只拼 <kind>/<id>.png，属性图标永远取到 element/ 的小图。
_ICON_ALIASES: dict[str, tuple[str, ...]] = {
    "element": ("element_hd", "element"),
}


def _local_icon(kind: str, iid) -> bytes:
    """本地图标缓存 icon_assets/<子目录>/<id>.png；没有才走网络。

    2026-11-16：宝石图标 245 张已全部下到 icon_assets/gem/（按 gem.id 命名），
    断网也能出图，_SRC 里那条只是兜底。
    """
    if iid is None or iid == "":
        return b""
    subs = _ICON_ALIASES.get(kind, (kind,))
    for root in _ICON_DIRS:
        for sub in subs:
            fp = os.path.join(root, sub, f"{iid}.png")
            try:
                if os.path.exists(fp) and os.path.getsize(fp) > 200:
                    with open(fp, "rb") as f:
                        return f.read()
            except OSError:
                continue
    return b""


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
            # 刻印上绑的宝石：本地优先（icon_assets/gem 共 245 张），CDN 兜底
            g = mm.get("gem")
            if g and g.get("id"):
                graw = _local_icon("gem", g["id"]) or await _fetch(s, "gem", g["id"])
                g["icon"] = _data_uri(graw) if graw else ""

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
