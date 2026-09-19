"""检索评估运行器：跑 golden 集 → recall@k / MRR 报告。

golden 集用「应命中片段」标注（子串匹配）而非 chunk UUID——UUID 每次入库都变，
子串匹配可跨次重跑。命中判据：检索返回 chunk 的 `content`（回 parent 后）或
`snippet`（child 原文）包含 golden 片段。配合 `app/rag/metrics.py`（id 版指标）
与 `app/rag/eval.py`（LLM-judge 端到端指标）。
"""

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.rag.schema import RetrievedChunk

RetrieveFn = Callable[[str, str], Awaitable[list[RetrievedChunk]]]


@dataclass(frozen=True)
class RetrievalCase:
    """一条检索评估用例：`golden_snippet` 是「应被召回的答案关键内容」子串。"""

    query: str
    kb_id: str
    golden_snippet: str


@dataclass(frozen=True)
class CaseResult:
    query: str
    hit_rank: int  # 首个命中 chunk 的排名（1-based），0 = 未命中


@dataclass(frozen=True)
class RetrievalEvalReport:
    cases: tuple[CaseResult, ...]
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr: float


def load_retrieval_cases(path: str | Path) -> list[RetrievalCase]:
    """从 JSON 文件加载 golden 集：`[{"query", "kb_id", "golden_snippet"}]`。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        RetrievalCase(
            query=str(item["query"]),
            kb_id=str(item["kb_id"]),
            golden_snippet=str(item["golden_snippet"]),
        )
        for item in raw
    ]


async def run_retrieval_eval(
    retrieve: RetrieveFn,
    cases: Sequence[RetrievalCase],
    top_k: int = 6,
) -> RetrievalEvalReport:
    """逐条跑检索，计算 recall@1/3/5 与 MRR。"""
    results = [await _run_case(retrieve, case, top_k) for case in cases]
    n = len(results) or 1
    return RetrievalEvalReport(
        cases=tuple(results),
        recall_at_1=_recall_at_k(results, 1),
        recall_at_3=_recall_at_k(results, 3),
        recall_at_5=_recall_at_k(results, 5),
        mrr=sum(1.0 / r.hit_rank for r in results if r.hit_rank > 0) / n,
    )


async def _run_case(retrieve: RetrieveFn, case: RetrievalCase, top_k: int) -> CaseResult:
    chunks = await retrieve(case.query, case.kb_id)
    for rank, chunk in enumerate(chunks[:top_k], start=1):
        if case.golden_snippet in chunk.content or case.golden_snippet in chunk.snippet:
            return CaseResult(query=case.query, hit_rank=rank)
    return CaseResult(query=case.query, hit_rank=0)


def _recall_at_k(results: Sequence[CaseResult], k: int) -> float:
    if not results:
        return 0.0
    return sum(1 for r in results if 0 < r.hit_rank <= k) / len(results)


def format_report(report: RetrievalEvalReport) -> str:
    """渲染可读报告（供 CLI 打印）。"""
    lines = [
        f"检索评估报告（{len(report.cases)} 条）",
        f"  recall@1: {report.recall_at_1:.3f}",
        f"  recall@3: {report.recall_at_3:.3f}",
        f"  recall@5: {report.recall_at_5:.3f}",
        f"  MRR:      {report.mrr:.3f}",
        "逐条：",
    ]
    for case in report.cases:
        mark = f"hit@{case.hit_rank}" if case.hit_rank else "miss"
        lines.append(f"  [{mark}] {case.query}")
    return "\n".join(lines)
