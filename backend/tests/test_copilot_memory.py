"""记忆召回：三型分型召回 + 软遗忘过滤 + superseded 跳过。"""

from datetime import UTC, datetime

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
    repository: FakeCopilotMemoryRepository | None = None,
    judge: FakeConflictJudge | None = None,
    **kwargs: int,
) -> CopilotMemoryService:
    return CopilotMemoryService(
        repository=repository or FakeCopilotMemoryRepository(),
        gateway=make_gateway(embedder=FakeEmbeddingClient(dimension=8)),
        judge=judge or FakeConflictJudge(),
        capacity=kwargs.get("capacity", 200),
        episodic_ttl_days=kwargs.get("episodic_ttl_days", 30),
        recall_floor=kwargs.get("recall_floor", 0.05),
        recency_window_days=kwargs.get("recency_window_days", 7),
        conflict_top_k=kwargs.get("conflict_top_k", 10),
    )


async def test_recall_procedural_full_and_others_topk() -> None:
    service = make_service()
    async with gateway_run():
        for i in range(3):
            await service.write_memory(MemoryKind.PROCEDURAL, f"规则 {i}")
        for i in range(3):
            await service.write_memory(MemoryKind.SEMANTIC, f"事实 {i}", entity_id=f"e{i}")
        await service.write_memory(MemoryKind.EPISODIC, "某次事件")

        recalled = await service.recall("任意查询")
    assert len(recalled.procedural) == 3  # 全量注入
    assert len(recalled.semantic) == 3  # top-k=5 覆盖 3 条
    assert len(recalled.episodic) == 1


async def test_recall_skips_superseded() -> None:
    repository = FakeCopilotMemoryRepository()
    embedder = FakeEmbeddingClient(dimension=8)
    superseded = CopilotMemory(
        kind=MemoryKind.SEMANTIC,
        content="旧事实",
        embedding=await embedder.embed_query("旧事实"),
        superseded=True,
    )
    await repository.add(superseded)
    service = make_service(repository=repository)

    async with gateway_run():
        recalled = await service.recall("查询")
    assert len(recalled.semantic) == 0  # superseded 被跳过


async def test_recall_filters_below_floor_episodic() -> None:
    repository = FakeCopilotMemoryRepository()
    embedder = FakeEmbeddingClient(dimension=8)
    # 极老的情节记忆 → TTL 衰减到 recall floor 以下，召回不到
    old = CopilotMemory(
        kind=MemoryKind.EPISODIC,
        content="很久以前的事件",
        embedding=await embedder.embed_query("很久以前的事件"),
        ttl_days=30,
        created_at=datetime.now(UTC).replace(year=2000),
    )
    await repository.add(old)
    service = make_service(repository=repository)

    async with gateway_run():
        recalled = await service.recall("查询")
    assert len(recalled.episodic) == 0
