"""四轴预算：BudgetTracker 预检 / 记录 / 计费 + 图内终止。"""

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from app.agent.runtime.budget import (
    BudgetExceeded,
    BudgetTracker,
    HardBudget,
    TokenPricing,
    Usage,
    compute_cost,
    extract_usage,
)
from app.agent.runtime.config import RuntimeConfig
from app.agent.runtime.reactive import build_reactive_graph


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


def test_compute_cost() -> None:
    pricing = TokenPricing(input_per_m=1.0, cache_read_per_m=0.5, output_per_m=2.0)
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=0)
    assert compute_cost(usage, pricing) == 3.0


def test_tracker_check_turns() -> None:
    tracker = BudgetTracker(HardBudget(max_turns=0))
    try:
        tracker.check()
    except BudgetExceeded as exc:
        assert "turns" in str(exc)
    else:
        raise AssertionError("应抛 BudgetExceeded")


def test_tracker_check_seconds() -> None:
    tracker = BudgetTracker(HardBudget(max_seconds=0.0))
    try:
        tracker.check()
    except BudgetExceeded as exc:
        assert "seconds" in str(exc)
    else:
        raise AssertionError("应抛 BudgetExceeded")


def test_tracker_record_accumulates() -> None:
    tracker = BudgetTracker(HardBudget(max_tokens=1000))
    tracker.record(Usage(input_tokens=100, output_tokens=50))
    tracker.record(Usage(input_tokens=100, output_tokens=50))
    assert tracker.turn_count == 2


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
    graph = build_reactive_graph(
        model, [dummy], runtime=RuntimeConfig(budget=HardBudget(max_turns=1))
    )
    initial = {
        "messages": [HumanMessage(content="hi")],
        "attempts": 0,
        "review_verdict": "",
        "review_issues": [],
        "correction": "",
    }
    try:
        async for _ in graph.astream(initial, stream_mode="updates"):
            pass
    except BudgetExceeded as exc:
        assert "turns" in str(exc)
    else:
        raise AssertionError("应抛 BudgetExceeded")
    assert calls == ["dummy"]
