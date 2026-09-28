"""Loop 五宪法落地的守卫测试：全局日预算 / 审查 fail-closed / 确定性副作用对账 / 历史截断。"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.gateway import run_budget
from app.agent.guardrail.review import (
    LLMOutputReviewer,
    ReviewResult,
    ReviewVerdict,
)
from app.agent.guardrail.review_node import build_review_node, trace_from_messages
from app.agent.helpers import truncate_history_tokens
from app.agent.runtime.budget import (
    BudgetExceeded,
    BudgetTracker,
    DailyBudget,
    HardBudget,
    Usage,
)
from app.agent.side_effect import DbSideEffectVerifier
from app.integrations.embedding import FakeEmbeddingClient
from app.integrations.llm import ChatMessage, loads_json_repair
from app.services.copilot import CopilotMemoryService
from app.services.note import NoteService
from tests.fakes import (
    FakeConflictJudge,
    FakeCopilotMemoryRepository,
    FakeKnowledgeBaseRepository,
    FakeMemoryClassifier,
    FakeNoteRepository,
    FakeOutputReviewer,
    ScriptedLLM,
    make_gateway,
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


def test_daily_budget_requires_store() -> None:
    """Bug #2：DailyBudget 无 store 应报错（构造即 TypeError，避免重启清零绕过日上限）。"""
    with pytest.raises(TypeError):
        # 故意缺必填 store，验证构造拒绝（Bug #2）。
        DailyBudget(max_cost_cny=100.0)  # type: ignore[call-arg]


def test_daily_budget_token_limit() -> None:
    budget = DailyBudget(max_cost_cny=100.0, max_tokens=100, store=_FakeDailyBudgetStore())
    budget.record(Usage(input_tokens=150, output_tokens=0), cost_cny=0.0)
    with pytest.raises(BudgetExceeded):
        budget.check()


class _FakeDailyBudgetStore:
    """内存版日预算 store：`add` 原子累加增量（对齐 DB 的 ON CONFLICT 语义）。"""

    def __init__(self) -> None:
        self._rows: dict[date, tuple[float, int]] = {}

    async def load(self, day: date) -> tuple[float, int] | None:
        return self._rows.get(day)

    async def add(self, day: date, cost_cny: float, tokens: int) -> None:
        prev = self._rows.get(day)
        if prev is None:
            self._rows[day] = (cost_cny, tokens)
        else:
            self._rows[day] = (prev[0] + cost_cny, prev[1] + tokens)


async def test_daily_budget_load_and_flush() -> None:
    store = _FakeDailyBudgetStore()
    today = datetime.now(UTC).date()
    await store.add(today, 0.5, 3000)  # 模拟重启前已累计

    budget = DailyBudget(max_cost_cny=10.0, max_tokens=100_000, store=store)
    await budget.load()
    assert budget.cost_cny == 0.5
    assert budget.tokens == 3000

    budget.record(Usage(input_tokens=1000, output_tokens=0), cost_cny=0.001)
    await budget.flush()
    loaded = await store.load(today)
    assert loaded is not None
    cost, tokens = loaded
    assert tokens == 4000
    assert cost > 0.5


async def test_daily_budget_flush_sends_delta_not_absolute() -> None:
    """flush 只投递增量：并发进程各自 flush 时累加，而非「后写覆盖先写」。"""
    store = _FakeDailyBudgetStore()
    today = datetime.now(UTC).date()
    await store.add(today, 0.5, 3000)  # 已有存量

    budget = DailyBudget(max_cost_cny=10.0, max_tokens=100_000, store=store)
    await budget.load()
    budget.record(Usage(input_tokens=1000, output_tokens=0), cost_cny=0.0)
    await budget.flush()
    budget.record(Usage(input_tokens=2000, output_tokens=0), cost_cny=0.0)
    await budget.flush()  # 第二次 flush 只投递第二次增量

    loaded = await store.load(today)
    assert loaded is not None
    _, tokens = loaded
    assert tokens == 6000  # 3000 + 1000 + 2000（增量累加，不丢任何一次）


async def test_daily_budget_rollover_preserves_unflushed() -> None:
    """Bug #3：跨天时未 flush 的增量不丢，转入待清账缓冲区并 flush 到旧 day。"""
    store = _FakeDailyBudgetStore()
    budget = DailyBudget(max_cost_cny=10.0, max_tokens=100_000, store=store)
    today = datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)

    # 手动构造「昨天有未 flush 增量」的状态（白盒：直接置 day 与 unflushed）
    budget._day = yesterday
    budget._cost_cny = 0.5
    budget._tokens = 1000
    budget._unflushed_cost_cny = 0.5
    budget._unflushed_tokens = 1000

    budget._rollover()  # 跨天：旧增量转入 pending，当天归零

    assert budget._day == today
    assert budget.cost_cny == 0.0
    assert budget._pending_rollover == [(yesterday, 0.5, 1000)]

    await budget.flush()
    row = await store.load(yesterday)
    assert row == (0.5, 1000)  # 旧 day 增量被正确落库，不丢


def test_budget_tracker_forwards_to_sink() -> None:
    sink = DailyBudget(max_cost_cny=100.0, max_tokens=100_000, store=_FakeDailyBudgetStore())
    tracker = BudgetTracker(HardBudget(), sink=sink)
    tracker.record(Usage(input_tokens=1000, output_tokens=500), cost_cny=0.0)
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


async def test_reviewer_fail_closed_on_semantic_mismatch() -> None:
    """语义层 mismatch（合法 JSON 但 verdict 越界）→ 单次调用即 fail-closed，不再自纠错。"""
    llm = ScriptedLLM(['{"verdict":"oops"}'])
    reviewer = LLMOutputReviewer(make_gateway(llm=llm))
    with run_budget(BudgetTracker(HardBudget()), run_id="r1"):
        result = await reviewer.review("最终回答", "轨迹")
    assert result.verdict is ReviewVerdict.UNVERIFIED
    assert len(llm.calls) == 1


def test_loads_json_repair_fixes_malformed_json() -> None:
    """语法坏 JSON（尾逗号/未加引号 key/代码块外壳）被 json_repair 确定性修复。"""
    assert loads_json_repair('{"verdict":"ok",}') == {"verdict": "ok"}
    assert loads_json_repair('{verdict: "ok"}') == {"verdict": "ok"}
    assert loads_json_repair("```json\n{\"verdict\":\"ok\"}\n```") == {"verdict": "ok"}


def test_loads_json_repair_keeps_json_word_inside_content() -> None:
    """回归：fence 无语言标签或标签大小写不同时，内容里的 'json' 不被误删。"""
    assert loads_json_repair('```\n{"note": "export to json"}\n```') == {
        "note": "export to json"
    }
    assert loads_json_repair('```JSON\n{"json": 1}\n```') == {"json": 1}


async def test_reviewer_repairs_malformed_json_without_retry() -> None:
    """尾逗号坏 JSON 以前要靠自纠错再烧一次，现在 json_repair 修好、单次调用即 OK。"""
    llm = ScriptedLLM(['{"verdict":"ok","issues":[],}'])
    reviewer = LLMOutputReviewer(make_gateway(llm=llm))
    with run_budget(BudgetTracker(HardBudget()), run_id="r1"):
        result = await reviewer.review("最终回答", "轨迹")
    assert result.verdict is ReviewVerdict.OK
    assert len(llm.calls) == 1


async def test_reviewer_propagates_budget_exceeded() -> None:
    """预算硬停（BudgetExceeded）应上抛中止 run，不被吞成 UNVERIFIED（决策 D2「该停就停」）。"""
    llm = ScriptedLLM(['{"verdict":"ok","issues":[]}'])
    gateway = make_gateway(llm=llm)
    reviewer = LLMOutputReviewer(gateway)
    # turn_count 0 >= 0 → 预检即超限
    with run_budget(BudgetTracker(HardBudget(max_turns=0)), run_id="r1"):
        with pytest.raises(BudgetExceeded):
            await reviewer.review("最终回答", "轨迹")
    assert len(llm.calls) == 0  # 预检即中止，根本没调 LLM


def testtrace_from_messages_pairs_calls_with_results_by_id() -> None:
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
    trace = trace_from_messages(messages)
    assert 'create_note({"title": "A"}) → 创建成功' in trace
    assert 'create_note({"title": "B"}) → 创建失败' in trace


def testtrace_from_messages_marks_missing_result() -> None:
    """某次调用没回 ToolMessage（如中断）时，应显式标「未返回」而非静默错位。"""
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "create_note", "args": {"title": "A"}, "id": "c1"}],
        ),
    ]
    trace = trace_from_messages(messages)
    assert 'create_note({"title": "A"}) → （未返回）' in trace


def testtrace_from_messages_keeps_orphan_results_in_stream_order() -> None:
    """配不上任何调用的孤儿返回，应夹在它在流中的位置，而非甩到末尾。"""
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "create_note", "args": {"title": "A"}, "id": "c1"}],
        ),
        ToolMessage(content="无主返回", tool_call_id="missing"),  # id 配不到任何调用
        AIMessage(
            content="",
            tool_calls=[{"name": "create_note", "args": {"title": "B"}, "id": "c2"}],
        ),
        ToolMessage(content="已创建 B", tool_call_id="c2"),
        ToolMessage(content="已创建 A", tool_call_id="c1"),
    ]
    lines = trace_from_messages(messages).splitlines()
    assert lines[0] == "工具轨迹："
    assert lines[1].startswith('- create_note({"title": "A"})')
    assert lines[2] == "- unknown → 无主返回"
    assert lines[3].startswith('- create_note({"title": "B"})')


def testtrace_from_messages_keeps_nameless_call_paired() -> None:
    """无名调用不跳过，渲染为 unknown(args) 并按 id 配对。"""
    messages = [
        AIMessage(content="", tool_calls=[{"name": "", "args": {"title": "X"}, "id": "c1"}]),
        ToolMessage(content="已创建 X", tool_call_id="c1"),
    ]
    trace = trace_from_messages(messages)
    assert 'unknown({"title": "X"}) → 已创建 X' in trace


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
    result = await node(_write_state(), {"configurable": {"thread_id": "t1"}})
    assert result["review_verdict"] == "unverified"
    assert result["correction"]


async def test_side_effect_verifier_note() -> None:
    kb_repo = FakeKnowledgeBaseRepository()
    note_service = NoteService(FakeNoteRepository(), kb_repo)
    memory_service = CopilotMemoryService(
        repository=FakeCopilotMemoryRepository(),
        gateway=make_gateway(embedder=FakeEmbeddingClient(dimension=8)),
        judge=FakeConflictJudge(),
        classifier=FakeMemoryClassifier(),
        episodic_ttl_days=30,
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


def testtruncate_history_tokens() -> None:
    msgs = [
        ChatMessage("user", "甲" * 100),
        ChatMessage("assistant", "乙" * 100),
    ]
    trimmed = truncate_history_tokens(msgs, budget=120)
    assert len(trimmed) == 1
    assert trimmed[0].content == "乙" * 100
    assert trimmed[0].role == "assistant"


async def test_side_effect_verifier_memory_missing() -> None:
    kb_repo = FakeKnowledgeBaseRepository()
    note_service = NoteService(FakeNoteRepository(), kb_repo)
    memory_service = CopilotMemoryService(
        repository=FakeCopilotMemoryRepository(),
        gateway=make_gateway(embedder=FakeEmbeddingClient(dimension=8)),
        judge=FakeConflictJudge(),
        classifier=FakeMemoryClassifier(),
        episodic_ttl_days=30,
        recency_window_days=7,
        conflict_top_k=10,
    )
    verifier = DbSideEffectVerifier(note_service, memory_service)

    missing = await verifier.verify(
        "write_memory",
        {"kind": "fact", "content": "x"},
        f"已写入 fact 记忆 {uuid.uuid4()}。",
    )
    assert missing is not None

    # soul/user 覆盖写（update_profile）不做 DB 对账
    skipped = await verifier.verify(
        "update_profile", {"kind": "soul", "content": "x"}, "已更新 soul 档案。"
    )
    assert skipped is None
