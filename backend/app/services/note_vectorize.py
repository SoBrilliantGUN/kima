"""笔记向量化服务：删旧 chunk → 两级分块 → embed child → 写 chunk → 回写 vectorized_at。"""

import uuid
from datetime import UTC, datetime

from app.chunking import ParentChunk, chunk_document, estimate_tokens
from app.integrations.embedding import EmbeddingClient
from app.models.note_chunk import NoteChunk
from app.repositories.note_chunk import NoteChunkRepository

EMBED_BATCH_SIZE = 32


class NoteVectorizeService:
    """向量化单篇笔记（只覆盖已入库笔记，游离笔记不会被 claim 到）。"""

    def __init__(self, *, repository: NoteChunkRepository, embedder: EmbeddingClient) -> None:
        self._repository = repository
        self._embedder = embedder

    async def vectorize(self, note_id: uuid.UUID) -> None:
        note = await self._repository.get_note(note_id)
        if note is None:
            return

        await self._repository.delete_chunks(note_id)
        chunks = self._build_chunks(note_id, chunk_document(note.content_markdown))
        await self._embed(chunks)
        await self._repository.add_chunks(chunks)
        await self._repository.mark_vectorized(note_id, datetime.now(UTC))

    def _build_chunks(self, note_id: uuid.UUID, parents: list[ParentChunk]) -> list[NoteChunk]:
        """把 chunk_document 的 ParentChunk 树拍平成 note_chunks 行（small-to-big）。

        parent 行不向量化（embedding=None），child 行以 parent_id 指向父行；
        chunk_index 各自层级内从 0 计。
        """
        rows: list[NoteChunk] = []
        for parent_index, parent in enumerate(parents):
            parent_row = NoteChunk(
                id=uuid.uuid4(),
                note_id=note_id,
                parent_id=None,
                chunk_index=parent_index,
                content=parent.content,
                doc_metadata={"heading_path": parent.heading_path} if parent.heading_path else None,
                token_count=estimate_tokens(parent.content),
                embedding=None,
            )
            rows.append(parent_row)
            for child_index, child in enumerate(parent.children):
                rows.append(
                    NoteChunk(
                        id=uuid.uuid4(),
                        note_id=note_id,
                        parent_id=parent_row.id,
                        chunk_index=child_index,
                        content=child.content,
                        doc_metadata=child.metadata,
                        token_count=estimate_tokens(child.content),
                        embedding=None,
                    )
                )
        return rows

    async def _embed(self, chunks: list[NoteChunk]) -> None:
        """只向量化 child（parent 不向量化），按 EMBED_BATCH_SIZE 分批回填 embedding。"""
        children = [chunk for chunk in chunks if chunk.parent_id is not None]
        if not children:
            return
        texts = [chunk.content for chunk in children]
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[start : start + EMBED_BATCH_SIZE]
            vectors.extend(await self._embedder.embed_documents(batch))
        for chunk, vector in zip(children, vectors, strict=True):
            chunk.embedding = vector

    async def close(self) -> None:
        await self._repository.close()
