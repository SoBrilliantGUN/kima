"""分路召回：约束硬召回（不过阈值）+ 写入分类器覆盖 + 语义/情节混合召回 + 记忆块预算合并。

对齐《分路召回架构》：约束是「确定域」，只要任务沾边就无条件在场，不靠余弦相似度碰运气；
事实/偏好/情节是「概率域」，走混合召回（向量 + 词法）。写入时加确定性分类兜底，防止
Agent 把「禁止 ORM」当语义事实写进去、从而走向量 top-k 漏召回。
"""

from datetime import UTC, datetime

from app.agent.memory import format_memory_block
from app.agent.memory_classifier import LLMMemoryClassifier, MemoryClassification
from app.integrations.embedding import FakeEmbeddingClient
from app.models.copilot import CopilotMemory, MemoryKind
from app.services.copilot import CopilotMemoryService, RecalledMemories
from tests.fakes import (
    FakeConflictJudge,
    FakeCopilotMemoryRepository,
    FakeMemoryClassifier,
    ScriptedLLM,
    gateway_run,
    make_gateway,
)


def make_service(
    repository: FakeCopilotMemoryRepository | None = None,
    classifier: FakeMemoryClassifier | None = None,
) -> CopilotMemoryService:
    return CopilotMemoryService(
        repository=repository or FakeCopilotMemoryRepository(),
        gateway=make_gateway(embedder=FakeEmbeddingClient(dimension=8)),
        judge=FakeConflictJudge(),
        classifier=classifier,
        capacity=200,
        episodic_ttl_days=30,
        recall_floor=0.05,
        recency_window_days=7,
        conflict_top_k=10,
    )


def _mem(kind: MemoryKind, content: str) -> CopilotMemory:
    return CopilotMemory(kind=kind, content=content)


async def test_recall_constraint_hard_recall_regardless_of_query() -> None:
    """约束是硬召回：即使 query 与约束毫无语义重叠，也全量在场（不过相似度阈值）。"""
    service = make_service()
    async with gateway_run():
        await service.write_memory(MemoryKind.CONSTRAINT, "禁止使用 ORM")
        recalled = await service.recall("给报表模块写数据库访问层")
    assert [m.content for m in recalled.constraint] == ["禁止使用 ORM"]


async def test_recall_constraint_not_filtered_by_floor() -> None:
    """约束走 list_active 全量、不经过 activation floor——极老约束也不会被软遗忘滤掉。"""
    repository = FakeCopilotMemoryRepository()
    embedder = FakeEmbeddingClient(dimension=8)
    await repository.add(
        CopilotMemory(
            kind=MemoryKind.CONSTRAINT,
            content="严禁执行 DROP TABLE",
            embedding=await embedder.embed_query("严禁执行 DROP TABLE"),
            created_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
    )
    service = make_service(repository=repository)
    async with gateway_run():
        recalled = await service.recall("任意查询")
    assert len(recalled.constraint) == 1


async def test_classifier_overrides_agent_kind_to_constraint() -> None:
    """防线一兜底：Agent 报 semantic，分类器判 constraint → 落库为 constraint（硬召回）。"""
    classifier = FakeMemoryClassifier(
        [
            MemoryClassification(
                kind=MemoryKind.CONSTRAINT,
                trigger_conditions={"type": "domain", "value": "database"},
            )
        ]
    )
    service = make_service(classifier=classifier)
    async with gateway_run():
        memory = await service.write_memory(MemoryKind.SEMANTIC, "禁止使用 ORM")
        assert memory.kind == MemoryKind.CONSTRAINT
        assert memory.trigger_conditions == {"type": "domain", "value": "database"}
        # 之后走 constraint 硬召回，不再走 semantic 向量
        recalled = await service.recall("任意")
    assert any(m.content == "禁止使用 ORM" for m in recalled.constraint)
    assert all(m.content != "禁止使用 ORM" for m in recalled.semantic)


async def test_classifier_none_falls_back_to_agent_kind() -> None:
    """分类器失败（None）→ 不覆盖，回退到 Agent 自报 kind（与现状一致）。"""
    classifier = FakeMemoryClassifier([None])
    service = make_service(classifier=classifier)
    async with gateway_run():
        memory = await service.write_memory(
            MemoryKind.SEMANTIC, "用户是产品经理", entity_id="user:role"
        )
    assert memory.kind == MemoryKind.SEMANTIC


async def test_recall_semantic_hybrid_returns_exact_term() -> None:
    """语义召回走 dense + lexical 混合：精确词命中（词法通道）也能进场（烟测不报错）。"""
    service = make_service()
    async with gateway_run():
        await service.write_memory(
            MemoryKind.SEMANTIC, "生产库连接串 postgres://prod", entity_id="e1"
        )
        recalled = await service.recall("postgres")
    assert any("postgres" in m.content for m in recalled.semantic)


async def test_llm_classifier_maps_constraint() -> None:
    llm = ScriptedLLM(
        ['{"memory_type":"constraint","entity_id":null,"trigger_condition":{"type":"domain","value":"database"}}']
    )
    classifier = LLMMemoryClassifier(make_gateway(llm=llm))
    async with gateway_run():
        result = await classifier.classify("禁止使用 ORM")
    assert result is not None
    assert result.kind == MemoryKind.CONSTRAINT
    assert result.trigger_conditions == {"type": "domain", "value": "database"}


async def test_llm_classifier_maps_fact_with_entity() -> None:
    llm = ScriptedLLM(['{"memory_type":"fact","entity_id":"user:role","trigger_condition":null}'])
    classifier = LLMMemoryClassifier(make_gateway(llm=llm))
    async with gateway_run():
        result = await classifier.classify("用户是产品经理")
    assert result is not None
    assert result.kind == MemoryKind.SEMANTIC
    assert result.entity_id == "user:role"


async def test_llm_classifier_fallback_none_on_parse_failure() -> None:
    """解析失败 → 回退 None，不静默降级成某个可能漏掉红线的桶。"""
    llm = ScriptedLLM(["not json"])
    classifier = LLMMemoryClassifier(make_gateway(llm=llm))
    async with gateway_run():
        result = await classifier.classify("禁止使用 ORM")
    assert result is None


def test_format_memory_block_constraint_first_and_budget_caps_low_priority() -> None:
    """约束排最前；超预算的低优先级记忆（偏好）被跳过，约束仍在场。"""
    recalled = RecalledMemories(
        constraint=[_mem(MemoryKind.CONSTRAINT, "禁止使用 ORM")],
        procedural=[_mem(MemoryKind.PROCEDURAL, "规则" + "长" * 100)],
        semantic=[_mem(MemoryKind.SEMANTIC, "用户是产品经理")],
        episodic=[],
    )
    block = format_memory_block(recalled, max_tokens=50)
    assert "禁止使用 ORM" in block
    assert "长" not in block  # 超预算的偏好被跳过
    assert block.index("禁止使用 ORM") < block.index("用户是产品经理")


def test_format_memory_block_no_budget_full_injection() -> None:
    """max_tokens 缺省：四型全量注入（向后兼容），且约束在最前。"""
    recalled = RecalledMemories(
        constraint=[_mem(MemoryKind.CONSTRAINT, "禁止使用 ORM")],
        procedural=[_mem(MemoryKind.PROCEDURAL, "回答要简洁")],
        semantic=[_mem(MemoryKind.SEMANTIC, "用户是产品经理")],
        episodic=[_mem(MemoryKind.EPISODIC, "昨天讨论了架构")],
    )
    block = format_memory_block(recalled)
    assert block.startswith("[MEMORY]\n")
    for text in ("禁止使用 ORM", "回答要简洁", "用户是产品经理", "昨天讨论了架构"):
        assert text in block
    assert (
        block.index("禁止使用 ORM")
        < block.index("回答要简洁")
        < block.index("用户是产品经理")
        < block.index("昨天讨论了架构")
    )
