"""退避重试（决策 #26）：瞬时异常退避重试，永久异常不重试。"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import wraps
from typing import Any, TypeVar, cast

from app.agent.resilience.error_classifier import is_retryable

_Fn = TypeVar("_Fn", bound=Callable[..., Awaitable[Any]])


class Backoff(StrEnum):
    FIXED = "fixed"
    EXPONENTIAL = "exponential"
    JITTERED = "jittered"


@dataclass(frozen=True)
class RetryPolicy:
    """重试策略：max_attempts 含首次调用（1 + 最多 max_attempts-1 次重试）。"""

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    backoff: Backoff = Backoff.JITTERED

    def delay(self, attempt: int) -> float:
        """第 attempt 次（1-based）重试前的等待秒数。"""
        if self.backoff is Backoff.FIXED:
            return self.base_delay
        exponential = min(self.base_delay * (2.0 ** (attempt - 1)), self.max_delay)
        if self.backoff is Backoff.JITTERED:
            return random.uniform(0.0, exponential)
        return exponential


DEFAULT_RETRY_POLICY = RetryPolicy()


def with_retry(policy: RetryPolicy) -> Callable[[_Fn], _Fn]:
    """给 async 函数加退避重试：仅瞬时异常（is_retryable）重试，永久异常立即上抛。"""

    def decorator(fn: _Fn) -> _Fn:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(policy.max_attempts):
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - 分级后决定重试或上抛
                    if not is_retryable(exc) or attempt == policy.max_attempts - 1:
                        raise
                    await asyncio.sleep(policy.delay(attempt + 1))
            raise RuntimeError("unreachable")  # pragma: no cover

        return cast(_Fn, wrapper)

    return decorator
