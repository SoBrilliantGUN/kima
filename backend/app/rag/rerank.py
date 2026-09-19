"""精排：对 RRF 融合后的候选集用 bge-reranker 重排，过滤低分后取 top-N。"""

from app.integrations.rerank import RerankerClient
from app.rag.schema import RetrievedChunk


async def rerank_chunks(
    query: str,
    chunks: list[RetrievedChunk],
    reranker: RerankerClient,
    top_n: int,
    min_score: float,
) -> list[RetrievedChunk]:
    """用命中片段（snippet）作为 rerank 输入，过滤低分后返回按相关度降序的 top-N。"""
    if not chunks:
        return []
    documents = [chunk.snippet for chunk in chunks]
    results = await reranker.rerank(query, documents)
    ordered = sorted(results, key=lambda r: r.score, reverse=True)
    ordered = [r for r in ordered if r.score >= min_score][:top_n]

    ranked: list[RetrievedChunk] = []
    for result in ordered:
        chunk = chunks[result.index]
        chunk.score = result.score
        ranked.append(chunk)
    return ranked
