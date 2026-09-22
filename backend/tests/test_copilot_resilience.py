"""错误分级 + 退避重试：瞬时异常重试、永久异常不重试。"""

from app.agent.resilience.error_classifier import is_retryable
from app.agent.resilience.retry import Backoff, RetryPolicy, with_retry
from app.core.exceptions import NotFoundError


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
