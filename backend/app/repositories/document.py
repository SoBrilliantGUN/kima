import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import delete, func, inspect, or_, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import MAX_RETRIES, Document, DocumentChunk, DocumentStatus


class DocumentRepository(Protocol):
    async def add(self, doc: Document) -> Document: ...
    async def get(self, doc_id: uuid.UUID) -> Document | None: ...
    async def update(self, doc: Document) -> Document: ...
    async def delete(self, doc: Document) -> None: ...
    async def claim_pending(self, limit: int) -> list[Document]: ...
    async def add_chunks(self, chunks: list[DocumentChunk]) -> None: ...
    async def delete_chunks(self, doc_id: uuid.UUID) -> None: ...
    async def recover_stuck(self, now: datetime, max_age: timedelta) -> int: ...
    async def list_by_kb(self, kb_id: uuid.UUID) -> Sequence[Document]: ...
    async def close(self) -> None: ...


class SqlAlchemyDocumentRepository:
    """SQLAlchemy 实现，持有 AsyncSession，写操作在其方法内提交。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- 基础 CRUD ---

    async def add(self, doc: Document) -> Document:
        """新增文档；refresh 回读 DB 生成的 id / 时间戳。"""
        self._session.add(doc)
        await self._session.commit()
        await self._session.refresh(doc)
        return doc

    async def get(self, doc_id: uuid.UUID) -> Document | None:
        """按主键取单条，无则返回 None。"""
        return await self._session.get(Document, doc_id)

    async def update(self, doc: Document) -> Document:
        """提交对持久对象的原地改动；对象须由本 session 的 get() 加载。"""
        if inspect(doc).session is not self._session:
            raise InvalidRequestError(
                "update() 只接受本 session 已加载的持久对象（detached/transient 请先 get）"
            )
        await self._session.commit()
        await self._session.refresh(doc)
        return doc

    async def delete(self, doc: Document) -> None:
        """删除文档；chunk 由 FK ondelete=CASCADE 级联清理。"""
        await self._session.delete(doc)
        await self._session.commit()

    # --- 查询 ---

    async def list_by_kb(self, kb_id: uuid.UUID) -> Sequence[Document]:
        """按知识库列出文档，创建时间倒序。"""
        rows = await self._session.scalars(
            select(Document).where(Document.kb_id == kb_id).order_by(Document.created_at.desc())
        )
        return list(rows)

    # --- 后台 worker：调度与超时兜底 ---

    async def claim_pending(self, limit: int) -> list[Document]:
        """FOR UPDATE SKIP LOCKED 拾取到期 pending 行并置 processing（并发安全）。"""
        rows = await self._session.scalars(
            select(Document)
            .where(
                Document.status == DocumentStatus.PENDING,
                or_(Document.next_retry_at.is_(None), Document.next_retry_at <= func.now()),
            )
            .order_by(Document.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
        docs = list(rows)
        for doc in docs:
            doc.status = DocumentStatus.PROCESSING
            doc.error_message = None
        if docs:
            await self._session.commit()
        return docs

    async def recover_stuck(self, now: datetime, max_age: timedelta) -> int:
        """超时兜底：把卡死的 processing 行重置回 pending（或超限置 error）。"""
        cutoff = now - max_age
        rows = await self._session.scalars(
            select(Document).where(
                Document.status == DocumentStatus.PROCESSING,
                Document.updated_at <= cutoff,
            )
        )
        docs = list(rows)
        for doc in docs:
            doc.retry_count += 1
            if doc.retry_count > MAX_RETRIES:
                doc.status = DocumentStatus.ERROR
                doc.next_retry_at = None
            else:
                doc.status = DocumentStatus.PENDING
                doc.next_retry_at = now
            doc.error_message = "处理超时，已重置"
        if docs:
            await self._session.commit()
        return len(docs)

    # --- 父子 chunk 存储 ---

    async def add_chunks(self, chunks: list[DocumentChunk]) -> None:
        """批量写入父子 chunk（parent 不向量化、child 已带 embedding）。"""
        self._session.add_all(chunks)
        await self._session.commit()

    async def delete_chunks(self, doc_id: uuid.UUID) -> None:
        """删除某文档全部 chunk（重试前先清旧数据）。"""
        await self._session.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id == doc_id)
        )
        await self._session.commit()

    # --- 生命周期 ---

    async def close(self) -> None:
        """关闭持有的会话，归还连接池（worker 每轮/每文档各开一次 session）。"""
        await self._session.close()
