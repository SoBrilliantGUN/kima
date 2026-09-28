import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

DEFAULT_CONVERSATION_TITLE = "新对话"


class ChatRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ChatKind(StrEnum):
    """会话类型：qa = 普通问答，copilot = 知识 Agent 浮窗。"""

    QA = "qa"
    COPILOT = "copilot"


class ChatConversation(Base, TimestampMixin):
    """会话：首页全局（kb_id 空）或知识库右面板（kb_id 挂库）。

    `kind` 区分普通问答（qa）与 Copilot（copilot）；Copilot 会话恒为 kb_id=NULL。
    """

    __tablename__ = "chat_conversations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kb_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ChatKind.QA.value, server_default=ChatKind.QA.value
    )
    title: Mapped[str] = mapped_column(
        String(255), nullable=False, default=DEFAULT_CONVERSATION_TITLE
    )


class ChatMessage(Base):
    """消息（append-only，仅 created_at，无 updated_at）。"""

    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[ChatRole] = mapped_column(
        Enum(ChatRole, native_enum=False, length=16), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # Copilot 工具轨迹投影 [{tool_name,args}]（UI 快读；完整思维链见 copilot_events）
    steps: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
