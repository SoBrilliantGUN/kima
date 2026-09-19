"""上下文组装：检索上下文格式化（引用编号）+ 历史 token 预算 + 超预算历史摘要。

核心原则：不是硬编码「喂几轮」，而是用 token 预算驱动——历史先全塞，超预算时
保留「尽可能多」的最近 N 轮 verbatim（recent 自身超预算则逐轮降级），把更早的
历史用 LLM 压缩成摘要，并硬截断到剩余预算内，保证总 token 不超预算。
"""

from collections.abc import Callable

from app.chunking.base import estimate_tokens
from app.integrations.llm import ChatMessage, LLMClient
from app.integrations.search import WebSearchResult
from app.rag.schema import Citation, RetrievedChunk, SourceType

SUMMARY_PREFIX = "[对话历史摘要]"
_SUMMARY_SYSTEM = (
    "你是对话压缩助手。把下面的对话历史压缩成一段简短摘要，"
    "保留关键信息（用户问过什么、得到什么结论）。只输出摘要，不要解释。"
)

TokenEstimator = Callable[[str], int]


def format_kb_context(chunks: list[RetrievedChunk]) -> tuple[str, list[Citation]]:
    """把 KB 检索命中组装成带 `[n]` 编号的上下文 + 引用列表。"""
    parts: list[str] = []
    citations: list[Citation] = []
    for index, chunk in enumerate(chunks, start=1):
        parts.append(f"[{index}] {chunk.title}\n{chunk.content}")
        citations.append(
            Citation(
                index=index,
                source_type=chunk.source_type,
                source_id=chunk.source_id,
                chunk_id=chunk.chunk_id,
                title=chunk.title,
                snippet=chunk.snippet,
            )
        )
    return "\n\n".join(parts), citations


def format_web_context(results: list[WebSearchResult]) -> tuple[str, list[Citation]]:
    """把全网搜索结果组装成带 `[n]` 编号的上下文 + 引用列表。"""
    parts: list[str] = []
    citations: list[Citation] = []
    for index, result in enumerate(results, start=1):
        parts.append(f"[{index}] {result.title}\n{result.snippet}")
        citations.append(
            Citation(
                index=index,
                source_type=SourceType.WEB,
                source_id=None,
                chunk_id=None,
                title=result.title,
                snippet=result.snippet,
                url=result.url,
            )
        )
    return "\n\n".join(parts), citations


def split_history(
    history: list[ChatMessage],
    *,
    recent_turns: int,
    history_budget: int,
    estimate: TokenEstimator,
) -> tuple[list[ChatMessage], list[ChatMessage]]:
    """返回 `(older, recent)`：全部历史在预算内 → older 空、recent=全部；
    超预算 → 从 recent_turns 起逐轮降级，保留「塞得下」的最近轮 verbatim，其余归 older。
    """
    total = sum(estimate(message.content) for message in history)
    if total <= history_budget:
        return [], history
    # 逐轮降级：保留尽可能多的 verbatim 轮次，recent 部分必须能塞进预算
    recent_count = 0
    for turns in range(recent_turns, 0, -1):
        count = turns * 2
        if count > len(history):
            continue
        recent_tokens = sum(estimate(m.content) for m in history[-count:])
        if recent_tokens <= history_budget:
            recent_count = count
            break
    if recent_count == 0:
        return history, []
    return history[:-recent_count], history[-recent_count:]


def _truncate_to_tokens(text: str, max_tokens: int, estimate: TokenEstimator) -> str:
    """把 text 硬截断到 max_tokens（安全网，防止摘要撑爆剩余预算）。"""
    if max_tokens <= 0:
        return ""
    if estimate(text) <= max_tokens:
        return text
    # 二分找最长前缀 ≤ max_tokens
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip()


async def summarize_history(
    llm: LLMClient,
    older: list[ChatMessage],
    *,
    budget: int,
    estimate: TokenEstimator = estimate_tokens,
) -> str:
    """把更早的历史压缩成一段摘要（一次 temperature=0 的 LLM 调用），并截断到 budget。"""
    if not older:
        return ""
    system = f"{_SUMMARY_SYSTEM}（尽量精简，控制在 {budget} token 以内）"
    messages = [ChatMessage("system", system), *older]
    result = await llm.chat(messages, temperature=0)
    return _truncate_to_tokens(result.content.strip(), budget, estimate)


async def assemble_history(
    llm: LLMClient,
    history: list[ChatMessage],
    *,
    recent_turns: int,
    history_budget: int,
    estimate: TokenEstimator = estimate_tokens,
) -> list[ChatMessage]:
    """按预算组装历史：超预算时摘要更早部分、保留最近 N 轮 verbatim；
    摘要被硬截断到「剩余预算」，保证 `[summary, *recent]` 总 token 不超预算。
    """
    older, recent = split_history(
        history, recent_turns=recent_turns, history_budget=history_budget, estimate=estimate
    )
    if not older:
        return recent
    recent_tokens = sum(estimate(m.content) for m in recent)
    summary_budget = history_budget - recent_tokens
    if summary_budget <= 0:
        return recent
    summary = await summarize_history(llm, older, budget=summary_budget, estimate=estimate)
    if not summary:
        return recent
    return [ChatMessage("user", f"{SUMMARY_PREFIX}\n{summary}"), *recent]
