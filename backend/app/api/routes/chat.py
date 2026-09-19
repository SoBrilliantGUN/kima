"""会话与问答端点：会话 CRUD（/conversations）+ 流式问答（/chat，SSE）。"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Response, status
from fastapi.responses import StreamingResponse

from app.api.deps import ChatServiceDep
from app.core.exceptions import DomainError
from app.schemas.chat import (
    ChatRequest,
    ConversationCreate,
    ConversationDetail,
    ConversationList,
    ConversationRead,
    MessageRead,
)
from app.services.chat import (
    ChatEvent,
    CitationsEvent,
    DeltaEvent,
    DoneEvent,
    MetaEvent,
)

router = APIRouter(prefix="/conversations", tags=["chat"])
chat_router = APIRouter(prefix="/chat", tags=["chat"])


@router.get("", response_model=ConversationList)
async def list_conversations(
    service: ChatServiceDep,
    kb_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ConversationList:
    conversations, total = await service.list_conversations(kb_id, limit=limit, offset=offset)
    return ConversationList(
        items=[ConversationRead.model_validate(c) for c in conversations], total=total
    )


@router.post("", response_model=ConversationRead, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    service: ChatServiceDep, payload: ConversationCreate
) -> ConversationRead:
    conversation = await service.create_conversation(payload.kb_id)
    return ConversationRead.model_validate(conversation)


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    service: ChatServiceDep, conversation_id: uuid.UUID
) -> ConversationDetail:
    conversation, messages = await service.get_conversation_detail(conversation_id)
    return ConversationDetail(
        id=conversation.id,
        kb_id=conversation.kb_id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=[MessageRead.model_validate(m) for m in messages],
    )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    service: ChatServiceDep, conversation_id: uuid.UUID
) -> Response:
    await service.delete_conversation(conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _event_to_sse(event: ChatEvent) -> str:
    if isinstance(event, MetaEvent):
        return _sse(
            "meta",
            {
                "conversation_id": str(event.conversation_id),
                "user_message_id": str(event.user_message_id),
                "assistant_message_id": str(event.assistant_message_id),
            },
        )
    if isinstance(event, DeltaEvent):
        return _sse("delta", {"text": event.text})
    if isinstance(event, CitationsEvent):
        return _sse("citations", event.citations)
    if isinstance(event, DoneEvent):
        return _sse("done", {"assistant_message_id": str(event.assistant_message_id)})
    raise AssertionError(f"未知事件类型: {type(event)}")


@chat_router.post("")
async def chat(request: ChatRequest, service: ChatServiceDep) -> StreamingResponse:
    """流式问答（SSE）：meta → delta* → citations → done；失败发 error 事件。"""

    async def stream() -> AsyncIterator[str]:
        try:
            async for event in service.ask(request):
                yield _event_to_sse(event)
        except DomainError as exc:
            yield _sse("error", {"code": exc.code, "message": str(exc)})

    return StreamingResponse(stream(), media_type="text/event-stream")
