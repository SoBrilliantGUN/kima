"""会话/问答 API 集成：会话 CRUD + POST /api/chat（SSE）冒烟（注入 Fake，不起真库/真网）。"""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_chat_service
from app.integrations.embedding import FakeEmbeddingClient
from app.integrations.llm import FakeLLMClient
from app.integrations.rerank import FakeRerankerClient
from app.integrations.search import FakeWebSearchClient
from app.main import app
from app.models.chat import ChatConversation, ChatMessage
from app.rag.retriever import RagRetriever
from app.rag.schema import RetrievedChunk
from app.rag.service import RagService
from app.services.chat import ChatService
from tests.fakes import FakeKnowledgeBaseRepository


class FakeChatRepository:
    """内存版会话仓库，模拟 SQLAlchemy 插入语义（分配 id + 时间戳 + 最近活动排序）。"""

    def __init__(self) -> None:
        self._conversations: dict[uuid.UUID, ChatConversation] = {}
        self._messages: dict[uuid.UUID, list[ChatMessage]] = {}

    async def add_conversation(self, conversation: ChatConversation) -> ChatConversation:
        conversation.id = uuid.uuid4()
        now = datetime.now(UTC)
        conversation.created_at = now
        conversation.updated_at = now
        self._conversations[conversation.id] = conversation
        return conversation

    async def get_conversation(self, conversation_id: uuid.UUID) -> ChatConversation | None:
        return self._conversations.get(conversation_id)

    async def list_conversations(
        self, kb_id: uuid.UUID | None, *, limit: int, offset: int
    ) -> tuple[list[ChatConversation], int]:
        items = [
            c
            for c in self._conversations.values()
            if (kb_id is None and c.kb_id is None) or c.kb_id == kb_id
        ]
        items.sort(key=lambda c: (c.updated_at, c.id), reverse=True)
        return items[offset : offset + limit], len(items)

    async def delete_conversation(self, conversation: ChatConversation) -> None:
        self._conversations.pop(conversation.id, None)
        self._messages.pop(conversation.id, None)

    async def add_message(self, message: ChatMessage) -> ChatMessage:
        if message.id is None:
            message.id = uuid.uuid4()
        message.created_at = datetime.now(UTC)
        self._messages.setdefault(message.conversation_id, []).append(message)
        conversation = self._conversations.get(message.conversation_id)
        if conversation is not None:
            conversation.updated_at = datetime.now(UTC)
        return message

    async def list_messages(self, conversation_id: uuid.UUID) -> list[ChatMessage]:
        return list(self._messages.get(conversation_id, []))


class _EmptyRetrievalRepo:
    async def search_dense(
        self, kb_id: uuid.UUID, query_vec: list[float], top_k: int
    ) -> list[RetrievedChunk]:
        return []

    async def search_lexical(
        self, kb_id: uuid.UUID, query: str, top_k: int
    ) -> list[RetrievedChunk]:
        return []

    async def get_parent_contents(
        self, doc_ids: set[uuid.UUID], note_ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        return {}


def _make_rag_service() -> RagService:
    retriever = RagRetriever(
        repository=_EmptyRetrievalRepo(),
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


@pytest.fixture
def chat_repo() -> FakeChatRepository:
    return FakeChatRepository()


@pytest.fixture
def kb_repo() -> FakeKnowledgeBaseRepository:
    return FakeKnowledgeBaseRepository()


@pytest.fixture
async def api_client(
    chat_repo: FakeChatRepository, kb_repo: FakeKnowledgeBaseRepository
) -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_chat_service] = lambda: ChatService(
        chat_repo, kb_repo, _make_rag_service()
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def test_conversation_crud(api_client: AsyncClient) -> None:
    created = (await api_client.post("/api/conversations", json={})).json()
    assert created["title"] == "新对话"

    listed = (await api_client.get("/api/conversations")).json()
    assert listed["total"] == 1

    detail = (await api_client.get(f"/api/conversations/{created['id']}")).json()
    assert detail["messages"] == []

    response = await api_client.delete(f"/api/conversations/{created['id']}")
    assert response.status_code == 204
    assert (await api_client.get("/api/conversations")).json()["total"] == 0


async def test_chat_kb_mode_requires_kb_id(api_client: AsyncClient) -> None:
    response = await api_client.post("/api/chat", json={"mode": "kb", "question": "hi"})
    assert response.status_code == 422


async def test_chat_web_mode_sse(api_client: AsyncClient) -> None:
    events: list[tuple[str | None, object]] = []
    async with api_client.stream(
        "POST", "/api/chat", json={"mode": "web", "question": "问一下"}
    ) as response:
        assert response.status_code == 200
        current_event: str | None = None
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                current_event = line[len("event: ") :]
            elif line.startswith("data: "):
                events.append((current_event, json.loads(line[len("data: ") :])))

    names = [name for name, _ in events]
    assert names[0] == "meta"
    assert "delta" in names
    assert "citations" in names
    assert names[-1] == "done"

    citations = next(data for name, data in events if name == "citations")
    assert len(citations) == 5  # FakeWebSearchClient 默认 top_k=5


async def test_chat_missing_conversation_error(api_client: AsyncClient) -> None:
    async with api_client.stream(
        "POST",
        "/api/chat",
        json={"mode": "web", "question": "hi", "conversation_id": str(uuid.uuid4())},
    ) as response:
        assert response.status_code == 200
        body = ""
        async for line in response.aiter_lines():
            body += line + "\n"
    assert "event: error" in body
    assert "not_found" in body
