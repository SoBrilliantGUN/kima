"""四轴预算：BudgetTracker 预检 / 记录 / 计费 + 图内终止。"""

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from app.agent.runtime.budget import (
    BudgetExceeded,
    BudgetTracker,
    HardBudget,
    RunAccounting,
    Usage,
    extract_usage,
)
from tests.fakes import FakeOutputReviewer, make_reactive_graph, make_runtime_config


def test_extract_usage() -> None:
    usage = extract_usage(
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "input_token_details": {"cache_read": 3},
        }
    )
    assert usage.input_tokens == 10
    assert usage.output_tokens == 5
    assert usage.cache_read_tokens == 3
    assert usage.billable_tokens == 12


def test_extract_usage_missing() -> None:
    assert extract_usage(None).billable_tokens == 0


def test_extract_usage_deepseek_cache_fallback() -> None:
    """DeepSeek 缓存命中是 usage 顶层 prompt_cache_hit_tokens，需从 raw_usage 兜底读。"""
    usage = extract_usage(
        {"input_tokens": 10_000, "output_tokens": 500},
        {"prompt_cache_hit_tokens": 8_000, "prompt_cache_miss_tokens": 2_000},
    )
    assert usage.cache_read_tokens == 8_000
    assert usage.billable_tokens == 2_500  # 10000 + 500 - 8000


def test_tracker_check_turns() -> None:
    tracker = BudgetTracker(HardBudget(max_turns=0))
    try:
        tracker.check()
    except BudgetExceeded as exc:
        assert "turns" in str(exc)
    else:
        raise AssertionError("应抛 BudgetExceeded")


def test_tracker_check_skips_seconds_axis() -> None:
    """seconds 轴不进 check()：时间超限由 reactive 节点的 asyncio.wait_for 硬熔断。

    check() 只预检 turns/tokens/cost/tool_calls 这些离散轴；seconds 是连续轴，靠
    ``wait_for(remaining_seconds())`` 在调用途中熔断，而非进模型前预检（见 budget.py
    里 ``# 时间使用asyncio.wait_for硬熔断`` 的注释）。
    """
    tracker = BudgetTracker(HardBudget(max_seconds=0.0))
    tracker.check()  # 不抛 BudgetExceeded：seconds 不参与 check() 预检


def test_tracker_record_accumulates() -> None:
    tracker = BudgetTracker(HardBudget(max_tokens=1000))
    tracker.record(Usage(input_tokens=100, output_tokens=50), cost_cny=0.0)
    tracker.record(Usage(input_tokens=100, output_tokens=50), cost_cny=0.0)
    assert tracker.turn_count == 2


def test_tracker_summary_and_cache_hit_rate() -> None:
    tracker = BudgetTracker(HardBudget(max_tokens=1000))
    tracker.record(Usage(input_tokens=100, output_tokens=50), cost_cny=0.0)
    tracker.record(Usage(input_tokens=400, output_tokens=100, cache_read_tokens=200), cost_cny=0.0)
    tracker.record_tool_calls(2)
    summary = tracker.summary()
    assert summary.input_tokens == 500
    assert summary.output_tokens == 150
    assert summary.cache_read_tokens == 200
    assert summary.billable_tokens == 450
    assert summary.turn_count == 2
    assert summary.tool_call_count == 2
    assert summary.cache_hit_rate == 0.4
    data = summary.to_dict()
    assert data["cost_cny"] >= 0.0
    assert data["tokens"] == 450
    assert data["cache_hit_rate"] == 0.4
    assert data["turn_count"] == 2


def test_run_accounting_cache_hit_rate_none_when_no_input() -> None:
    tracker = BudgetTracker(HardBudget(max_tokens=1000))
    tracker.record(Usage(input_tokens=0, output_tokens=10), cost_cny=0.0)
    accounting = tracker.summary()
    assert isinstance(accounting, RunAccounting)
    assert accounting.cache_hit_rate is None
    assert accounting.to_dict()["cache_hit_rate"] is None


async def test_budget_terminates_graph() -> None:
    calls: list[str] = []

    @tool
    async def dummy() -> str:
        """测试用工具：记录调用并返回 ok。"""
        calls.append("dummy")
        return "ok"

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools: object, **kwargs: object) -> "Model":
            return self

    model = Model(
        responses=[
            AIMessage(content="", tool_calls=[{"name": "dummy", "args": {}, "id": "c1"}]),
            AIMessage(content="", tool_calls=[{"name": "dummy", "args": {}, "id": "c2"}]),
        ]
    )
    graph = make_reactive_graph(
        model,
        [dummy],
        reviewer=FakeOutputReviewer(),
        runtime=make_runtime_config(budget=HardBudget(max_turns=1)),
    )
    initial = {
        "messages": [HumanMessage(content="hi")],
        "attempts": 0,
        "review_verdict": "",
        "review_issues": [],
        "correction": "",
    }
    try:
        async for _ in graph.astream(initial, config={"configurable": {"thread_id": "t1"}}, stream_mode="updates"):
            pass
    except BudgetExceeded as exc:
        assert "turns" in str(exc)
    else:
        raise AssertionError("应抛 BudgetExceeded")
    assert calls == ["dummy"]
