"""检索评估运行器测试：golden 集加载 + recall@k/MRR 计算（Fake retrieve）。"""

import json
import uuid

from app.rag.eval_runner import (
    CaseResult,
    RetrievalCase,
    RetrievalEvalReport,
    format_report,
    load_retrieval_cases,
    run_retrieval_eval,
)
from app.rag.schema import RetrievedChunk, SourceType


def _chunk(content: str) -> RetrievedChunk:
    return RetrievedChunk(
        source_type=SourceType.DOCUMENT,
        source_id=uuid.uuid4(),
        chunk_id=uuid.uuid4(),
        parent_id=None,
        content=content,
        title="t",
        snippet=content,
    )


async def _retrieve(query: str, kb_id: str) -> list[RetrievedChunk]:
    # 固定返回：第一条含 golden 片段、第二条无关
    return [_chunk("包含目标片段的内容"), _chunk("无关内容")]


def test_load_retrieval_cases(tmp_path) -> None:
    path = tmp_path / "golden.json"
    path.write_text(
        json.dumps([{"query": "q", "kb_id": "k", "golden_snippet": "s"}]),
        encoding="utf-8",
    )
    assert load_retrieval_cases(path) == [RetrievalCase("q", "k", "s")]


async def test_run_retrieval_eval_recall_and_mrr() -> None:
    cases = [
        RetrievalCase(query="q1", kb_id="k", golden_snippet="目标片段"),
        RetrievalCase(query="q2", kb_id="k", golden_snippet="不存在的片段"),
    ]
    report = await run_retrieval_eval(_retrieve, cases, top_k=3)
    assert report.cases[0].hit_rank == 1
    assert report.cases[1].hit_rank == 0
    assert report.recall_at_1 == 0.5
    assert report.mrr == 0.5  # (1/1 + 0) / 2


def test_format_report_contains_metrics() -> None:
    report = RetrievalEvalReport(
        cases=(CaseResult("q", 1),),
        recall_at_1=1.0,
        recall_at_3=1.0,
        recall_at_5=1.0,
        mrr=1.0,
    )
    text = format_report(report)
    assert "recall@1" in text
    assert "MRR" in text
