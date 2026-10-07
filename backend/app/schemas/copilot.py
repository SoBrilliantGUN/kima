"""Copilot API 的请求 / 响应 schema。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.copilot import MemoryKind


class CopilotRequest(BaseModel):
    conversation_id: uuid.UUID | None = None
    question: str

    @field_validator("question")
    @classmethod
    def _strip_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("问题不能为空")
        return value


class CopilotChatStarted(BaseModel):
    """POST /chat 的同步响应：四个 id（前端据此显示占位 + 打开订阅流）。"""

    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID
    run_id: uuid.UUID


class CopilotDecision(BaseModel):
    """单张审批单的裁决：approval_id 定位审批单，decision ∈ approve/reject。"""

    approval_id: uuid.UUID
    decision: str


class CopilotApproveRequest(BaseModel):
    """HITL 审批回执：run_id 定位 checkpoint，逐单裁决（一张单一个 decision）。"""

    run_id: uuid.UUID
    decisions: list[CopilotDecision]
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID


class CopilotApprovalRead(BaseModel):
    """待审审批单的读取模型（「找回挂起审批」端点用）。

    ``conversation_id``/``assistant_message_id`` 是续批上下文：前端刷新/关闭后据此直接调
    `/approve` 续批，无需再凭内存里的 meta 事件。
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    conversation_id: uuid.UUID | None
    assistant_message_id: uuid.UUID | None
    tool: str
    args: dict[str, Any]
    summary: str
    level: str
    status: str
    expires_at: datetime | None
    created_at: datetime


class CopilotApprovalList(BaseModel):
    items: list[CopilotApprovalRead]


class CopilotMemoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: MemoryKind
    content: str
    entity_id: str | None
    access_count: int
    last_access: datetime | None
    superseded: bool
    version: int
    created_at: datetime


class CopilotMemoryList(BaseModel):
    """只读记忆面板数据：Soul/User 全文 + 四型记忆条目（按 kind 分组）。"""

    soul: str
    user: str
    memories: list[CopilotMemoryRead]


class CopilotSkillRead(BaseModel):
    name: str
    description: str
    has_side_effect: bool


class CopilotSkillsList(BaseModel):
    items: list[CopilotSkillRead]


class CopilotCustomSkillRead(BaseModel):
    """自定义 Skill（L2 技能层）的读取模型：一个 MD 文件一个 skill（只读）。"""

    name: str
    description: str
    content: str


class CopilotCustomSkillList(BaseModel):
    items: list[CopilotCustomSkillRead]
