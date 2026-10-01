"""错误分级 + 退避重试 + 幂等键稳定序号 + 熔断持久化/级联 + last_error 分类。

对照《Agent 挂了，如何不丢状态、不丢钱、不丢数据地拉起来？》的三大容错：
- exactly-once：幂等键必须来自持久化稳定序号，而非「消息内下标」或「运行时局部变量」；
- 动作指纹持久化：熔断失败计数跨崩溃不归零；
- 级联熔断：共享依赖挂 → 同资源工具一起熔断；
- 崩溃现场：last_error 分类标签随检查点持久化。
"""

import time
from typing import cast

from langchain_core.messages import AIMessage, ToolMessage

from app.agent.resilience.circuit_breaker import CircuitBreaker
from app.agent.resilience.error_classifier import classify_error, is_retryable
from app.agent.resilience.result import ToolFailure, ToolOutcome
from app.agent.resilience.retry import Backoff, RetryPolicy, with_retry
from app.agent.runtime.loop_guard import fingerprint
from app.agent.runtime.planner import Plan, PlanStep
from app.agent.runtime.reactive_helpers import (
    blocked_permanent_calls,
    failed_tool_call,
    inject_idempotency_keys,
)
from app.agent.runtime.state import AgentState
from app.agent.toolmeta import SideEffectLevel, ToolMeta
from app.core.exceptions import NotFoundError
from app.repositories.breaker import InMemoryBreakerStore


def test_is_retryable() -> None:
    assert is_retryable(TimeoutError()) is True
    assert is_retryable(ConnectionError()) is True
    assert is_retryable(OSError()) is True
    assert is_retryable(NotFoundError("x")) is False
    assert is_retryable(ValueError("x")) is False


async def test_with_retry_retries_transient() -> None:
    attempts = 0
    policy = RetryPolicy(max_attempts=3, base_delay=0.0, backoff=Backoff.FIXED)

    @with_retry(policy)
    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("boom")
        return "ok"

    assert await flaky() == "ok"
    assert attempts == 3


async def test_with_retry_no_retry_permanent() -> None:
    attempts = 0
    policy = RetryPolicy(max_attempts=3, base_delay=0.0, backoff=Backoff.FIXED)

    @with_retry(policy)
    async def broken() -> str:
        nonlocal attempts
        attempts += 1
        raise NotFoundError("不存在")

    try:
        await broken()
    except NotFoundError:
        pass
    else:
        raise AssertionError("应抛 NotFoundError")
    assert attempts == 1


# —— 幂等键稳定序号 / 熔断持久化 / 级联 / last_error 分类 ——

# 两个强制幂等的写工具（create_note / write_memory），用于幂等键派生回归
_WRITE_REGISTRY = {
    "create_note": ToolMeta(
        "create_note",
        "新建笔记",
        SideEffectLevel.MEDIUM,
        "tool_result",
        500,
        True,
        idempotency_key_fields=("content",),
    ),
    "write_memory": ToolMeta(
        "write_memory",
        "写长期记忆",
        SideEffectLevel.MEDIUM,
        "tool_result",
        5000,
        True,
        idempotency_key_fields=("kind", "content", "entity_id"),
    ),
}


def _idempotency_key(state: AgentState) -> str:
    last = state["messages"][-1]
    assert isinstance(last, AIMessage)
    return str(last.tool_calls[0]["args"]["idempotency_key"])


def test_idempotency_keys_content_derived_stable() -> None:
    """P0-1 回归：幂等键由「业务意图」（key_fields 内容哈希）派生，而非位置序号。

    同一内容跨轮/重发拿到同一个键（快路径去重能命中）；不同内容键不同；键含 tool_name
    与 run_id 前缀（跨工具/跨 run 隔离）。非 key_fields 的参数（如 title）不影响键。
    """
    run_id = "r1"
    turn1 = cast(
        AgentState,
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "create_note",
                            "args": {"title": "a", "content": "正文一"},
                            "id": "c1",
                        },
                    ],
                )
            ]
        },
    )
    s1 = inject_idempotency_keys(turn1, run_id, _WRITE_REGISTRY)
    key1 = _idempotency_key(s1)
    assert key1.startswith(f"{run_id}:create_note:")

    # 同内容、不同标题（非 key_fields）→ 同一键（业务身份只看 content）
    turn1b = cast(
        AgentState,
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "create_note",
                            "args": {"title": "b", "content": "正文一"},
                            "id": "c1b",
                        },
                    ],
                )
            ]
        },
    )
    s1b = inject_idempotency_keys(turn1b, run_id, _WRITE_REGISTRY)
    assert _idempotency_key(s1b) == key1

    # 不同内容 → 不同键
    turn2 = cast(
        AgentState,
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "create_note",
                            "args": {"title": "a", "content": "正文二"},
                            "id": "c2",
                        },
                    ],
                )
            ]
        },
    )
    s2 = inject_idempotency_keys(turn2, run_id, _WRITE_REGISTRY)
    assert _idempotency_key(s2) != key1


def test_plan_roundtrip_preserves_last_error() -> None:
    """P0-2：plan 检查点序列化往返保留 last_error（崩溃现场，含工具名）。"""
    plan = Plan(steps=(PlanStep(step_id="1", action="create_note", params={}),))
    plan.last_error = {"tool": "create_note", "message": "boom", "kind": "permanent"}
    restored = Plan.from_dict(plan.to_dict())
    assert restored.last_error == {"tool": "create_note", "message": "boom", "kind": "permanent"}


def test_classify_error_transient_vs_permanent() -> None:
    """崩溃现场（第五层）的分类标签：瞬时可重试 / 永久别重试。"""
    assert classify_error(ConnectionError("x")) == "transient"
    assert classify_error(TimeoutError()) == "transient"
    assert classify_error(ToolFailure(outcome=ToolOutcome.TRANSIENT, reason="r")) == "transient"
    assert classify_error(ToolFailure(outcome=ToolOutcome.PERMANENT, reason="r")) == "permanent"
    assert classify_error(NotFoundError("不存在")) == "permanent"


def test_failed_tool_call() -> None:
    """崩溃现场补失败 tool_call（含 name/args）：从 raw result 反查失败 ToolMessage。"""
    state = cast(
        AgentState,
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "read_note", "args": {"document_id": "d1"}, "id": "c1"},
                        {"name": "search_web", "args": {"query": "x"}, "id": "c2"},
                    ],
                )
            ]
        },
    )
    # 最后一个失败的是 search_web（Error: 前缀识别）
    result = {
        "messages": [
            ToolMessage(content="已找到 3 条", tool_call_id="c1"),
            ToolMessage(content="Error: timeout", tool_call_id="c2"),
        ]
    }
    failed = failed_tool_call(state, result)
    assert failed is not None
    assert failed["name"] == "search_web"
    assert failed["args"] == {"query": "x"}
    # 全成功 → None
    ok = {
        "messages": [
            ToolMessage(content="已找到 3 条", tool_call_id="c1"),
            ToolMessage(content="共 1 条结果", tool_call_id="c2"),
        ]
    }
    assert failed_tool_call(state, ok) is None


def test_blocked_permanent_calls() -> None:
    """崩溃恢复/防死循环：permanent 且同工具+同参数（fingerprint 匹配）才拦截。

    换参数/transient/混合放行。
    """
    calls = [{"name": "read_note", "args": {"document_id": "d1"}, "id": "c1"}]
    permanent = cast(
        AgentState,
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "read_note", "args": {"document_id": "d1"}, "id": "c1"}],
                )
            ],
            "last_error": {
                "tool": "read_note",
                "kind": "permanent",
                "message": "不存在",
                "fingerprint": fingerprint("read_note", {"document_id": "d1"}),
            },
        },
    )
    blocked = blocked_permanent_calls(permanent, calls)
    assert len(blocked) == 1
    assert blocked[0].tool_call_id == "c1"
    assert "请勿重复重试" in str(blocked[0].content)

    # 换参数（同工具不同 id）→ 放行（合理重试，不误伤）
    changed = [{"name": "read_note", "args": {"document_id": "d2"}, "id": "c2"}]
    assert blocked_permanent_calls(permanent, changed) == []

    # transient → 放行（重试）
    transient = cast(
        AgentState,
        {
            "messages": permanent["messages"],
            "last_error": {
                "tool": "read_note",
                "kind": "transient",
                "message": "超时",
                "fingerprint": fingerprint("read_note", {"document_id": "d1"}),
            },
        },
    )
    assert blocked_permanent_calls(transient, calls) == []

    # 混合场景（本轮含其他工具）→ 不整轮拦截
    mixed = [
        {"name": "read_note", "args": {"document_id": "d1"}, "id": "c1"},
        {"name": "search_web", "args": {"query": "x"}, "id": "c2"},
    ]
    assert blocked_permanent_calls(permanent, mixed) == []

    # 无 last_error → 放行
    assert (
        blocked_permanent_calls(cast(AgentState, {"messages": permanent["messages"]}), calls)
        == []
    )


async def test_breaker_load_restores_failure_count() -> None:
    """熔断失败计数跨崩溃不归零：崩溃前已失败 2 次，恢复后第 3 次即熔断（而非从头算）。"""
    store = InMemoryBreakerStore()
    # 模拟崩溃前落库的失败计数（threshold=3，还差 1 次即熔断）
    await store.save("flaky", "closed", [time.time(), time.time()])

    breaker = CircuitBreaker(failure_threshold=3, store=store)
    await breaker.load_all()
    assert breaker.is_available("flaky") is True  # 尚未到阈值

    breaker.record_failure("flaky")  # 恢复后第 3 次 → OPEN
    assert breaker.is_available("flaky") is False


def test_breaker_cascade_resource_trips_all_tools() -> None:
    """级联熔断：共享依赖（db）挂 → 同资源所有工具一起熔断，不同资源不受影响。"""
    breaker = CircuitBreaker(failure_threshold=2)
    # 两个 db 工具各自失败 1 次，db 资源累计 2 次 → resource:db 熔断
    breaker.record_failure("search_kb", resource="db")
    breaker.record_failure("list_notes", resource="db")

    assert breaker.is_available("search_kb", "db") is False
    assert breaker.is_available("read_doc", "db") is False  # 自己没失败过，但被资源拖累
    assert breaker.is_available("search_web", "web") is True  # 非 db 不受影响

    # available() 按资源过滤：db 工具全部剔除
    remaining = breaker.available(
        ["search_kb", "read_doc", "search_web"],
        {"search_kb": "db", "read_doc": "db", "search_web": "web"},
    )
    assert remaining == ["search_web"]
