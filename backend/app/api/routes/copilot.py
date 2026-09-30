"""Copilot 端点：流式对话（SSE）+ 只读记忆面板 + 内置技能清单。

对话运行时入口（``run`` / ``resume``）收装配好的 ``CopilotRuntime``；只读查询
（记忆面板 / 技能清单）不再经过对话运行时，直接依赖底层 store / service。
"""

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.agent.events import (
    CopilotApprovalEvent,
    CopilotDeltaEvent,
    CopilotDoneEvent,
    CopilotMetaEvent,
    CopilotReviewEvent,
    CopilotStepEvent,
    CopilotStreamEvent,
)
from app.agent.resume import list_pending_approvals, resume
from app.agent.run import run
from app.api.deps import (
    CopilotMemoryServiceDep,
    CopilotRuntimeDep,
    MemoryFileStoreDep,
    SkillFileStoreDep,
)
from app.core.exceptions import DomainError
from app.schemas.copilot import (
    CopilotApprovalList,
    CopilotApprovalRead,
    CopilotApproveRequest,
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


def _event_to_sse(event: CopilotStreamEvent) -> str:
    if isinstance(event, CopilotMetaEvent):
        return _sse(
            "meta",
            {
                "conversation_id": str(event.conversation_id),
                "user_message_id": str(event.user_message_id),
                "assistant_message_id": str(event.assistant_message_id),
            },
        )
    if isinstance(event, CopilotStepEvent):
        return _sse("step", {"tool_name": event.tool_name, "args": event.args})
    if isinstance(event, CopilotDeltaEvent):
        return _sse("delta", {"text": event.text})
    if isinstance(event, CopilotReviewEvent):
        return _sse("review", {"verdict": event.verdict, "issues": event.issues})
    if isinstance(event, CopilotApprovalEvent):
        return _sse(
            "approval",
            {
                "run_id": event.run_id,
                "tool": event.tool,
                "args": event.args,
                "summary": event.summary,
                "level": event.level,
            },
        )
    if isinstance(event, CopilotDoneEvent):
        return _sse("done", {"assistant_message_id": str(event.assistant_message_id)})
    raise AssertionError(f"未知事件类型: {type(event)}")


@router.post("/chat")
async def copilot_chat(request: CopilotRequest, rt: CopilotRuntimeDep) -> StreamingResponse:
    """Copilot 流式对话（SSE）：meta → step* → delta* → review? → done；失败发 error 事件。"""

    async def stream() -> AsyncIterator[str]:
        try:
            async for event in run(rt, request):
                yield _event_to_sse(event)
        except DomainError as exc:
            yield _sse("error", {"code": exc.code, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 兜底：非业务异常也走 error 事件，不裸抛
            yield _sse("error", {"code": "internal_error", "message": str(exc)})

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/approve")
async def copilot_approve(
    request: CopilotApproveRequest, rt: CopilotRuntimeDep
) -> StreamingResponse:
    """HITL 审批回执：按 run_id 续跑，approve 重放写工具 / reject 返回拒绝。"""

    async def stream() -> AsyncIterator[str]:
        try:
            async for event in resume(
                rt,
                str(request.run_id),
                request.decision,
                request.conversation_id,
                request.assistant_message_id,
            ):
                yield _event_to_sse(event)
        except DomainError as exc:
            yield _sse("error", {"code": exc.code, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            yield _sse("error", {"code": "internal_error", "message": str(exc)})

    return StreamingResponse(stream(), media_type="text/event-stream")


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
