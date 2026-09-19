"""向量检索：query embedding → pgvector 余弦 top-k（按 kb_id 过滤）。

文档与笔记均只检索 child（embedding 非空，parent 不向量化）；
笔记另经 note_knowledge_bases 关联表过滤出「已入库」。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document, DocumentChunk
from app.models.note import Note, note_knowledge_bases
from app.models.note_chunk import NoteChunk
from app.rag.schema import RetrievedChunk, SourceType


async def dense_search(
    session: AsyncSession, kb_id: uuid.UUID, query_vec: list[float], top_k: int
) -> list[RetrievedChunk]:
    """文档 + 笔记两路向量检索，合并返回（各自顺序即 rank，供 RRF 使用）。"""
    hits: list[RetrievedChunk] = []
    hits.extend(await _dense_documents(session, kb_id, query_vec, top_k))
    hits.extend(await _dense_notes(session, kb_id, query_vec, top_k))
    return hits


async def _dense_documents(
    session: AsyncSession, kb_id: uuid.UUID, query_vec: list[float], top_k: int
) -> list[RetrievedChunk]:
    distance = DocumentChunk.embedding.cosine_distance(query_vec)
    rows = (
        await session.execute(
            select(
                DocumentChunk.id,
                DocumentChunk.document_id,
                DocumentChunk.parent_id,
                DocumentChunk.content,
                Document.title,
            )
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(DocumentChunk.embedding.is_not(None), DocumentChunk.kb_id == kb_id)
            .order_by(distance)
            .limit(top_k)
        )
    ).all()
    return [
        RetrievedChunk(
            source_type=SourceType.DOCUMENT,
            source_id=document_id,
            chunk_id=chunk_id,
            parent_id=parent_id,
            content=content,
            title=title,
            snippet=content,
        )
        for chunk_id, document_id, parent_id, content, title in rows
    ]


async def _dense_notes(
    session: AsyncSession, kb_id: uuid.UUID, query_vec: list[float], top_k: int
) -> list[RetrievedChunk]:
    distance = NoteChunk.embedding.cosine_distance(query_vec)
    rows = (
        await session.execute(
            select(
                NoteChunk.id,
                NoteChunk.note_id,
                NoteChunk.parent_id,
                NoteChunk.content,
                Note.title,
            )
            .join(note_knowledge_bases, note_knowledge_bases.c.note_id == NoteChunk.note_id)
            .join(Note, Note.id == NoteChunk.note_id)
            .where(
                NoteChunk.embedding.is_not(None),
                note_knowledge_bases.c.knowledge_base_id == kb_id,
            )
            .order_by(distance)
            .limit(top_k)
        )
    ).all()
    return [
        RetrievedChunk(
            source_type=SourceType.NOTE,
            source_id=note_id,
            chunk_id=chunk_id,
            parent_id=parent_id,
            content=content,
            title=title,
            snippet=content,
        )
        for chunk_id, note_id, parent_id, content, title in rows
    ]
