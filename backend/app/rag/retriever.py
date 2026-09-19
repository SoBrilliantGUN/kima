"""检索编排：embed → dense + lexical → RRF → rerank → 命中 child 回 parent。"""

import uuid

from app.integrations.embedding import EmbeddingClient
from app.integrations.rerank import RerankerClient
from app.rag.hybrid import RRF_K, rrf_fuse
from app.rag.repository import RetrievalRepository
from app.rag.rerank import rerank_chunks
from app.rag.schema import RetrievedChunk, SourceType

DENSE_TOP_K = 20
LEXICAL_TOP_K = 20
RERANK_TOP_N = 6
RERANK_MIN_SCORE = 0.3


class RagRetriever:
    """混合检索编排：可替换组件（embedding/rerank/repository）注入，纯编排逻辑。"""

    def __init__(
        self,
        *,
        repository: RetrievalRepository,
        embedder: EmbeddingClient,
        reranker: RerankerClient,
        dense_top_k: int = DENSE_TOP_K,
        lexical_top_k: int = LEXICAL_TOP_K,
        rerank_top_n: int = RERANK_TOP_N,
        rerank_min_score: float = RERANK_MIN_SCORE,
        rrf_k: int = RRF_K,
    ) -> None:
        self._repository = repository
        self._embedder = embedder
        self._reranker = reranker
        self._dense_top_k = dense_top_k
        self._lexical_top_k = lexical_top_k
        self._rerank_top_n = rerank_top_n
        self._rerank_min_score = rerank_min_score
        self._rrf_k = rrf_k

    async def retrieve(self, query: str, kb_ids: list[uuid.UUID]) -> list[RetrievedChunk]:
        """检索指定知识库集合：混合检索 + RRF + rerank + 回 parent。"""
        query_vec = await self._embedder.embed_query(query)
        dense_hits = await self._repository.search_dense(kb_ids, query_vec, self._dense_top_k)
        lexical_hits = await self._repository.search_lexical(kb_ids, query, self._lexical_top_k)

        fused = rrf_fuse([dense_hits, lexical_hits], self._rrf_k)
        ranked = await rerank_chunks(
            query, fused, self._reranker, self._rerank_top_n, self._rerank_min_score
        )
        await self._resolve_parents(ranked)
        return ranked

    async def _resolve_parents(self, chunks: list[RetrievedChunk]) -> None:
        """命中 child 回 parent：把 child 的 `content` 替换为 parent 全文（snippet 保持 child）。

        文档与笔记均为 small-to-big，命中 child 后回 parent 出上下文。
        按 source_type 分桶，各查各表，避免跨表撞号覆盖与冗余查询。
        """
        doc_ids = {
            chunk.parent_id
            for chunk in chunks
            if chunk.source_type == SourceType.DOCUMENT and chunk.parent_id is not None
        }
        note_ids = {
            chunk.parent_id
            for chunk in chunks
            if chunk.source_type == SourceType.NOTE and chunk.parent_id is not None
        }
        if not doc_ids and not note_ids:
            return
        parent_contents = await self._repository.get_parent_contents(doc_ids, note_ids)
        for chunk in chunks:
            if chunk.parent_id is not None:
                chunk.content = parent_contents.get(chunk.parent_id, chunk.content)
