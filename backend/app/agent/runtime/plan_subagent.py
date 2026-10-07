"""plan 子 Agent：复用 plan 图（``build_plan_graph`` + for），独立窗口规划执行，只回结论。

与 reactive 子 Agent（``RagSubagent``）对等：同一套 plan 图，仅靠「工具集 + checkpointer +
独立初始 state」区分。图在 ``run`` 时动态构建（需从 ContextVar 取主循环 tracker/run_id 共享
成本），工具集由调用方筛好传入（全套、含 spawn，递归靠预算兜底），checkpointer 用内存版（独立
窗口、不持久化、不撞主循环 thread_id）。

约束由父 Agent 显式下传：``run(task, constraints)`` 的 ``constraints`` 写入过滤后 rt 的
``constraints`` 字段，planner 的 ``plan_node`` 从 ``rt.constraints`` 取红线约束。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.gateway_context import current
from app.agent.handoff import HandoffPacket
from app.agent.orchestrate import make_config
from app.agent.runtime.budget import BudgetTracker
from app.agent.runtime.plan_graph import build_plan_graph
from app.agent.toolmeta import ToolRegistry

if TYPE_CHECKING:
    from app.agent.compose import CopilotRuntime

PLAN_SUBAGENT_SYSTEM = (
    "你是任务规划执行子 Agent。根据任务规划出步骤并逐步执行，最后给出简明结论。"
    "只输出结论，不要解释过程。"
)


class PlanSubagent:
    """plan 子 Agent 门面：``spawn_plan`` 工具调它，复用 plan 图跑完只回结论（截断）。"""

    def __init__(
        self,
        rt: CopilotRuntime,
        *,
        tools: list[BaseTool],
        registry: ToolRegistry,
        system_prompt: str = PLAN_SUBAGENT_SYSTEM,
        max_result_chars: int = 2000,
    ) -> None:
        # 过滤后的 rt：工具集换成全套（含 spawn，递归靠预算兜底），checkpointer 换内存版（独立窗口）
        tool_map = {t.name: t for t in tools}
        self._rt = replace(
            rt,
            tools=tools,
            registry=registry,
            tool_map=tool_map,
            checkpointer=InMemorySaver(),
        )
        self._system_prompt = system_prompt
        self._max_result_chars = max_result_chars

    async def run(self, packet: HandoffPacket) -> str:
        """复用 plan 图跑子任务（共享主循环 tracker/run_id、独立 turn），截断后回结论。"""
        task = packet.to_task_prompt()
        try:
            ctx = current()  # spawn_plan 工具在 run_budget 里，此处即主循环 run context
            tracker = ctx.tracker
            run_id = ctx.run_id
        except RuntimeError:
            # 测试/无网关路径：自建独立账本
            tracker = BudgetTracker(self._rt.runtime.budget, sink=self._rt.runtime.daily_budget)
            run_id = str(uuid.uuid4())

        # 约束显式下传：写入过滤后 rt 的 constraints，planner 的 plan_node 从 rt.constraints 取
        constraints_text = "\n".join(packet.constraints) if packet.constraints else ""
        rt = (
            replace(self._rt, constraints={"constraints": constraints_text})
            if constraints_text
            else self._rt
        )
        graph = build_plan_graph(rt, tracker)
        config = make_config(rt, uuid.UUID(run_id), uuid.uuid4(), task)
        plan_state: dict[str, Any] = {
            "task": task,
            "system_prompt": self._system_prompt,
            "memory_block": "",
            "skills_block": "",
            "plan": {},
            "results": {},
            "failures": {},
            "trace": [],
            "completed_steps": [],
            "final_answer": "",
            "plan_error": "",
            "step_id": "",
            "last_error": None,
        }
        result = await graph.ainvoke(plan_state, config=config)
        final_answer = str(result.get("final_answer", ""))
        if len(final_answer) > self._max_result_chars:
            final_answer = final_answer[: self._max_result_chars] + "…"
        return final_answer
