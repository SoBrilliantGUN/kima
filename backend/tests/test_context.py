"""上下文组装单测：引用编号 + 历史 token 预算 + 超预算摘要。"""

import uuid

from app.integrations.llm import ChatMessage, ChatResult
from app.integrations.search import WebSearchResult
from app.rag.context import (
    SUMMARY_PREFIX,
    assemble_history,
    format_kb_context,
    format_web_context,
    split_history,
    summarize_history,
)
from app.rag.schema import RetrievedChunk, SourceType


def _chunk(
    content: str, title: str = "t", source_type: SourceType = SourceType.DOCUMENT
) -> RetrievedChunk:
    return RetrievedChunk(
        source_type=source_type,
        source_id=uuid.uuid4(),
        chunk_id=uuid.uuid4(),
        parent_id=None,
        content=content,
        title=title,
        snippet=content,
    )


class _FixedLLM:
    """返回固定文本的 LLM 替身，用于摘要。"""

    def __init__(self, text: str) -> None:
        self._text = text

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult:
        return ChatResult(content=self._text)

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ):
        yield self._text


def test_format_kb_context() -> None:
    chunks = [
        _chunk("内容A", title="文档A"),
        _chunk("内容B", title="笔记B", source_type=SourceType.NOTE),
    ]
    context, citations = format_kb_context(chunks)
    assert "[1] 文档A" in context and "内容A" in context
    assert "[2] 笔记B" in context
    assert len(citations) == 2
    assert citations[0].index == 1 and citations[0].title == "文档A"
    assert citations[1].source_type == SourceType.NOTE


def test_format_web_context() -> None:
    results = [WebSearchResult(title="网页", url="https://x.com", snippet="摘要")]
    context, citations = format_web_context(results)
    assert "[1] 网页" in context
    assert citations[0].url == "https://x.com"
    assert citations[0].source_type == SourceType.WEB


def test_split_history_within_budget() -> None:
    history = [ChatMessage("user", "hi"), ChatMessage("assistant", "hello")]
    older, recent = split_history(history, recent_turns=3, history_budget=1000, estimate=len)
    assert older == []
    assert recent == history


def test_split_history_over_budget() -> None:
    history = [
        ChatMessage("user", "a" * 100),
        ChatMessage("assistant", "b" * 100),
        ChatMessage("user", "c" * 100),
        ChatMessage("assistant", "d" * 100),
    ]
    older, recent = split_history(history, recent_turns=1, history_budget=250, estimate=len)
    assert len(recent) == 2
    assert len(older) == 2


def test_split_history_degrades_recent_turns() -> None:
    history = [
        ChatMessage("user", "a" * 150),
        ChatMessage("assistant", "b" * 150),
        ChatMessage("user", "c" * 100),
        ChatMessage("assistant", "d" * 50),
    ]
    # recent_turns=2 → 最近 2 轮（4 条）450 token 超预算，降级到 1 轮（2 条）150 token
    older, recent = split_history(history, recent_turns=2, history_budget=250, estimate=len)
    assert len(older) == 2
    assert len(recent) == 2
    assert recent[0].content == "c" * 100


def test_split_history_recent_never_fits() -> None:
    history = [
        ChatMessage("user", "a" * 300),
        ChatMessage("assistant", "b" * 300),
    ]
    # 最近 1 轮（2 条）600 token 仍超预算 → recent 降为 0，全部归 older 去摘要
    older, recent = split_history(history, recent_turns=1, history_budget=250, estimate=len)
    assert older == history
    assert recent == []


async def test_summarize_history_truncates_to_budget() -> None:
    llm = _FixedLLM("x" * 1000)
    older = [ChatMessage("user", "q")]
    summary = await summarize_history(llm, older, budget=10, estimate=len)
    assert len(summary) <= 10


async def test_assemble_history_no_summary() -> None:
    llm = _FixedLLM("摘要")
    history = [ChatMessage("user", "hi"), ChatMessage("assistant", "hello")]
    result = await assemble_history(
        llm, history, recent_turns=3, history_budget=1000, estimate=len
    )
    assert result == history


async def test_assemble_history_with_summary() -> None:
    llm = _FixedLLM("这是摘要")
    history = [
        ChatMessage("user", "a" * 100),
        ChatMessage("assistant", "b" * 100),
        ChatMessage("user", "c" * 100),
        ChatMessage("assistant", "d" * 100),
    ]
    result = await assemble_history(
        llm, history, recent_turns=1, history_budget=250, estimate=len
    )
    assert len(result) == 3  # 摘要 + 最近 2 条
    assert result[0].role == "user"
    assert result[0].content.startswith(SUMMARY_PREFIX)
