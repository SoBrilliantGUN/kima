import uuid
from typing import Protocol

from sqlalchemy import func, inspect, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_base import KnowledgeBase


class KnowledgeBaseRepository(Protocol):
    async def list(self, *, limit: int, offset: int) -> tuple[list[KnowledgeBase], int]: ...
    async def get(self, kb_id: uuid.UUID) -> KnowledgeBase | None: ...
    async def get_by_name(self, name: str) -> KnowledgeBase | None: ...
    async def add(self, kb: KnowledgeBase) -> KnowledgeBase: ...
    async def update(self, kb: KnowledgeBase) -> KnowledgeBase: ...
    async def delete(self, kb: KnowledgeBase) -> None: ...


class SqlAlchemyKnowledgeBaseRepository:
    """SQLAlchemy 实现，持有 AsyncSession，写操作在其方法内提交。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(self, *, limit: int, offset: int) -> tuple[list[KnowledgeBase], int]:
        total = await self._session.scalar(select(func.count()).select_from(KnowledgeBase))
        rows = await self._session.scalars(
            select(KnowledgeBase)
            .order_by(KnowledgeBase.updated_at.desc(), KnowledgeBase.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows), int(total or 0)

    async def get(self, kb_id: uuid.UUID) -> KnowledgeBase | None:
        return await self._session.get(KnowledgeBase, kb_id)

    async def get_by_name(self, name: str) -> KnowledgeBase | None:
        rows = await self._session.scalars(
            select(KnowledgeBase).where(KnowledgeBase.name == name)
        )
        return rows.first()

    async def add(self, kb: KnowledgeBase) -> KnowledgeBase:
        self._session.add(kb)
        await self._session.commit()
        await self._session.refresh(kb)
        return kb

    async def update(self, kb: KnowledgeBase) -> KnowledgeBase:
        if inspect(kb).session is not self._session:
            raise InvalidRequestError(
                "update() 只接受本 session 已加载的持久对象（detached/transient 请先 get）"
            )
        await self._session.commit()
        await self._session.refresh(kb)
        return kb

    async def delete(self, kb: KnowledgeBase) -> None:
        await self._session.delete(kb)
        await self._session.commit()
