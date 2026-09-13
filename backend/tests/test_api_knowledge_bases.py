import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_kb_service
from app.main import app
from app.services.knowledge_base import KnowledgeBaseService
from tests.fakes import FakeKnowledgeBaseRepository


@pytest.fixture
def kb_repo() -> FakeKnowledgeBaseRepository:
    return FakeKnowledgeBaseRepository()


@pytest.fixture
async def api_client(kb_repo: FakeKnowledgeBaseRepository) -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_kb_service] = lambda: KnowledgeBaseService(kb_repo)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def test_list_empty(api_client: AsyncClient) -> None:
    response = await api_client.get("/api/knowledge-bases")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


async def test_create_and_get(api_client: AsyncClient) -> None:
    response = await api_client.post("/api/knowledge-bases", json={"name": "工作"})
    assert response.status_code == 201
    created = response.json()
    assert created["name"] == "工作"
    assert created["color"] == "#5B8DEF"

    response = await api_client.get(f"/api/knowledge-bases/{created['id']}")
    assert response.status_code == 200
    assert response.json()["name"] == "工作"


async def test_list_pagination(api_client: AsyncClient) -> None:
    for i in range(5):
        await api_client.post("/api/knowledge-bases", json={"name": f"库{i}"})
    response = await api_client.get("/api/knowledge-bases", params={"limit": 2, "offset": 1})
    body = response.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2


async def test_create_duplicate_conflict(api_client: AsyncClient) -> None:
    await api_client.post("/api/knowledge-bases", json={"name": "工作"})
    response = await api_client.post("/api/knowledge-bases", json={"name": "工作"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "conflict"


async def test_get_missing_not_found(api_client: AsyncClient) -> None:
    response = await api_client.get(f"/api/knowledge-bases/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"


async def test_update(api_client: AsyncClient) -> None:
    created = (await api_client.post("/api/knowledge-bases", json={"name": "旧名"})).json()
    response = await api_client.patch(
        f"/api/knowledge-bases/{created['id']}",
        json={"name": "新名", "description": "描述"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "新名"
    assert body["description"] == "描述"


async def test_delete(api_client: AsyncClient) -> None:
    await api_client.post("/api/knowledge-bases", json={"name": "保留"})
    created = (await api_client.post("/api/knowledge-bases", json={"name": "待删"})).json()
    response = await api_client.delete(f"/api/knowledge-bases/{created['id']}")
    assert response.status_code == 204

    response = await api_client.get(f"/api/knowledge-bases/{created['id']}")
    assert response.status_code == 404


async def test_delete_last_knowledge_base_conflict(api_client: AsyncClient) -> None:
    created = (await api_client.post("/api/knowledge-bases", json={"name": "唯一"})).json()
    response = await api_client.delete(f"/api/knowledge-bases/{created['id']}")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "last_knowledge_base"


async def test_create_invalid_color_422(api_client: AsyncClient) -> None:
    response = await api_client.post(
        "/api/knowledge-bases", json={"name": "工作", "color": "blue"}
    )
    assert response.status_code == 422


async def test_update_empty_payload_422(api_client: AsyncClient) -> None:
    created = (await api_client.post("/api/knowledge-bases", json={"name": "工作"})).json()
    response = await api_client.patch(f"/api/knowledge-bases/{created['id']}", json={})
    assert response.status_code == 422
