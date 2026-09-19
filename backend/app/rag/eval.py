"""端到端 RAG 指标（LLM-judge）：faithfulness / answer_relevancy / context_relevancy。

最小集：只实现无需 ground-truth 的三项（对照 RAGAS 语义），1–5 分归一化到 0–1。
不引入 ragas 重包（避免拖入 langchain），直接复用现有 LLMClient；judge 用
temperature=0 提高稳定性，但仍建议多次采样取均值。
"""

import re
from dataclasses import dataclass

from app.integrations.llm import ChatMessage, LLMClient

_SCORE_SYSTEM = (
    "你是严格的中文评估助手。根据评分标准给出 1–5 的整数分，只输出一个数字，不要解释。"
)


@dataclass(frozen=True)
class EvalSample:
    """一条端到端评估样本（无需黄金答案）。"""

    question: str
    answer: str
    contexts: list[str]


async def faithfulness(llm: LLMClient, sample: EvalSample) -> float:
    """答案是否忠于上下文（每个事实都能被上下文支持，无编造）。"""
    ctx = "\n\n".join(sample.contexts)
    instruction = (
        f"问题：{sample.question}\n\n"
        f"上下文：\n{ctx}\n\n"
        f"答案：{sample.answer}\n\n"
        "评分标准：答案中的每个事实是否都能由上下文支持；1=大量编造，5=完全忠实。"
    )
    return await _score(llm, instruction)


async def answer_relevancy(llm: LLMClient, question: str, answer: str) -> float:
    """答案是否直接、完整地回答了问题（不含无关冗余内容）。"""
    instruction = (
        f"问题：{question}\n\n答案：{answer}\n\n"
        "评分标准：答案是否切题、无冗余；1=完全无关，5=精准切题。"
    )
    return await _score(llm, instruction)


async def context_relevancy(llm: LLMClient, question: str, contexts: list[str]) -> float:
    """检索到的上下文与问题的相关度（衡量召回质量）。"""
    ctx = "\n\n".join(contexts)
    instruction = (
        f"问题：{question}\n\n上下文：\n{ctx}\n\n"
        "评分标准：上下文是否包含回答问题所需信息、无关内容少；1=几乎无关，5=高度相关。"
    )
    return await _score(llm, instruction)


async def _score(llm: LLMClient, instruction: str) -> float:
    result = await llm.chat(
        [ChatMessage("system", _SCORE_SYSTEM), ChatMessage("user", instruction)],
        temperature=0,
    )
    return _parse_score(result.content)


def _parse_score(text: str) -> float:
    """从 judge 输出里提取第一个 1–5 数字，归一化到 0–1；无数字记 0。"""
    match = re.search(r"[1-5]", text)
    if match is None:
        return 0.0
    return (int(match.group()) - 1) / 4
