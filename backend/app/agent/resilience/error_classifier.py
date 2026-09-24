"""错误分级：异常 → 是否可重试（决策 #26）。

按异常类型分级而非硬编码业务错误：传输类（超时/连接/OSError）判瞬时可重试；
领域异常（DomainError：不存在/冲突/校验）判永久不重试；其余保守不重试。
"""

from app.agent.resilience.result import ToolFailure
from app.core.exceptions import DomainError

# 瞬时异常（可重试）：网络/超时/底层 IO 抖动
_TRANSIENT = (TimeoutError, ConnectionError, OSError)


def is_retryable(exc: BaseException) -> bool:
    """判断异常是否可重试：结构化失败按其红绿灯，其余按类型分级。

    - ``ToolFailure``：黄灯（TRANSIENT）可重试，红灯（PERMANENT）不重试；
    - ``DomainError``：领域异常（不存在/冲突/校验）判永久不重试；
    - 传输类（超时/连接/OSError）：判瞬时可重试；其余保守不重试。
    """
    if isinstance(exc, ToolFailure):
        return exc.retryable
    if isinstance(exc, DomainError):
        return False
    return isinstance(exc, _TRANSIENT)


def classify_error(exc: BaseException) -> str:
    """把异常分类成 ``transient`` / ``permanent``（第五层状态「崩溃现场/判决书」的标签）。

    与 :func:`is_retryable` 同源，但返回字符串标签，供 ``last_error`` 落库——崩溃恢复后
    Agent/框架据此判断「别重试」还是「瞬态可重试」，而非恢复后重新猜一遍。分类结果随
    checkpoint 持久化，跨进程传递。
    """
    return "transient" if is_retryable(exc) else "permanent"
