"""记忆遗忘：activation 公式 + 容量硬淘汰 + search_memory 回写 access_count。"""

import math
from datetime import UTC, datetime, timedelta

import pytest

from app.integrations.embedding import FakeEmbeddingClient
from app.models.copilot import CopilotMemory, MemoryKind
from app.services.copilot import CopilotMemoryService
from tests.fakes import FakeConflictJudge, FakeCopilotMemoryRepository


def make_service(
    repository: FakeCopilotMemoryRepository | None = None, *, capacity: int = 200
) -> CopilotMemoryService:
    return CopilotMemoryService(
        repository=repository or FakeCopilotMemoryRepository(),
        embedder=FakeEmbeddingClient(dimension=8),
        judge=FakeConflictJudge(),
        capacity=capacity,
        episodic_ttl_days=30,
        recall_floor=0.05,
        recency_window_days=7,
        conflict_top_k=10,
    )


def _memory(kind: MemoryKind, *, ttl_days: int | None = None) -> CopilotMemory:
    memory = CopilotMemory(kind=kind, content="x", ttl_days=ttl_days)
    memory.created_at = datetime.now(UTC)
    return memory


async def test_activation_procedural_constant() -> None:
    service = make_service()
    now = datetime.now(UTC)
    memory = _memory(MemoryKind.PROCEDURAL)
    memory.created_at = now - timedelta(days=1000)
    assert service.compute_activation(memory, now) == pytest.approx(1.0)


async def test_activation_episodic_decays_by_ttl() -> None:
    service = make_service()
    now = datetime.now(UTC)
    fresh = _memory(MemoryKind.EPISODIC, ttl_days=30)
    assert service.compute_activation(fresh, now) == pytest.approx(1.0)

    aged = _memory(MemoryKind.EPISODIC, ttl_days=30)
    aged.created_at = now - timedelta(days=30)
    assert service.compute_activation(aged, now) == pytest.approx(math.exp(-1))


async def test_activation_recency_bonus() -> None:
    service = make_service()
    now = datetime.now(UTC)
    memory = _memory(MemoryKind.EPISODIC, ttl_days=30)
    memory.last_access = now
    assert service.compute_activation(memory, now) == pytest.approx(1.0 + 0.3)


async def test_capacity_eviction_keeps_latest() -> None:
    repository = FakeCopilotMemoryRepository()
    service = make_service(repository, capacity=2)
    await service.write_memory(MemoryKind.EPISODIC, "事件 1")
    await service.write_memory(MemoryKind.EPISODIC, "事件 2")
    await service.write_memory(MemoryKind.EPISODIC, "事件 3")  # 触发淘汰

    active = await repository.count_active(MemoryKind.EPISODIC)
    assert active == 2  # 硬淘汰到容量


async def test_capacity_eviction_skips_procedural() -> None:
    repository = FakeCopilotMemoryRepository()
    service = make_service(repository, capacity=2)
    await service.write_memory(MemoryKind.PROCEDURAL, "规则 1")
    await service.write_memory(MemoryKind.PROCEDURAL, "规则 2")
    await service.write_memory(MemoryKind.PROCEDURAL, "规则 3")  # procedural 不淘汰

    assert await repository.count_active(MemoryKind.PROCEDURAL) == 3


async def test_search_memory_touches_access_count() -> None:
    repository = FakeCopilotMemoryRepository()
    service = make_service(repository)
    await service.write_memory(MemoryKind.SEMANTIC, "用户是副总经理", entity_id="user:role")

    hits = await service.search_memory("用户是副总经理", MemoryKind.SEMANTIC)
    assert len(hits) == 1
    assert hits[0].access_count == 1
    assert hits[0].last_access is not None
