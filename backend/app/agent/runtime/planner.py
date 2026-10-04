"""LLM Planner：生成带依赖的步骤 DAG + 失败 replan（决策 #5/#21）。

长程多步任务（「读遍库里关于 X 的文档、归纳、成文」）由 planner 模式处理：一次 LLM 调用
产出步骤 DAG（只使用给定工具），executor 拓扑执行，某步失败则增量重规划（局部作废、绝不
推倒重来）。解析失败回退空 plan（退化为 reactive）。

数据模型与解析见 ``plan_model.py``；本模块只保留 LLM 规划器实现。
"""

from collections.abc import Sequence

from app.agent.gateway import LLMGateway
from app.agent.runtime.budget import BudgetExceeded
from app.agent.runtime.plan_model import (
    Plan,
    PlanModel,
    Planner,
    PlanStep,
    StepStatus,
    parse_plan,
    parse_plan_strict,
    validate_plan,
)
from app.integrations.llm import ChatMessage, StructuredParseError, format_instructions

# 合成步骤动作名：plan 的最后一步，把前置步骤产物整理成最终回答。它不注册进工具集，
# 由 worker 识别后调 LLM 合成（而非调工具）。作为保留动作加入规划器可用动作白名单。
SYNTHESIZE_ACTION = "finalize_answer"


class LLMPlanner:
    """真实实现：一次 LLM 调用产出 DAG；解析/语义失败 fail-closed 回退空 plan（决策 D5）。"""

    _SYSTEM = (
        "你是任务规划器。把用户任务拆成步骤 DAG，只使用给定工具。\n"
        "规划约束：\n"
        "1. 无依赖的只读步骤用 depends_on=[] 声明并行（扇出）。\n"
        "2. 每个写操作前必须有校验步骤，后必须有补偿步骤。\n"
        "3. 每个写操作必须包含 idempotency_key。\n"
        "4. 不可逆操作前必须有 human_approval 步骤。\n"
        f'5. 最后必须有一个合成步骤：action="{SYNTHESIZE_ACTION}"、terminal=true、'
        "depends_on 所有其他步骤、params 留空。系统会自动收集它依赖步骤的结果整理成最终回答，"
        "不要在 params 里写任何引用。\n"
    ) + format_instructions(PlanModel)

    def __init__(self, gateway: LLMGateway) -> None:
        self._gateway = gateway

    async def generate(
        self,
        task: str,
        tool_names: list[str],
        constraints: str = "",
        skills: str = "",
    ) -> Plan:
        # 约束/技能显式携带（文档「约束蒸发」）：规划器是路由 Agent 派给「子 Agent」的下行任务包，
        # 用户的底线/红线与「怎么做」的技能经验必须随任务一起送达，否则规划器在不知道它们
        # 的情况下拆步骤。
        user = f"任务：{task}\n可用工具：{', '.join(tool_names)}"
        if skills.strip():
            user += (
                f"\n可参考的技能指引（沉淀的「怎么做」经验，规划步骤时尽量遵循）："
                f"\n{skills.strip()}"
            )
        if constraints.strip():
            user += f"\n必须遵守的约束（红线，规划时不得违反）：\n{constraints.strip()}"
        # 自纠错循环：解析/语义失败（字段漂移、幻觉工具、依赖缺失、成环）把精确错误回喂
        # 重试，最多 3 次；耗尽才降级空 plan（退化为 reactive）。「计划整体质量」类软问题
        # 由 plan_node 的体检循环打回，不在这里处理。
        feedback = ""
        for _ in range(3):
            prompt = user + (f"\n上次规划被拒绝，请修正：\n{feedback}" if feedback else "")
            messages = [ChatMessage("system", self._SYSTEM), ChatMessage("user", prompt)]
            try:
                result = await self._gateway.complete("planner.generate", messages, temperature=0)
            except BudgetExceeded:
                raise  # 预算硬停：向上终止整个 run，不降级（reactive 会继续烧钱）
            except Exception:
                # 网关失败（熔断/网络）→ fail-closed 回退空 plan（退化为 reactive）
                return Plan(steps=())
            try:
                plan = parse_plan_strict(result.content)
            except StructuredParseError as exc:
                feedback = str(exc)
                continue
            errors = validate_plan(plan, tool_names)
            if errors:
                feedback = "；".join(errors)
                continue
            return plan
        return Plan(steps=())

    async def replan(
        self,
        plan: Plan,
        failed_step: PlanStep,
        error: str,
        tool_names: Sequence[str],
        skills: str = "",
    ) -> list[PlanStep]:
        """增量重规划：让 LLM 针对失败步骤给出替换步骤（带 replaces 声明 + 完整 depends_on）。

        返回的替换步骤交给 executor 的 ``Plan.merge`` 做工具名合法性（幻觉工具在并入边界
        拦截）、依赖/环校验与 id 撞车改名；这里只做结构解析。``skills`` 与 ``generate`` 同源
        显式携带——失败步骤若受某 skill 指引，替换步骤也应遵循该 skill，故随 replan 一起送达。
        """
        user = (
            f"步骤 {failed_step.step_id}（{failed_step.action}）失败：{error}\n"
            f"可用工具：{', '.join(tool_names)}\n"
        )
        if skills.strip():
            user += (
                f"\n可参考的技能指引（沉淀的「怎么做」经验，规划替换步骤时尽量遵循）："
                f"\n{skills.strip()}"
            )
        user += (
            f"\n当前计划状态：\n{self._describe_plan(plan)}\n"
            "请给出替换步骤。规则：\n"
            "- 用 replaces 声明你替换的旧步骤 id（被替换的旧步骤会被作废）；\n"
            "- depends_on 可引用已完成的步骤（复用其产物）或同批新步骤 id；\n"
            "- 失败步骤的下游已作废，若它们仍需执行，必须一并给出其替换步骤。"
        )
        try:
            result = await self._gateway.complete(
                "planner.replan",
                [ChatMessage("system", self._SYSTEM), ChatMessage("user", user)],
                temperature=0,
            )
        except Exception:
            return []
        try:
            steps = parse_plan_strict(result.content).steps
        except StructuredParseError:
            return []
        return list(steps)

    def _describe_plan(self, plan: Plan) -> str:
        """把计划当前状态（含每步状态/依赖）序列化给 LLM 做增量重规划。"""
        lines: list[str] = []
        for s in plan.steps:
            line = f"- {s.step_id} ({s.action}) [{s.status.value}]"
            if s.depends_on:
                line += f" 依赖={list(s.depends_on)}"
            if s.output_ref is not None:
                line += " 有产物"
            lines.append(line)
        return "\n".join(lines) or "（空计划）"


# 重导出：数据模型/解析供 executor / plan_mode / 测试从原路径 import。
__all__ = [
    "LLMPlanner",
    "Planner",
    "Plan",
    "PlanStep",
    "StepStatus",
    "parse_plan",
    "validate_plan",
    "SYNTHESIZE_ACTION",
]
