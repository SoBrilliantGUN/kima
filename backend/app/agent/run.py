"""Copilot 对话运行时主入口：``run`` 模块级函数。

注入记忆 → graph.astream → SSE + 事件日志落库。原 `CopilotService.run` 收敛为模块级
函数：意图路由 → 拒绝分支 → 建四轴账本 → 组装上下文 → 按意图分派（plan/reactive，
见 `runtime/entry.py`）→ 收尾。只读查询（技能清单/记忆快照）已搬出本模块，见 `routes/copilot.py`。

`thread_id` 用 `run_id`（每 run 唯一）：多轮上下文靠「注入会话历史」而非 checkpoint 续跑。

「run 与请求解耦」后拆成两段：`prepare_run`（建会话/用户消息/assistant 占位 + 生成
run_id，同步返回 ids 供 POST 立即响应）与 `stream_run`（真正的图流式，后台任务驱动）。
`run` 是两者组合，保留给既有调用方/测试。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.agent.compose import CopilotRuntime
from app.agent.events import CopilotMetaEvent, CopilotStreamEvent
from app.agent.orchestrate import (
    assemble_context,
    get_or_create_conversation,
    make_tracker,
    reject_run,
)
from app.agent.runtime.entry import run_reactive
from app.agent.runtime.router import Intent, classify_intent
from app.agent.runtime.workflow import COMPLAINT_RESPONSE, REJECT_RESPONSE
from app.models.chat import ChatMessage, ChatRole
from app.schemas.copilot import CopilotRequest

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedRun:
    """prepare_run 的产物：POST 响应直接返回的四个 id，后台任务据此驱动 stream_run。"""

    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID
    run_id: uuid.UUID


async def prepare_run(rt: CopilotRuntime, request: CopilotRequest) -> PreparedRun:
    """同步准备：日预算预检 + 建会话 + 落用户消息与 assistant 空占位 + 生成 run_id。"""
    rt.runtime.daily_budget.check()
    assistant_message_id = uuid.uuid4()
    async with rt.db_lock:
        conversation = await get_or_create_conversation(rt, request)
        user_message = await rt.chat_repository.add_message(
            ChatMessage(
                conversation_id=conversation.id, role=ChatRole.USER, content=request.question
            )
        )
        # 落库空 assistant 占位：流式结束时 commit_assistant 回填 content/steps，
        # 中断/断开时至少保留占位，刷新后不丢「AI 已回复」这一事实。
        await rt.chat_repository.add_message(
            ChatMessage(
                id=assistant_message_id,
                conversation_id=conversation.id,
                role=ChatRole.ASSISTANT,
                content="",
            )
        )
        logger.info(
            "[copilot] 落库 assistant 占位 conv=%s msg=%s", conversation.id, assistant_message_id
        )
    run_id = uuid.uuid4()
    return PreparedRun(conversation.id, user_message.id, assistant_message_id, run_id)


async def stream_run(
    rt: CopilotRuntime, request: CopilotRequest, prepared: PreparedRun
) -> AsyncIterator[CopilotStreamEvent]:
    """驱动一轮 Copilot 对话的流式部分（meta 起，done/error 止），不含会话准备。"""
    yield CopilotMetaEvent(
        prepared.conversation_id, prepared.user_message_id, prepared.assistant_message_id
    )

    # 意图路由：投诉/注入走确定性分支（不碰工具/不写记忆/不进模型循环）
    intent = classify_intent(request.question)
    if intent in (Intent.INJECTION, Intent.COMPLAINT):
        response = REJECT_RESPONSE if intent == Intent.INJECTION else COMPLAINT_RESPONSE
        async for event in reject_run(
            rt,
            prepared.run_id,
            prepared.conversation_id,
            prepared.assistant_message_id,
            response,
            request.question,
            intent,
        ):
            yield event
        return

    # 本 run 四轴账本（service 层创建、下传共用）：召回 embedding / 历史摘要 / 图内 LLM
    # 全接网关，须先建 tracker 供它们的 run_budget 使用；run 结束导出单位成本落 done 事件。
    tracker = make_tracker(rt)

    # 约束显式携带：召回 + 组装上移到执行模式之前，planner/qa/synthesizer 与 reactive
    # 共用同一份 soul/user 底线 + 召回约束。
    system_prompt, memory_block, reminder = await assemble_context(
        rt, request.question, prepared.run_id, tracker
    )

    # 主 agent 默认 reactive（不再按意图硬编码路由到 plan）；复杂任务由 reactive loop 里
    # 的 spawn 子 agent 表达「要不要规划」。
    async for event in run_reactive(
        rt,
        run_id=prepared.run_id,
        conversation_id=prepared.conversation_id,
        assistant_message_id=prepared.assistant_message_id,
        tracker=tracker,
        system_prompt=system_prompt,
        memory_block=memory_block,
        reminder=reminder,
        question=request.question,
        intent=intent,
    ):
        yield event


async def run(rt: CopilotRuntime, request: CopilotRequest) -> AsyncIterator[CopilotStreamEvent]:
    """跑一轮 Copilot 对话（prepare + stream 组合，保留给既有调用方/测试）。"""
    prepared = await prepare_run(rt, request)
    async for event in stream_run(rt, request, prepared):
        yield event
