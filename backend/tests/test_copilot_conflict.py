"""记忆冲突判定：LLM 判定（duplicate/contradiction/none）+ semantic 同 entity 覆盖。"""

import pytest

from app.agent.runtime.budget import DailyBudget
from app.integrations.embedding import FakeEmbeddingClient
from app.integrations.llm import StructuredParseError
from app.models.copilot import MemoryKind
from app.services.copilot import ConflictVerdict, CopilotMemoryService, LLMConflictJudge
from tests.fakes import FakeConflictJudge, FakeCopilotMemoryRepository, ScriptedLLM


def make_service(judge: FakeConflictJudge) -> CopilotMemoryService:
    return CopilotMemoryService(
        repository=FakeCopilotMemoryRepository(),
        embedder=FakeEmbeddingClient(dimension=8),
        judge=judge,
        capacity=200,
        episodic_ttl_days=30,
        recall_floor=0.05,
        recency_window_days=7,
        conflict_top_k=10,
    )


async def test_duplicate_supersedes_old() -> None:
    judge = FakeConflictJudge(["duplicate"])
    service = make_service(judge)
    first = await service.write_memory(MemoryKind.EPISODIC, "用户喜欢简洁回答")
    second = await service.write_memory(MemoryKind.EPISODIC, "用户喜欢简洁回答")

    assert first.id != second.id
    assert first.superseded is True  # 输家留痕
    assert second.superseded is False
    assert judge.calls  # LLM 判定被调用


async def test_contradiction_new_wins() -> None:
    judge = FakeConflictJudge(["contradiction"])
    service = make_service(judge)
    first = await service.write_memory(MemoryKind.EPISODIC, "用户用 Vue")
    second = await service.write_memory(MemoryKind.EPISODIC, "用户改用 React")

    assert first.superseded is True
    assert second.superseded is False


async def test_none_keeps_both() -> None:
    judge = FakeConflictJudge(["none"])
    service = make_service(judge)
    first = await service.write_memory(MemoryKind.EPISODIC, "事件 A")
    second = await service.write_memory(MemoryKind.EPISODIC, "事件 B")

    assert first.superseded is False
    assert second.superseded is False


async def test_semantic_entity_override_increments_version() -> None:
    service = make_service(FakeConflictJudge())
    first = await service.write_memory(MemoryKind.SEMANTIC, "用户是经理", entity_id="user:role")
    assert first.version == 1

    second = await service.write_memory(
        MemoryKind.SEMANTIC, "用户是副总经理", entity_id="user:role"
    )
    assert second.id == first.id  # 直接覆盖同一行，不新建
    assert second.version == 2
    assert second.content == "用户是副总经理"


def test_conflict_parse_strict_rejects_bad_length() -> None:
    with pytest.raises(StructuredParseError):
        LLMConflictJudge._parse_strict('["duplicate"]', expected=2)


def test_conflict_parse_strict_rejects_non_array() -> None:
    with pytest.raises(StructuredParseError):
        LLMConflictJudge._parse_strict('{"a":"duplicate"}', expected=1)


def test_conflict_parse_strict_rejects_bad_value() -> None:
    with pytest.raises(StructuredParseError):
        LLMConflictJudge._parse_strict('["duplicate","maybe"]', expected=2)


def test_conflict_parse_strict_normalizes_whitespace() -> None:
    verdicts = LLMConflictJudge._parse_strict('[" DUPLICATE ", "none"]', expected=2)
    assert verdicts == [ConflictVerdict.DUPLICATE, ConflictVerdict.NONE]


async def test_conflict_self_corrects_bad_length() -> None:
    llm = ScriptedLLM(['["duplicate"]', '["duplicate","none"]'])
    judge = LLMConflictJudge(llm)
    verdicts = await judge.judge("新记忆", ["候选0", "候选1"])
    assert verdicts == [ConflictVerdict.DUPLICATE, ConflictVerdict.NONE]
    assert len(llm.calls) == 2


async def test_conflict_records_usage_to_sink() -> None:
    sink = DailyBudget(max_cost_usd=100.0, max_tokens=100_000)
    llm = ScriptedLLM(['["duplicate"]'], prompt_tokens=[300], completion_tokens=[30])
    judge = LLMConflictJudge(llm, sink=sink)
    await judge.judge("新记忆", ["候选0"])
    assert sink.tokens == 330  # 300 + 30（无 cache）
