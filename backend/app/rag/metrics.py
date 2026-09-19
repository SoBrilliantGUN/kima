"""检索质量指标（纯函数，无 IO）：recall@k / MRR。

驱动 chunking 决策的核心是检索层指标，而非端到端 faithfulness——大模型足够强时
切分好坏常被"兜底"覆盖，只有 recall@k / MRR 能直接反映 chunk 是否被召回。
chunk id 用 `str`（文档/笔记的 `chunk_id` 字符串化）作键即可。
"""

from collections.abc import Iterable

GoldenIds = set[str] | frozenset[str]
EvalCase = tuple[GoldenIds, list[str]]  # (golden chunk ids, 检索返回的 id 顺序列表)


def recall_at_k(golden_ids: GoldenIds, retrieved_ids: list[str], k: int) -> float:
    """top-k 召回率：黄金 chunk 命中前 k 的比例。"""
    if not golden_ids:
        return 0.0
    hits = len(set(golden_ids) & set(retrieved_ids[:k]))
    return hits / len(golden_ids)


def mrr(golden_ids: GoldenIds, retrieved_ids: list[str]) -> float:
    """倒数排名（单 query）：首个黄金 chunk 的 1/rank，无命中为 0。"""
    for rank, chunk_id in enumerate(retrieved_ids, start=1):
        if chunk_id in golden_ids:
            return 1.0 / rank
    return 0.0


def mean_recall_at_k(cases: Iterable[EvalCase], k: int) -> float:
    """多 query 平均 recall@k。"""
    case_list = list(cases)
    if not case_list:
        return 0.0
    total = sum(recall_at_k(golden, retrieved, k) for golden, retrieved in case_list)
    return total / len(case_list)


def mean_mrr(cases: Iterable[EvalCase]) -> float:
    """多 query 平均 MRR。"""
    case_list = list(cases)
    if not case_list:
        return 0.0
    return sum(mrr(golden, retrieved) for golden, retrieved in case_list) / len(case_list)
