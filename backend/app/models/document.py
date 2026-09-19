import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, DateTime, Enum, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.models.base import Base, TimestampMixin

# embedding 维度唯一真源 = settings.embedding_dim（默认 1024，bge-m3）
EMBEDDING_DIM = get_settings().embedding_dim

# 重试上限：worker 级全局策略，不随文档而异，故为常量而非列。
MAX_RETRIES = 3


class DocumentType(StrEnum):
    PDF = "pdf"
    WORD = "word"
    URL = "url"


# 文档类型 → 文件扩展名（注意 WORD 的 source_type.value 是 "word"，但扩展名是 "docx"）
DOCUMENT_EXTENSIONS: dict[DocumentType, str] = {
    DocumentType.PDF: "pdf",
    DocumentType.WORD: "docx",
}


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    ERROR = "error"


class Document(Base, TimestampMixin):
    """知识库内文档（kb_id 必填，与笔记「全局」相反）。"""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[DocumentType] = mapped_column(
        Enum(DocumentType, native_enum=False, length=16), nullable=False
    )
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, native_enum=False, length=16),
        nullable=False,
        default=DocumentStatus.PENDING,
    )
    content_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 注意：`metadata` 是 SQLAlchemy 保留名，列名映射为 "metadata"、属性名用 doc_metadata
    doc_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def filename(self) -> str:
        """原始文件名（title + 扩展名），用于下载与 MinerU 上传的 name。"""
        ext = DOCUMENT_EXTENSIONS.get(self.source_type)
        return f"{self.title}.{ext}" if ext else self.title


class DocumentChunk(Base):
    """父子切割（small-to-big）：单表自引用 parent_id，parent 不向量化、child 向量化。"""

    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 同上：列名 "metadata"，属性名 doc_metadata（避开 SQLAlchemy 保留名）
    doc_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    # 词法检索列（生成列，pg_jieba 分词），由 to_tsvector('jiebacfg', content) 自动计算
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR(), Computed("to_tsvector('jiebacfg', content)", persisted=True), nullable=True
    )
