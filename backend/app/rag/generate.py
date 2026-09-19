"""流式生成事件：`answer()` 按顺序 yield 文本增量，最后 yield 引用。"""

from dataclasses import dataclass

from app.rag.schema import Citation


@dataclass(frozen=True)
class AnswerDelta:
    """一段回答文本增量（SSE 的 delta 事件）。"""

    text: str


@dataclass(frozen=True)
class AnswerCitations:
    """回答的引用列表（SSE 的 citations 事件，生成完成后补齐）。"""

    citations: list[Citation]


AnswerEvent = AnswerDelta | AnswerCitations
