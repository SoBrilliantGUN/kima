"""Copilot 对话运行时主入口：``run`` 模块级函数。

注入记忆 → graph.astream → SSE + 事件日志落库。原 `CopilotService.run` 收敛为模块级
函数：意图路由 → 拒绝分支 → 建四轴账本 → 组装上下文 → plan 分支（`PlanRunner`）→
reactive 主循环 → 收尾。只读查询（技能清单/记忆快照）已搬出本模块，见 `routes/copilot.py`。

`thread_id` 用 `run_id`（每 run 唯一）：多轮上下文靠「注入会话历史」而非 checkpoint 续跑。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage

from app.agent.compose import CopilotRuntime
from app.agent.events import CopilotMetaEvent, CopilotStreamEvent
from app.agent.orchestrate import (
    assemble_context,
    build_input_messages,
    commit_assistant,
    finalize_answer,
    get_or_create_conversation,
    make_config,
    make_tracker,
    reject_run,
    stream_graph,
)
from app.agent.runtime.context import RunState
from app.agent.runtime.plan_graph import build_plan_graph, stream_plan_graph
from app.agent.runtime.router import Intent, classify_intent
from app.agent.runtime.workflow import COMPLAINT_RESPONSE, REJECT_RESPONSE
from app.agent.session import RunSession
from app.agent.tools import QA_TOOL_NAMES
from app.models.chat import ChatMessage, ChatRole
from app.schemas.copilot import CopilotRequest


async def run(rt: CopilotRuntime, request: CopilotRequest) -> AsyncIterator[CopilotStreamEvent]:
    """跑一轮 Copilot 对话（每 run 一次 agent run，review 自检回环已进图）。"""
    # 全局日预算：run 入口预检（成本/token 跨 run 累计），超限即拒，不落任何副作用
    rt.runtime.daily_budget.check()
    async with rt.db_lock:
        conversation = await get_or_create_conversation(rt, request)
        user_message = await rt.chat_repository.add_message(
            ChatMessage(
                conversation_id=conversation.id, role=ChatRole.USER, content=request.question
            )
        )
    assistant_message_id = uuid.uuid4()
    run_id = uuid.uuid4()
    yield CopilotMetaEvent(conversation.id, user_message.id, assistant_message_id)

    # 意图路由：投诉/注入走确定性分支（不碰工具/不写记忆/不进模型循环）
    intent = classify_intent(request.question)
    if intent in (Intent.INJECTION, Intent.COMPLAINT):
        response = REJECT_RESPONSE if intent == Intent.INJECTION else COMPLAINT_RESPONSE
        async for event in reject_run(
            rt, run_id, conversation, assistant_message_id, response, request.question, intent
        ):
            yield event
        return

    # 本 run 四轴账本（service 层创建、下传共用）：召回 embedding / 历史摘要 / 图内 LLM
    # 全接网关，须先建 tracker 供它们的 run_budget 使用；run 结束导出单位成本落 done 事件。
    tracker = make_tracker(rt)

    # 约束显式携带：召回 + 组装上移到三种执行模式之前，planner/qa/synthesizer 与 reactive
    # 共用同一份 soul/user 底线 + 召回约束。
    system_prompt, memory_block, reminder = await assemble_context(
        rt, request.question, run_id, tracker
    )

    if intent == Intent.PLAN:
        graph = build_plan_graph(rt, tracker)
        config = make_config(rt, run_id, conversation.id, request.question)
        plan_state: dict[str, Any] = {
            "task": request.question,
            "system_prompt": system_prompt,
            "memory_block": memory_block,
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
        async for event in stream_plan_graph(
            rt,
            graph,
            config,
            run_id,
            plan_state,
            conversation.id,
            assistant_message_id,
            tracker,
        ):
            yield event
        return

    base_input = await build_input_messages(rt, conversation.id, request.question)

    # L5 history 单独进 messages；L0/L2/L4 作为固定层字段（agent 节点每轮临时拼装）。
    initial_messages: list[BaseMessage] = list(base_input)

    # QA 走主循环但限只读检索工具集（含 spawn_rag）；TASK 用全量工具。
    tool_names = QA_TOOL_NAMES if intent == Intent.QA else None
    graph = rt.graph_builder(tracker=tracker, tool_names=tool_names)
    config = make_config(rt, run_id, conversation.id, request.question)

    # 流式累加器：delta 文本 / 工具轨迹 / review 步骤随流写入，流结束后取最终回答。
    session = RunSession(write_tool_names=rt.write_tool_names)

    initial_state = {
        "messages": initial_messages,
        "system_prompt": system_prompt,
        "memory_block": memory_block,
        "reminder": reminder,
        "run_state": RunState.RUNNING.value,
        "turn_count": 0,
        "tool_failures": 0,
        "last_action": "",
        "attempts": 0,
        "review_verdict": "",
        "review_issues": [],
        "correction": "",
        "compression_level": 0,
        "last_error": None,
    }
    async for event in stream_graph(
        rt, graph, config, run_id, initial_state, session, conversation.id, assistant_message_id
    ):
        yield event

    session.flush_review()
    final_answer = finalize_answer(rt, session.answer_parts, session.behavior_tracker.score())

    yield await commit_assistant(
        rt,
        run_id,
        conversation.id,
        assistant_message_id,
        final_answer,
        session.all_steps,
        tracker,
        intent.value,
        run_state=session.run_state,
    )
