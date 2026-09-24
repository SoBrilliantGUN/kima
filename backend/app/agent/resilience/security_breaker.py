"""安全熔断：安全行为信号驱动的熔断器（防线④，手动恢复）。

与基础设施熔断 ``circuit_breaker.py`` 严格区分：

- 基础设施熔断管「工具/服务持续不可用」，按**瞬时异常**计数，恢复期后自动 HALF_OPEN
  探测、成功则自动 CLOSED——机器故障会自动好，故可自动恢复。
- 安全熔断管「Agent 反复越界」：连续多次参数契约违规 / 越权尝试，往往代表 Prompt 注入
  或枚举攻击，是**安全漏洞，不会自己好起来**——故只能手动 ``reset()``，不自动恢复。

文档防线④「运行时熔断」的落地：连续 N 次安全违规 → 熔断（本 run 内拒绝再执行任何工具、
冻结上下文）。违规来源由参数契约校验（``toolmeta.validate_param_contract``）在 Loop 侧
喂入；阈值/开关经 ``RuntimeConfig`` 层可配。熔断态由 ``SecurityBreakerTripped`` 异常上抛，
使 run 硬停（与 ``BudgetExceeded`` / ``InfiniteLoopDetected`` 同一条终止路径）。
"""

from app.core.exceptions import DomainError


class SecurityBreakerTripped(DomainError):
    """安全熔断已触发：Agent 行为疑似越界，冻结本次 run。"""

    code = "security_breaker_tripped"


class SecurityBreaker:
    """安全违规计数器：达阈值即熔断，只可手动 ``reset()``，不自动恢复。"""

    def __init__(self, threshold: int = 5) -> None:
        self._threshold = threshold
        self._violations = 0
        self._tripped = False

    @property
    def violations(self) -> int:
        return self._violations

    @property
    def threshold(self) -> int:
        return self._threshold

    def record_violation(self) -> bool:
        """记一次安全违规；达到阈值即置熔断态，返回本次是否触发熔断。"""
        if self._tripped:
            return False
        self._violations += 1
        if self._violations >= self._threshold:
            self._tripped = True
        return self._tripped

    def is_tripped(self) -> bool:
        return self._tripped

    def reset(self) -> None:
        """手动恢复（运维介入调查后调用）；无自动恢复路径。"""
        self._violations = 0
        self._tripped = False
