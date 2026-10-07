"""Copilot 端点：流式对话（SSE）+ 只读记忆面板 + 内置技能清单。

run 与请求解耦：``POST /chat`` 只同步准备（建会话/消息占位）+ 启动后台任务并返回 ids；
``GET /runs/{assistant_message_id}/stream`` 订阅——先按 seq 回放已持久化事件、再 tail 新
事件直到终态；``POST /approve`` 只把裁决回填给后台任务（触发续跑，事件从订阅流来）。
只读查询（记忆面板 / 技能清单 / 待审审批单）不再经过对话运行时，直接依赖底层 store/service。
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.agent.resume import list_pending_approvals
from app.agent.run import prepare_run
from app.agent.run_manager import HEARTBEAT_SECONDS, RunManager, is_terminal_state
from app.api.deps import (
    CopilotMemoryServiceDep,
    CopilotRuntimeDep,
    CopilotStreamEventRepositoryDep,
    MemoryFileStoreDep,
    SkillFileStoreDep,
)
from app.schemas.copilot import (
    CopilotApprovalList,
    CopilotApprovalRead,
    CopilotApproveRequest,
    CopilotChatStarted,
    CopilotCustomSkillList,
    CopilotCustomSkillRead,
    CopilotMemoryList,
    CopilotMemoryRead,
    CopilotRequest,
    CopilotSkillRead,
    CopilotSkillsList,
)

router = APIRouter(prefix="/copilot", tags=["copilot"])


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _get_run_manager(request: Request) -> RunManager:
    manager = getattr(request.app.state, "copilot_run_manager", None)
    if manager is None:
        raise RuntimeError("RunManager 未装配（app.state.copilot_run_manager 必须在场）")
    return cast(RunManager, manager)


@router.post("/chat", response_model=CopilotChatStarted)
async def copilot_chat(
    request: CopilotRequest, rt: CopilotRuntimeDep, http: Request
) -> CopilotChatStarted:
    """启动一轮 Copilot 对话：同步准备会话/消息占位 + 起后台任务，立即返回 ids。"""
    prepared = await prepare_run(rt, request)
    _get_run_manager(http).start(prepared, request)
    return CopilotChatStarted(
        conversation_id=prepared.conversation_id,
        user_message_id=prepared.user_message_id,
        assistant_message_id=prepared.assistant_message_id,
        run_id=prepared.run_id,
    )


@router.get("/runs/{assistant_message_id}/stream")
async def copilot_run_stream(
    assistant_message_id: uuid.UUID,
    http: Request,
    stream_repo: CopilotStreamEventRepositoryDep,
    after: int = 0,
) -> StreamingResponse:
    """订阅一轮 run：先按 seq 回放已持久化事件（> after），再 tail 新事件直到终态。

    刷新后续上：前端打开本流即可补上「之前没收到的内容」；run 已终态（事件流已删）时
    立即关闭，前端据 ``chat_messages`` 已固化的最终内容兜底。
    """
    run_manager = _get_run_manager(http)

    async def stream() -> AsyncIterator[str]:
        cursor = after
        while True:
            events = await stream_repo.list_events_after(assistant_message_id, cursor)
            for row in events:
                cursor = row.seq
                yield _sse(row.type, row.payload)
                if row.type in ("done", "error"):
                    return
            handle = run_manager.get(assistant_message_id)
            if handle is None or is_terminal_state(handle.state):
                return
            # 无新事件且非终态 → 等新事件（心跳保活）；清空+复读避免 lost-wakeup。
            handle.events.clear()
            if await stream_repo.list_events_after(assistant_message_id, cursor):
                continue
            try:
                await asyncio.wait_for(handle.events.wait(), timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/approve")
async def copilot_approve(request: CopilotApproveRequest, http: Request) -> dict[str, bool]:
    """HITL 审批回执：把裁决回填给后台任务续跑（触发式，续跑事件从订阅流来）。"""
    decisions = [
        {"approval_id": d.approval_id, "decision": d.decision} for d in request.decisions
    ]
    ok = _get_run_manager(http).submit_decision(request.assistant_message_id, decisions)
    return {"ok": ok}


@router.post("/runs/{assistant_message_id}/cancel")
async def copilot_run_cancel(assistant_message_id: uuid.UUID, http: Request) -> dict[str, bool]:
    """前端「停止」：取消后台 run（触发 finally 回填部分内容），保持「停止即停止」语义。"""
    ok = _get_run_manager(http).cancel(assistant_message_id)
    return {"ok": ok}


@router.get("/approvals/pending", response_model=CopilotApprovalList)
async def get_pending_approvals(rt: CopilotRuntimeDep) -> CopilotApprovalList:
    """找回挂起的审批单（前端刷新/关闭后仍可据此续批）；惰性失效已过期的 pending。"""
    approvals = await list_pending_approvals(rt)
    return CopilotApprovalList(items=[CopilotApprovalRead.model_validate(a) for a in approvals])


@router.get("/memory", response_model=CopilotMemoryList)
async def get_copilot_memory(
    memory_store: MemoryFileStoreDep,
    memory_service: CopilotMemoryServiceDep,
) -> CopilotMemoryList:
    """只读记忆面板：Soul/User 全文 + 三型记忆条目。"""
    soul = await memory_store.read("soul")
    user = await memory_store.read("user")
    memories = await memory_service.list_all()
    return CopilotMemoryList(
        soul=soul,
        user=user,
        memories=[CopilotMemoryRead.model_validate(m) for m in memories],
    )


@router.get("/skills", response_model=CopilotSkillsList)
async def get_copilot_skills(rt: CopilotRuntimeDep) -> CopilotSkillsList:
    """内置技能清单（名称 + 描述 + 是否写）。"""
    items = [
        {
            "name": tool.name,
            "description": tool.description,
            "has_side_effect": tool.name in rt.write_tool_names,
        }
        for tool in rt.tools
    ]
    return CopilotSkillsList(items=[CopilotSkillRead.model_validate(s) for s in items])


@router.get("/custom-skills", response_model=CopilotCustomSkillList)
async def get_copilot_custom_skills(
    skill_store: SkillFileStoreDep,
) -> CopilotCustomSkillList:
    """自定义 Skill 清单（L2 技能层，一个 MD 一个 skill，只读）。"""
    skills, _ = await skill_store.list_skills()
    return CopilotCustomSkillList(
        items=[
            CopilotCustomSkillRead(name=s.name, description=s.description, content=s.content)
            for s in skills
        ]
    )
