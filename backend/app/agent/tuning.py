"""CopilotService 的 service 层数值旋钮（构造参数收敛）。

与 `runtime/config.py` 的 `RuntimeConfig`（预算/循环守卫/注入策略，下传到图）不同，本数据类
只收 `CopilotService` 自身的旋钮：上下文排布（历史/记忆 token 预算）、审查开关、工具结果
裁剪、HITL 超时与缓存命中告警阈值。收敛成一个对象，避免 `__init__` 参数膨胀。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CopilotTuning:
    """service 层数值旋钮（默认值对齐原构造参数的默认）。"""

    max_result_chars: int = 4000
    review_enabled: bool = True
    review_max_attempts: int = 2
    history_max_tokens: int = 4000
    history_recent_turns: int = 3
    context_max_tokens: int = 32000
    memory_block_max_tokens: int = 2000
    approval_timeout_seconds: float | None = None
    cache_hit_rate_warn: float = 0.3
