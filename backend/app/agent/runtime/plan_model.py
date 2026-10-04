"""计划数据模型与解析（Plan-as-Data 的第一、二道防线）。

计划是「系统数据」而非「模型的临时口水话」：LLM 只负责生成蓝图与填充参数，运行时状态
（status/output_ref/error/version_created）由 executor 填写。计划带版本号、支持局部作废
（mark_downstream_obsolete）与防御性合并（merge）。

解析侧（``parse_plan``/``validate_plan``）做结构化 + 语义校验：字段名漂移、幻觉工具名、
环依赖都在契约边界被拦下，而不是拖到 executor 执行时才报错。
"""

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.integrations.llm import StructuredParseError, parse_json


class StepStatus(StrEnum):
    """计划步骤的运行时状态（系统填写，非 LLM）。

    PENDING 排队 / RUNNING 执行中 / COMPLETED 完成 / FAILED 失败（审计信号，保留）/
    OBSOLETE 作废（重规划时被替换或随失败步骤下游一起作废）。
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    OBSOLETE = "obsolete"


@dataclass
class PlanStep:
    """一个计划步骤：可寻址、可追溯、可查询。

    定义字段（action/params/depends_on/is_terminal）由 LLM 生成；``step_id`` 的最终值由
    系统保证唯一（LLM 给的 id 仅作批内标签，撞车时被改名）；运行时字段
    （status/output_ref/error/version_created/replaces_step_id）由系统填写。``output_ref``
    让下游步骤引用上游产物（如第一步 dump 出的文件），不必重新跑第一步；``replaces_step_id``
    是重规划时新步骤声明「我替代谁」的身份证。
    """

    step_id: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    is_terminal: bool = False

    # 运行时状态（系统填写，非 LLM）
    status: StepStatus = StepStatus.PENDING
    # 上游产物：通常是字符串（文件路径/裁剪后的文本），也可是 dict（结构化产物）。
    output_ref: str | dict[str, Any] | None = None
    error: str | None = None
    version_created: int = 1
    replaces_step_id: str | None = None


class Plan:
    """版本化的 DAG 执行计划：由数据结构驱动的状态机。

    持有 ``_steps``（id → step）与 ``_dependents``（反向邻接：id → 依赖它的步骤），
    支持按拓扑序取就绪步骤、局部作废失败步骤下游、防御性合并重规划替换步骤。
    与 ReAct 的区别：ReAct「下一步做什么」由模型临时决定，这里由状态机驱动。
    """

    def __init__(self, steps: Sequence[PlanStep]) -> None:
        self.version: int = 1
        # 崩溃现场/判决书（第五层状态）：最近一次工具失败的分类标签，随检查点持久化，崩溃恢复后
        # 据此判断「别重试」（permanent）还是「瞬态可重试」（transient）。
        self.last_error: dict[str, Any] | None = None
        self._steps: dict[str, PlanStep] = {}
        self._dependents: dict[str, list[str]] = {}
        for step in steps:
            self._add_step(step)

    # --- 只读 ---

    @property
    def steps(self) -> tuple[PlanStep, ...]:
        """全部步骤（插入序），供 validate_plan / 序列化 / 测试读取。"""
        return tuple(self._steps.values())

    def get_step(self, step_id: str) -> PlanStep | None:
        return self._steps.get(step_id)

    def get_parallel_ready(self) -> list[PlanStep]:
        """获取所有依赖已完成的 PENDING 步骤，支持并行（executor 当前顺序取首）。"""
        ready: list[PlanStep] = []
        for step in self._steps.values():
            if step.status != StepStatus.PENDING:
                continue
            if all(
                self._steps.get(d) is not None and self._steps[d].status == StepStatus.COMPLETED
                for d in step.depends_on
            ):
                ready.append(step)
        return ready

    # --- 变更 ---

    def mark_downstream_obsolete(self, failed_step_id: str) -> list[str]:
        """标记失败步骤的所有下游为 OBSOLETE（失败步骤本身保持 FAILED）。

        已完成的步骤不标记（那是工程资产，重规划可复用其 output_ref），但遍历继续——
        它的下游仍可能是 PENDING。返回被作废的步骤 id 列表。
        """
        obsolete: list[str] = []
        queue: deque[str] = deque(self._dependents.get(failed_step_id, ()))
        seen: set[str] = set(queue)
        while queue:
            sid = queue.popleft()
            step = self._steps.get(sid)
            if step is None:
                continue
            if step.status != StepStatus.COMPLETED:
                step.status = StepStatus.OBSOLETE
                obsolete.append(sid)
            for child_id in self._dependents.get(sid, ()):
                if child_id not in seen:
                    seen.add(child_id)
                    queue.append(child_id)
        return obsolete

    def merge(self, new_steps: list[PlanStep], tool_names: Sequence[str]) -> None:
        """合并 LLM 提出的替换步骤（防御性编程：不信任概率模型按套路出牌）。

        关键不变量：``step_id`` 的最终值由**系统**保证唯一——LLM 给的 id 只当「批内局部
        标签」用来解析 ``depends_on``/``replaces``，撞上已有 id 时系统改名（``#2``/``#3``…），
        绝不覆盖旧步骤。这样一个节点被替换多次时，每次替换都是独立条目，靠
        ``replaces_step_id`` 串成链、``version_created`` 区分版本，溯源不丢。

        ``tool_names`` 是本 run 的可用工具集：幻觉工具名在此并入边界拦截（抛 ValueError），
        避免白烧一次执行才发现「未知工具」。replan 只做结构解析，不重复校验工具名。
        """
        # 0. 工具名合法性：幻觉工具在并入边界拦截
        bad_tools = invalid_tools(new_steps, tool_names)
        if bad_tools:
            first = bad_tools[0]
            raise ValueError(
                f"步骤 {first.step_id!r} 的工具 {first.action!r} 不在可用工具集 "
                f"{sorted(set(tool_names))!r} 中"
            )
        # 1. 分配系统唯一 id：撞车改名，批内 depends_on 引用跟随改名
        taken: set[str] = set(self._steps)
        label_map: dict[str, str] = {}
        resolved: list[PlanStep] = []
        for ns in new_steps:
            final_id = self._claim_id(ns.step_id, taken)
            taken.add(final_id)
            label_map[ns.step_id] = final_id
            resolved.append(
                PlanStep(
                    step_id=final_id,
                    action=ns.action,
                    params=ns.params,
                    depends_on=tuple(label_map.get(d, d) for d in ns.depends_on),
                    is_terminal=ns.is_terminal,
                    replaces_step_id=ns.replaces_step_id,
                )
            )
        # 2. 引用完整性：replaces 必须指向存在的旧步骤；依赖要么指向已有步骤（非作废），
        #    要么指向本批新步骤
        resolved_ids = {r.step_id for r in resolved}
        for r in resolved:
            if r.replaces_step_id is not None and r.replaces_step_id not in self._steps:
                raise ValueError(
                    f"Step {r.step_id!r}: replaces 指向不存在的步骤 {r.replaces_step_id!r}"
                )
            for dep_id in r.depends_on:
                dep = self._steps.get(dep_id)
                if dep is None:
                    if dep_id not in resolved_ids:
                        raise ValueError(f"Step {r.step_id!r}: dependency {dep_id!r} not found")
                elif dep.status == StepStatus.OBSOLETE and dep_id not in resolved_ids:
                    raise ValueError(
                        f"Step {r.step_id!r}: dependency {dep_id!r} is OBSOLETE "
                        f"(and not replaced in this merge)"
                    )
        # 3. 环检测：防止 LLM 生成循环依赖（S1 依赖 S2，S2 又依赖 S1）
        self._assert_acyclic(resolved)
        # 4. 替代旧步骤：只标记 OBSOLETE，不物理删除（保留溯源）
        self.version += 1
        for r in resolved:
            if r.replaces_step_id:
                old = self._steps.get(r.replaces_step_id)
                if old is not None:
                    old.status = StepStatus.OBSOLETE
            r.version_created = self.version
            self._add_step(r)

    def _claim_id(self, proposed: str, taken: set[str] | None = None) -> str:
        """保证 step_id 唯一：撞车则追加 ``#2``/``#3``…，不覆盖旧步骤。"""
        taken = taken if taken is not None else set(self._steps)
        if proposed not in taken:
            return proposed
        n = 2
        while f"{proposed}#{n}" in taken:
            n += 1
        return f"{proposed}#{n}"

    def _assert_acyclic(self, new_steps: list[PlanStep]) -> None:
        """在「现有非作废步骤 + 新步骤（被替换的旧步骤排除）」的投影图上检测环。

        构图（挑出「还活着」的节点、并入本批新步骤）交给本方法，环检测复用模块级
        ``_has_cycle``（Kahn 拓扑：入度 0 反复出队，访问不全即存在环）。
        """
        replaced_ids = {ns.replaces_step_id for ns in new_steps if ns.replaces_step_id}
        steps_map = {
            sid: s
            for sid, s in self._steps.items()
            if s.status != StepStatus.OBSOLETE and sid not in replaced_ids
        }
        for ns in new_steps:
            steps_map[ns.step_id] = ns
        if _has_cycle(steps_map):
            raise ValueError("新步骤引入循环依赖")

    # --- 内部 ---

    def _add_step(self, step: PlanStep) -> None:
        self._steps[step.step_id] = step
        self._index_deps(step)

    def _index_deps(self, step: PlanStep) -> None:
        for dep_id in step.depends_on:
            self._dependents.setdefault(dep_id, []).append(step.step_id)

    # --- 序列化（事件溯源 / 检查点） ---

    def to_dict(self) -> dict[str, Any]:
        """序列化计划状态（含版本号与每步运行时状态），供检查点落库。"""
        return {
            "version": self.version,
            "last_error": self.last_error,
            "steps": [
                {
                    "step_id": s.step_id,
                    "action": s.action,
                    "params": s.params,
                    "depends_on": list(s.depends_on),
                    "is_terminal": s.is_terminal,
                    "status": s.status.value,
                    "output_ref": s.output_ref,
                    "error": s.error,
                    "version_created": s.version_created,
                    "replaces_step_id": s.replaces_step_id,
                }
                for s in self.steps
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Plan":
        """从序列化状态重建计划（崩溃恢复：跳过已完成步骤、从断点续跑）。"""
        steps = [
            PlanStep(
                step_id=s["step_id"],
                action=s["action"],
                params=s.get("params", {}),
                depends_on=tuple(s.get("depends_on", ())),
                is_terminal=s.get("is_terminal", False),
                status=StepStatus(s.get("status", StepStatus.PENDING.value)),
                output_ref=s.get("output_ref"),
                error=s.get("error"),
                version_created=s.get("version_created", 1),
                replaces_step_id=s.get("replaces_step_id"),
            )
            for s in data.get("steps", [])
        ]
        plan = cls(steps=steps)
        plan.version = data.get("version", 1)
        plan.last_error = data.get("last_error")
        return plan


class Planner(Protocol):
    """规划器（可注入 Fake 做确定性测试）。"""

    async def generate(
        self, task: str, tool_names: list[str], constraints: str = "", skills: str = ""
    ) -> Plan: ...

    async def replan(
        self,
        plan: Plan,
        failed_step: PlanStep,
        error: str,
        tool_names: Sequence[str],
        skills: str = "",
    ) -> list[PlanStep]: ...


class _PlanStepModel(BaseModel):
    """单个计划步骤的结构契约（第二层防线：字段名/类型强制）。

    字段语义写进 ``description``，``model_json_schema()`` 直接产出带语义的完整 schema，
    供提示词格式说明引用（不再手写 JSON 示例）。
    """

    id: str = Field(
        description="批内唯一标签，供 depends_on/replaces 引用；撞车系统改名，无需跨计划唯一"
    )
    action: str = Field(description="要调用的工具名")
    params: dict[str, Any] = Field(default_factory=dict, description="工具参数对象")
    depends_on: list[str] = Field(
        default_factory=list, description="依赖步骤 id 列表；无依赖时为空数组 []"
    )
    terminal: bool = Field(default=False, description="是否产出最终答案的步骤")
    replaces: str | None = Field(
        default=None, description="重规划时声明被替换的旧步骤 id，否则为 null"
    )


class PlanModel(BaseModel):
    """DAG 顶层结构契约。"""

    steps: list[_PlanStepModel] = Field(default_factory=list, description="步骤数组")


def parse_plan(raw: str) -> Plan:
    """解析 LLM 输出的 JSON DAG；容忍代码块包裹；解析/结构失败返回空 plan。

    结构层失败（字段名漂移、params 非对象、depends_on 非数组等）不再像手写
    ``.get()`` 那样静默转成空串/None，而是整体判空——由 ``generate`` 的自纠错循环
    回喂精确错误后再降级。
    """
    try:
        return parse_plan_strict(raw)
    except StructuredParseError:
        return Plan(steps=())


def parse_plan_strict(raw: str) -> Plan:
    """严格解析：语法/结构任一失败抛 StructuredParseError（带精确错误）。"""
    out = parse_json(raw, PlanModel)
    # id 非空 + 唯一：必须在构造 Plan（内部 dict 去重）之前校验，否则重复 id 会被静默折叠
    seen_ids: set[str] = set()
    for s in out.steps:
        if not s.id:
            raise StructuredParseError("存在缺失 id 的步骤")
        if s.id in seen_ids:
            raise StructuredParseError(f"步骤 id 重复：{s.id!r}")
        seen_ids.add(s.id)
    steps = tuple(
        PlanStep(
            step_id=s.id,
            action=s.action,
            params=s.params,
            depends_on=tuple(s.depends_on),
            is_terminal=s.terminal,
            replaces_step_id=s.replaces,
        )
        for s in out.steps
    )
    return Plan(steps=steps)


def invalid_tools(steps: Sequence[PlanStep], tool_names: Sequence[str]) -> list[PlanStep]:
    """返回工具名不在可用工具集内的步骤（空 = 全部合法）。

    供 ``validate_plan``（收集精确错误）与 ``Plan.merge``（并入边界拦截）复用。
    """
    valid = set(tool_names)
    return [s for s in steps if s.action not in valid]


def _has_cycle(steps_map: dict[str, PlanStep]) -> bool:
    """Kahn 拓扑检测环：入度为 0 的节点反复出队，访问不全即存在环。

    纯函数，只收「已构好的图」（id → 步骤）；构图（全量 / 投影排除作废）由调用方负责。
    """
    indegree: dict[str, int] = {sid: 0 for sid in steps_map}
    children: dict[str, list[str]] = {sid: [] for sid in steps_map}
    for sid, s in steps_map.items():
        for dep in s.depends_on:
            if dep in steps_map:
                children[dep].append(sid)
                indegree[sid] += 1
    queue = deque(sid for sid, d in indegree.items() if d == 0)
    visited = 0
    while queue:
        sid = queue.popleft()
        visited += 1
        for child in children[sid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    return visited != len(steps_map)


def validate_plan(plan: Plan, tool_names: Sequence[str]) -> list[str]:
    """语义校验 DAG：工具名合法、依赖步骤存在、无自依赖、无环。

    id 非空/唯一已在 ``parse_plan_strict``（构造 Plan 之前）校验，故这里不再重复——
    Plan 内部以 dict 按 id 去重，重复 id 在构造时已被折叠、无法在此二次检测。

    返回错误列表（空 = 合法）。这是文章「第二层防线」的业务层——Pydantic 只保证
    「字段名与类型合法」，这里进一步保证「值有意义」：模型幻觉一个不存在的工具名，
    在契约边界就被拦下，而不是拖到 executor 执行时才报「未知工具」烧一次 replan。
    """
    errors: list[str] = []
    valid_tools = set(tool_names)
    all_ids = {s.step_id for s in plan.steps}
    for step in invalid_tools(plan.steps, tool_names):
        errors.append(
            f"步骤 {step.step_id} 的工具 {step.action!r} 不在可用工具集 {sorted(valid_tools)!r} 中"
        )
    for step in plan.steps:
        for dep in step.depends_on:
            if dep not in all_ids:
                errors.append(f"步骤 {step.step_id} 依赖的步骤 {dep!r} 不存在")
            elif dep == step.step_id:
                errors.append(f"步骤 {step.step_id} 不能依赖自身")
    # 环检测（复用 _has_cycle）：仅当无结构性错误时才做，避免在已坏计划上二次报错
    if not errors and plan.steps and _has_cycle({s.step_id: s for s in plan.steps}):
        errors.append("计划存在循环依赖")
    return errors
