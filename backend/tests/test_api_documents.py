"""文档 API 集成测试：6 端点 + contents 含 document 条目。"""

import uuid
from collections.abc import AsyncIterator
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_document_service, get_note_service
from app.main import app
from app.models.document import DocumentStatus
from app.models.knowledge_base import KnowledgeBase
from app.services.document import DocumentService
from app.services.note import NoteService
from tests.fakes import (
    FakeDocumentRepository,
    FakeFileStore,
    FakeKnowledgeBaseRepository,
    FakeNoteRepository,
)


@pytest.fixture
def doc_repo() -> FakeDocumentRepository:
    return FakeDocumentRepository()


@pytest.fixture
def kb_repo() -> FakeKnowledgeBaseRepository:
    return FakeKnowledgeBaseRepository()


@pytest.fixture
def note_repo() -> FakeNoteRepository:
    return FakeNoteRepository()


@pytest.fixture
def file_store() -> FakeFileStore:
    return FakeFileStore()


@pytest.fixture
async def api_client(
    doc_repo: FakeDocumentRepository,
    kb_repo: FakeKnowledgeBaseRepository,
    note_repo: FakeNoteRepository,
    file_store: FakeFileStore,
) -> AsyncIterator[AsyncClient]:
    doc_service = DocumentService(doc_repo, kb_repo, file_store)
    note_service = NoteService(note_repo, kb_repo)
    app.dependency_overrides[get_document_service] = lambda: doc_service
    app.dependency_overrides[get_note_service] = lambda: note_service
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def _make_kb(kb_repo: FakeKnowledgeBaseRepository) -> KnowledgeBase:
    return await kb_repo.add(KnowledgeBase(name="工作"))


async def test_upload_pdf(api_client: AsyncClient, kb_repo: FakeKnowledgeBaseRepository) -> None:
    kb = await _make_kb(kb_repo)
    response = await api_client.post(
        "/api/documents",
        files={"file": ("报告.pdf", b"%PDF-1.4", "application/pdf")},
        data={"kb_id": str(kb.id)},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    assert body["source_type"] == "pdf"
    assert body["title"] == "报告"


async def test_upload_rejects_unsupported(
    api_client: AsyncClient, kb_repo: FakeKnowledgeBaseRepository,
) -> None:
    kb = await _make_kb(kb_repo)
    response = await api_client.post(
        "/api/documents",
        files={"file": ("笔记.doc", b"x", "application/msword")},
        data={"kb_id": str(kb.id)},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "validation_error"


async def test_upload_missing_kb_404(api_client: AsyncClient) -> None:
    response = await api_client.post(
        "/api/documents",
        files={"file": ("a.pdf", b"x", "application/pdf")},
        data={"kb_id": str(uuid.uuid4())},
    )
    assert response.status_code == 404


async def test_create_from_url(
    api_client: AsyncClient, kb_repo: FakeKnowledgeBaseRepository,
) -> None:
    kb = await _make_kb(kb_repo)
    response = await api_client.post(
        "/api/documents/from-url",
        json={"url": "https://example.com", "knowledge_base_id": str(kb.id)},
    )
    assert response.status_code == 201
    assert response.json()["source_type"] == "url"


async def test_create_from_url_invalid_422(
    api_client: AsyncClient, kb_repo: FakeKnowledgeBaseRepository,
) -> None:
    kb = await _make_kb(kb_repo)
    response = await api_client.post(
        "/api/documents/from-url",
        json={"url": "not-a-url", "knowledge_base_id": str(kb.id)},
    )
    assert response.status_code == 422


async def test_get_document(api_client: AsyncClient, kb_repo: FakeKnowledgeBaseRepository) -> None:
    kb = await _make_kb(kb_repo)
    created = (
        await api_client.post(
            "/api/documents",
            files={"file": ("a.pdf", b"x", "application/pdf")},
            data={"kb_id": str(kb.id)},
        )
    ).json()

    response = await api_client.get(f"/api/documents/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


async def test_get_missing_404(api_client: AsyncClient) -> None:
    response = await api_client.get(f"/api/documents/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_get_file_pdf(api_client: AsyncClient, kb_repo: FakeKnowledgeBaseRepository) -> None:
    kb = await _make_kb(kb_repo)
    created = (
        await api_client.post(
            "/api/documents",
            files={"file": ("报告.pdf", b"%PDF-1.4", "application/pdf")},
            data={"kb_id": str(kb.id)},
        )
    ).json()

    response = await api_client.get(f"/api/documents/{created['id']}/file")
    assert response.status_code == 200
    assert response.content == b"%PDF-1.4"
    disposition = response.headers["content-disposition"]
    assert "inline" in disposition
    # 文件名用原始标题（而非写死的 document.pdf），非 ASCII 走 RFC 5987 编码
    assert quote("报告.pdf", safe="") in disposition
    assert "document.pdf" not in disposition


async def test_get_file_url_409(
    api_client: AsyncClient, kb_repo: FakeKnowledgeBaseRepository,
) -> None:
    kb = await _make_kb(kb_repo)
    created = (
        await api_client.post(
            "/api/documents/from-url",
            json={"url": "https://example.com", "knowledge_base_id": str(kb.id)},
        )
    ).json()

    response = await api_client.get(f"/api/documents/{created['id']}/file")
    assert response.status_code == 409


async def test_get_content_done(
    api_client: AsyncClient,
    kb_repo: FakeKnowledgeBaseRepository,
    doc_repo: FakeDocumentRepository,
) -> None:
    kb = await _make_kb(kb_repo)
    created = (
        await api_client.post(
            "/api/documents",
            files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
            data={"kb_id": str(kb.id)},
        )
    ).json()
    doc = await doc_repo.get(uuid.UUID(created["id"]))
    assert doc is not None
    doc.status = DocumentStatus.DONE
    doc.content_markdown = "# 正文"

    response = await api_client.get(f"/api/documents/{created['id']}/content")
    assert response.status_code == 200
    assert response.json()["markdown"] == "# 正文"


async def test_get_content_pending_409(
    api_client: AsyncClient, kb_repo: FakeKnowledgeBaseRepository,
) -> None:
    kb = await _make_kb(kb_repo)
    created = (
        await api_client.post(
            "/api/documents",
            files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
            data={"kb_id": str(kb.id)},
        )
    ).json()

    response = await api_client.get(f"/api/documents/{created['id']}/content")
    assert response.status_code == 409


async def test_retry_error(
    api_client: AsyncClient, kb_repo: FakeKnowledgeBaseRepository, doc_repo: FakeDocumentRepository
) -> None:
    kb = await _make_kb(kb_repo)
    created = (
        await api_client.post(
            "/api/documents",
            files={"file": ("a.pdf", b"x", "application/pdf")},
            data={"kb_id": str(kb.id)},
        )
    ).json()
    doc = await doc_repo.get(uuid.UUID(created["id"]))
    assert doc is not None
    doc.status = DocumentStatus.ERROR
    doc.retry_count = 3

    response = await api_client.post(f"/api/documents/{created['id']}/retry")
    assert response.status_code == 200
    assert response.json()["status"] == "pending"


async def test_retry_non_error_409(
    api_client: AsyncClient, kb_repo: FakeKnowledgeBaseRepository,
) -> None:
    kb = await _make_kb(kb_repo)
    created = (
        await api_client.post(
            "/api/documents",
            files={"file": ("a.pdf", b"x", "application/pdf")},
            data={"kb_id": str(kb.id)},
        )
    ).json()
    response = await api_client.post(f"/api/documents/{created['id']}/retry")
    assert response.status_code == 409


async def test_delete_document(
    api_client: AsyncClient, kb_repo: FakeKnowledgeBaseRepository,
) -> None:
    kb = await _make_kb(kb_repo)
    created = (
        await api_client.post(
            "/api/documents",
            files={"file": ("a.pdf", b"x", "application/pdf")},
            data={"kb_id": str(kb.id)},
        )
    ).json()

    response = await api_client.delete(f"/api/documents/{created['id']}")
    assert response.status_code == 204
    response = await api_client.get(f"/api/documents/{created['id']}")
    assert response.status_code == 404


async def test_contents_includes_documents(
    api_client: AsyncClient, kb_repo: FakeKnowledgeBaseRepository
) -> None:
    kb = await _make_kb(kb_repo)
    await api_client.post(
        "/api/documents",
        files={"file": ("a.pdf", b"x", "application/pdf")},
        data={"kb_id": str(kb.id)},
    )

    response = await api_client.get(f"/api/knowledge-bases/{kb.id}/contents")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["type"] == "document"
    assert body["items"][0]["document"]["source_type"] == "pdf"
