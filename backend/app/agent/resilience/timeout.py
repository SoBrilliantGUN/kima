"""工具超时（反馈契约）：per-tool 硬熔断。

LLM 对延迟很敏感，一个卡住的工具会让整轮对话僵死。与四轴预算里的秒轴（LLM 调用级
``asyncio.wait_for``）互补：这里管「单个工具执行」的超时，超时上限由 ToolMeta 的
``estimated_latency_ms`` × LATENCY_TIMEOUT_FACTOR 决定。超时抛黄灯 ToolFailure
（TRANSIENT），由 with_retry 退避重试。
"""

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, TypeVar, cast

from app.agent.resilience.result import ToolFailure, ToolOutcome

_Fn = TypeVar("_Fn", bound=Callable[..., Awaitable[Any]])


def with_timeout(seconds: float) -> Callable[[_Fn], _Fn]:
    """给 async 函数加超时硬熔断：超时抛黄灯 ToolFailure，可被 with_retry 重试。"""

    def decorator(fn: _Fn) -> _Fn:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await asyncio.wait_for(fn(*args, **kwargs), timeout=seconds)
            except TimeoutError as exc:
                raise ToolFailure(
                    outcome=ToolOutcome.TRANSIENT,
                    reason=f"工具执行超时（>{seconds:.0f}s 未返回）",
                    hint="该工具响应过慢，可稍后重试，或改用其他工具。",
                    code="tool_timeout",
                ) from exc

        return cast(_Fn, wrapper)

    return decorator
