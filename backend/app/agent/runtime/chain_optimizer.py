"""计划诊断层（ChainOptimizer）：关键路径 + 四维体检 + 分级 verdict。

核心：串行链的端到端可靠性 = 单步可靠性的 n 次方（R^n，链杀手）——真正决定可靠性的是
「必须排队等的串行步骤」，并行分支不拖垮成功率。审查层算**最长串行链**（DAG 最长路径），
据此给三档：

- ``ok``（≥75）：健康。
- ``warn``（50-75）：可观察。
- ``critical``（<50 或极长链）：高风险提示。

本模块只「说真话」：算分给 verdict，不改 Plan、也不阻塞执行（plan_node 只落报告，不再
因 critical 打回重生成）——长任务本就可能是长参数链，硬砍串行链是错的；真正的执行兜底
是预算（按计划步数推导）与 ``validate_plan`` 的环/幻觉工具校验。写步骤的人工审批由 worker
的防线③（``resolve_approvals``）独立承担，与本 verdict 无关。
"""

from dataclasses import dataclass, field
from typing import Any

from app.agent.runtime.planner import Plan
from app.agent.toolmeta import SideEffectLevel, ToolRegistry

# 极长链提示线：关键路径超过此值仅诊断 critical（R^n 端到端可靠性已跌破 ~0.5），
# 不阻塞执行——plan_node 已不再打回重生成。
_CRITICAL_PATH_HARD_FLOOR = 14


@dataclass(frozen=True)
class PlanReport:
    """计划体检报告：关键路径 + 四维分 + verdict + 人话问题清单。"""

    critical_path: int
    structure_score: float
    risk_score: float
    safety_score: float
    cost_score: float
    verdict: str  # ok / warn / critical
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "critical_path": self.critical_path,
            "structure_score": self.structure_score,
            "risk_score": self.risk_score,
            "safety_score": self.safety_score,
            "cost_score": self.cost_score,
            "verdict": self.verdict,
            "issues": self.issues,
        }


def _critical_path(plan: Plan) -> int:
    """DAG 最长路径（最长串行链）长度，缓存递归。"""
    steps = {s.step_id: s for s in plan.steps}
    depth: dict[str, int] = {}

    def _depth(sid: str) -> int:
        if sid in depth:
            return depth[sid]
        step = steps.get(sid)
        if not step or not step.depends_on:
            depth[sid] = 1
            return 1
        d = 1 + max(_depth(dep) for dep in step.depends_on)
        depth[sid] = d
        return d

    for sid in steps:
        _depth(sid)
    return max(depth.values()) if depth else 0


def _side_effect(registry: ToolRegistry, action: str) -> SideEffectLevel:
    meta = registry.get(action)
    return meta.side_effect_level if meta is not None else SideEffectLevel.LOW


def _stepped(value: int, bands: list[tuple[int, float]], floor: float) -> float:
    """按区间阶梯打分：value 落在某个 band 内取对应分，超最大 band 取 floor。"""
    for threshold, score in bands:
        if value <= threshold:
            return score
    return floor


def analyse_plan(plan: Plan, registry: ToolRegistry) -> PlanReport:
    """体检计划：关键路径 + 四维分 + verdict + 问题清单。"""
    cp = _critical_path(plan)
    steps = list(plan.steps)
    n = max(len(steps), 1)
    high = [s for s in steps if _side_effect(registry, s.action) is SideEffectLevel.HIGH]

    # 结构分：最长串行链越短越高（≤5 满分、≤13 可观察，对齐 R^n 的 OK/WARNING 线）
    structure = _stepped(cp, [(5, 100.0), (13, 70.0)], 40.0)
    # 风险分：不可逆写（HIGH 副作用）占比越低越高
    risk = 100.0 - (len(high) / n) * 100.0
    # 兜底分：无不可逆写则满分，有则默认人工审批兜底（降 20）
    safety = 100.0 if not high else 80.0
    # 成本分：步骤数越少越高
    cost = _stepped(len(steps), [(5, 100.0), (10, 80.0)], 60.0)

    total = structure * 0.4 + risk * 0.2 + safety * 0.2 + cost * 0.2
    verdict = "critical" if total < 50 else ("warn" if total < 75 else "ok")
    if cp > _CRITICAL_PATH_HARD_FLOOR:
        verdict = "critical"  # 极长链高风险提示（不阻塞执行）

    issues: list[str] = []
    if cp > 13:
        issues.append(f"最长串行链 {cp} 步，端到端可靠性偏低，可考虑拆并行组")
    if high:
        names = "、".join(s.action for s in high)
        issues.append(f"{len(high)} 个不可逆写步骤（{names}），需人工审批")
    if len(steps) > 10:
        issues.append(f"{len(steps)} 个步骤偏多，可考虑合并或模板化")

    return PlanReport(
        critical_path=cp,
        structure_score=structure,
        risk_score=risk,
        safety_score=safety,
        cost_score=cost,
        verdict=verdict,
        issues=issues,
    )
