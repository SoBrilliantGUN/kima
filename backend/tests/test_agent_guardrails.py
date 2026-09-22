"""Loop 五宪法落地的守卫测试：全局日预算 / 审查 fail-closed / 确定性副作用对账 / 历史截断。"""

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.guardrail.review import (
    LLMOutputReviewer,
    ReviewResult,
    ReviewVerdict,
)
from app.agent.guardrail.review_node import _trace_from_messages, build_review_node
from app.agent.runtime.budget import (
    BudgetExceeded,
    BudgetTracker,
    DailyBudget,
    HardBudget,
    Usage,
)
from app.agent.service import DbSideEffectVerifier, _truncate_history_tokens
from app.integrations.embedding import FakeEmbeddingClient
from app.integrations.llm import ChatMessage
from app.services.copilot import CopilotMemoryService
from app.services.note import NoteService
from tests.fakes import (
    FakeConflictJudge,
    FakeCopilotMemoryRepository,
    FakeKnowledgeBaseRepository,
    FakeNoteRepository,
    FakeOutputReviewer,
    ScriptedLLM,
)


class _FailingVerifier:
    """确定性对账器替身：对任何写工具返回失败原因（模拟「声称成功但副作用未落库」）。"""

    async def verify(self, tool_name: str, args: dict[str, Any], result: str) -> str:
        return f"{tool_name} 副作用缺失"


def _write_state() -> dict[str, Any]:
    """构造一次「写笔记 + 成功返回 + 最终回答」的消息流，供 review 节点测试。"""
    return {
        "messages": [
            HumanMessage(content="写个笔记"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "create_note", "args": {"title": "t", "content": "c"}, "id": "c1"}
                ],
            ),
            ToolMessage(
                content="已创建笔记 00000000-0000-0000-0000-000000000001（标题：t）",
                tool_call_id="c1",
            ),
            AIMessage(content="写好了。"),
        ],
        "attempts": 0,
        "review_verdict": "",
        "review_issues": [],
        "correction": "",
    }


def test_daily_budget_token_limit() -> None:
    budget = DailyBudget(max_cost_usd=100.0, max_tokens=100)
    budget.record(Usage(input_tokens=150, output_tokens=0))
    with pytest.raises(BudgetExceeded):
        budget.check()


class _FakeDailyBudgetStore:
    """内存版日预算 store，记录 save 调用供断言。"""

    def __init__(self) -> None:
        self._rows: dict[date, tuple[float, int]] = {}

    async def load(self, day: date) -> tuple[float, int] | None:
        return self._rows.get(day)

    async def save(self, day: date, cost_usd: float, tokens: int) -> None:
        self._rows[day] = (cost_usd, tokens)


async def test_daily_budget_load_and_flush() -> None:
    store = _FakeDailyBudgetStore()
    today = datetime.now(UTC).date()
    await store.save(today, 0.5, 3000)  # 模拟重启前已累计

    budget = DailyBudget(max_cost_usd=10.0, max_tokens=100_000, store=store)
    await budget.load()
    assert budget.cost_usd == 0.5
    assert budget.tokens == 3000

    budget.record(Usage(input_tokens=1000, output_tokens=0))
    await budget.flush()
    loaded = await store.load(today)
    assert loaded is not None
    cost, tokens = loaded
    assert tokens == 4000
    assert cost > 0.5


def test_budget_tracker_forwards_to_sink() -> None:
    sink = DailyBudget(max_cost_usd=100.0, max_tokens=100_000)
    tracker = BudgetTracker(HardBudget(), sink=sink)
    tracker.record(Usage(input_tokens=1000, output_tokens=500))
    assert sink.tokens == 1500


def test_reviewer_parse_fail_closed() -> None:
    assert LLMOutputReviewer._parse("这不是 JSON").verdict is ReviewVerdict.UNVERIFIED
    assert LLMOutputReviewer._parse('{"verdict":"ok","issues":[]}').verdict is ReviewVerdict.OK


def test_reviewer_parse_fail_closed_on_structural_mismatch() -> None:
    """结构/语义层 mismatch（合法 JSON 但形状错）必须 fail-closed，不能默认 OK。"""
    assert LLMOutputReviewer._parse("{}").verdict is ReviewVerdict.UNVERIFIED
    assert LLMOutputReviewer._parse('{"verdict":"maybe"}').verdict is ReviewVerdict.UNVERIFIED
    assert LLMOutputReviewer._parse('{"verdicts":"mismatch"}').verdict is ReviewVerdict.UNVERIFIED
    assert (
        LLMOutputReviewer._parse('{"verdict":"ok","issues":"oops"}').verdict
        is ReviewVerdict.UNVERIFIED
    )
    # 容忍大小写/首尾空格（细微格式偏差不算结构失败）
    assert LLMOutputReviewer._parse('{"verdict":" OK "}').verdict is ReviewVerdict.OK


async def test_reviewer_self_corrects_parse_failure() -> None:
    llm = ScriptedLLM(
        [
            '{"verdict":"oops"}',
            '{"verdict":"mismatch","issues":[{"claim":"已写入","tool":"write_memory",'
            '"evidence":"no_tool_call"}]}',
        ]
    )
    reviewer = LLMOutputReviewer(llm)
    result = await reviewer.review("最终回答", "轨迹")
    assert result.verdict is ReviewVerdict.MISMATCH
    assert len(llm.calls) == 2
    # 第二次调用带上了精确错误反馈
    assert "校验失败" in llm.calls[-1][-1].content


async def test_reviewer_records_usage_to_sink() -> None:
    sink = DailyBudget(max_cost_usd=100.0, max_tokens=100_000)
    llm = ScriptedLLM(['{"verdict":"ok","issues":[]}'], prompt_tokens=[500], completion_tokens=[50])
    reviewer = LLMOutputReviewer(llm, sink=sink)
    await reviewer.review("最终回答", "轨迹")
    assert sink.tokens == 550  # 500 + 50（无 cache）


def test_trace_from_messages_pairs_calls_with_results_by_id() -> None:
    """同名工具多次调用时，返回必须按 tool_call_id 配到对应调用，而非按位置对齐。"""
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "create_note", "args": {"title": "A"}, "id": "c1"},
                {"name": "create_note", "args": {"title": "B"}, "id": "c2"},
            ],
        ),
        # 故意打乱返回顺序：位置对齐会把「创建失败」误配给 A
        ToolMessage(content="创建失败", tool_call_id="c2"),
        ToolMessage(content="创建成功", tool_call_id="c1"),
    ]
    trace = _trace_from_messages(messages)
    assert 'create_note({"title": "A"}) → 创建成功' in trace
    assert 'create_note({"title": "B"}) → 创建失败' in trace


def test_trace_from_messages_marks_missing_result() -> None:
    """某次调用没回 ToolMessage（如中断）时，应显式标「未返回」而非静默错位。"""
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "create_note", "args": {"title": "A"}, "id": "c1"}],
        ),
    ]
    trace = _trace_from_messages(messages)
    assert 'create_note({"title": "A"}) → （未返回）' in trace


async def test_review_node_verifier_forces_mismatch() -> None:
    """确定性对账失败必须强制 mismatch，即使 LLM 审查器判 ok（防「伪造证据骗校验器」）。"""
    reviewer = FakeOutputReviewer(results=[ReviewResult(verdict=ReviewVerdict.OK, issues=[])])
    node = build_review_node(
        reviewer,
        review_max_attempts=2,
        verifier=_FailingVerifier(),
        write_tool_names=frozenset({"create_note", "write_memory", "update_profile"}),
    )
    result = await node(_write_state())
    assert result["review_verdict"] == "mismatch"
    assert result["review_issues"][0]["evidence"] == "side_effect_missing"


async def test_review_node_unverified_correction() -> None:
    """审查器失能（UNVERIFIED）应 fail-closed 追加诚实更正，而不是放行。"""
    reviewer = FakeOutputReviewer(
        results=[ReviewResult(verdict=ReviewVerdict.UNVERIFIED, issues=[])]
    )
    node = build_review_node(reviewer, review_max_attempts=2)
    result = await node(_write_state())
    assert result["review_verdict"] == "unverified"
    assert result["correction"]


async def test_side_effect_verifier_note() -> None:
    kb_repo = FakeKnowledgeBaseRepository()
    note_service = NoteService(FakeNoteRepository(), kb_repo)
    memory_service = CopilotMemoryService(
        repository=FakeCopilotMemoryRepository(),
        embedder=FakeEmbeddingClient(dimension=8),
        judge=FakeConflictJudge(),
        capacity=200,
        episodic_ttl_days=30,
        recall_floor=0.05,
        recency_window_days=7,
        conflict_top_k=10,
    )
    verifier = DbSideEffectVerifier(note_service, memory_service)

    # 不存在的 id → 判副作用缺失
    missing = await verifier.verify(
        "create_note",
        {"title": "t", "content": "c"},
        "已创建笔记 00000000-0000-0000-0000-000000000001（标题：t）",
    )
    assert missing is not None

    # 真实落库的笔记 → 确认通过
    note = await note_service.create_with_content("t", "c")
    ok = await verifier.verify(
        "create_note", {"title": "t", "content": "c"}, f"已创建笔记 {note.id}（标题：t）"
    )
    assert ok is None

    # 明确失败 → 跳过（交给 LLM 审查器）
    skipped = await verifier.verify("create_note", {}, "创建失败：boom")
    assert skipped is None


def test_truncate_history_tokens() -> None:
    msgs = [
        ChatMessage("user", "甲" * 100),
        ChatMessage("assistant", "乙" * 100),
    ]
    trimmed = _truncate_history_tokens(msgs, budget=120)
    assert len(trimmed) == 1
    assert trimmed[0].content == "乙" * 100
    assert trimmed[0].role == "assistant"


async def test_side_effect_verifier_memory_missing() -> None:
    kb_repo = FakeKnowledgeBaseRepository()
    note_service = NoteService(FakeNoteRepository(), kb_repo)
    memory_service = CopilotMemoryService(
        repository=FakeCopilotMemoryRepository(),
        embedder=FakeEmbeddingClient(dimension=8),
        judge=FakeConflictJudge(),
        capacity=200,
        episodic_ttl_days=30,
        recall_floor=0.05,
        recency_window_days=7,
        conflict_top_k=10,
    )
    verifier = DbSideEffectVerifier(note_service, memory_service)

    missing = await verifier.verify(
        "write_memory",
        {"kind": "semantic", "content": "x"},
        f"已写入 semantic 记忆 {uuid.uuid4()}。",
    )
    assert missing is not None

    # soul/user 覆盖写（update_profile）不做 DB 对账
    skipped = await verifier.verify(
        "update_profile", {"kind": "soul", "content": "x"}, "已更新 soul 档案。"
    )
    assert skipped is None
