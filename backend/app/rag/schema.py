import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SourceType(StrEnum):
    DOCUMENT = "document"
    NOTE = "note"
    WEB = "web"


@dataclass
class RetrievedChunk:
    """检索命中单元（知识库内：document child / note child）。

    文档与笔记命中均为 small-to-big：`snippet` = 命中 child 原文（引用展示），
    `content` 经「回 parent」后替换为 parent 完整上下文。
    """

    source_type: SourceType
    source_id: uuid.UUID
    chunk_id: uuid.UUID
    parent_id: uuid.UUID | None
    content: str
    title: str
    snippet: str
    score: float = 0.0


@dataclass(frozen=True)
class Citation:
    """生成引用（落库 `chat_messages.citations`）。`index` 对应正文 `[n]`。"""

    index: int
    source_type: SourceType
    source_id: uuid.UUID | None
    chunk_id: uuid.UUID | None
    title: str
    snippet: str
    url: str | None = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSONB 友好的 dict（UUID → str、SourceType → str）。"""
        return {
            "index": self.index,
            "source_type": self.source_type.value,
            "source_id": str(self.source_id) if self.source_id is not None else None,
            "chunk_id": str(self.chunk_id) if self.chunk_id is not None else None,
            "title": self.title,
            "snippet": self.snippet,
            "url": self.url,
        }
