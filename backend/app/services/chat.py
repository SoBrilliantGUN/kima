"""ChatService：会话 CRUD + 提问编排（SSE 事件流）。"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from app.core.exceptions import NotFoundError
from app.integrations.llm import ChatMessage as LlmMessage
from app.models.chat import DEFAULT_CONVERSATION_TITLE, ChatConversation, ChatMessage, ChatRole
from app.rag.generate import AnswerCitations, AnswerDelta
from app.rag.schema import Citation
from app.rag.service import RagService
from app.repositories.chat import ChatRepository
from app.repositories.knowledge_base import KnowledgeBaseRepository
from app.schemas.chat import ChatRequest

TITLE_MAX_LENGTH = 50


@dataclass(frozen=True)
class MetaEvent:
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID


@dataclass(frozen=True)
class DeltaEvent:
    text: str


@dataclass(frozen=True)
class CitationsEvent:
    citations: list[dict[str, Any]]


@dataclass(frozen=True)
class DoneEvent:
    assistant_message_id: uuid.UUID


ChatEvent = MetaEvent | DeltaEvent | CitationsEvent | DoneEvent


def _to_llm_messages(messages: list[ChatMessage]) -> list[LlmMessage]:
    """把 ORM 消息转成 LLM 协议消息（role + content）。"""
    return [
        LlmMessage(role=message.role.value, content=message.content)
        for message in messages
    ]


class ChatService:
    def __init__(
        self,
        repository: ChatRepository,
        kb_repository: KnowledgeBaseRepository,
        rag: RagService,
    ) -> None:
        self._repository = repository
        self._kb_repository = kb_repository
        self._rag = rag

    # --- 会话 CRUD ---

    async def list_conversations(
        self, kb_id: uuid.UUID | None, *, limit: int, offset: int
    ) -> tuple[list[ChatConversation], int]:
        if kb_id is not None:
            await self._validate_kb(kb_id)
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        return await self._repository.list_conversations(kb_id, limit=limit, offset=offset)

    async def create_conversation(self, kb_id: uuid.UUID | None) -> ChatConversation:
        if kb_id is not None:
            await self._validate_kb(kb_id)
        return await self._repository.add_conversation(
            ChatConversation(kb_id=kb_id, title=DEFAULT_CONVERSATION_TITLE)
        )

    async def get_conversation_detail(
        self, conversation_id: uuid.UUID
    ) -> tuple[ChatConversation, list[ChatMessage]]:
        conversation = await self._repository.get_conversation(conversation_id)
        if conversation is None:
            raise NotFoundError("会话不存在")
        messages = await self._repository.list_messages(conversation_id)
        return conversation, messages

    async def delete_conversation(self, conversation_id: uuid.UUID) -> None:
        conversation = await self._repository.get_conversation(conversation_id)
        if conversation is None:
            raise NotFoundError("会话不存在")
        await self._repository.delete_conversation(conversation)

    # --- 提问（SSE 流） ---

    async def ask(self, request: ChatRequest) -> AsyncIterator[ChatEvent]:
        # 校验检索范围内的每个知识库都存在（联网搜索 kb_ids 为空则跳过）
        for kb_id in request.kb_ids:
            await self._validate_kb(kb_id)
        conversation = await self._get_or_create_conversation(request)

        # 历史 = 本轮之前已有消息（不含本轮问题，问题单独传给 answer）
        previous = await self._repository.list_messages(conversation.id)
        llm_history = _to_llm_messages(previous)

        user_message = await self._repository.add_message(
            ChatMessage(
                conversation_id=conversation.id,
                role=ChatRole.USER,
                content=request.question,
            )
        )

        assistant_message_id = uuid.uuid4()
        yield MetaEvent(
            conversation_id=conversation.id,
            user_message_id=user_message.id,
            assistant_message_id=assistant_message_id,
        )

        buffer: list[str] = []
        citations: list[Citation] = []
        async for event in self._rag.answer(
            query=request.question,
            kb_ids=request.kb_ids,
            history=llm_history,
        ):
            if isinstance(event, AnswerDelta):
                buffer.append(event.text)
                yield DeltaEvent(event.text)
            elif isinstance(event, AnswerCitations):
                citations = event.citations

        await self._repository.add_message(
            ChatMessage(
                id=assistant_message_id,
                conversation_id=conversation.id,
                role=ChatRole.ASSISTANT,
                content="".join(buffer),
                citations=[citation.to_dict() for citation in citations],
            )
        )

        yield CitationsEvent([citation.to_dict() for citation in citations])
        yield DoneEvent(assistant_message_id=assistant_message_id)

    async def _get_or_create_conversation(self, request: ChatRequest) -> ChatConversation:
        if request.conversation_id is not None:
            conversation = await self._repository.get_conversation(request.conversation_id)
            if conversation is None:
                raise NotFoundError("会话不存在")
            return conversation
        if request.kb_id is not None:
            await self._validate_kb(request.kb_id)
        title = request.question[:TITLE_MAX_LENGTH]
        return await self._repository.add_conversation(
            ChatConversation(kb_id=request.kb_id, title=title)
        )

    async def _validate_kb(self, kb_id: uuid.UUID) -> None:
        if await self._kb_repository.get(kb_id) is None:
            raise NotFoundError("知识库不存在")
