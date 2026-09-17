import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.document import DocumentRead
from app.schemas.note import NoteRead

COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    color: str | None = Field(default=None, pattern=COLOR_PATTERN)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("description", mode="before")
    @classmethod
    def _strip_description(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    color: str | None = Field(default=None, pattern=COLOR_PATTERN)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("description", mode="before")
    @classmethod
    def _strip_description(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @model_validator(mode="after")
    def _validate_update(self) -> "KnowledgeBaseUpdate":
        if not self.model_fields_set:
            raise ValueError("至少提供一个要更新的字段")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name 不能为空")
        return self


class KnowledgeBaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    color: str
    created_at: datetime
    updated_at: datetime


class KnowledgeBaseList(BaseModel):
    items: list[KnowledgeBaseRead]
    total: int


class ContentItem(BaseModel):
    """知识库内容列表的异构条目，靠 `type` 判别（note / document）。"""

    type: Literal["note", "document"]
    note: NoteRead | None = None
    document: DocumentRead | None = None


class ContentList(BaseModel):
    items: list[ContentItem]
    total: int
