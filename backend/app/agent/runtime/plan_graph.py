"""planner 执行图：supervisor-worker 静态图 + 动态 Plan 状态。

把原 ``PlanRunner._execute_plan``（graph 外的 ``iter_plan_execution`` 拓扑循环）迁进
LangGraph 图：plan 生成 → supervisor 标记 COMPLETED/FAILED + 算 ready 标记 RUNNING →
dispatch 经 ``Send`` 扇出 worker 并行执行 → worker 回写 results/failures/trace → supervisor
增量 replan → 全部完成后 finalize 挑 terminal 合成产物 + review 自检。

与 reactive 同接安全闸：四轴预算、写工具 HITL（P3 起）、工具结果零信任处置、参数契约
校验、幂等键注入。崩溃恢复统一走 LangGraph checkpointer。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.agent.compose import CopilotRuntime
from app.agent.events import (
    CopilotApprovalEvent,
    CopilotDeltaEvent,
    CopilotStepEvent,
    CopilotStreamEvent,
)
from app.agent.gateway import run_budget
from app.agent.guardrail.review import ReviewResult, ReviewVerdict
from app.agent.guardrail.review_node import format_trace, write_side_effects_from_trace
from app.agent.guardrail.trust import sanitize_content
from app.agent.helpers import TOOL_RESULT_CHARS, bounded_call, clip, tool_source
from app.agent.memory import format_skills_block
from app.agent.orchestrate import (
    SKILL_BLOCK_MAX_TOKENS,
    commit_assistant,
    finalize_answer,
    log,
    recall_relevant_skills,
    record_approval,
    wrap_question,
)
from app.agent.resilience.error_classifier import classify_error
from app.agent.runtime.budget import BudgetExceeded, BudgetTracker, Usage
from app.agent.runtime.chain_optimizer import analyse_plan
from app.agent.runtime.planner import SYNTHESIZE_ACTION, Plan, PlanStep, StepStatus
from app.agent.runtime.planner_state import PlannerState
from app.agent.runtime.reactive_helpers import resolve_approvals
from app.agent.toolmeta import (
    apply_output_contract,
    idempotency_key_for,
    validate_param_contract,
)

logger = logging.getLogger(__name__)


def build_plan_graph(rt: CopilotRuntime, tracker: BudgetTracker) -> Any:
    """构建 planner 图：plan → supervisor ⇄ dispatch/worker → finalize → review。"""
    planner = rt.planner
    # 规划候选工具：HITL 已就绪（worker 内 interrupt 审批），REQUIRE_APPROVAL 不再剔除；
    # 熔断工具仍剔除（注意力铁律 #5）。
    names = [tool.name for tool in rt.tools]
    resources = {name: meta.resource for name, meta in rt.registry.items()}
    tool_names = rt.breaker.available(names, resources)
    # 合成步骤是保留动作（不注册进工具集），加入规划器可用动作白名单，避免被 validate_plan 判幻觉。
    tool_names = list(tool_names) + [SYNTHESIZE_ACTION]

    async def plan_node(state: PlannerState, config: RunnableConfig) -> dict[str, Any]:
        run_id = str(config.get("configurable", {}).get("thread_id", ""))
        # 红线约束只取「宪法铁律 + constraint 型硬召回」——四型记忆里的偏好/事实/情节是
        # 上下文不是红线（合成步骤才用），由 assemble_context 每轮写入 rt.constraints。
        constraints = rt.constraints.get("constraints", "")
        # skill 语义召回（检索式预选）：planner 拆解前就知道「怎么做」的经验。fail-open——
        # 召回失败（预算/熔断/embedding 异常）降级为空块、不阻断规划，真正的预算硬停由下方
        # planner.generate 的 preflight 统一触发。
        skills_block = ""
        try:
            selected = await recall_relevant_skills(rt, state["task"], tracker, run_id)
            skills_block = format_skills_block(selected, SKILL_BLOCK_MAX_TOKENS)
        except Exception:  # noqa: BLE001 - 技能召回是可选增强，失败不阻断规划
            skills_block = ""
        with run_budget(tracker, run_id=run_id):
            plan = await planner.generate(
                wrap_question(rt, state["task"]),
                tool_names,
                constraints=constraints,
                skills=skills_block,
            )
        # 计划诊断：只落报告不打回（长参数链是合法形态，不因「串行链太长」砍断）。
        report = analyse_plan(plan, rt.registry) if plan.steps else None
        # 执行预算随计划规模伸缩：tool_calls 轴按步数上调（turns/seconds/cost 轴全局兜底）。
        tracker.scale_tool_calls_for_plan(len(plan.steps))
        # 事件溯源：plan_created（供 resume 判断 run 模式）+ 诊断结论
        await log(
            rt,
            uuid.UUID(run_id),
            "plan_created",
            {
                "version": plan.version,
                "steps": [
                    {"step_id": s.step_id, "action": s.action, "depends_on": list(s.depends_on)}
                    for s in plan.steps
                ],
            },
        )
        if report is not None:
            await log(rt, uuid.UUID(run_id), "plan_review", report.to_dict())
        return {"plan": plan.to_dict(), "skills_block": skills_block}

    async def supervisor_node(state: PlannerState, config: RunnableConfig) -> dict[str, Any]:
        plan = Plan.from_dict(state["plan"])
        results = state.get("results", {})
        failures = state.get("failures", {})
        run_id = str(config.get("configurable", {}).get("thread_id", ""))
        # 1. 从 results 推导 COMPLETED（幂等，worker 完成的 step）
        for step in plan.steps:
            if step.step_id in results and step.status is StepStatus.RUNNING:
                step.status = StepStatus.COMPLETED
                step.output_ref = results[step.step_id]
        # 2. 从 failures 推导 FAILED + 增量 replan（有界：replan 失败即终止）
        for step in plan.steps:
            if step.step_id in failures and step.status is StepStatus.RUNNING:
                step.status = StepStatus.FAILED
                step.error = failures[step.step_id]
                plan.mark_downstream_obsolete(step.step_id)
                try:
                    with run_budget(tracker, run_id=run_id):
                        replacement = list(
                            await planner.replan(
                                plan,
                                step,
                                failures[step.step_id],
                                tool_names,
                                skills=state.get("skills_block", ""),
                            )
                        )
                    if replacement:
                        plan.merge(replacement, tool_names)
                    else:
                        return {"plan": plan.to_dict(), "plan_error": failures[step.step_id]}
                except ValueError:
                    return {"plan": plan.to_dict(), "plan_error": failures[step.step_id]}
        # 3. 算 ready，标记 RUNNING（供 dispatch Send 派发）
        for step in plan.get_parallel_ready():
            step.status = StepStatus.RUNNING
        return {"plan": plan.to_dict()}

    def route_after_supervisor(state: PlannerState) -> list[Send] | str:
        plan = Plan.from_dict(state["plan"])
        running = [s for s in plan.steps if s.status is StepStatus.RUNNING]
        if running:
            # Send 的 arg 是 worker 的完整输入（非 merge 进共享 state），须自带 plan。
            plan_dict = state["plan"]
            return [Send("worker", {"step_id": s.step_id, "plan": plan_dict}) for s in running]
        return "finalize"

    async def _synthesize_step(
        plan: Plan, step: PlanStep, state: PlannerState, run_id: str, run_uuid: uuid.UUID
    ) -> dict[str, Any]:
        """合成步骤：不调工具，收集 depends_on 步骤产物后调 LLM 写成最终回答。"""
        parts: list[str] = []
        for dep_id in step.depends_on:
            dep = plan.get_step(dep_id)
            if dep is not None and dep.output_ref is not None:
                parts.append(f"[{dep_id}] {dep.output_ref}")
        summary = "\n".join(parts)
        task = state.get("task", "")
        user_content = f"任务：{task}\n\n工具执行结果：\n{summary}\n\n请给出最终回答。"
        skills_block = state.get("skills_block", "")
        if skills_block:
            user_content = f"{skills_block}\n\n{user_content}"
        memory_block = state.get("memory_block", "")
        if memory_block:
            user_content = f"{memory_block}\n\n{user_content}"
        messages = [
            SystemMessage(
                content=state.get("system_prompt", "")
                or "你是任务执行助手，根据工具执行结果回答用户任务，不要编造。"
            ),
            HumanMessage(content=user_content),
        ]
        with run_budget(tracker, run_id=run_id):
            response = await rt.gateway.invoke_model("plan.finalize_answer", rt.model, messages)
        answer = str(response.content)
        await log(
            rt,
            run_uuid,
            "tool_result",
            {"tool_name": SYNTHESIZE_ACTION, "content": clip(answer, TOOL_RESULT_CHARS)},
        )
        # 合成步骤是内部动作，不产出用户可见的 step 事件，故不写 completed_steps。
        return {
            "results": {step.step_id: answer},
            "trace": [
                {
                    "tool": SYNTHESIZE_ACTION,
                    "args": {},
                    "result": answer,
                    "ok": True,
                    "step_id": step.step_id,
                }
            ],
        }

    async def worker_node(state: PlannerState, config: RunnableConfig) -> dict[str, Any]:
        run_id = str(config.get("configurable", {}).get("thread_id", ""))
        run_uuid = uuid.UUID(run_id)
        step_id = state["step_id"]
        plan = Plan.from_dict(state["plan"])
        step = plan.get_step(step_id)
        if step is None:
            return {"failures": {step_id: f"未知步骤 {step_id}"}}
        name = step.action
        # 合成步骤：不调工具，走调 LLM 合成分支（产物即最终回答，写入 results）。
        if name == SYNTHESIZE_ACTION:
            return await _synthesize_step(plan, step, state, run_id, run_uuid)
        params = step.params
        meta = rt.registry.get(name)
        # 防线② 参数级校验：违规 fail-closed 返回 failures（触发 replan 而非脏参数执行）
        violation = validate_param_contract(
            name, params, meta.param_contract if meta is not None else None
        )
        if violation is not None:
            return {
                "failures": {step_id: violation},
                "trace": [
                    {
                        "tool": name,
                        "args": params,
                        "result": violation,
                        "ok": False,
                        "step_id": step_id,
                    }
                ],
            }
        # 防线③ 写工具分级审批：REQUIRE_APPROVAL 挂起等人裁决，拒绝则返回 failures（触发 replan）
        denied = resolve_approvals(
            [{"name": name, "args": params, "id": step_id}], rt.registry, rt.runtime
        )
        if denied is not None:
            return {
                "failures": {step_id: "用户拒绝执行该写操作。"},
                "trace": [
                    {
                        "tool": name,
                        "args": params,
                        "result": "用户拒绝",
                        "ok": False,
                        "step_id": step_id,
                    }
                ],
            }
        # 幂等键注入（业务意图派生，跨重试稳定）
        if meta is not None and meta.enforced_idempotent and meta.idempotency_key_fields:
            params = {
                **params,
                "idempotency_key": idempotency_key_for(
                    name, params, meta.idempotency_key_fields, run_id
                ),
            }
        await log(rt, run_uuid, "tool_call", {"tool_name": name, "args": params})
        tool = rt.tool_map.get(name)
        if tool is None:
            return {
                "failures": {step_id: f"未知工具 {name}"},
                "trace": [
                    {
                        "tool": name,
                        "args": params,
                        "result": f"未知工具 {name}",
                        "ok": False,
                        "step_id": step_id,
                    }
                ],
            }
        # 防线② 工具调用量上限（第五轴）：执行前计数 + 预检，超限抛 BudgetExceeded 硬停。
        # 合成步骤不调工具（走 LLM 合成），不计此轴。
        tracker.record_tool_calls(1)
        try:
            with run_budget(tracker, run_id=run_id):
                raw = str(
                    await bounded_call(
                        tool.ainvoke(params), tracker, lambda _r: Usage(), label=name
                    )
                )
        except Exception as exc:  # noqa: BLE001 - 崩溃现场分类标签随 failures 回传
            last_error = {"message": str(exc), "kind": classify_error(exc), "tool": name}
            await log(rt, run_uuid, "tool_result", {"tool_name": name, "content": str(exc)})
            return {
                "failures": {step_id: str(exc)},
                "trace": [
                    {
                        "tool": name,
                        "args": params,
                        "result": str(exc),
                        "ok": False,
                        "step_id": step_id,
                    }
                ],
                "last_error": last_error,
            }
        # 工具结果零信任处置：sanitize（trace 用完整），output_contract 裁剪（results 用）
        sanitized = sanitize_content(raw, tool_source(name, rt.registry))
        result = apply_output_contract(
            name, sanitized, meta.output_contract if meta is not None else None
        )
        await log(
            rt,
            run_uuid,
            "tool_result",
            {"tool_name": name, "content": clip(result, TOOL_RESULT_CHARS)},
        )
        return {
            "results": {step_id: result},
            "trace": [
                {
                    "tool": name,
                    "args": params,
                    "result": sanitized,
                    "ok": True,
                    "step_id": step_id,
                }
            ],
            "completed_steps": [{"step_id": step_id, "action": name, "params": params}],
        }

    async def finalize_node(state: PlannerState, config: RunnableConfig) -> dict[str, Any]:
        plan = Plan.from_dict(state["plan"])
        if state.get("plan_error"):
            return {"final_answer": f"抱歉，任务执行失败：{state['plan_error']}"}
        if not plan.steps:
            return {"final_answer": "抱歉，我没能规划出可执行的步骤，请换个更具体的说法。"}
        results = state.get("results", {})
        # 只挑 terminal 合成步骤的产物作为最终回答（合成已在 worker 内完成，此处不再汇总/调 LLM）。
        terminal = next(
            (s for s in plan.steps if s.is_terminal and s.status is StepStatus.COMPLETED),
            None,
        )
        final_answer = results.get(terminal.step_id) if terminal is not None else ""
        if not final_answer:
            final_answer = "抱歉，任务未能产出最终答案。"
        return {"final_answer": final_answer}

    async def review_node(state: PlannerState, config: RunnableConfig) -> dict[str, Any]:
        trace = state.get("trace", [])
        final_answer = state.get("final_answer", "")
        run_id = str(config.get("configurable", {}).get("thread_id", ""))
        # 确定性副作用对账先于 LLM 判定（防「伪造证据骗校验器」）
        for name, _args, tool_result in write_side_effects_from_trace(trace, rt.write_tool_names):
            reason = await rt.verifier.verify(name, _args, tool_result)
            if reason is not None:
                return {
                    "final_answer": final_answer + f"\n\n> ⚠️ 自检更正：我上面声称已完成「{name}」，"
                    "但该写操作的副作用并未真正落库。请以实际执行结果为准。"
                }
        try:
            with run_budget(tracker, run_id=run_id):
                result = await rt.reviewer.review(final_answer, format_trace(trace))
        except BudgetExceeded:
            raise
        except Exception:  # noqa: BLE001 - 审查器失能 fail-closed，不阻断已完成的回答
            result = ReviewResult(verdict=ReviewVerdict.UNVERIFIED)
        if result.verdict is ReviewVerdict.OK:
            return {"final_answer": final_answer}
        if result.verdict is ReviewVerdict.UNVERIFIED:
            return {
                "final_answer": final_answer
                + "\n\n> ⚠️ 自检未完成：本次回答未能完成完整性审查，请以实际执行结果为准。"
            }
        claims = "、".join(i.claim for i in result.issues) or "上述步骤"
        return {
            "final_answer": final_answer
            + f"\n\n> ⚠️ 自检更正：我上面声称已完成「{claims}」，但与计划实际执行结果不一致。"
            "请以工具执行结果为准。"
        }

    graph = StateGraph(PlannerState)
    graph.add_node("plan", plan_node)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("worker", worker_node)
    graph.add_node("finalize", finalize_node)
    graph.add_node("review", review_node)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "supervisor")
    graph.add_conditional_edges("supervisor", route_after_supervisor, ["worker", "finalize"])
    graph.add_edge("worker", "supervisor")
    graph.add_edge("finalize", "review")
    graph.add_edge("review", END)
    return graph.compile(checkpointer=rt.checkpointer)


async def stream_plan_graph(
    rt: CopilotRuntime,
    graph: Any,
    config: dict[str, Any],
    run_id: uuid.UUID,
    initial_state: Any,
    conversation_id: uuid.UUID,
    assistant_message_id: uuid.UUID,
    tracker: BudgetTracker,
) -> AsyncIterator[CopilotStreamEvent]:
    """流式执行 planner 图：worker 完成 → step 事件；finalize/review → delta；收尾 done。"""
    all_steps: list[dict[str, Any]] = []
    final_answer = ""
    try:
        async for _mode, payload in graph.astream(
            initial_state, config=config, stream_mode=["updates"]
        ):
            if "__interrupt__" in payload:
                # 写工具审批挂起（并行可多个 interrupt）：逐张落审批单 + 发 approval 事件。
                # interrupt.id 是 LangGraph resume map 的键、approval_id 是前端逐单回传裁决
                # 的主键，二者都在此固化，续批时按 interrupt_id 精确路由每张单的裁决。
                for interrupt in payload["__interrupt__"]:
                    approval = interrupt.value
                    approval_id = (
                        uuid.UUID(approval["approval_id"])
                        if approval.get("approval_id")
                        else uuid.uuid4()
                    )
                    _interrupt_id = getattr(interrupt, "id", None)
                    await record_approval(
                        rt,
                        run_id,
                        approval_id,
                        str(_interrupt_id) if _interrupt_id is not None else None,
                        approval.get("tool", ""),
                        approval.get("args") or {},
                        approval.get("summary", ""),
                        approval.get("level", "high"),
                        conversation_id,
                        assistant_message_id,
                    )
                    yield CopilotApprovalEvent(
                        approval_id,
                        str(run_id),
                        approval.get("tool", ""),
                        approval.get("args") or {},
                        approval.get("summary", ""),
                        approval.get("level", "high"),
                    )
                return
            for node, update in payload.items():
                if node == "worker":
                    for step in update.get("completed_steps", []):
                        all_steps.append({"tool_name": step["action"], "args": step["params"]})
                        yield CopilotStepEvent(step["action"], step["params"])
                elif node in ("finalize", "review"):
                    if update.get("final_answer"):
                        final_answer = update["final_answer"]
    except Exception as exc:  # noqa: BLE001 - 兜底：记录错误后上抛，由路由层发 error 事件
        await log(rt, run_id, "error", {"message": str(exc)})
        raise
    final_answer = finalize_answer(rt, [final_answer], 100.0)
    yield CopilotDeltaEvent(final_answer)
    yield await commit_assistant(
        rt, run_id, conversation_id, assistant_message_id, final_answer, all_steps, tracker, "plan"
    )
