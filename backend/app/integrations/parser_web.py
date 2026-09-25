"""URL → markdown 解析器（复用 WebFetcher：静态 trafilatura + Playwright 回退）。"""

import re

from app.integrations.parser import ParsedDocument
from app.integrations.web import (
    FallbackWebFetcher,
    PlaywrightWebFetcher,
    TrafilaturaWebFetcher,
    WebFetcher,
    get_browser,
)

# 网页 `<title>` 常带「标题 - 站点名」等后缀，而正文 `<h1>` 是纯标题。
# 二者语义相同时剥掉正文首行 heading，避免阅读弹窗里标题（header + 正文）出现两次。
_TITLE_SEPARATORS = (" - ", " – ", " — ", " | ", " · ", "｜")


def _normalize_title(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _titles_match(heading: str, title: str) -> bool:
    """判断正文首行 heading 与 `<title>` 是否为同一标题（含「标题 + 站点后缀」）。"""
    head = _normalize_title(heading)
    title_norm = _normalize_title(title)
    if not head or not title_norm:
        return False
    if head == title_norm:
        return True
    return any(
        title_norm.startswith(head + sep) or head.startswith(title_norm + sep)
        for sep in _TITLE_SEPARATORS
    )


def _strip_duplicate_heading(markdown: str, title: str | None) -> str:
    """若正文首个 `#` 标题与 `<title>` 语义相同，则删除该行及其后的空行。"""
    if not title:
        return markdown
    lines = markdown.splitlines()
    # 跳过前导空行，定位第一个非空行
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        stripped = line.lstrip()
        if not stripped.startswith("# ") or not _titles_match(stripped[2:].strip(), title):
            break
        rest = lines[index + 1 :]
        while rest and not rest[0].strip():
            rest = rest[1:]
        return "\n".join(rest)
    return markdown


class WebDocumentParser:
    """URL → markdown，复用模块 3 的 WebFetcher（静态 trafilatura + Playwright 回退）。"""

    def __init__(self, fetcher: WebFetcher | None = None) -> None:
        self._fetcher = fetcher

    async def parse(self, *, url: str) -> ParsedDocument:
        page = await self._build_fetcher().fetch(url)
        return ParsedDocument(
            markdown=_strip_duplicate_heading(page.markdown, page.title),
            title=page.title,
            metadata={"source_url": page.source_url or url},
        )

    def _build_fetcher(self) -> WebFetcher:
        if self._fetcher is not None:
            return self._fetcher
        browser = get_browser()
        spa = PlaywrightWebFetcher(browser) if browser is not None else None
        return FallbackWebFetcher(TrafilaturaWebFetcher(), spa)
