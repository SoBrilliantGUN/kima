import uuid

import pytest
from pydantic import ValidationError

from app.core.exceptions import ConflictError, NotFoundError
from app.models.knowledge_base import DEFAULT_KB_COLOR
from app.schemas.knowledge_base import KnowledgeBaseCreate, KnowledgeBaseUpdate
from app.services.knowledge_base import KnowledgeBaseService
from tests.fakes import FakeKnowledgeBaseRepository


@pytest.fixture
def service() -> KnowledgeBaseService:
    return KnowledgeBaseService(FakeKnowledgeBaseRepository())


async def test_create_assigns_default_color(service: KnowledgeBaseService) -> None:
    kb = await service.create(KnowledgeBaseCreate(name="工作"))
    assert kb.id is not None
    assert kb.color == DEFAULT_KB_COLOR


async def test_create_rejects_duplicate_name(service: KnowledgeBaseService) -> None:
    await service.create(KnowledgeBaseCreate(name="工作"))
    with pytest.raises(ConflictError):
        await service.create(KnowledgeBaseCreate(name="工作"))


async def test_get_missing_raises_not_found(service: KnowledgeBaseService) -> None:
    with pytest.raises(NotFoundError):
        await service.get(uuid.uuid4())


async def test_update_renames(service: KnowledgeBaseService) -> None:
    kb = await service.create(KnowledgeBaseCreate(name="旧名"))
    updated = await service.update(kb.id, KnowledgeBaseUpdate(name="新名"))
    assert updated.name == "新名"


async def test_update_keeps_same_name(service: KnowledgeBaseService) -> None:
    kb = await service.create(KnowledgeBaseCreate(name="甲"))
    updated = await service.update(kb.id, KnowledgeBaseUpdate(name="甲"))
    assert updated.name == "甲"


async def test_update_conflicting_name_raises(service: KnowledgeBaseService) -> None:
    await service.create(KnowledgeBaseCreate(name="甲"))
    kb_b = await service.create(KnowledgeBaseCreate(name="乙"))
    with pytest.raises(ConflictError):
        await service.update(kb_b.id, KnowledgeBaseUpdate(name="甲"))


async def test_delete_removes(service: KnowledgeBaseService) -> None:
    kb = await service.create(KnowledgeBaseCreate(name="待删"))
    await service.create(KnowledgeBaseCreate(name="保留"))
    await service.delete(kb.id)
    with pytest.raises(NotFoundError):
        await service.get(kb.id)


async def test_delete_last_knowledge_base_raises(service: KnowledgeBaseService) -> None:
    kb = await service.create(KnowledgeBaseCreate(name="唯一"))
    with pytest.raises(ConflictError):
        await service.delete(kb.id)


async def test_ensure_default_creates_when_empty(service: KnowledgeBaseService) -> None:
    await service.ensure_default()
    items, total = await service.list(limit=10, offset=0)
    assert total == 1
    assert items[0].name == "我的知识库"


async def test_ensure_default_noop_when_not_empty(service: KnowledgeBaseService) -> None:
    await service.create(KnowledgeBaseCreate(name="已有"))
    await service.ensure_default()
    _, total = await service.list(limit=10, offset=0)
    assert total == 1


async def test_delete_missing_raises_not_found(service: KnowledgeBaseService) -> None:
    with pytest.raises(NotFoundError):
        await service.delete(uuid.uuid4())


def test_create_strips_name() -> None:
    assert KnowledgeBaseCreate(name="  工作  ").name == "工作"


def test_create_rejects_invalid_color() -> None:
    with pytest.raises(ValidationError):
        KnowledgeBaseCreate(name="工作", color="blue")


def test_update_requires_at_least_one_field() -> None:
    with pytest.raises(ValidationError):
        KnowledgeBaseUpdate()


def test_update_rejects_null_name() -> None:
    with pytest.raises(ValidationError):
        KnowledgeBaseUpdate(name=None)
