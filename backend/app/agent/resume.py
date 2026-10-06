"""Copilot 对话运行的 HITL 恢复 / 崩溃恢复入口（模块级函数）。

原 `ResumeMixin`（`service_resume.py`）改为模块级函数：负责写工具审批挂起后的续跑
（``resume``）、崩溃恢复（``resume_after_crash``）与审批单实体（第一类实体）的查询。
共用收尾（``stream_graph`` / ``finalize_answer`` / ``commit_assistant``）在 `orchestrate.py`。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from langgraph.types import Command

from app.agent.compose import CopilotRuntime
from app.agent.events import CopilotDeltaEvent, CopilotStreamEvent
from app.agent.orchestrate import commit_assistant, finalize_answer, make_tracker, stream_graph
from app.agent.runtime.plan_graph import build_plan_graph, stream_plan_graph
from app.agent.session import RunSession
from app.models.copilot import CopilotApproval


async def list_pending_approvals(rt: CopilotRuntime) -> list[CopilotApproval]:
    """当前待审审批单（供「找回挂起审批」端点）。"""
    return await rt.approval_store.list_pending(datetime.now(UTC))


async def _has_terminal_event(rt: CopilotRuntime, run_id: uuid.UUID) -> bool:
    """该 run 是否已落终态事件（done/error）——已结束的 run 不应再续跑，防重复副作用。"""
    events = await rt.event_repository.list_events(run_id)
    return any(e.type in ("done", "error") for e in events)


async def _is_plan_run(rt: CopilotRuntime, run_id: uuid.UUID) -> bool:
    """该 run 是否 planner 模式（曾落 plan_created 事件）——resume 据此选图。"""
    events = await rt.event_repository.list_events(run_id)
    return any(e.type == "plan_created" for e in events)


async def resume(
    rt: CopilotRuntime,
    run_id: str,
    decisions: list[dict[str, Any]],
    conversation_id: uuid.UUID,
    assistant_message_id: uuid.UUID,
) -> AsyncIterator[CopilotStreamEvent]:
    """HITL 恢复：按 run_id 从 checkpoint 续跑，逐单裁决（Command(resume=...) 重放写工具）。

    ``decisions`` 是「审批单 → 裁决」的列表，每项 ``{"approval_id": ..., "decision": ...}``
    ——一张单一个 decision，不再一刀切。并行多 interrupt 时按 ``interrupt_id`` 精确路由
    （LangGraph 1.x 要求 ID 键 resume map）；单 interrupt 用裸值（单值契约）。
    """
    run_uuid = uuid.UUID(run_id)
    # 恢复门禁：已落终态事件（done/error）的 run 不应再续跑，防对已结束 run 重复副作用。
    if await _has_terminal_event(rt, run_uuid):
        yield CopilotDeltaEvent("该 run 已结束，无需恢复。")
        return
    # 审批单逐张裁决 + 超时 fail-close：过期即拒绝（写操作不执行）。逐单各自判、互不污染
    # （旧实现用同一变量承载 decision，一张过期会把后续未过期单也污染成 reject）。
    approvals = await rt.approval_store.get_pending(run_uuid)
    n = len(approvals)
    if n == 0:
        # 无待审单（已被裁决 / 审批单落库降级）：不再续跑，避免对已结束 run 重复副作用。
        yield CopilotDeltaEvent("无待审审批单，无需恢复。")
        return
    decisions_by_id = {uuid.UUID(str(d["approval_id"])): d["decision"] for d in decisions}
    now = datetime.now(UTC)
    resume_map: dict[str, str] = {}
    single_decision: str = "reject"
    for approval in approvals:
        if approval.expires_at is not None and approval.expires_at < now:
            await rt.approval_store.expire(approval.id)
            verdict = "reject"  # fail-close：超时即阻断，不执行写操作
        else:
            # 未给裁决（前端漏发 / 单子已被别的路径裁决）默认拒绝，安全优先。
            verdict = decisions_by_id.get(approval.id, "reject")
            await rt.approval_store.decide(approval.id, verdict, now)
        single_decision = verdict
        if approval.interrupt_id:
            resume_map[approval.interrupt_id] = verdict
    # 单 interrupt 用裸值（LangGraph 1.x 单值契约）；多 interrupt 用 ID 键 map（多值契约，
    # 列表会被 1.x 以 RuntimeError 拒绝）。
    resume_input: Any = single_decision if n == 1 else resume_map
    tracker = make_tracker(rt)
    config: dict[str, Any] = {"configurable": {"thread_id": run_id}}

    # planner 模式：plan 状态在 PlannerState 里，走 plan_graph + stream_plan_graph 续跑
    if await _is_plan_run(rt, run_uuid):
        graph = build_plan_graph(rt, tracker)
        async for event in stream_plan_graph(
            rt,
            graph,
            config,
            run_uuid,
            Command(resume=resume_input),
            conversation_id,
            assistant_message_id,
            tracker,
        ):
            yield event
        return

    graph = rt.graph_builder(tracker=tracker, tool_names=None)

    # HITL 续跑：可信度随各数据块的 <data trust> 标记在 checkpoint 里续传
    session = RunSession(write_tool_names=rt.write_tool_names)

    async for event in stream_graph(
        rt,
        graph,
        config,
        run_uuid,
        Command(resume=resume_input),
        session,
        conversation_id,
        assistant_message_id,
    ):
        yield event

    session.flush_review()
    final_answer = finalize_answer(rt, session.answer_parts, session.behavior_tracker.score())

    yield await commit_assistant(
        rt,
        run_uuid,
        conversation_id,
        assistant_message_id,
        final_answer,
        session.all_steps,
        tracker,
        "task",
        run_state=session.run_state,
    )


async def resume_after_crash(
    rt: CopilotRuntime,
    run_id: str,
    conversation_id: uuid.UUID,
    assistant_message_id: uuid.UUID,
) -> AsyncIterator[CopilotStreamEvent]:
    """宕机/中断后恢复：按 run_id 从 checkpoint 续跑（``None`` 输入触发 LangGraph resume）。

    与 ``resume``（HITL 写工具审批，用 ``Command(resume=decision)``）不同，本方法是
    崩溃恢复：无待审批 interrupt，LangGraph 从最后一个已完成 super-step 的 checkpoint
    续跑，重放崩溃所在节点。LLM 调用级快照（决策 D3）在网关侧兜住「节点内已成功的
    调用不复跑」——该去重待 Phase 5 网关注入后生效，本方法提供续跑入口。

    last_error 的「重试裁决」在 reactive 图内落地、崩溃重放时自动生效：重放 tool_node 时
    读 checkpoint 里的 ``last_error``，permanent 且同工具+同参数（fingerprint 匹配）直接
    拦截不重放（``blocked_permanent_calls``），transient 放行重试；重放 agent_node 时
    ``last_error`` 经 ``format_last_error`` 注入 [STATE] 快照作软信号。本方法不自行读
    last_error，续跑即触发上述裁决。
    """
    run_uuid = uuid.UUID(run_id)
    # 恢复门禁：已落终态事件（done/error）的 run 不应再续跑，防对已结束 run 重复副作用。
    if await _has_terminal_event(rt, run_uuid):
        yield CopilotDeltaEvent("该 run 已结束，无需恢复。")
        return
    tracker = make_tracker(rt)
    config: dict[str, Any] = {"configurable": {"thread_id": run_id}}

    # planner 模式：崩溃恢复同样走 plan_graph（checkpointer 续跑）
    if await _is_plan_run(rt, run_uuid):
        graph = build_plan_graph(rt, tracker)
        async for event in stream_plan_graph(
            rt,
            graph,
            config,
            run_uuid,
            None,
            conversation_id,
            assistant_message_id,
            tracker,
        ):
            yield event
        return

    graph = rt.graph_builder(tracker=tracker, tool_names=None)

    # 崩溃恢复：可信度随各数据块的 <data trust> 标记在 checkpoint 里续传
    session = RunSession(write_tool_names=rt.write_tool_names)

    async for event in stream_graph(
        rt,
        graph,
        config,
        run_uuid,
        None,
        session,  # 空输入 → LangGraph 从 checkpoint 续跑
        conversation_id,
        assistant_message_id,
    ):
        yield event

    session.flush_review()
    final_answer = finalize_answer(rt, session.answer_parts, session.behavior_tracker.score())

    yield await commit_assistant(
        rt,
        run_uuid,
        conversation_id,
        assistant_message_id,
        final_answer,
        session.all_steps,
        tracker,
        "task",
        run_state=session.run_state,
    )
