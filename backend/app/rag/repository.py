"""检索数据访问层：Protocol + SQLAlchemy 实现。

把 dense/lexical/parent 解析三类 DB 查询收口成可替换的仓库接口，
便于 `RagRetriever` 在测试中注入 Fake。
"""

import uuid
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import DocumentChunk
from app.models.note_chunk import NoteChunk
from app.rag.dense import dense_search
from app.rag.lexical import lexical_search
from app.rag.schema import RetrievedChunk


class RetrievalRepository(Protocol):
    async def search_dense(
        self, kb_id: uuid.UUID, query_vec: list[float], top_k: int
    ) -> list[RetrievedChunk]: ...

    async def search_lexical(
        self, kb_id: uuid.UUID, query: str, top_k: int
    ) -> list[RetrievedChunk]: ...

    async def get_parent_contents(
        self, doc_ids: set[uuid.UUID], note_ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, str]: ...


class SqlAlchemyRetrievalRepository:
    """SQLAlchemy 实现，持有 AsyncSession。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search_dense(
        self, kb_id: uuid.UUID, query_vec: list[float], top_k: int
    ) -> list[RetrievedChunk]:
        return await dense_search(self._session, kb_id, query_vec, top_k)

    async def search_lexical(
        self, kb_id: uuid.UUID, query: str, top_k: int
    ) -> list[RetrievedChunk]:
        return await lexical_search(self._session, kb_id, query, top_k)

    async def get_parent_contents(
        self, doc_ids: set[uuid.UUID], note_ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """批量取 parent 全文（文档/笔记命中 child 回 parent 出上下文）。

        文档与笔记分表存储、各自独立 uuid4 主键；按 source_type 分桶后只查
        各自表，从结构上杜绝跨表撞号覆盖，也省去对空集合的冗余查询。
        """
        result: dict[uuid.UUID, str] = {}
        if doc_ids:
            doc_rows = (
                await self._session.execute(
                    select(DocumentChunk.id, DocumentChunk.content).where(
                        DocumentChunk.id.in_(doc_ids)
                    )
                )
            ).all()
            result.update({chunk_id: content for chunk_id, content in doc_rows})
        if note_ids:
            note_rows = (
                await self._session.execute(
                    select(NoteChunk.id, NoteChunk.content).where(NoteChunk.id.in_(note_ids))
                )
            ).all()
            result.update({chunk_id: content for chunk_id, content in note_rows})
        return result
