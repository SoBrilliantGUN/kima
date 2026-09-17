import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.document import DocumentStatus, DocumentType


class DocumentCreateFromUrl(BaseModel):
    url: str
    knowledge_base_id: uuid.UUID

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


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kb_id: uuid.UUID
    title: str
    source_type: DocumentType
    source_url: str | None
    status: DocumentStatus
    error_message: str | None
    metadata: dict[str, Any] | None = Field(validation_alias="doc_metadata")
    created_at: datetime
    updated_at: datetime
    # 不含 content_markdown / file_path：正文走 /content 端点，文件走 /file 端点


class DocumentContentRead(BaseModel):
    """解析后的 markdown 正文（word/url 文档阅读用）。"""

    markdown: str
