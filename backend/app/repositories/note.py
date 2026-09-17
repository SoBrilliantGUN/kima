import uuid
from collections.abc import Sequence
from typing import Any, Protocol, cast

from sqlalchemy import CursorResult, func, inspect, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.note import Note, note_knowledge_bases


class NoteRepository(Protocol):
    async def list(self, *, limit: int, offset: int) -> tuple[list[Note], int]: ...
    async def get(self, note_id: uuid.UUID) -> Note | None: ...
    async def add(self, note: Note) -> Note: ...
    async def update(self, note: Note) -> Note: ...
    async def delete(self, note: Note) -> None: ...
    async def associate(self, note_id: uuid.UUID, kb_id: uuid.UUID) -> bool: ...
    async def list_by_kb(self, kb_id: uuid.UUID) -> Sequence[Note]: ...


class SqlAlchemyNoteRepository:
    """SQLAlchemy 实现，持有 AsyncSession，写操作在其方法内提交。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(self, *, limit: int, offset: int) -> tuple[list[Note], int]:
        total = await self._session.scalar(select(func.count()).select_from(Note))
        rows = await self._session.scalars(
            select(Note)
            .order_by(Note.updated_at.desc(), Note.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows), int(total or 0)

    async def get(self, note_id: uuid.UUID) -> Note | None:
        return await self._session.get(Note, note_id)

    async def add(self, note: Note) -> Note:
        self._session.add(note)
        await self._session.commit()
        await self._session.refresh(note)
        return note

    async def update(self, note: Note) -> Note:
        if inspect(note).session is not self._session:
            raise InvalidRequestError(
                "update() 只接受本 session 已加载的持久对象（detached/transient 请先 get）"
            )
        await self._session.commit()
        await self._session.refresh(note)
        return note

    async def delete(self, note: Note) -> None:
        await self._session.delete(note)
        await self._session.commit()

    async def associate(self, note_id: uuid.UUID, kb_id: uuid.UUID) -> bool:
        statement = (
            pg_insert(note_knowledge_bases)
            .values(note_id=note_id, knowledge_base_id=kb_id)
            .on_conflict_do_nothing()
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        await self._session.commit()
        return result.rowcount == 1

    async def list_by_kb(self, kb_id: uuid.UUID) -> Sequence[Note]:
        rows = await self._session.scalars(
            select(Note)
            .join(note_knowledge_bases, note_knowledge_bases.c.note_id == Note.id)
            .where(note_knowledge_bases.c.knowledge_base_id == kb_id)
            .order_by(note_knowledge_bases.c.created_at.desc())
        )
        return list(rows)
