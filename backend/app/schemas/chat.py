import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


class ConversationCreate(BaseModel):
    kb_id: uuid.UUID | None = None


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kb_id: uuid.UUID | None
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationList(BaseModel):
    items: list[ConversationRead]
    total: int


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    citations: list[dict[str, Any]] | None
    created_at: datetime


class ConversationDetail(ConversationRead):
    messages: list[MessageRead]


class ChatRequest(BaseModel):
    # 检索范围：空列表 = 联网搜索，非空 = 仅检索这些知识库（可 @ 多个）
    kb_ids: list[uuid.UUID] = []
    # 会话归属知识库：首页全局会话为 None，知识库右面板挂当前库
    kb_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    question: str

    @field_validator("question")
    @classmethod
    def _strip_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("问题不能为空")
        return value
