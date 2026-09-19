"""RAG 评估最小集测试：检索指标（纯函数）+ LLM-judge 指标（Fake judge）。"""

from app.integrations.llm import ChatResult
from app.rag.eval import (
    EvalSample,
    _parse_score,
    answer_relevancy,
    context_relevancy,
    faithfulness,
)
from app.rag.metrics import mean_mrr, mean_recall_at_k, mrr, recall_at_k


class _JudgeLLM:
    """固定返回指定分数的 judge，供确定性测试。"""

    def __init__(self, content: str) -> None:
        self._content = content

    async def chat(self, messages, *, temperature=0.7, max_tokens=None) -> ChatResult:
        return ChatResult(content=self._content)


def test_recall_at_k() -> None:
    golden = {"a", "b"}
    assert recall_at_k(golden, ["a", "c", "b"], k=2) == 0.5
    assert recall_at_k(golden, ["a", "b", "c"], k=2) == 1.0
    assert recall_at_k(set(), ["a"], k=2) == 0.0


def test_mrr() -> None:
    assert mrr({"a"}, ["x", "a", "y"]) == 0.5
    assert mrr({"a"}, ["a", "b"]) == 1.0
    assert mrr({"a"}, ["x", "y"]) == 0.0


def test_mean_metrics() -> None:
    cases = [
        ({"a"}, ["a", "b"]),
        ({"c"}, ["x", "c"]),
    ]
    assert mean_recall_at_k(cases, k=1) == 0.5  # 只有第 1 条命中 top-1
    assert mean_mrr(cases) == 0.75  # (1.0 + 0.5) / 2


def test_parse_score() -> None:
    assert _parse_score("5") == 1.0
    assert _parse_score("1") == 0.0
    assert _parse_score("3") == 0.5
    assert _parse_score("评分：4分") == 0.75
    assert _parse_score("没有数字") == 0.0


async def test_faithfulness_uses_judge_score() -> None:
    sample = EvalSample(question="q", answer="a", contexts=["c"])
    assert await faithfulness(_JudgeLLM("4"), sample) == 0.75


async def test_answer_relevancy_uses_judge_score() -> None:
    assert await answer_relevancy(_JudgeLLM("2"), "q", "a") == 0.25


async def test_context_relevancy_uses_judge_score() -> None:
    assert await context_relevancy(_JudgeLLM("5"), "q", ["c"]) == 1.0
