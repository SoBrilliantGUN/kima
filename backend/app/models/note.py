import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Table, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

DEFAULT_NOTE_TITLE = "无标题笔记"


note_knowledge_bases = Table(
    "note_knowledge_bases",
    Base.metadata,
    Column("note_id", Uuid, ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "knowledge_base_id",
        Uuid,
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)


class Note(Base, TimestampMixin):
    """笔记实体（全局，不挂知识库）。纯 Markdown 空白笔记。"""

    __tablename__ = "notes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 上次向量化时间戳（NULL=从未向量化）；笔记向量化 worker 用它做 idle 检测
    vectorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
