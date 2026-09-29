import asyncio
from dataclasses import dataclass
from typing import Protocol, cast

import httpx
import trafilatura
from playwright.async_api import (
    Browser,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from app.core.exceptions import FetchError

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
MAX_BODY_BYTES = 5 * 1024 * 1024

# Playwright 渲染参数（毫秒）
GO_TO_TIMEOUT_MS = 30_000
NETWORK_IDLE_TIMEOUT_MS = 5_000
RENDER_SETTLE_MS = 1_000


@dataclass(frozen=True)
class FetchedPage:
    markdown: str
    title: str | None = None
    source_url: str | None = None


class WebFetcher(Protocol):
    async def fetch(self, url: str) -> FetchedPage: ...


class EmptyContentError(FetchError):
    """静态抓取成功但正文为空（典型 SPA 空壳），作为回退到浏览器渲染的信号。"""


def extract_markdown(html: bytes) -> str:
    # with_metadata=False：正文只留正文，元信息（title/author/…）由 extract_title 单独取，
    # 避免 trafilatura 把 YAML 元数据块塞进 markdown 头部，污染正文展示与 RAG 切块。
    extracted = trafilatura.extract(
        html,
        output_format="markdown",
        include_images=True,
        with_metadata=False,
    )
    return extracted or ""


def extract_title(html: bytes) -> str | None:
    # trafilatura 的 extract_metadata 类型标注漏标了 bytes（运行时支持），此处显式 cast
    metadata = trafilatura.extract_metadata(cast(str, html))
    return metadata.title if metadata is not None else None


class TrafilaturaWebFetcher:
    """httpx 抓取网页，trafilatura 提取 Markdown 正文与标题。

    提取属 CPU 密集的同步操作，用 `asyncio.to_thread` 包一层避免阻塞事件循环。
    """

    async def fetch(self, url: str) -> FetchedPage:
        try:
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                html = response.content
                final_url = str(response.url)
        except httpx.HTTPError as exc:
            raise FetchError("无法抓取该网页") from exc

        html = html[:MAX_BODY_BYTES]
        markdown = await asyncio.to_thread(extract_markdown, html)
        if not markdown:
            raise EmptyContentError("网页无正文内容")
        title = await asyncio.to_thread(extract_title, html)
        return FetchedPage(
            markdown=markdown,
            title=title,
            source_url=final_url if final_url != url else None,
        )


class PlaywrightWebFetcher:
    """Playwright 无头浏览器渲染 SPA，再交给 trafilatura 提取正文与标题。

    先等 `domcontentloaded` 拿到初始 HTML，再等 `networkidle`（最多 5s，超时继续）
    让 JS 发起的异步数据请求完成，最后多等 1s 让框架完成 DOM 渲染。
    """

    def __init__(self, browser: Browser) -> None:
        self._browser = browser

    async def fetch(self, url: str) -> FetchedPage:
        page = await self._browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=GO_TO_TIMEOUT_MS)
            try:
                await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                pass
            await page.wait_for_timeout(RENDER_SETTLE_MS)
            html = await page.content()
            final_url = page.url
        finally:
            await page.close()

        html_bytes = html.encode("utf-8")[:MAX_BODY_BYTES]
        markdown = await asyncio.to_thread(extract_markdown, html_bytes)
        if not markdown:
            raise FetchError("无法抓取该网页")
        title = await asyncio.to_thread(extract_title, html_bytes)
        return FetchedPage(
            markdown=markdown,
            title=title,
            source_url=final_url if final_url != url else None,
        )


class FallbackWebFetcher:
    """先静态抓取；正文为空（SPA 空壳）时回退到 Playwright 浏览器渲染。

    仅对 `EmptyContentError` 回退——HTTP 错误（如 404）说明页面本身不可达，
    用浏览器渲染也无法救回，直接抛出。SPA 渲染器恒在场（非 None）：如今网页大多是 SPA，
    playwright 是必须能力，不再允许「无 SPA 回退」的静默降级。
    """

    def __init__(self, static: WebFetcher, spa: WebFetcher) -> None:
        self._static = static
        self._spa = spa

    async def fetch(self, url: str) -> FetchedPage:
        try:
            return await self._static.fetch(url)
        except EmptyContentError:
            return await self._spa.fetch(url)


class FakeWebFetcher:
    """测试替身：返回固定页面，可配置抛 FetchError。"""

    def __init__(self, page: FetchedPage | None = None, *, fail: bool = False) -> None:
        self._page = page or FetchedPage(
            markdown="# 示例正文\n\n这是用于测试的网页正文。",
            title="示例标题",
            source_url=None,
        )
        self._fail = fail

    async def fetch(self, url: str) -> FetchedPage:
        if self._fail:
            raise FetchError("无法抓取该网页")
        return self._page


# --- 共享浏览器生命周期（由应用 lifespan 驱动） ---

_playwright: Playwright | None = None
_browser: Browser | None = None


async def start_browser() -> Browser:
    """启动共享的无头浏览器；失败抛异常拒绝启动（playwright 必须在场，不静默降级）。"""
    global _playwright, _browser
    if _browser is not None:
        return _browser
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(headless=True)
    return _browser


async def stop_browser() -> None:
    """关闭共享浏览器与 Playwright 驱动。"""
    global _playwright, _browser
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright is not None:
        await _playwright.stop()
        _playwright = None


def get_browser() -> Browser:
    """返回已启动的共享浏览器；未启动即报错（playwright 必须在场）。"""
    if _browser is None:
        raise RuntimeError("Playwright 浏览器未启动（必须在场，先经 lifespan start_browser）")
    return _browser
