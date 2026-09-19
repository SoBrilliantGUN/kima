"""检索层单测：RRF 融合、rerank 精排、RagRetriever 编排（注入 Fake，不起真库/真网）。"""

import uuid

import pytest

from app.integrations.embedding import FakeEmbeddingClient
from app.integrations.rerank import FakeRerankerClient, RerankResult
from app.rag.hybrid import rrf_fuse
from app.rag.rerank import rerank_chunks
from app.rag.retriever import RagRetriever
from app.rag.schema import RetrievedChunk, SourceType


def _chunk(
    source_type: SourceType,
    chunk_id: uuid.UUID,
    *,
    parent_id: uuid.UUID | None = None,
    content: str = "",
    title: str = "t",
) -> RetrievedChunk:
    return RetrievedChunk(
        source_type=source_type,
        source_id=uuid.uuid4(),
        chunk_id=chunk_id,
        parent_id=parent_id,
        content=content,
        title=title,
        snippet=content,
    )


class FakeRetrievalRepository:
    """内存版检索仓库：返回预置 dense/lexical/parent 结果，记录调用次数。"""

    def __init__(
        self,
        dense: list[RetrievedChunk],
        lexical: list[RetrievedChunk],
        parents: dict[uuid.UUID, str],
    ) -> None:
        self._dense = dense
        self._lexical = lexical
        self._parents = parents
        self.dense_calls = 0
        self.lexical_calls = 0

    async def search_dense(
        self, kb_id: uuid.UUID, query_vec: list[float], top_k: int
    ) -> list[RetrievedChunk]:
        self.dense_calls += 1
        return list(self._dense)

    async def search_lexical(
        self, kb_id: uuid.UUID, query: str, top_k: int
    ) -> list[RetrievedChunk]:
        self.lexical_calls += 1
        return list(self._lexical)

    async def get_parent_contents(
        self, doc_ids: set[uuid.UUID], note_ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        parent_ids = doc_ids | note_ids
        return {pid: self._parents[pid] for pid in parent_ids if pid in self._parents}


# --- RRF ---


def test_rrf_fuse_dedup_and_order() -> None:
    a_id, b_id, c_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    a = _chunk(SourceType.DOCUMENT, a_id)
    b = _chunk(SourceType.NOTE, b_id)
    c = _chunk(SourceType.DOCUMENT, c_id)
    # a 在两路都出现（均为 rank1），应去重并获最高分；b/c 各 rank2 平分
    fused = rrf_fuse([[a, b], [a, c]], k=60)
    assert [chunk.chunk_id for chunk in fused] == [a_id, b_id, c_id]


def test_rrf_fuse_score() -> None:
    a_id = uuid.uuid4()
    a = _chunk(SourceType.DOCUMENT, a_id)
    fused = rrf_fuse([[a], [a]], k=60)
    assert fused[0].score == pytest.approx(2 / 61)


# --- rerank ---


async def test_rerank_chunks_top_n() -> None:
    ids = [uuid.uuid4() for _ in range(5)]
    chunks = [_chunk(SourceType.DOCUMENT, chunk_id) for chunk_id in ids]
    ranked = await rerank_chunks("q", chunks, FakeRerankerClient(), top_n=3, min_score=0.0)
    # FakeReranker 按 index 递减分，故 top_n 保留前 3 个
    assert [chunk.chunk_id for chunk in ranked] == ids[:3]


async def test_rerank_chunks_filters_low_score() -> None:
    ids = [uuid.uuid4() for _ in range(3)]
    chunks = [_chunk(SourceType.DOCUMENT, chunk_id) for chunk_id in ids]

    class _Reranker:
        async def rerank(self, query: str, documents: list[str]) -> list[RerankResult]:
            return [
                RerankResult(index=0, score=0.9),
                RerankResult(index=1, score=0.1),
                RerankResult(index=2, score=0.8),
            ]

    ranked = await rerank_chunks("q", chunks, _Reranker(), top_n=3, min_score=0.3)
    # 0.1 低于阈值被过滤，剩余按分数降序
    assert [chunk.chunk_id for chunk in ranked] == [ids[0], ids[2]]


# --- RagRetriever 编排 ---


async def test_retriever_resolves_parent() -> None:
    kb_id = uuid.uuid4()
    child_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    note_parent_id = uuid.uuid4()
    child = _chunk(
        SourceType.DOCUMENT, child_id, parent_id=parent_id, content="child snippet", title="doc"
    )
    note = _chunk(
        SourceType.NOTE, uuid.uuid4(), parent_id=note_parent_id, content="note child", title="note"
    )

    repository = FakeRetrievalRepository(
        dense=[child, note],
        lexical=[],
        parents={parent_id: "parent full context", note_parent_id: "note parent context"},
    )
    retriever = RagRetriever(
        repository=repository,
        embedder=FakeEmbeddingClient(dimension=4),
        reranker=FakeRerankerClient(),
        dense_top_k=20,
        lexical_top_k=20,
        rerank_top_n=6,
    )

    result = await retriever.retrieve("问一下", kb_id)

    assert repository.dense_calls == 1
    assert repository.lexical_calls == 1
    doc_hit = next(c for c in result if c.source_type == SourceType.DOCUMENT)
    note_hit = next(c for c in result if c.source_type == SourceType.NOTE)
    # 文档与笔记命中均回 parent：content 回 parent、snippet 保持 child
    assert doc_hit.content == "parent full context"
    assert doc_hit.snippet == "child snippet"
    assert note_hit.content == "note parent context"
    assert note_hit.snippet == "note child"
