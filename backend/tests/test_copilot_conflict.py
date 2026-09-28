"""记忆冲突判定：LLM 判定（duplicate/contradiction/none）+ fact 同 entity 覆盖。"""

import pytest

from app.integrations.embedding import FakeEmbeddingClient
from app.integrations.llm import StructuredParseError
from app.models.copilot import MemoryKind
from app.services.conflict import ConflictVerdict, LLMConflictJudge
from app.services.copilot import CopilotMemoryService
from tests.fakes import (
    FakeConflictJudge,
    FakeCopilotMemoryRepository,
    FakeMemoryClassifier,
    ScriptedLLM,
    gateway_run,
    make_gateway,
)


def make_service(judge: FakeConflictJudge) -> CopilotMemoryService:
    return CopilotMemoryService(
        repository=FakeCopilotMemoryRepository(),
        gateway=make_gateway(embedder=FakeEmbeddingClient(dimension=8)),
        judge=judge,
        classifier=FakeMemoryClassifier(),
        episodic_ttl_days=30,
        recency_window_days=7,
        conflict_top_k=10,
    )


async def test_duplicate_dedups_no_new_row() -> None:
    judge = FakeConflictJudge(["duplicate"])
    service = make_service(judge)
    async with gateway_run():
        first = await service.write_memory(MemoryKind.EPISODIC, "用户喜欢简洁回答")
        second = await service.write_memory(MemoryKind.EPISODIC, "用户喜欢简洁回答")

    assert second.id == first.id  # 去重：不新建行，返回旧条目
    assert first.superseded is False  # 旧条目不被废弃
    assert first.access_count == 1  # 去重即「又命中」：touch 续命
    assert judge.calls  # LLM 判定被调用（第二次写入时）


async def test_cross_kind_duplicate_does_not_dedup() -> None:
    """跨型「语义等价」不去重：情节/事实角色不同，同文本也各留一条。"""
    judge = FakeConflictJudge(["duplicate"])
    service = make_service(judge)
    async with gateway_run():
        fact = await service.write_memory(MemoryKind.FACT, "用户是副总经理")
        episode = await service.write_memory(MemoryKind.EPISODIC, "用户是副总经理")

    assert episode.id != fact.id  # 不同型不去重，照写新行
    assert fact.superseded is False  # 事实也不被废弃


async def test_contradiction_new_wins() -> None:
    judge = FakeConflictJudge(["contradiction"])
    service = make_service(judge)
    async with gateway_run():
        first = await service.write_memory(MemoryKind.EPISODIC, "用户用 Vue")
        second = await service.write_memory(MemoryKind.EPISODIC, "用户改用 React")

    assert first.superseded is True
    assert second.superseded is False


async def test_none_keeps_both() -> None:
    judge = FakeConflictJudge(["none"])
    service = make_service(judge)
    async with gateway_run():
        first = await service.write_memory(MemoryKind.EPISODIC, "事件 A")
        second = await service.write_memory(MemoryKind.EPISODIC, "事件 B")

    assert first.superseded is False
    assert second.superseded is False


async def test_fact_entity_override_increments_version() -> None:
    service = make_service(FakeConflictJudge())
    async with gateway_run():
        first = await service.write_memory(MemoryKind.FACT, "用户是经理", entity_id="user:role")
        assert first.version == 1

        second = await service.write_memory(
            MemoryKind.FACT, "用户是副总经理", entity_id="user:role"
        )
    assert second.id == first.id  # 直接覆盖同一行，不新建
    assert second.version == 2
    assert second.content == "用户是副总经理"


async def test_cross_kind_conflict_supersedes_preference() -> None:
    """跨型冲突（文章「套餐灾难」）：新情节 supersede 旧偏好。"""
    judge = FakeConflictJudge(["contradiction"])
    service = make_service(judge)
    async with gateway_run():
        preference = await service.write_memory(MemoryKind.PREFERENCE, "喜欢 VIP 免费洗车权益")
        episodic = await service.write_memory(MemoryKind.EPISODIC, "因成本控制降级为基础版")

    assert preference.superseded is True  # 情节压过偏好（跨型）
    assert preference.superseded_by == episodic.id  # 留痕：谁压了它
    assert episodic.superseded is False


async def test_soft_write_does_not_supersede_constraint() -> None:
    """约束是红线孤岛：软记忆（情节）的跨型冲突候选不含 constraint，约束不被覆盖。"""
    judge = FakeConflictJudge(["contradiction"])
    service = make_service(judge)
    async with gateway_run():
        constraint = await service.write_memory(MemoryKind.CONSTRAINT, "禁止使用 ORM")
        await service.write_memory(MemoryKind.EPISODIC, "昨天用 ORM 连了数据库")

    assert constraint.superseded is False


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


async def test_conflict_fail_closed_on_bad_length() -> None:
    llm = ScriptedLLM(['["duplicate"]'])
    judge = LLMConflictJudge(make_gateway(llm=llm))
    async with gateway_run():
        verdicts = await judge.judge("新记忆", ["候选0", "候选1"])
    assert verdicts == [ConflictVerdict.NONE, ConflictVerdict.NONE]
    assert len(llm.calls) == 1
