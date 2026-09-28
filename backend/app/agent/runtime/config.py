"""运行时安全配置：打包四轴预算 / 防循环 / 注入闸 / HITL。

原本这些旋钮在 service → graph → reactive 之间逐项透传（budget / loop_guard /
injection_policy / require_write_approval 四个标量），构造签名随之膨胀。收敛成一个
不可变 ``RuntimeConfig``，一处构造、整体传递（决策 #23 收尾）。
"""

from dataclasses import dataclass, field

from app.agent.approval import ApprovalPolicy
from app.agent.guardrail.injection import InjectionPolicy
from app.agent.runtime.budget import DailyBudget, HardBudget
from app.agent.runtime.loop_guard import LoopGuard


@dataclass(frozen=True)
class RuntimeConfig:
    """Agent 运行时的安全旋钮集。

    ``budget`` 与 ``loop_guard`` 恒有 concrete 默认值（无预算 = 变相无限、无防循环 = 变相
    允许死循环，都不允许 None）——这两道是「必须存在」的硬闸；其余安全闸可空（None = 关闭）。
    """

    budget: HardBudget = HardBudget()  # 四轴硬上限（恒有界，默认 20turns/120s/100k/¥1）
    loop_guard: LoopGuard = field(default_factory=LoopGuard)  # 防循环（恒在场，默认 5/5/4）
    injection_policy: InjectionPolicy | None = None
    require_write_approval: bool = False
    daily_budget: DailyBudget | None = None  # 跨 run 全局日预算（成本/token 上限）
    constraint_reminder: str | None = None  # 尾部约束重放（首尾三明治），None = 关闭
    approval_policy: ApprovalPolicy | None = None  # 写工具分级审批（分级；None 则退回布尔门禁）
