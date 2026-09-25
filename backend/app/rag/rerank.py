"""精排：对 RRF 融合后的候选集用 bge-reranker 重排，过滤低分后取 top-N。

``rerank`` 参数是「精排函数」而非固定 ``RerankerClient``：Copilot 经网关统一门禁/记账/
快照（传 ``lambda q, docs: gateway.rerank("rag", q, docs)``），模块 5 直接传
``reranker.rerank``，两边共用同一套排序/过滤逻辑。
"""

from collections.abc import Awaitable, Callable

from app.integrations.rerank import RerankResult
from app.rag.schema import RetrievedChunk

RerankFn = Callable[[str, list[str]], Awaitable[list[RerankResult]]]


async def rerank_chunks(
    query: str,
    chunks: list[RetrievedChunk],
    rerank: RerankFn,
    top_n: int,
    min_score: float,
) -> list[RetrievedChunk]:
    """用命中片段（snippet）作为 rerank 输入，过滤低分后返回按相关度降序的 top-N。"""
    if not chunks:
        return []
    documents = [chunk.snippet for chunk in chunks]
    results = await rerank(query, documents)
    ordered = sorted(results, key=lambda r: r.score, reverse=True)
    ordered = [r for r in ordered if r.score >= min_score][:top_n]

    ranked: list[RetrievedChunk] = []
    for result in ordered:
        chunk = chunks[result.index]
        chunk.score = result.score
        ranked.append(chunk)
    return ranked
