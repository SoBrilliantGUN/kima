import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, ForeignKey, Integer, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.models.base import Base

# embedding 维度唯一真源 = settings.embedding_dim（默认 1024，bge-m3）
EMBEDDING_DIM = get_settings().embedding_dim


class NoteChunk(Base):
    """笔记分块（父子两级，small-to-big）：parent 不向量化、child 向量化。

    笔记全局、多对多入库，故无 kb_id（按库过滤走 note_knowledge_bases 关联表）；
    游离笔记（未关联任何库）不向量化，故本表不含游离笔记数据。
    """

    __tablename__ = "note_chunks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    note_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("notes.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("note_chunks.id", ondelete="CASCADE"), nullable=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 列名 "metadata"，属性名 doc_metadata（避开 SQLAlchemy 保留名 Base.metadata）
    doc_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    # 词法检索列（生成列，pg_jieba 分词），由 to_tsvector('jiebacfg', content) 自动计算
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR(), Computed("to_tsvector('jiebacfg', content)", persisted=True), nullable=True
    )
