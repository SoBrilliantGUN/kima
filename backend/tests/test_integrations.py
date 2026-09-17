import io
import json
import zipfile
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings
from app.integrations import get_document_parser, get_embedding_client, get_llm_client
from app.integrations.llm import ChatMessage
from app.integrations.parser import (
    DispatchDocumentParser,
    MinerUDocumentParser,
    SourceType,
    WebDocumentParser,
    WordDocumentParser,
)
from app.integrations.web import FakeWebFetcher
from tests.fakes import FakeFileParser, FakeUrlParser


async def test_fake_llm_echoes_last_user_message() -> None:
    client = get_llm_client(Settings(llm_provider="fake"))
    result = await client.chat(
        [
            ChatMessage(role="user", content="第一句"),
            ChatMessage(role="assistant", content="忽略我"),
            ChatMessage(role="user", content="最后一句"),
        ]
    )
    assert result.content == "[fake] 最后一句"


async def test_fake_embedding_dimension_and_determinism() -> None:
    client = get_embedding_client(Settings(embedding_provider="fake", embedding_dim=1024))
    assert client.dimension == 1024

    vectors = await client.embed_documents(["你好", "世界"])
    assert len(vectors) == 2
    assert all(len(vector) == 1024 for vector in vectors)

    assert await client.embed_query("你好") == vectors[0]


async def test_siliconflow_embedding_factory_constructs() -> None:
    client = get_embedding_client(
        Settings(embedding_provider="siliconflow", embedding_dim=1024)
    )
    assert client.dimension == 1024


async def test_document_parser_factory_returns_dispatch() -> None:
    parser = get_document_parser(Settings())
    assert isinstance(parser, DispatchDocumentParser)


async def test_dispatch_routes_by_source_type() -> None:
    pdf = FakeFileParser("pdf")
    word = FakeFileParser("word")
    web = FakeUrlParser("web")
    dispatch = DispatchDocumentParser(pdf=pdf, word=word, web=web)

    await dispatch.parse(source_type=SourceType.PDF, content=b"x")
    await dispatch.parse(source_type=SourceType.WORD, content=b"x")
    await dispatch.parse(source_type=SourceType.URL, url="https://example.com")

    assert pdf.calls == ["pdf"]
    assert word.calls == ["word"]
    assert web.calls == ["web"]


async def test_web_parser_uses_fetcher() -> None:
    parser = WebDocumentParser(FakeWebFetcher())
    result = await parser.parse(url="https://example.com")
    assert result.title == "示例标题"
    assert result.markdown.startswith("#")


async def test_word_parser_preserves_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    html = (
        "<table><thead><tr><th>名称</th><th>说明</th></tr></thead>"
        "<tbody><tr><td>RAG</td><td>检索增强</td></tr></tbody></table>"
    )
    monkeypatch.setattr(
        "app.integrations.parser.mammoth.convert_to_html",
        lambda _: SimpleNamespace(value=html),
    )

    result = await WordDocumentParser().parse(content=b"fake docx")

    assert "| 名称 | 说明 |" in result.markdown
    assert "| RAG | 检索增强 |" in result.markdown


async def test_mineru_parser_uploads_polls_and_extracts_markdown() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("full.md", "# 解析正文")
    zip_bytes = buffer.getvalue()

    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url}")
        if request.url.path == "/api/v4/file-urls/batch":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "batch_id": "b1",
                        "file_urls": ["https://upload.example/document.pdf"],
                    },
                },
            )
        if request.url.host == "upload.example":
            return httpx.Response(200)
        if request.url.path == "/api/v4/extract-results/batch/b1":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {
                                "file_name": "document.pdf",
                                "state": "done",
                                "full_zip_url": "https://result.example/result.zip",
                            }
                        ]
                    },
                },
            )
        if request.url.host == "result.example":
            return httpx.Response(200, content=zip_bytes)
        return httpx.Response(404)

    parser = MinerUDocumentParser(
        base_url="https://mineru.net",
        token="tok",
        transport=httpx.MockTransport(handler),
    )
    result = await parser.parse(content=b"%PDF-1.4")

    assert result.markdown == "# 解析正文"
    assert calls == [
        "POST https://mineru.net/api/v4/file-urls/batch",
        "PUT https://upload.example/document.pdf",
        "GET https://mineru.net/api/v4/extract-results/batch/b1",
        "GET https://result.example/result.zip",
    ]


async def test_mineru_parser_uses_filename_in_upload_payload() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("full.md", "# 正文")
    zip_bytes = buffer.getvalue()

    uploaded_names: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v4/file-urls/batch":
            payload = json.loads(request.content)
            uploaded_names.extend(f["name"] for f in payload["files"])
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "batch_id": "b1",
                        "file_urls": ["https://upload.example/f.pdf"],
                    },
                },
            )
        if request.url.host == "upload.example":
            return httpx.Response(200)
        if request.url.path == "/api/v4/extract-results/batch/b1":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {
                                "state": "done",
                                "full_zip_url": "https://result.example/result.zip",
                            }
                        ]
                    },
                },
            )
        if request.url.host == "result.example":
            return httpx.Response(200, content=zip_bytes)
        return httpx.Response(404)

    parser = MinerUDocumentParser(
        base_url="https://mineru.net",
        token="tok",
        transport=httpx.MockTransport(handler),
    )
    await parser.parse(content=b"%PDF-1.4", filename="我的报告.pdf")

    assert uploaded_names == ["我的报告.pdf"]
