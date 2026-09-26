"""记忆遗忘：activation 公式 + 容量硬淘汰 + search_memory 回写 access_count。"""

import math
from datetime import UTC, datetime, timedelta

import pytest

from app.integrations.embedding import FakeEmbeddingClient
from app.models.copilot import CopilotMemory, MemoryKind
from app.services.copilot import CopilotMemoryService
from tests.fakes import (
    FakeConflictJudge,
    FakeCopilotMemoryRepository,
    gateway_run,
    make_gateway,
)


def make_service(
    repository: FakeCopilotMemoryRepository | None = None, *, capacity: int = 200
) -> CopilotMemoryService:
    return CopilotMemoryService(
        repository=repository or FakeCopilotMemoryRepository(),
        gateway=make_gateway(embedder=FakeEmbeddingClient(dimension=8)),
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


async def test_activation_preference_constant() -> None:
    service = make_service()
    now = datetime.now(UTC)
    memory = _memory(MemoryKind.PREFERENCE)
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
    async with gateway_run():
        await service.write_memory(MemoryKind.EPISODIC, "事件 1")
        await service.write_memory(MemoryKind.EPISODIC, "事件 2")
        await service.write_memory(MemoryKind.EPISODIC, "事件 3")  # 触发淘汰

    active = await repository.count_active(MemoryKind.EPISODIC)
    assert active == 2  # 硬淘汰到容量


async def test_capacity_eviction_skips_preference() -> None:
    repository = FakeCopilotMemoryRepository()
    service = make_service(repository, capacity=2)
    async with gateway_run():
        await service.write_memory(MemoryKind.PREFERENCE, "规则 1")
        await service.write_memory(MemoryKind.PREFERENCE, "规则 2")
        await service.write_memory(MemoryKind.PREFERENCE, "规则 3")  # preference 不淘汰

    assert await repository.count_active(MemoryKind.PREFERENCE) == 3


async def test_search_memory_touches_access_count() -> None:
    repository = FakeCopilotMemoryRepository()
    service = make_service(repository)
    async with gateway_run():
        await service.write_memory(MemoryKind.FACT, "用户是副总经理", entity_id="user:role")
        hits = await service.search_memory("用户是副总经理", MemoryKind.FACT)
    assert len(hits) == 1
    assert hits[0].access_count == 1
    assert hits[0].last_access is not None


async def test_recall_touches_recalled_memories() -> None:
    """召回命中回写 access_count/last_access（ACT-R 频率/近期增益的活水）。"""
    repository = FakeCopilotMemoryRepository()
    service = make_service(repository)
    async with gateway_run():
        await service.write_memory(MemoryKind.FACT, "用户是副总经理", entity_id="user:role")
        recalled = await service.recall("用户是副总经理")
    assert len(recalled.fact) == 1
    assert recalled.fact[0].access_count >= 1
    assert recalled.fact[0].last_access is not None


async def test_recall_revives_superseded_within_window() -> None:
    """软删除窗口：窗口期内被强命中的 superseded 记忆翻回 active，重新上场。"""
    repository = FakeCopilotMemoryRepository()
    embedder = FakeEmbeddingClient(dimension=8)
    memory = CopilotMemory(
        kind=MemoryKind.FACT,
        content="用户是副总经理",
        embedding=await embedder.embed_query("用户是副总经理"),
        superseded=True,
        superseded_at=datetime.now(UTC),
    )
    await repository.add(memory)
    service = make_service(repository)

    async with gateway_run():
        recalled = await service.recall("用户是副总经理")
    assert len(recalled.fact) == 1
    assert recalled.fact[0].superseded is False  # 已复活
    assert recalled.fact[0].superseded_at is None


async def test_recall_does_not_revive_when_superseder_alive() -> None:
    """复活守卫：压它的那条记忆还活着 → 真冲突仍成立，不复活。"""
    repository = FakeCopilotMemoryRepository()
    embedder = FakeEmbeddingClient(dimension=8)
    winner = CopilotMemory(
        kind=MemoryKind.FACT,
        content="用户改用 React",
        embedding=await embedder.embed_query("用户改用 React"),
        superseded=False,
    )
    await repository.add(winner)
    loser = CopilotMemory(
        kind=MemoryKind.FACT,
        content="用户用 Vue",
        embedding=await embedder.embed_query("用户用 Vue"),
        superseded=True,
        superseded_at=datetime.now(UTC),
        superseded_by=winner.id,
    )
    await repository.add(loser)
    service = make_service(repository)

    async with gateway_run():
        recalled = await service.recall("用户用 Vue")
    assert all(m.content != "用户用 Vue" for m in recalled.fact)
    assert loser.superseded is True  # 未被复活


async def test_recall_revives_when_superseder_gone() -> None:
    """压它的那条已废弃（superseded）→ 复活守卫放行，旧记忆翻回。"""
    repository = FakeCopilotMemoryRepository()
    embedder = FakeEmbeddingClient(dimension=8)
    winner = CopilotMemory(
        kind=MemoryKind.FACT,
        content="用户改用 React",
        embedding=await embedder.embed_query("用户改用 React"),
        superseded=True,
    )
    await repository.add(winner)
    loser = CopilotMemory(
        kind=MemoryKind.FACT,
        content="用户用 Vue",
        embedding=await embedder.embed_query("用户用 Vue"),
        superseded=True,
        superseded_at=datetime.now(UTC),
        superseded_by=winner.id,
    )
    await repository.add(loser)
    service = make_service(repository)

    async with gateway_run():
        recalled = await service.recall("用户用 Vue")
    assert any(m.content == "用户用 Vue" for m in recalled.fact)


async def test_recall_does_not_revive_outside_window() -> None:
    """软删除窗口过期：superseded_at 超过 N 天，不再复活。"""
    repository = FakeCopilotMemoryRepository()
    embedder = FakeEmbeddingClient(dimension=8)
    memory = CopilotMemory(
        kind=MemoryKind.FACT,
        content="用户是副总经理",
        embedding=await embedder.embed_query("用户是副总经理"),
        superseded=True,
        superseded_at=datetime.now(UTC) - timedelta(days=30),
    )
    await repository.add(memory)
    service = make_service(repository)

    async with gateway_run():
        recalled = await service.recall("用户是副总经理")
    assert len(recalled.fact) == 0


async def test_recall_does_not_revive_below_floor() -> None:
    """激活门槛：衰减到 recall floor 以下的 superseded 情节不复活（防僵尸 active 行）。"""
    repository = FakeCopilotMemoryRepository()
    embedder = FakeEmbeddingClient(dimension=8)
    memory = CopilotMemory(
        kind=MemoryKind.EPISODIC,
        content="很久以前的事件",
        embedding=await embedder.embed_query("很久以前的事件"),
        ttl_days=30,
        created_at=datetime.now(UTC).replace(year=2000),
        superseded=True,
        superseded_at=datetime.now(UTC),
    )
    await repository.add(memory)
    service = make_service(repository)

    async with gateway_run():
        await service.recall("很久以前的事件")
    assert memory.superseded is True  # 未被复活


async def test_delete_superseded_older_than_removes_expired() -> None:
    """遗忘收尾：软删除窗口过期的 superseded 记忆被硬删，窗口内的保留。"""
    repository = FakeCopilotMemoryRepository()
    now = datetime.now(UTC)
    expired = CopilotMemory(
        kind=MemoryKind.FACT,
        content="过期记忆",
        superseded=True,
        superseded_at=now - timedelta(days=8),
    )
    in_window = CopilotMemory(
        kind=MemoryKind.FACT,
        content="窗口内记忆",
        superseded=True,
        superseded_at=now - timedelta(days=1),
    )
    await repository.add(expired)
    await repository.add(in_window)

    deleted = await repository.delete_superseded_older_than(now - timedelta(days=7))
    assert deleted == 1
    assert await repository.get(expired.id) is None  # 已删
    assert await repository.get(in_window.id) is not None  # 保留
