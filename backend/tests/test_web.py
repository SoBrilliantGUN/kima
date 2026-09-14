from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.core.exceptions import FetchError
from app.integrations.web import (
    GO_TO_TIMEOUT_MS,
    EmptyContentError,
    FallbackWebFetcher,
    FetchedPage,
    PlaywrightWebFetcher,
)


def _fake_extract_markdown(html: bytes) -> str:
    return "# 渲染正文"


def _fake_extract_title(html: bytes) -> str | None:
    return "渲染标题"


def _none_extract_title(html: bytes) -> str | None:
    return None


class _Fetcher:
    """可控假抓取器：正常返回 / 抛 EmptyContentError / 抛 FetchError。"""

    def __init__(
        self,
        page: FetchedPage | None = None,
        *,
        empty: bool = False,
        fail: bool = False,
    ) -> None:
        self._page = page or FetchedPage(markdown="# 正文")
        self._empty = empty
        self._fail = fail
        self.calls = 0

    async def fetch(self, url: str) -> FetchedPage:
        self.calls += 1
        if self._fail:
            raise FetchError("无法抓取该网页")
        if self._empty:
            raise EmptyContentError("网页无正文内容")
        return self._page


async def test_fallback_returns_static_when_content_present() -> None:
    static = _Fetcher(FetchedPage(markdown="# 静态", title="静态标题"))
    spa = _Fetcher(FetchedPage(markdown="# SPA"))
    fetcher = FallbackWebFetcher(static, spa)
    result = await fetcher.fetch("https://example.com")
    assert result.title == "静态标题"
    assert spa.calls == 0


async def test_fallback_uses_spa_when_static_empty() -> None:
    static = _Fetcher(empty=True)
    spa = _Fetcher(FetchedPage(markdown="# SPA", title="SPA标题"))
    fetcher = FallbackWebFetcher(static, spa)
    result = await fetcher.fetch("https://example.com")
    assert result.title == "SPA标题"
    assert spa.calls == 1


async def test_fallback_does_not_use_spa_on_http_error() -> None:
    static = _Fetcher(fail=True)
    spa = _Fetcher()
    fetcher = FallbackWebFetcher(static, spa)
    with pytest.raises(FetchError):
        await fetcher.fetch("https://example.com")
    assert spa.calls == 0


async def test_fallback_raises_when_spa_unavailable() -> None:
    fetcher = FallbackWebFetcher(_Fetcher(empty=True), None)
    with pytest.raises(FetchError):
        await fetcher.fetch("https://example.com")


async def test_playwright_fetcher_renders_and_extracts(monkeypatch: pytest.MonkeyPatch) -> None:
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.content = AsyncMock(return_value="<html><body><p>x</p></body></html>")
    page.url = "https://example.com"
    page.close = AsyncMock()

    browser = MagicMock()
    browser.new_page = AsyncMock(return_value=page)

    monkeypatch.setattr("app.integrations.web.extract_markdown", _fake_extract_markdown)
    monkeypatch.setattr("app.integrations.web.extract_title", _fake_extract_title)

    fetcher = PlaywrightWebFetcher(browser)
    result = await fetcher.fetch("https://example.com")

    assert result.markdown == "# 渲染正文"
    assert result.title == "渲染标题"
    assert result.source_url is None  # 最终 URL 未变
    page.goto.assert_awaited_once_with(
        "https://example.com", wait_until="domcontentloaded", timeout=GO_TO_TIMEOUT_MS
    )
    page.close.assert_awaited_once()


async def test_playwright_fetcher_tolerates_networkidle_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock(side_effect=PlaywrightTimeoutError("timeout"))
    page.wait_for_timeout = AsyncMock()
    page.content = AsyncMock(return_value="<html></html>")
    page.url = "https://example.com"
    page.close = AsyncMock()

    browser = MagicMock()
    browser.new_page = AsyncMock(return_value=page)

    monkeypatch.setattr("app.integrations.web.extract_markdown", _fake_extract_markdown)
    monkeypatch.setattr("app.integrations.web.extract_title", _none_extract_title)

    fetcher = PlaywrightWebFetcher(browser)
    result = await fetcher.fetch("https://example.com")
    assert result.markdown == "# 渲染正文"
    page.close.assert_awaited_once()
