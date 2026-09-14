import uuid
from datetime import datetime
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.models.note import NoteType


class NoteCreateFromUrl(BaseModel):
    url: str
    knowledge_base_id: uuid.UUID | None = None

    @field_validator("url", mode="before")
    @classmethod
    def _strip_url(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("请输入合法的 http/https 链接")
        return value


class NoteCreate(BaseModel):
    title: str | None = None
    knowledge_base_id: uuid.UUID | None = None

    @field_validator("title", mode="before")
    @classmethod
    def _strip_title(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class NoteUpdate(BaseModel):
    title: str | None = None
    content_markdown: str | None = None

    @field_validator("title", mode="before")
    @classmethod
    def _strip_title(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate_update(self) -> "NoteUpdate":
        if not self.model_fields_set:
            raise ValueError("至少提供一个要更新的字段")
        return self


class NoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    type: NoteType
    content_markdown: str
    summary: str | None
    source_url: str | None
    created_at: datetime
    updated_at: datetime


class NoteList(BaseModel):
    items: list[NoteRead]
    total: int


class NoteAddToKnowledgeBase(BaseModel):
    knowledge_base_id: uuid.UUID
