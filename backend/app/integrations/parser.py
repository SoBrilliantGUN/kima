import asyncio
import io
import logging
import zipfile
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

import httpx
import mammoth
from markdownify import markdownify as html_to_markdown

from app.integrations.web import (
    FallbackWebFetcher,
    PlaywrightWebFetcher,
    TrafilaturaWebFetcher,
    WebFetcher,
    get_browser,
)

logger = logging.getLogger(__name__)


class SourceType(StrEnum):
    PDF = "pdf"
    URL = "url"
    WORD = "word"


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


class ParserError(Exception):
    """文档解析失败（由 worker 判重试）。"""


# --- MinerU（PDF，托管 API v4） ---

MINERU_UPLOAD_PATH = "/api/v4/file-urls/batch"
MINERU_RESULT_PATH = "/api/v4/extract-results/batch/{batch_id}"
MINERU_POLL_INTERVAL = 2.0
MINERU_POLL_TIMEOUT = 300.0
MINERU_DONE_STATE = "done"
MINERU_FAILED_STATE = "failed"
MINERU_MODEL_VERSION = "pipeline"  # 官方默认；vlm 效果更好（需自行确认计费）


class MinerUDocumentParser:
    """MinerU 官方云 API v4（申请上传链接 → PUT 上传 → 轮询 → 下载结果 zip → markdown）。

    流程：
      ① POST /api/v4/file-urls/batch          → batch_id + file_urls（预签名上传地址）
      ② PUT 文件字节到 file_urls
      ③ GET  /api/v4/extract-results/batch/{id} → extract_result[].state / full_zip_url
      ④ 下载 full_zip_url（zip），解出 markdown
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or "https://mineru.net").rstrip("/")
        self._token = token
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        # transport 是注入缝：默认 None 走真实网络，测试时换 httpx.MockTransport 离线验证
        return httpx.AsyncClient(timeout=60, transport=self._transport)

    async def parse(self, *, content: bytes, filename: str | None = None) -> ParsedDocument:
        if not self._token:
            raise ParserError("未配置 MINERU_API_TOKEN")

        batch_id = await self._apply_for_upload(content, filename)
        zip_url = await self._poll(batch_id)
        markdown = await self._download_markdown(zip_url)
        if not markdown:
            raise ParserError("MinerU 结果 zip 中无 markdown")
        return ParsedDocument(markdown=markdown, metadata={})

    async def _apply_for_upload(self, content: bytes, filename: str | None) -> str:
        headers = {"Authorization": f"Bearer {self._token}"}
        name = filename or "document.pdf"  # 原始文件名透传给 MinerU，便于云端对号入座
        payload = {"files": [{"name": name}], "model_version": MINERU_MODEL_VERSION}
        try:
            async with self._client() as client:
                response = await client.post(
                    f"{self._base_url}{MINERU_UPLOAD_PATH}", json=payload, headers=headers
                )
                response.raise_for_status()
            data = response.json()
            batch_id, file_urls = self._extract_upload_info(data)
            if not batch_id or not file_urls:
                raise ParserError("MinerU 未返回 batch_id / 上传地址")
            async with self._client() as client:
                upload = await client.put(file_urls[0], content=content)
                upload.raise_for_status()
        except httpx.HTTPError as exc:
            raise ParserError("MinerU 上传失败") from exc
        return batch_id

    async def _poll(self, batch_id: str) -> str:
        headers = {"Authorization": f"Bearer {self._token}"}
        deadline = asyncio.get_event_loop().time() + MINERU_POLL_TIMEOUT
        async with self._client() as client:
            while True:
                try:
                    response = await client.get(
                        f"{self._base_url}{MINERU_RESULT_PATH.format(batch_id=batch_id)}",
                        headers=headers,
                    )
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise ParserError("MinerU 轮询失败") from exc
                state, zip_url = self._extract_result_state(response.json())
                if state == MINERU_DONE_STATE and zip_url:
                    return zip_url
                if state == MINERU_FAILED_STATE:
                    raise ParserError(f"MinerU 解析失败（state={state}）")
                if asyncio.get_event_loop().time() > deadline:
                    raise ParserError("MinerU 解析超时")
                await asyncio.sleep(MINERU_POLL_INTERVAL)

    async def _download_markdown(self, zip_url: str) -> str:
        try:
            async with self._client() as client:
                response = await client.get(zip_url)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ParserError("MinerU 结果下载失败") from exc
        return self._extract_markdown_from_zip(response.content)

    @staticmethod
    def _extract_upload_info(data: object) -> tuple[str, list[str]]:
        inner = data.get("data") if isinstance(data, dict) else None
        batch_id = ""
        file_urls: list[str] = []
        if isinstance(inner, dict):
            if isinstance(inner.get("batch_id"), str):
                batch_id = inner["batch_id"]
            urls = inner.get("file_urls")
            if isinstance(urls, list):
                file_urls = [str(u) for u in urls if u]
        return batch_id, file_urls

    @staticmethod
    def _extract_result_state(data: object) -> tuple[str, str]:
        inner = data.get("data") if isinstance(data, dict) else None
        results = inner.get("extract_result") if isinstance(inner, dict) else None
        if isinstance(results, list) and results and isinstance(results[0], dict):
            first = results[0]
            state = str(first.get("state") or "").lower()
            zip_url = str(first.get("full_zip_url") or "")
            return state, zip_url
        return "", ""

    @staticmethod
    def _extract_markdown_from_zip(zip_bytes: bytes) -> str:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            md_names = [n for n in zf.namelist() if n.lower().endswith(".md")]
            if not md_names:
                return ""
            # 正文 md 通常在 zip 根目录，取路径最浅的那个
            md_names.sort(key=lambda n: (n.count("/"), n))
            return zf.read(md_names[0]).decode("utf-8", errors="replace")


# --- Word（本地 mammoth） ---


class WordDocumentParser:
    """`.docx` → markdown。mammoth 的 markdown 输出不支持表格，故先转 HTML 再经
    markdownify 转回 markdown，把 `<table>` 保留成 GFM 表格。CPU 同步转换用 to_thread 包一层。"""

    async def parse(self, *, content: bytes) -> ParsedDocument:
        def _convert() -> str:
            with io.BytesIO(content) as buffer:
                html = mammoth.convert_to_html(buffer).value
            return html_to_markdown(html)

        markdown = await asyncio.to_thread(_convert)
        return ParsedDocument(
            markdown=markdown,
            metadata={
                "mime_type": (
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                )
            },
        )


# --- URL（复用 WebFetcher） ---


class WebDocumentParser:
    """URL → markdown，复用模块 3 的 WebFetcher（静态 trafilatura + Playwright 回退）。"""

    def __init__(self, fetcher: WebFetcher | None = None) -> None:
        self._fetcher = fetcher

    async def parse(self, *, url: str) -> ParsedDocument:
        page = await self._build_fetcher().fetch(url)
        return ParsedDocument(
            markdown=page.markdown,
            title=page.title,
            metadata={"source_url": page.source_url or url},
        )

    def _build_fetcher(self) -> WebFetcher:
        if self._fetcher is not None:
            return self._fetcher
        browser = get_browser()
        spa = PlaywrightWebFetcher(browser) if browser is not None else None
        return FallbackWebFetcher(TrafilaturaWebFetcher(), spa)


# --- 分发工厂 ---


class DispatchDocumentParser:
    """按 source_type 路由到 pdf/word/web 三个窄解析器；对外统一收 source_type + content/url。"""

    def __init__(
        self,
        *,
        pdf: PdfParser,
        word: WordParser,
        web: UrlParser,
    ) -> None:
        self._pdf = pdf
        self._word = word
        self._web = web

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
        raise ParserError(f"不支持的来源类型: {source_type}")
