"""运行时配置：打包安全闸（四轴预算/防循环/注入闸/HITL）+ 数值旋钮 + 上下文分层。

原本这些旋钮在 service → graph → reactive 之间逐项透传（budget / loop_guard /
injection_policy / require_write_approval 四个标量），构造签名随之膨胀；后来一度拆成
``RuntimeConfig``（安全闸）与 ``CopilotTuning``（service 层数值旋钮）两袋，但边界被侵蚀
（``review_max_attempts``/``max_result_chars`` 明明下传图/tools，HITL 的
``approval_timeout_seconds`` 却流落在 tuning 一侧）。收敛成一个不可变 ``RuntimeConfig``，
一处构造、整体传递（决策 #23 收尾）。
"""

from dataclasses import dataclass, field

from app.agent.approval import ApprovalPolicy
from app.agent.guardrail.injection import InjectionPolicy
from app.agent.runtime.budget import DailyBudget, HardBudget
from app.agent.runtime.context import ContextConfig
from app.agent.runtime.loop_guard import LoopGuard


@dataclass(frozen=True)
class RuntimeConfig:
    """Agent 运行时的完整旋钮集（安全闸 + 数值旋钮 + 上下文分层）。

    ``budget`` 与 ``loop_guard`` 恒有 concrete 默认值（无预算 = 变相无限、无防循环 = 变相
    允许死循环，都不允许 None）——这两道是「必须存在」的硬闸；其余安全闸可空（None = 关闭）。
    """

    # —— 安全闸（行为对象，下传 graph/tools）——
    budget: HardBudget = HardBudget()  # 四轴硬上限（恒有界，默认 20turns/120s/100k/¥1）
    loop_guard: LoopGuard = field(default_factory=LoopGuard)  # 防循环（恒在场，默认 5/5/4）
    injection_policy: InjectionPolicy | None = None
    require_write_approval: bool = False
    daily_budget: DailyBudget | None = None  # 跨 run 全局日预算（成本/token 上限）
    approval_policy: ApprovalPolicy | None = None  # 写工具分级审批（分级；None 则退回布尔门禁）
    approval_timeout_seconds: float | None = None  # HITL 审批单超时 fail-close（None = 不设超时）

    # —— 数值旋钮（service 层）——
    max_result_chars: int = 4000  # read/search 工具返回截断/落盘阈值
    review_max_attempts: int = 2  # 输出审查发现不一致时的自动修复重试上限
    cache_hit_rate_warn: float = 0.3  # 前缀缓存命中率告警阈值

    # —— 上下文分层（L0-L5，见 runtime/context.py）——
    context: ContextConfig = field(default_factory=ContextConfig)
