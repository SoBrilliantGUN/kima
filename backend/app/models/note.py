import uuid
from enum import StrEnum

from sqlalchemy import Column, DateTime, Enum, ForeignKey, String, Table, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

DEFAULT_NOTE_TITLE = "无标题笔记"


class NoteType(StrEnum):
    MARKDOWN = "markdown"
    URL = "url"


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
    """笔记实体（全局，不挂知识库）。"""

    __tablename__ = "notes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[NoteType] = mapped_column(
        Enum(NoteType, native_enum=False, length=16), nullable=False, default=NoteType.MARKDOWN
    )
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
