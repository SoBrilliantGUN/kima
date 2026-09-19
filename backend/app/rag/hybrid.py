"""RRF 融合：多路检索结果按倒数排名融合（Reciprocal Rank Fusion）。

每路结果的列表顺序即 rank（1-based），对每个 chunk 累加 `1/(k+rank)`，
按分数降序、以 (source_type, chunk_id) 去重，返回融合后的候选集。
"""

import uuid
from collections.abc import Iterable

from app.rag.schema import RetrievedChunk

RRF_K = 60


def rrf_fuse(result_lists: Iterable[list[RetrievedChunk]], k: int = RRF_K) -> list[RetrievedChunk]:
    scores: dict[tuple[str, uuid.UUID], float] = {}
    chunk_by_key: dict[tuple[str, uuid.UUID], RetrievedChunk] = {}
    for results in result_lists:
        for rank, chunk in enumerate(results, start=1):
            key = (chunk.source_type.value, chunk.chunk_id)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            chunk_by_key.setdefault(key, chunk)

    fused: list[RetrievedChunk] = []
    for key, score in sorted(scores.items(), key=lambda item: item[1], reverse=True):
        chunk = chunk_by_key[key]
        chunk.score = score
        fused.append(chunk)
    return fused
