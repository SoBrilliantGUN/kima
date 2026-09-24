"""工具熔断：CLOSED → OPEN → HALF_OPEN 状态机（决策 #26）+ 级联 + 持久化。

窗口内连续失败达阈值 → OPEN（快速失败，不再调用工具）；恢复期后转 HALF_OPEN 放一次
探测；探测成功 → CLOSED，失败 → 回 OPEN。与重试互补：重试管「单次瞬时抖动」，熔断管
「工具持续不可用」。

两处生产级增强（对应《Agent 挂了…》的第三层状态「动作指纹」与「级联故障」）：

- **级联**：工具可声明 ``resource``（共享依赖，如 ``db``/``web``）。失败/成功同时记到
  ``resource:<name>`` 的共享熔断器上，任一工具的失败都累加共享计数——DB 挂了 → 所有
  db 工具一起熔断，而不是每个工具各自凑满阈值、空转 token。
- **持久化**：状态经 ``store``（``BreakerStore``）外置，失败计数跨进程存活。崩溃恢复后
  计数器不归零，否则「崩溃前已连续失败 9 次，恢复后又从 0 算，永远触发不了熔断」。

时间用 ``time.time()``（墙钟）而非 ``time.monotonic()``：墙钟跨重启仍可比，才能支持
失败计数持久化后正确续算窗口。
"""

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import wraps
from typing import Any, Protocol, TypeVar, cast

from app.agent.resilience.error_classifier import is_retryable

_Fn = TypeVar("_Fn", bound=Callable[..., Awaitable[Any]])

# 级联熔断的共享资源命名空间前缀（工具名与资源名共用同一张 breaker 表，前缀避免撞名）
_RESOURCE_PREFIX = "resource:"


def _resource_name(resource: str) -> str:
    """资源名加前缀，与工具名同表不撞名（级联共享计数）。"""
    return f"{_RESOURCE_PREFIX}{resource}"


class BreakerState(StrEnum):
    """三态：CLOSED 正常放行；OPEN 快速失败；HALF_OPEN 恢复期放一次探测。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _ToolState:
    """单个 name（工具或资源）的熔断状态。

    ``failures`` 存窗口内每次失败的时间戳；用 deque 是为了从头部高效滑出过期记录。
    """

    failures: deque[float] = field(default_factory=deque)
    state: BreakerState = BreakerState.CLOSED


class BreakerStore(Protocol):
    """熔断状态的持久化原语（可注入内存版 Fake 做确定性测试）。

    ``load_all`` 返回 ``{name: {"state": str, "failures": list[float]}}``；``save`` 幂等覆盖写
    单个 name 的状态。实现负责独立会话、异常不影响熔断主流程。
    """

    async def load_all(self) -> dict[str, dict[str, Any]]: ...
    async def save(self, name: str, state: str, failures: list[float]) -> None: ...


class CircuitBreaker:
    """每工具 + 每资源一个熔断状态（进程内单例，跨请求保留；store 开启则跨进程保留）。"""

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 60.0,
        window_seconds: float = 300.0,
        store: BreakerStore | None = None,
    ) -> None:
        self._threshold = failure_threshold
        self._recovery = recovery_timeout_seconds
        self._window = window_seconds
        self._store = store
        self._states: dict[str, _ToolState] = {}

    async def load_all(self) -> None:
        """启动续读熔断状态（崩溃后计数不归零）；store 为空或读失败则保持空。"""
        if self._store is None:
            return
        try:
            rows = await self._store.load_all()
        except Exception:  # noqa: BLE001 - 续读 best-effort
            return
        for name, data in rows.items():
            st = _ToolState()
            try:
                st.state = BreakerState(data.get("state", BreakerState.CLOSED.value))
            except ValueError:
                st.state = BreakerState.CLOSED
            failures = data.get("failures", [])
            st.failures = deque(float(f) for f in failures)
            self._states[name] = st

    def _schedule_persist(self, name: str) -> None:
        """fire-and-forget 落库（不阻塞工具调用）；store 为空/无事件循环则跳过。"""
        if self._store is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._persist(name))

    async def _persist(self, name: str) -> None:
        if self._store is None:
            return
        try:
            st = self._states[name]
            await self._store.save(name, st.state.value, list(st.failures))
        except Exception:  # noqa: BLE001 - 熔断持久化 best-effort（DB 挂时静默放弃）
            pass

    def _is_available(self, name: str) -> bool:
        """单个 name 是否可用。OPEN 已过恢复期则转 HALF_OPEN 并放行一次探测。

        用 ``failures[-1]``（最近一次失败时间）近似「进入 OPEN 的时刻」：进入 OPEN 时
        必然刚记过失败，最后一次失败时间即熔断起点，无需单独存 ``opened_at`` 字段。
        """
        s = self._states.setdefault(name, _ToolState())
        if s.state is BreakerState.OPEN:
            last = s.failures[-1] if s.failures else 0.0
            if time.time() - last >= self._recovery:
                s.state = BreakerState.HALF_OPEN
                return True
            return False
        return True

    def is_available(self, name: str, resource: str | None = None) -> bool:
        """工具可用当且仅当自身与所属资源都未熔断（级联：资源挂 → 同资源工具全不可用）。"""
        if not self._is_available(name):
            return False
        if resource is not None and not self._is_available(_resource_name(resource)):
            return False
        return True

    def record_success(self, name: str, resource: str | None = None) -> None:
        """记成功：同时清除自身与所属资源的失败计数（成功即复位）。"""
        self._record_success(name)
        if resource is not None:
            self._record_success(_resource_name(resource))

    def _record_success(self, name: str) -> None:
        s = self._states.setdefault(name, _ToolState())
        s.failures.clear()
        # 成功即复位到 CLOSED——HALF_OPEN 的探测成功也走这里闭合。
        s.state = BreakerState.CLOSED
        self._schedule_persist(name)

    def record_failure(self, name: str, resource: str | None = None) -> None:
        """记失败：自身与所属资源各记一次（级联累计共享计数）。"""
        self._record_failure(name)
        if resource is not None:
            self._record_failure(_resource_name(resource))

    def _record_failure(self, name: str) -> None:
        s = self._states.setdefault(name, _ToolState())
        s.failures.append(time.time())
        if s.state is BreakerState.HALF_OPEN:
            # 探测失败：立即回 OPEN 重新进入恢复期，无需凑满阈值。
            s.state = BreakerState.OPEN
            self._schedule_persist(name)
            return
        # 先滑出窗口外的旧失败，再判断窗口内累计是否达阈值。
        cutoff = time.time() - self._window
        while s.failures and s.failures[0] < cutoff:
            s.failures.popleft()
        if len(s.failures) >= self._threshold:
            s.state = BreakerState.OPEN
        self._schedule_persist(name)

    def available(
        self, names: Iterable[str], resources: dict[str, str | None] | None = None
    ) -> list[str]:
        """返回 ``names`` 中未熔断（含级联资源未熔断）的工具名子集。

        注意力稀释铁律 #5：熔断的工具应从候选集**剔除**（模型根本看不到它），而不是
        留在候选集里等模型调用后再返回「暂时不可用」字符串——那样工具 schema 仍占
        prompt token、模型仍会空转 tool_call token。``resources`` 是「工具名 → 资源名」
        映射（可选），缺省则只按工具自身熔断状态过滤。
        """
        res = resources or {}
        return [n for n in names if self.is_available(n, res.get(n))]


def with_circuit_breaker(
    breaker: CircuitBreaker, resource: str | None = None
) -> Callable[[_Fn], _Fn]:
    """给 async 函数加熔断：不可用时快速失败，异常记失败，成功记成功。

    ``resource`` 声明工具所属共享依赖（级联）：失败/成功同时记到 ``resource:<name>``，
    一个工具反复失败会拖累同资源的所有工具。
    """

    def decorator(fn: _Fn) -> _Fn:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            name = fn.__name__
            if not breaker.is_available(name, resource):
                return "该工具暂时不可用（熔断中），请稍后重试。"
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 记失败后上抛，由重试/上层处理
                # 只对「基础设施不可用」（可重试的瞬时异常）记失败并可能熔断；
                # 领域级永久失败（如「笔记不存在」）是「正常执行返回了坏结果」，不该熔断。
                if is_retryable(exc):
                    breaker.record_failure(name, resource)
                raise
            breaker.record_success(name, resource)
            return result

        return cast(_Fn, wrapper)

    return decorator
