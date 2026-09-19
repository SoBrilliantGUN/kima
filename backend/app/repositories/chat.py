import uuid
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatConversation, ChatMessage


class ChatRepository(Protocol):
    async def add_conversation(self, conversation: ChatConversation) -> ChatConversation: ...
    async def get_conversation(self, conversation_id: uuid.UUID) -> ChatConversation | None: ...
    async def list_conversations(
        self, kb_id: uuid.UUID | None, *, limit: int, offset: int
    ) -> tuple[list[ChatConversation], int]: ...
    async def delete_conversation(self, conversation: ChatConversation) -> None: ...
    async def add_message(self, message: ChatMessage) -> ChatMessage: ...
    async def list_messages(self, conversation_id: uuid.UUID) -> list[ChatMessage]: ...


class SqlAlchemyChatRepository:
    """SQLAlchemy 实现，持有 AsyncSession，写操作在其方法内提交。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_conversation(self, conversation: ChatConversation) -> ChatConversation:
        self._session.add(conversation)
        await self._session.commit()
        await self._session.refresh(conversation)
        return conversation

    async def get_conversation(self, conversation_id: uuid.UUID) -> ChatConversation | None:
        return await self._session.get(ChatConversation, conversation_id)

    async def list_conversations(
        self, kb_id: uuid.UUID | None, *, limit: int, offset: int
    ) -> tuple[list[ChatConversation], int]:
        condition = (
            ChatConversation.kb_id == kb_id
            if kb_id is not None
            else ChatConversation.kb_id.is_(None)
        )
        total = await self._session.scalar(
            select(func.count()).select_from(ChatConversation).where(condition)
        )
        rows = await self._session.scalars(
            select(ChatConversation)
            .where(condition)
            .order_by(ChatConversation.updated_at.desc(), ChatConversation.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows), int(total or 0)

    async def delete_conversation(self, conversation: ChatConversation) -> None:
        await self._session.delete(conversation)
        await self._session.commit()

    async def add_message(self, message: ChatMessage) -> ChatMessage:
        """新增消息，并在同一提交里把所属会话的 updated_at 置为当前（最近活动排序）。"""
        self._session.add(message)
        conversation = await self._session.get(ChatConversation, message.conversation_id)
        if conversation is not None:
            conversation.updated_at = datetime.now(UTC)
        await self._session.commit()
        await self._session.refresh(message)
        return message

    async def list_messages(self, conversation_id: uuid.UUID) -> list[ChatMessage]:
        rows = await self._session.scalars(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        )
        return list(rows)
