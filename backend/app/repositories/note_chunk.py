"""笔记分块数据访问层：Protocol + SQLAlchemy 实现（笔记向量化 worker 用）。"""

import uuid
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.note import Note, note_knowledge_bases
from app.models.note_chunk import NoteChunk


class NoteChunkRepository(Protocol):
    async def claim_due(self, now: datetime, idle: timedelta, limit: int) -> list[Note]: ...
    async def get_note(self, note_id: uuid.UUID) -> Note | None: ...
    async def delete_chunks(self, note_id: uuid.UUID) -> None: ...
    async def add_chunks(self, chunks: list[NoteChunk]) -> None: ...
    async def mark_vectorized(self, note_id: uuid.UUID, timestamp: datetime) -> None: ...
    async def close(self) -> None: ...


class SqlAlchemyNoteChunkRepository:
    """SQLAlchemy 实现，持有 AsyncSession。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_due(self, now: datetime, idle: timedelta, limit: int) -> list[Note]:
        """拾取「内容有变 且 已停止编辑超过 idle 阈值 且 已入库」的笔记。

        单进程轮询，无需 FOR UPDATE SKIP LOCKED；`mark_vectorized` 在结尾回写，
        处理中途崩溃的笔记下一轮会重新被拾起（delete_chunks 先清旧数据，幂等）。
        """
        cutoff = now - idle
        in_kb = exists().where(note_knowledge_bases.c.note_id == Note.id)
        rows = await self._session.scalars(
            select(Note)
            .where(
                or_(Note.vectorized_at.is_(None), Note.updated_at > Note.vectorized_at),
                Note.updated_at < cutoff,
                in_kb,
            )
            .order_by(Note.updated_at.asc())
            .limit(limit)
        )
        return list(rows)

    async def get_note(self, note_id: uuid.UUID) -> Note | None:
        return await self._session.get(Note, note_id)

    async def delete_chunks(self, note_id: uuid.UUID) -> None:
        """重向量化前清掉旧 chunk（删除 + 新增，不做原地 diff）。"""
        await self._session.execute(delete(NoteChunk).where(NoteChunk.note_id == note_id))
        await self._session.commit()

    async def add_chunks(self, chunks: list[NoteChunk]) -> None:
        self._session.add_all(chunks)
        await self._session.commit()

    async def mark_vectorized(self, note_id: uuid.UUID, timestamp: datetime) -> None:
        """回写 vectorized_at；用 Core update 避免触发 onupdate 把 updated_at 顶到 now。"""
        await self._session.execute(
            update(Note).where(Note.id == note_id).values(vectorized_at=timestamp)
        )
        await self._session.commit()

    async def close(self) -> None:
        await self._session.close()
