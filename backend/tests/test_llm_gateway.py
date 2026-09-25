"""LLM 网关单测：门禁（预算/熔断）、重试、超时、记账（turn 语义）、非 run 上下文回退。"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.gateway import (
    GatewayConfig,
    LLMCircuitOpenError,
    UsageMissingError,
    run_budget,
)
from app.agent.resilience.circuit_breaker import CircuitBreaker
from app.agent.resilience.retry import Backoff, RetryPolicy
from app.agent.runtime.budget import (
    BudgetExceeded,
    BudgetTracker,
    DailyBudget,
    HardBudget,
    Usage,
)
from app.agent.snapshot import InMemorySnapshotStore
from app.integrations.embedding import FakeEmbeddingClient
from app.integrations.llm import ChatMessage, ChatResult
from app.integrations.rerank import FakeRerankerClient
from tests.fakes import FakeDailyBudgetStore, ScriptedLLM, make_gateway


class _FlakyLLM:
    """前 ``fail_times`` 次抛瞬时异常，之后成功；记录调用次数。"""

    def __init__(self, fail_times: int = 1) -> None:
        self._fail = fail_times
        self.calls = 0

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult:
        self.calls += 1
        if self.calls <= self._fail:
            raise ConnectionError("网络抖动")
        return ChatResult(content="ok", prompt_tokens=10, completion_tokens=5)

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        # 网关测试只走 complete（chat），stream 占位满足 LLMClient 协议
        yield "ok"


class _HangingLLM:
    """永远不返回的 LLM，用于超时熔断测试。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult:
        self.calls += 1
        await asyncio.sleep(10)
        return ChatResult(content="太晚了")

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        yield "ok"


def _fast_retry(attempts: int) -> RetryPolicy:
    return RetryPolicy(max_attempts=attempts, base_delay=0.0, backoff=Backoff.FIXED)


async def test_complete_records_usage_without_counting_turn() -> None:
    sink = DailyBudget(max_cost_cny=100.0, max_tokens=100_000, store=FakeDailyBudgetStore())
    tracker = BudgetTracker(HardBudget(), sink=sink)
    gateway = make_gateway(llm=_FlakyLLM(fail_times=0), retry=_fast_retry(1))

    with run_budget(tracker, run_id="r1"):
        result = await gateway.complete("review", [ChatMessage("user", "x")])

    assert result.content == "ok"
    assert tracker.turn_count == 0  # 辅助调用不计 turn
    assert sink.tokens == 15  # 10 + 5，经 tracker sink 回写日预算


async def test_invoke_model_counts_turn() -> None:
    tracker = BudgetTracker(HardBudget())
    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="hi",
                usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            )
        ]
    )
    gateway = make_gateway(retry=_fast_retry(1))

    with run_budget(tracker, run_id="r1"):
        result = await gateway.invoke_model("agent", model, [HumanMessage(content="hi")])

    assert result.content == "hi"
    assert tracker.turn_count == 1  # agent 主循环计 turn


async def test_usage_missing_fails_closed_on_real_vendor() -> None:
    """真实厂商下用量缺失（不报 token）→ 中止而非静默按 0 记账（防 token/cost 轴假死烧钱）。"""
    gateway = make_gateway(
        llm=ScriptedLLM(["ok"]),
        config=GatewayConfig(llm_vendor="deepseek", llm_model="deepseek-chat"),
    )

    with run_budget(BudgetTracker(HardBudget()), run_id="r1"):
        with pytest.raises(UsageMissingError):
            await gateway.complete("review", [ChatMessage("user", "x")])


async def test_usage_present_on_real_vendor_does_not_raise() -> None:
    """真实厂商 + 正常 token 用量 → 不触发 fail-closed，正常放行。"""
    gateway = make_gateway(
        llm=ScriptedLLM(["ok"], prompt_tokens=[10], completion_tokens=[5]),
        config=GatewayConfig(llm_vendor="deepseek", llm_model="deepseek-chat"),
    )

    with run_budget(BudgetTracker(HardBudget()), run_id="r1"):
        result = await gateway.complete("review", [ChatMessage("user", "x")])

    assert result.content == "ok"


async def test_hard_budget_stop() -> None:
    tracker = BudgetTracker(HardBudget(max_turns=0))
    gateway = make_gateway(llm=_FlakyLLM(fail_times=0), retry=_fast_retry(1))

    with run_budget(tracker, run_id="r1"):
        with pytest.raises(BudgetExceeded):
            await gateway.complete("review", [ChatMessage("user", "x")])


async def test_daily_budget_stop() -> None:
    daily = DailyBudget(max_cost_cny=100.0, max_tokens=0, store=FakeDailyBudgetStore())
    gateway = make_gateway(llm=_FlakyLLM(fail_times=0), daily_budget=daily, retry=_fast_retry(1))

    with run_budget(BudgetTracker(HardBudget()), run_id="r1"):
        with pytest.raises(BudgetExceeded):
            await gateway.complete("review", [ChatMessage("user", "x")])


async def test_retry_on_transient() -> None:
    llm = _FlakyLLM(fail_times=1)
    gateway = make_gateway(llm=llm, retry=_fast_retry(2))

    with run_budget(BudgetTracker(HardBudget()), run_id="r1"):
        result = await gateway.complete("review", [ChatMessage("user", "x")])

    assert result.content == "ok"
    assert llm.calls == 2  # 首次失败 + 一次重试成功


async def test_timeout_retries_then_raises() -> None:
    llm = _HangingLLM()
    gateway = make_gateway(llm=llm, retry=_fast_retry(2), config=GatewayConfig(timeout=0.01))

    with run_budget(BudgetTracker(HardBudget()), run_id="r1"):
        with pytest.raises(TimeoutError):
            await gateway.complete("review", [ChatMessage("user", "x")])
    assert llm.calls == 2  # 超时属瞬时可重试，重试到 max_attempts 后上抛


async def test_breaker_open_fast_fails() -> None:
    llm = _FlakyLLM(fail_times=999)  # 永远失败（ConnectionError 可重试 → 记熔断失败）
    breaker = CircuitBreaker(failure_threshold=2)
    gateway = make_gateway(llm=llm, breaker=breaker, retry=_fast_retry(1))

    with run_budget(BudgetTracker(HardBudget()), run_id="r1"):
        for _ in range(2):
            with pytest.raises(ConnectionError):
                await gateway.complete("review", [ChatMessage("user", "x")])

        # 两次失败后 breaker OPEN：第三次在预检阶段就快速失败，不再真正调用
        with pytest.raises(LLMCircuitOpenError):
            await gateway.complete("review", [ChatMessage("user", "x")])
    assert llm.calls == 2  # 第三次根本没发请求


async def test_snapshot_reuses_completed_call() -> None:
    store = InMemorySnapshotStore()
    llm = _FlakyLLM(fail_times=0)
    gateway = make_gateway(llm=llm, snapshots=store, retry=_fast_retry(1))
    messages = [ChatMessage("user", "hi")]

    with run_budget(BudgetTracker(HardBudget()), run_id="r1"):
        first = await gateway.complete("review", messages)
        second = await gateway.complete("review", messages)

    assert first.content == "ok"
    assert second.content == "ok"
    assert llm.calls == 1  # 第二次命中快照，未真正调用


async def test_snapshot_scoped_by_run_id() -> None:
    store = InMemorySnapshotStore()
    llm = _FlakyLLM(fail_times=0)
    gateway = make_gateway(llm=llm, snapshots=store, retry=_fast_retry(1))
    messages = [ChatMessage("user", "hi")]

    with run_budget(BudgetTracker(HardBudget()), run_id="r1"):
        await gateway.complete("review", messages)
    with run_budget(BudgetTracker(HardBudget()), run_id="r2"):
        await gateway.complete("review", messages)

    assert llm.calls == 2  # 不同 run 不共享快照，各自真发请求


async def test_soft_reminder_injected_at_threshold() -> None:
    daily = DailyBudget(max_cost_cny=100.0, max_tokens=100, store=FakeDailyBudgetStore())
    daily.record(Usage(input_tokens=90, output_tokens=0), cost_cny=0.0)  # 90/100 = 0.9 ≥ 0.8
    llm = ScriptedLLM(["ok"])
    gateway = make_gateway(
        llm=llm, daily_budget=daily, retry=_fast_retry(1),
        config=GatewayConfig(soft_threshold=0.8),
    )

    with run_budget(BudgetTracker(HardBudget()), run_id="r1"):
        await gateway.complete("review", [ChatMessage("user", "x")])

    assert "预算" in llm.calls[-1][-1].content  # 尾三明治提醒注入到最后一条消息


async def test_no_soft_reminder_below_threshold() -> None:
    daily = DailyBudget(max_cost_cny=100.0, max_tokens=100, store=FakeDailyBudgetStore())
    daily.record(Usage(input_tokens=10, output_tokens=0), cost_cny=0.0)  # 0.1 < 0.8
    llm = ScriptedLLM(["ok"])
    gateway = make_gateway(
        llm=llm, daily_budget=daily, retry=_fast_retry(1),
        config=GatewayConfig(soft_threshold=0.8),
    )

    with run_budget(BudgetTracker(HardBudget()), run_id="r1"):
        await gateway.complete("review", [ChatMessage("user", "x")])

    assert all("预算" not in m.content for m in llm.calls[-1])


async def test_soft_reminder_does_not_break_snapshot() -> None:
    store = InMemorySnapshotStore()
    llm = ScriptedLLM(["ok", "ok"])
    daily = DailyBudget(max_cost_cny=100.0, max_tokens=100, store=FakeDailyBudgetStore())
    gateway = make_gateway(
        llm=llm, daily_budget=daily, snapshots=store, retry=_fast_retry(1),
        config=GatewayConfig(soft_threshold=0.0),
    )
    messages = [ChatMessage("user", "hi")]

    with run_budget(BudgetTracker(HardBudget()), run_id="r1"):
        await gateway.complete("review", messages)
        await gateway.complete("review", messages)

    assert len(llm.calls) == 1  # 提醒始终注入，但指纹按原 messages 算 → 第二次命中快照


async def test_embed_records_usage_without_counting_turn() -> None:
    sink = DailyBudget(max_cost_cny=100.0, max_tokens=100_000, store=FakeDailyBudgetStore())
    tracker = BudgetTracker(HardBudget(), sink=sink)
    gateway = make_gateway(embedder=FakeEmbeddingClient(dimension=8), retry=_fast_retry(1))

    with run_budget(tracker, run_id="r1"):
        vectors = await gateway.embed("ingest", ["hello world"])

    assert len(vectors) == 1
    assert len(vectors[0]) == 8
    assert tracker.turn_count == 0  # 嵌入不计 turn
    assert sink.tokens > 0  # 粗估计入（非 0）


async def test_rerank_records_usage_without_counting_turn() -> None:
    sink = DailyBudget(max_cost_cny=100.0, max_tokens=100_000, store=FakeDailyBudgetStore())
    tracker = BudgetTracker(HardBudget(), sink=sink)
    gateway = make_gateway(reranker=FakeRerankerClient(), retry=_fast_retry(1))

    with run_budget(tracker, run_id="r1"):
        results = await gateway.rerank("rerank", "q", ["doc1", "doc2"])

    assert len(results) == 2
    assert tracker.turn_count == 0
    assert sink.tokens > 0


async def test_embed_snapshot_reuses() -> None:
    store = InMemorySnapshotStore()
    gateway = make_gateway(
        embedder=FakeEmbeddingClient(dimension=8), snapshots=store, retry=_fast_retry(1)
    )

    with run_budget(BudgetTracker(HardBudget()), run_id="r1"):
        first = await gateway.embed("ingest", ["hello"])
        second = await gateway.embed("ingest", ["hello"])

    assert first == second
    assert len(store._rows) == 1  # 只落一条快照：第二次命中缓存、不重跑嵌入
