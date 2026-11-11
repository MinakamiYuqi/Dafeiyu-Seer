import asyncio
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import jinja2
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from jinja2.sandbox import SandboxedEnvironment
from playwright._impl._errors import TargetClosedError
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from ..service import save_bytes_to_temp_file
from .render_cache import RenderCache, cache_key, source_fingerprint

DEFAULT_TIMEOUT = 30000
DEFAULT_VIEWPORT_WIDTH = 1200

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

# 渲染缓存与并发闸门（借鉴上游 IronsBot 的 FileRenderCache / RenderCoordinator）
_render_cache_enabled = True
_render_cache_max_mb = 300
_render_cache: RenderCache | None = None
_cache_hits = 0
_cache_misses = 0
# 上游用 Semaphore(1) 串行截图；我们再加超时，卡死的页面不会一直占着池子
_render_gate: asyncio.Semaphore | None = None


def _get_gate() -> asyncio.Semaphore:
    global _render_gate
    if _render_gate is None:
        _render_gate = asyncio.Semaphore(1)
    return _render_gate


def _get_cache_dir() -> Path:
    return (
        Path(get_astrbot_data_path())
        / "plugin_data"
        / "astrbot_plugin_seer_info"
        / "render_cache"
    )


def configure_render_cache(enabled: bool = True, max_size_mb: int = 300) -> None:
    """由插件启动时调用；enabled=False 或 max_size_mb=0 即关闭。"""
    global _render_cache_enabled, _render_cache_max_mb, _render_cache
    _render_cache_enabled = bool(enabled) and int(max_size_mb) > 0
    _render_cache_max_mb = max(0, int(max_size_mb))
    _render_cache = None
    logger.info(
        f"渲染缓存：{'开启' if _render_cache_enabled else '关闭'}"
        f"（上限 {_render_cache_max_mb} MB）"
    )


def _cache() -> RenderCache | None:
    global _render_cache
    if not _render_cache_enabled or _render_cache_max_mb <= 0:
        return None
    if _render_cache is None:
        _render_cache = RenderCache(
            _get_cache_dir(), _render_cache_max_mb * 1024 * 1024
        )
    return _render_cache


def render_cache_stats() -> tuple[int, int, int, int]:
    """(命中, 未命中, 文件数, 总字节)"""
    cache = _cache()
    count, total = cache.stats() if cache is not None else (0, 0)
    return _cache_hits, _cache_misses, count, total


def _fingerprint() -> str:
    """模板 + 所有参与「生成渲染数据」的源码。

    只算模板会漏掉 `pet_renderer.py` 这类改动——数据字段变了但模板没变时，
    缓存会继续返回旧图，看起来就像「改了代码没生效」。
    """
    here = Path(__file__).resolve().parent
    sources = [
        _TEMPLATES_DIR,
        here / "renderer.py",
        here / "pet_renderer.py",
        here / "type_matchup_renderer.py",
        here / "analyzer.py",
        here / "soulmark_format.py",
    ]
    return source_fingerprint(sources)


async def _cached_render(
    html_content: str,
    *,
    viewport_width: int,
    timeout_ms: float,
    image_format: str,
    jpeg_quality: int,
) -> bytes:
    """先查缓存，未命中再走（串行 + 带超时）的截图。"""
    global _cache_hits, _cache_misses
    cache = _cache()
    key = ""
    suffix = ".png" if image_format == "png" else ".jpeg"
    if cache is not None:
        key = cache_key(
            html_content,
            image_format,
            jpeg_quality,
            viewport_width,
            _fingerprint(),
        )
        hit = cache.get(key, suffix)
        if hit is not None:
            _cache_hits += 1
            return hit

    _cache_misses += 1
    started = time.monotonic()
    async with _get_gate():
        renderer = await get_renderer()
        data = await asyncio.wait_for(
            renderer.render_string(
                html_content,
                viewport_width=viewport_width,
                timeout_ms=timeout_ms,
                image_format=image_format,
                jpeg_quality=jpeg_quality,
            ),
            timeout=max(timeout_ms / 1000.0, 5.0) + 10.0,
        )
    if cache is not None and key:
        cache.put(key, suffix, data)
    logger.debug(
        f"渲染完成 {len(data) / 1024:.0f} KB，用时 {time.monotonic() - started:.2f}s"
    )
    return data


class TemplateCache:
    _file_cache: OrderedDict[str, str] = OrderedDict()
    _compiled_cache: OrderedDict[str, jinja2.Template] = OrderedDict()
    _MAX_FILE_CACHE = 32
    _MAX_COMPILED_CACHE = 32

    @classmethod
    def get_content(cls, relative_path: str) -> str:
        relative_path = relative_path.replace("/", "\\")
        cached = cls._file_cache.get(relative_path)
        if cached is not None:
            cls._file_cache.move_to_end(relative_path)
            return cached
        full_path = _TEMPLATES_DIR / relative_path
        content = full_path.read_text(encoding="utf-8")
        cls._file_cache[relative_path] = content
        if len(cls._file_cache) > cls._MAX_FILE_CACHE:
            cls._file_cache.popitem(last=False)
        return content

    @classmethod
    def compile_string(cls, env: SandboxedEnvironment, template_string: str) -> jinja2.Template:
        cached = cls._compiled_cache.get(template_string)
        if cached is not None:
            cls._compiled_cache.move_to_end(template_string)
            return cached
        template = env.from_string(template_string)
        cls._compiled_cache[template_string] = template
        if len(cls._compiled_cache) > cls._MAX_COMPILED_CACHE:
            cls._compiled_cache.popitem(last=False)
        return template


class LocalRenderer:
    def __init__(self, page_pool_size: int = 4):
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page_pool: asyncio.Queue[Page] = asyncio.Queue(maxsize=page_pool_size)
        self._page_pool_size = page_pool_size
        self._env = SandboxedEnvironment(
            loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=jinja2.select_autoescape(["html", "xml"]),
            keep_trailing_newline=True,
        )

    async def _get_browser(self) -> Browser:
        if self._browser is None or not self._browser.is_connected():
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--ignore-gpu-blocklist",
                    "--enable-gpu-rasterization",
                    "--enable-zero-copy",
                    "--disable-features=PaintHolding",
                    "--disable-ipc-flooding-protection",
                ],
            )
            logger.info("Playwright 浏览器已启动")
        return self._browser

    async def _get_context(self) -> BrowserContext:
        browser = await self._get_browser()
        if self._context is None or not self._context.browser:
            self._context = await browser.new_context(
                viewport={"width": DEFAULT_VIEWPORT_WIDTH, "height": 600},
                device_scale_factor=2,
                ignore_https_errors=True,
            )
        return self._context

    async def _get_page(self) -> Page:
        try:
            page = self._page_pool.get_nowait()
            if not page.is_closed():
                return page
        except asyncio.QueueEmpty:
            pass
        context = await self._get_context()
        return await context.new_page()

    async def _return_page(self, page: Page) -> None:
        if page.is_closed():
            return
        try:
            self._page_pool.put_nowait(page)
        except asyncio.QueueFull:
            await page.close()

    async def render_template(
        self,
        template_name: str,
        data: dict[str, Any],
        *,
        viewport_width: int = DEFAULT_VIEWPORT_WIDTH,
        timeout_ms: float = DEFAULT_TIMEOUT,
        image_format: str = "jpeg",
        jpeg_quality: int = 85,
    ) -> bytes:
        template = self._env.get_template(template_name)
        html_content = template.render(**data)
        return await self._render_html(
            html_content, viewport_width, timeout_ms, image_format, jpeg_quality
        )

    async def render_string(
        self,
        html_string: str,
        *,
        viewport_width: int = DEFAULT_VIEWPORT_WIDTH,
        timeout_ms: float = DEFAULT_TIMEOUT,
        image_format: str = "jpeg",
        jpeg_quality: int = 85,
    ) -> bytes:
        return await self._render_html(
            html_string, viewport_width, timeout_ms, image_format, jpeg_quality
        )

    async def _render_html(
        self,
        html_content: str,
        viewport_width: int,
        timeout_ms: float,
        image_format: str = "jpeg",
        jpeg_quality: int = 85,
    ) -> bytes:
        page = await self._get_page()
        try:
            return await self._screenshot(
                page, html_content, viewport_width, timeout_ms, image_format, jpeg_quality
            )
        except TargetClosedError:
            page = await self._get_page()
            return await self._screenshot(
                page, html_content, viewport_width, timeout_ms, image_format, jpeg_quality
            )
        except Exception as e:
            logger.error(f"渲染图片失败: {e}")
            raise
        finally:
            if not page.is_closed():
                await self._return_page(page)

    async def _screenshot(
        self,
        page: Page,
        html_content: str,
        viewport_width: int,
        timeout_ms: float,
        image_format: str = "jpeg",
        jpeg_quality: int = 85,
    ) -> bytes:
        await page.set_viewport_size({"width": viewport_width, "height": 600})
        await page.set_content(html_content, wait_until="domcontentloaded", timeout=timeout_ms)
        screenshot_kwargs = {
            "full_page": True,
            "timeout": timeout_ms,
            "animations": "disabled",
            "caret": "hide",
        }
        if image_format == "png":
            screenshot_kwargs["type"] = "png"
        else:
            screenshot_kwargs["type"] = "jpeg"
            screenshot_kwargs["quality"] = jpeg_quality
        return await page.screenshot(**screenshot_kwargs)

    async def close(self):
        while not self._page_pool.empty():
            try:
                page = self._page_pool.get_nowait()
                if not page.is_closed():
                    await page.close()
            except asyncio.QueueEmpty:
                break
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser:
            if self._browser.is_connected():
                await self._browser.close()
            self._browser = None
            logger.info("Playwright 浏览器已关闭")
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def prewarm(self):
        context = await self._get_context()
        for _ in range(self._page_pool_size):
            page = await context.new_page()
            try:
                self._page_pool.put_nowait(page)
            except asyncio.QueueFull:
                await page.close()
                break
        logger.info("Playwright 浏览器已预热")


_renderer: LocalRenderer | None = None
_renderer_lock = asyncio.Lock()


async def get_renderer() -> LocalRenderer:
    global _renderer
    if _renderer is not None:
        return _renderer
    async with _renderer_lock:
        if _renderer is not None:
            return _renderer
        _renderer = LocalRenderer()
        return _renderer


async def close_renderer():
    global _renderer
    if _renderer:
        await _renderer.close()
        _renderer = None


def get_template_content(relative_path: str) -> str:
    return TemplateCache.get_content(relative_path)


async def render_html_to_bytes(
    template_string: str,
    data: dict[str, Any],
    *,
    viewport_width: int = DEFAULT_VIEWPORT_WIDTH,
    timeout_ms: float = DEFAULT_TIMEOUT,
    image_format: str = "jpeg",
    jpeg_quality: int = 85,
) -> bytes:
    renderer = await get_renderer()
    template = TemplateCache.compile_string(renderer._env, template_string)
    html_content = template.render(**data)
    return await _cached_render(
        html_content,
        viewport_width=viewport_width,
        timeout_ms=timeout_ms,
        image_format=image_format,
        jpeg_quality=jpeg_quality,
    )


async def render_template_to_bytes(
    template_name: str,
    data: dict[str, Any],
    *,
    viewport_width: int = DEFAULT_VIEWPORT_WIDTH,
    timeout_ms: float = DEFAULT_TIMEOUT,
    image_format: str = "jpeg",
    jpeg_quality: int = 85,
) -> bytes:
    renderer = await get_renderer()
    template = renderer._env.get_template(template_name)
    html_content = template.render(**data)
    return await _cached_render(
        html_content,
        viewport_width=viewport_width,
        timeout_ms=timeout_ms,
        image_format=image_format,
        jpeg_quality=jpeg_quality,
    )


async def render_to_image(
    template_string: str,
    data: dict[str, Any],
    *,
    html_render=None,
    image_format: str = "jpeg",
    jpeg_quality: int = 85,
) -> str:
    if html_render is not None:
        return await html_render(
            template_string,
            data,
            options={"scale": "device", "type": image_format},
        )
    image_bytes = await render_html_to_bytes(
        template_string,
        data,
        image_format=image_format,
        jpeg_quality=jpeg_quality,
    )
    return save_bytes_to_temp_file(image_bytes)
