import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_document_service, get_note_service
from app.main import app
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
def note_repo() -> FakeNoteRepository:
    return FakeNoteRepository()


@pytest.fixture
def kb_repo() -> FakeKnowledgeBaseRepository:
    return FakeKnowledgeBaseRepository()


@pytest.fixture
def doc_repo() -> FakeDocumentRepository:
    return FakeDocumentRepository()


@pytest.fixture
async def api_client(
    note_repo: FakeNoteRepository,
    kb_repo: FakeKnowledgeBaseRepository,
    doc_repo: FakeDocumentRepository,
) -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_note_service] = lambda: NoteService(note_repo, kb_repo)
    app.dependency_overrides[get_document_service] = lambda: DocumentService(
        doc_repo, kb_repo, FakeFileStore()
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def _make_kb(kb_repo: FakeKnowledgeBaseRepository) -> KnowledgeBase:
    return await kb_repo.add(KnowledgeBase(name="工作"))


async def test_list_empty(api_client: AsyncClient) -> None:
    response = await api_client.get("/api/notes")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


async def test_create_blank_and_get(api_client: AsyncClient) -> None:
    response = await api_client.post("/api/notes", json={})
    assert response.status_code == 201
    created = response.json()
    assert created["title"] == "无标题笔记"
    # 网页笔记三字段已移除
    assert "type" not in created
    assert "summary" not in created
    assert "source_url" not in created

    response = await api_client.get(f"/api/notes/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


async def test_create_blank_with_kb_associates(
    api_client: AsyncClient, kb_repo: FakeKnowledgeBaseRepository
) -> None:
    kb = await _make_kb(kb_repo)
    response = await api_client.post("/api/notes", json={"knowledge_base_id": str(kb.id)})
    assert response.status_code == 201
    note_id = response.json()["id"]

    response = await api_client.get(f"/api/knowledge-bases/{kb.id}/contents")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["type"] == "note"
    assert body["items"][0]["note"]["id"] == note_id


async def test_create_blank_missing_kb_404(
    api_client: AsyncClient, note_repo: FakeNoteRepository
) -> None:
    response = await api_client.post("/api/notes", json={"knowledge_base_id": str(uuid.uuid4())})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"

    _, total = await note_repo.list(limit=10, offset=0)
    assert total == 0


async def test_from_url_endpoint_removed(api_client: AsyncClient) -> None:
    response = await api_client.post("/api/notes/from-url", json={"url": "https://example.com"})
    assert response.status_code in (404, 405)


async def test_patch_note(api_client: AsyncClient) -> None:
    created = (await api_client.post("/api/notes", json={})).json()
    response = await api_client.patch(
        f"/api/notes/{created['id']}",
        json={"title": "新标题", "content_markdown": "# 正文"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "新标题"
    assert body["content_markdown"] == "# 正文"


async def test_patch_empty_422(api_client: AsyncClient) -> None:
    created = (await api_client.post("/api/notes", json={})).json()
    response = await api_client.patch(f"/api/notes/{created['id']}", json={})
    assert response.status_code == 422


async def test_patch_missing_404(api_client: AsyncClient) -> None:
    response = await api_client.patch(f"/api/notes/{uuid.uuid4()}", json={"title": "x"})
    assert response.status_code == 404


async def test_delete(api_client: AsyncClient) -> None:
    created = (await api_client.post("/api/notes", json={})).json()
    response = await api_client.delete(f"/api/notes/{created['id']}")
    assert response.status_code == 204

    response = await api_client.get(f"/api/notes/{created['id']}")
    assert response.status_code == 404


async def test_add_to_kb_and_idempotent(
    api_client: AsyncClient, kb_repo: FakeKnowledgeBaseRepository
) -> None:
    kb = await _make_kb(kb_repo)
    created = (await api_client.post("/api/notes", json={})).json()

    response = await api_client.post(
        f"/api/notes/{created['id']}/knowledge-bases", json={"knowledge_base_id": str(kb.id)}
    )
    assert response.status_code == 204

    response = await api_client.post(
        f"/api/notes/{created['id']}/knowledge-bases", json={"knowledge_base_id": str(kb.id)}
    )
    assert response.status_code == 204

    response = await api_client.get(f"/api/knowledge-bases/{kb.id}/contents")
    assert response.json()["total"] == 1


async def test_add_to_kb_missing_note_404(
    api_client: AsyncClient, kb_repo: FakeKnowledgeBaseRepository
) -> None:
    kb = await _make_kb(kb_repo)
    response = await api_client.post(
        f"/api/notes/{uuid.uuid4()}/knowledge-bases", json={"knowledge_base_id": str(kb.id)}
    )
    assert response.status_code == 404
