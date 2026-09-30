"""审批分级（Policy-as-Code）：把「要不要人审批」从布尔开关升级为确定性规则。

同步阻塞地「所有写操作都推给人审批」会制造
审批疲劳——红灯不稀缺，人就闭眼点通过，安全阀变成橡皮图章。根治法是把决策集中成
**清晰可读、可代码评审的规则**，而不是一个黑盒风险分或一刀切的布尔。

故本模块不引入任何 LLM / 概率打分，只做一件小事：把工具的 ``SideEffectLevel`` 映射为
三档处置。规则即代码，改规则 = 改这里 + 走 CR。

- ``ALLOW``：低风险自动放行（只记日志）。
- ``NOTIFY``：中风险异步通知 + 事后审计（自动执行，落审计事件，不阻塞）。
- ``REQUIRE_APPROVAL``：高风险同步审批（``interrupt()`` 挂起，等人裁决）。
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from app.agent.toolmeta import SideEffectLevel, ToolRegistry

if TYPE_CHECKING:  # 仅类型检查用，避免运行期循环导入（config 不反向依赖 approval）
    from app.agent.runtime.config import RuntimeConfig


class ApprovalDecision(StrEnum):
    """审批处置：低风险放行 / 中风险通知 / 高风险审批。"""

    ALLOW = "allow"
    NOTIFY = "notify"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class ApprovalPolicy:
    """规则化分级策略：``level_map`` 把副作用等级映射为三档处置。

    这是 Policy-as-Code 的最小载体——一张可读的表，胜过散落 if-else。
    缺省映射缺失时 fail-closed 到 ``REQUIRE_APPROVAL``（安全优先，见 ``decide``）。
    """

    level_map: dict[SideEffectLevel, ApprovalDecision] = field(default_factory=dict)

    def decide(self, level: SideEffectLevel) -> ApprovalDecision:
        """裁决：查 level 映射，缺省时 fail-closed 到审批。"""
        return self.level_map.get(level, ApprovalDecision.REQUIRE_APPROVAL)

    @classmethod
    def graded(cls) -> "ApprovalPolicy":
        """分级模式（默认推荐）：HIGH 审批 / MEDIUM 通知 / LOW 放行。

        让「红灯稀缺」——只有真正不可逆的高危写（覆盖人设档案）才打断人，一般增量写
        （建笔记/写记忆）自动执行但留痕，供事后审计与撤销。
        """
        return cls(
            level_map={
                SideEffectLevel.LOW: ApprovalDecision.ALLOW,
                SideEffectLevel.MEDIUM: ApprovalDecision.NOTIFY,
                SideEffectLevel.HIGH: ApprovalDecision.REQUIRE_APPROVAL,
            }
        )

    @classmethod
    def strict(cls) -> "ApprovalPolicy":
        """严格模式：所有写操作（副作用 ≥ MEDIUM）一律审批，等价旧布尔门禁。"""
        return cls(
            level_map={
                SideEffectLevel.LOW: ApprovalDecision.ALLOW,
                SideEffectLevel.MEDIUM: ApprovalDecision.REQUIRE_APPROVAL,
                SideEffectLevel.HIGH: ApprovalDecision.REQUIRE_APPROVAL,
            }
        )


def resolve_approval_decision(
    name: str, registry: ToolRegistry | None, runtime: "RuntimeConfig"
) -> ApprovalDecision:
    """共享裁决：reactive 工具门禁 / planner 候选集剔除都用这一份，避免规则漂移。

    只读工具（无副作用）恒 ``ALLOW``；有副作用时按 ``approval_policy`` 分级（恒在场，
    无「关闭审批」降级路径）。
    """
    meta = registry.get(name) if registry is not None else None
    if meta is None or not meta.has_side_effect:
        return ApprovalDecision.ALLOW
    return runtime.approval_policy.decide(meta.side_effect_level)


# —— 证据包：确定性的人话摘要（结构化解析，不引入 LLM）——

_KIND_LABEL = {
    "constraint": "硬约束",
    "fact": "事实",
    "preference": "偏好",
    "episodic": "事件",
}


def approval_summary(tool: str, args: dict[str, Any] | None) -> str:
    """把冰冷的工具参数翻译成一句业务风险话术（证据包的第一步）。

    与前端 ``TOOL_LABELS`` 不同，这里产出的是**带具体目标**的句子（如「新建笔记《x》」），
    让人一眼看清「动的是什么」，而不是一串 JSON。确定性生成、可审计，必要时再叠加
    LLM 摘要（针对自然语言意图），本函数先覆盖结构可解析的部分。
    """
    args = args or {}
    if tool == "create_note":
        return f"新建笔记《{args.get('title', '未命名')}》"
    if tool == "write_memory":
        kind = str(args.get("kind", ""))
        return f"写入一条{_KIND_LABEL.get(kind, '')}记忆"
    if tool == "update_profile":
        kind = str(args.get("kind", ""))
        return f"覆盖 {kind} 人设档案"
    if tool == "write_skill":
        return f"写入/覆盖 skill「{args.get('name', '未命名')}」"
    if tool == "delete_skill":
        return f"删除 skill「{args.get('name', '')}」"
    return tool
