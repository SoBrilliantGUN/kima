"""防循环：死循环（工具指纹）+ 幽灵循环（上下文 hash）。"""

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.tools import tool

from app.agent.runtime.config import RuntimeConfig
from app.agent.runtime.loop_guard import (
    InfiniteLoopDetected,
    LoopGuard,
    fingerprint,
)
from app.agent.runtime.reactive import build_reactive_graph
from tests.fakes import FakeOutputReviewer


def testfingerprint_ignores_volatile_keys() -> None:
    a = fingerprint("search", {"query": "x", "limit": 5})
    b = fingerprint("search", {"query": "x", "limit": 10})
    assert a == b
    assert a != fingerprint("search", {"query": "y"})


def test_dead_loop_detected() -> None:
    guard = LoopGuard(repeat_threshold=3)
    tool_calls = [{"name": "list_notes", "args": {}}]
    guard.check_tool_calls(tool_calls)
    guard.check_tool_calls(tool_calls)
    try:
        guard.check_tool_calls(tool_calls)
    except InfiniteLoopDetected as exc:
        assert "死循环" in str(exc)
    else:
        raise AssertionError("应抛 InfiniteLoopDetected")


def test_ghost_loop_detected() -> None:
    guard = LoopGuard(stall_threshold=3)
    messages: list[AnyMessage] = [HumanMessage(content="hi")]
    guard.check_context(messages)
    guard.check_context(messages)
    try:
        guard.check_context(messages)
    except InfiniteLoopDetected as exc:
        assert "幽灵循环" in str(exc)
    else:
        raise AssertionError("应抛 InfiniteLoopDetected")


async def test_loop_guard_terminates_graph() -> None:
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
            AIMessage(content="", tool_calls=[{"name": "dummy", "args": {}, "id": f"c{i}"}])
            for i in range(10)
        ]
    )
    graph = build_reactive_graph(
        model,
        [dummy],
        reviewer=FakeOutputReviewer(),
        runtime=RuntimeConfig(loop_guard=LoopGuard(repeat_threshold=3)),
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
    except InfiniteLoopDetected as exc:
        assert "死循环" in str(exc)
    else:
        raise AssertionError("应抛 InfiniteLoopDetected")
    assert len(calls) == 2
