"""执行模式分派入口：意图 → 选择并运行 planner 图 / reactive 图。

两张图（planner / reactive）独立演进；本文件是唯一「选哪个入口」的分派点，
把 run() 里的分流收敛到一处，不把 reactive 当统一入口。run() 只做会话/记账/
上下文准备，然后按意图调 ``run_plan`` / ``run_reactive``。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.agent.compose import CopilotRuntime
from app.agent.events import CopilotStreamEvent
from app.agent.orchestrate import (
    build_input_messages,
    commit_assistant,
    finalize_answer,
    make_config,
    stream_graph,
)
from app.agent.runtime.budget import BudgetTracker
from app.agent.runtime.context import RunState
from app.agent.runtime.plan_graph import build_plan_graph, stream_plan_graph
from app.agent.runtime.router import Intent
from app.agent.session import RunSession
from app.agent.tools import QA_TOOL_NAMES

logger = logging.getLogger(__name__)


def is_plan(intent: Intent) -> bool:
    """意图是否为长程多步任务（走 planner 图）。"""
    return intent == Intent.PLAN


async def run_plan(
    rt: CopilotRuntime,
    *,
    run_id: uuid.UUID,
    conversation_id: uuid.UUID,
    assistant_message_id: uuid.UUID,
    tracker: BudgetTracker,
    system_prompt: str,
    memory_block: str,
    question: str,
) -> AsyncIterator[CopilotStreamEvent]:
    """planner 图入口：build_plan_graph → stream_plan_graph（合成已在图内完成）。"""
    graph = build_plan_graph(rt, tracker)
    config = make_config(rt, run_id, conversation_id, question)
    plan_state: dict[str, Any] = {
        "task": question,
        "system_prompt": system_prompt,
        "memory_block": memory_block,
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
    async for event in stream_plan_graph(
        rt, graph, config, run_id, plan_state, conversation_id, assistant_message_id, tracker
    ):
        yield event


async def run_reactive(
    rt: CopilotRuntime,
    *,
    run_id: uuid.UUID,
    conversation_id: uuid.UUID,
    assistant_message_id: uuid.UUID,
    tracker: BudgetTracker,
    system_prompt: str,
    memory_block: str,
    reminder: str,
    question: str,
    intent: Intent,
) -> AsyncIterator[CopilotStreamEvent]:
    """reactive 图入口：graph_builder → stream_graph → 收尾落库。"""
    base_input = await build_input_messages(rt, conversation_id, question)
    # QA 走主循环但限只读检索工具集（含 spawn_rag）；TASK 用全量工具。
    tool_names = QA_TOOL_NAMES if intent == Intent.QA else None
    graph = rt.graph_builder(tracker=tracker, tool_names=tool_names)
    config = make_config(rt, run_id, conversation_id, question)

    # 流式累加器：delta 文本 / 工具轨迹 / review 步骤随流写入，流结束后取最终回答。
    session = RunSession(write_tool_names=rt.write_tool_names)

    initial_state: dict[str, Any] = {
        "messages": list(base_input),
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
    committed = False
    try:
        async for event in stream_graph(
            rt, graph, config, run_id, initial_state, session, conversation_id, assistant_message_id
        ):
            yield event
        session.flush_review()
        final_answer = finalize_answer(rt, session.answer_parts, session.behavior_tracker.score())
        yield await commit_assistant(
            rt,
            run_id,
            conversation_id,
            assistant_message_id,
            final_answer,
            session.all_steps,
            tracker,
            intent.value,
            run_state=session.run_state,
        )
        committed = True
    finally:
        # 中断（前端 abort → GeneratorExit / CancelledError）：兜底落库已累计的部分回答，
        # 避免刷新后看不到之前的内容。
        logger.info(
            "[copilot] run_reactive 收尾 committed=%s parts=%d",
            committed,
            len(session.answer_parts),
        )
        if not committed and session.answer_parts:
            session.flush_review()
            partial = finalize_answer(rt, session.answer_parts, session.behavior_tracker.score())
            if partial:
                await asyncio.shield(
                    commit_assistant(
                        rt,
                        run_id,
                        conversation_id,
                        assistant_message_id,
                        partial,
                        session.all_steps,
                        tracker,
                        intent.value,
                        run_state=RunState.INTERRUPTED.value,
                    )
                )
