"""词法检索：pg_jieba 中文全文检索 ts_rank top-k（按 kb_ids 过滤）。

文档与笔记均只检索 child（parent_id 非空）；笔记另经 note_knowledge_bases 关联表过滤。
"""

import uuid

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.rag.schema import RetrievedChunk, SourceType

_DOCUMENT_LEXICAL_SQL = text(
    """
    SELECT dc.id AS chunk_id, dc.document_id AS source_id, dc.parent_id AS parent_id,
           dc.content AS content, d.title AS title,
           ts_rank(dc.tsv, plainto_tsquery('jiebacfg', :query)) AS rank
    FROM document_chunks dc
    JOIN documents d ON d.id = dc.document_id
    WHERE dc.parent_id IS NOT NULL
      AND dc.kb_id IN :kb_ids
      AND dc.tsv @@ plainto_tsquery('jiebacfg', :query)
    ORDER BY rank DESC
    LIMIT :limit
    """
).bindparams(bindparam("kb_ids", expanding=True))

_NOTE_LEXICAL_SQL = text(
    """
    SELECT nc.id AS chunk_id, nc.note_id AS source_id, nc.parent_id AS parent_id,
           nc.content AS content, n.title AS title,
           ts_rank(nc.tsv, plainto_tsquery('jiebacfg', :query)) AS rank
    FROM note_chunks nc
    JOIN notes n ON n.id = nc.note_id
    JOIN note_knowledge_bases nkb ON nkb.note_id = nc.note_id
    WHERE nkb.knowledge_base_id IN :kb_ids
      AND nc.parent_id IS NOT NULL
      AND nc.tsv @@ plainto_tsquery('jiebacfg', :query)
    ORDER BY rank DESC
    LIMIT :limit
    """
).bindparams(bindparam("kb_ids", expanding=True))


async def lexical_search(
    session: AsyncSession, kb_ids: list[uuid.UUID], query: str, top_k: int
) -> list[RetrievedChunk]:
    """文档 + 笔记两路词法检索，合并返回（各自顺序即 rank）。"""
    hits: list[RetrievedChunk] = []
    hits.extend(await _lexical_documents(session, kb_ids, query, top_k))
    hits.extend(await _lexical_notes(session, kb_ids, query, top_k))
    return hits


async def _lexical_documents(
    session: AsyncSession, kb_ids: list[uuid.UUID], query: str, top_k: int
) -> list[RetrievedChunk]:
    rows = (
        await session.execute(
            _DOCUMENT_LEXICAL_SQL, {"query": query, "kb_ids": kb_ids, "limit": top_k}
        )
    ).mappings().all()
    return [
        RetrievedChunk(
            source_type=SourceType.DOCUMENT,
            source_id=row["source_id"],
            chunk_id=row["chunk_id"],
            parent_id=row["parent_id"],
            content=row["content"],
            title=row["title"],
            snippet=row["content"],
        )
        for row in rows
    ]


async def _lexical_notes(
    session: AsyncSession, kb_ids: list[uuid.UUID], query: str, top_k: int
) -> list[RetrievedChunk]:
    rows = (
        await session.execute(
            _NOTE_LEXICAL_SQL, {"query": query, "kb_ids": kb_ids, "limit": top_k}
        )
    ).mappings().all()
    return [
        RetrievedChunk(
            source_type=SourceType.NOTE,
            source_id=row["source_id"],
            chunk_id=row["chunk_id"],
            parent_id=row["parent_id"],
            content=row["content"],
            title=row["title"],
            snippet=row["content"],
        )
        for row in rows
    ]
