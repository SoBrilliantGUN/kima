"""工具熔断：CLOSED → OPEN → HALF_OPEN 状态机 + 快速失败 + 候选集剔除。"""

from typing import Any, cast

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.tools import tool

from app.agent.resilience.circuit_breaker import CircuitBreaker, with_circuit_breaker
from app.agent.runtime.reactive import build_reactive_graph
from tests.fakes import FakeOutputReviewer


def test_breaker_opens_after_failures() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout_seconds=60.0)
    assert breaker.is_available("t") is True
    breaker.record_failure("t")
    assert breaker.is_available("t") is True
    breaker.record_failure("t")
    assert breaker.is_available("t") is False  # OPEN


def test_breaker_half_open_recover() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.0)
    breaker.record_failure("t")  # OPEN
    assert breaker.is_available("t") is True  # 立即 HALF_OPEN
    breaker.record_success("t")  # CLOSED
    assert breaker.is_available("t") is True


async def test_with_circuit_breaker_fails_fast() -> None:
    breaker = CircuitBreaker(failure_threshold=1)
    calls = 0

    @with_circuit_breaker(breaker)
    async def flaky() -> str:
        nonlocal calls
        calls += 1
        raise ConnectionError("boom")

    try:
        await flaky()
    except ConnectionError:
        pass
    assert calls == 1

    result = await flaky()
    assert "熔断" in result
    assert calls == 1  # 底层未被再次调用


def test_breaker_available_filters_open_tools() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=60.0)
    breaker.record_failure("bad")
    assert breaker.available(["bad", "good"]) == ["good"]


def test_reactive_graph_filters_broken_tool_from_bind() -> None:
    """注意力稀释铁律 #5：熔断工具应从 bind_tools 候选集剔除，模型根本看不到它。"""

    @tool
    async def good_tool(x: str) -> str:
        """好工具。"""
        return "ok"

    @tool
    async def bad_tool(x: str) -> str:
        """坏工具。"""
        return "ok"

    bound: list[list[str]] = []

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools: object, **kwargs: object) -> "Model":
            bound.append([t.name for t in cast(list[Any], tools)])
            return self

    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=60.0)
    breaker.record_failure("bad_tool")  # OPEN

    build_reactive_graph(
        Model(responses=[]), [good_tool, bad_tool], reviewer=FakeOutputReviewer(), breaker=breaker
    )

    assert bound and "bad_tool" not in bound[0]
    assert "good_tool" in bound[0]
