"""文档解析对外入口：按 source_type 分发到 pdf/word/web 三个窄解析器。

具体解析器实现见 ``parser_mineru.py`` / ``parser_word.py`` / ``parser_web.py``。
本模块保留对外协议与数据类型 + 分发工厂，并把三个具体解析器重导出（
``integrations/__init__.py`` 从本模块 import 它们）。
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class SourceType(StrEnum):
    PDF = "pdf"
    URL = "url"
    WORD = "word"
    MARKDOWN = "markdown"


@dataclass(frozen=True)
class ParsedDocument:
    markdown: str
    title: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class DocumentParser(Protocol):
    """对外统一入口（分发边界）：按 source_type 路由，收 content / url 之一。

    filename 为可选原始文件名（含扩展名），仅 PDF 使用（透传给 MinerU 上传 name）。
    """

    async def parse(
        self,
        *,
        source_type: SourceType,
        content: bytes | None = None,
        url: str | None = None,
        filename: str | None = None,
    ) -> ParsedDocument: ...


class PdfParser(Protocol):
    """PDF 解析器：收 bytes + 可选原始文件名（MinerU 上传用 name）出 markdown。"""

    async def parse(self, *, content: bytes, filename: str | None = None) -> ParsedDocument: ...


class WordParser(Protocol):
    """Word 解析器：收 bytes 出 markdown（mammoth 无需文件名）。"""

    async def parse(self, *, content: bytes) -> ParsedDocument: ...


class UrlParser(Protocol):
    """URL 类解析器：收 url 出 markdown。"""

    async def parse(self, *, url: str) -> ParsedDocument: ...


class MarkdownParser(Protocol):
    """Markdown 解析器：收 bytes 出 markdown（内容本身即 markdown，只做编码解码）。"""

    async def parse(self, *, content: bytes) -> ParsedDocument: ...


class ParserError(Exception):
    """文档解析失败（由 worker 判重试）。"""


class DispatchDocumentParser:
    """按 source_type 路由到 pdf/word/web/markdown 四个窄解析器。

    对外统一收 source_type + content/url；markdown 仅做字节解码。
    """

    def __init__(
        self,
        *,
        pdf: PdfParser,
        word: WordParser,
        web: UrlParser,
        markdown: MarkdownParser,
    ) -> None:
        self._pdf = pdf
        self._word = word
        self._web = web
        self._markdown = markdown

    async def parse(
        self,
        *,
        source_type: SourceType,
        content: bytes | None = None,
        url: str | None = None,
        filename: str | None = None,
    ) -> ParsedDocument:
        if source_type == SourceType.PDF:
            if content is None:
                raise ParserError("缺少 PDF 文件内容")
            return await self._pdf.parse(content=content, filename=filename)
        if source_type == SourceType.WORD:
            if content is None:
                raise ParserError("缺少 Word 文件内容")
            return await self._word.parse(content=content)
        if source_type == SourceType.URL:
            if url is None:
                raise ParserError("缺少 URL")
            return await self._web.parse(url=url)
        if source_type == SourceType.MARKDOWN:
            if content is None:
                raise ParserError("缺少 Markdown 文件内容")
            return await self._markdown.parse(content=content)
        raise ParserError(f"不支持的来源类型: {source_type}")


# 重导出：放在模块末尾——四个具体解析器依赖上方的 ``ParsedDocument``/``ParserError``，
# 若在顶部 import 会与它们形成 import 环（partial initialization）。
from app.integrations.parser_markdown import MarkdownDocumentParser  # noqa: E402
from app.integrations.parser_mineru import MinerUDocumentParser  # noqa: E402
from app.integrations.parser_web import WebDocumentParser  # noqa: E402
from app.integrations.parser_word import WordDocumentParser  # noqa: E402

__all__ = [
    "SourceType",
    "ParsedDocument",
    "DocumentParser",
    "PdfParser",
    "WordParser",
    "UrlParser",
    "MarkdownParser",
    "ParserError",
    "DispatchDocumentParser",
    "MinerUDocumentParser",
    "WordDocumentParser",
    "WebDocumentParser",
    "MarkdownDocumentParser",
]
