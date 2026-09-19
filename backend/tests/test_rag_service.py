"""RagService.answer() 编排单测：五步编排 + 流式事件顺序（注入 Fake，不起真库/真网）。"""

import uuid
from collections.abc import AsyncIterator

from app.integrations.embedding import FakeEmbeddingClient
from app.integrations.llm import FakeLLMClient
from app.integrations.rerank import FakeRerankerClient
from app.integrations.search import FakeWebSearchClient
from app.rag.generate import AnswerCitations, AnswerDelta, AnswerEvent
from app.rag.retriever import RagRetriever
from app.rag.schema import RetrievedChunk, SourceType
from app.rag.service import RagService


class _FakeRepo:
    async def search_dense(
        self, kb_ids: list[uuid.UUID], query_vec: list[float], top_k: int
    ) -> list[RetrievedChunk]:
        return []

    async def search_lexical(
        self, kb_ids: list[uuid.UUID], query: str, top_k: int
    ) -> list[RetrievedChunk]:
        return []

    async def get_parent_contents(
        self, doc_ids: set[uuid.UUID], note_ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        return {}


def _service() -> RagService:
    retriever = RagRetriever(
        repository=_FakeRepo(),
        embedder=FakeEmbeddingClient(dimension=4),
        reranker=FakeRerankerClient(),
    )
    return RagService(
        retriever=retriever,
        llm=FakeLLMClient(),
        web_search=FakeWebSearchClient(),
        context_max_tokens=6000,
        history_recent_turns=3,
    )


async def _collect(agen: AsyncIterator[AnswerEvent]) -> list[AnswerEvent]:
    return [event async for event in agen]


async def test_answer_kb_mode_event_order() -> None:
    events = await _collect(
        _service().answer(query="问一下", kb_ids=[uuid.uuid4()], web_search=False, history=[])
    )
    assert any(isinstance(event, AnswerDelta) for event in events)
    assert isinstance(events[-1], AnswerCitations)
    assert events[-1].citations == []  # 空检索 → 无引用


async def test_answer_web_mode_citations() -> None:
    events = await _collect(
        _service().answer(query="问一下", kb_ids=[], web_search=True, history=[])
    )
    assert isinstance(events[-1], AnswerCitations)
    assert len(events[-1].citations) == 5  # FakeWebSearchClient 默认 top_k=5
    assert all(citation.source_type == SourceType.WEB for citation in events[-1].citations)


async def test_answer_none_mode_no_citations() -> None:
    events = await _collect(
        _service().answer(query="问一下", kb_ids=[], web_search=False, history=[])
    )
    assert isinstance(events[-1], AnswerCitations)
    assert events[-1].citations == []  # 不联网也不检索知识库 → 纯 LLM、无引用
