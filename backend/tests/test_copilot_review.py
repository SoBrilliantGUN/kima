"""Copilot 输出审查节点：声称完成但没调用工具 → 自动补做 / 诚实更正。"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.agent.events import CopilotReviewEvent
from app.agent.guardrail.review import ReviewIssue, ReviewResult, ReviewVerdict
from app.agent.service import CopilotService
from app.agent.tuning import CopilotTuning
from app.core.memory_store import FileMemoryStore
from app.integrations.embedding import FakeEmbeddingClient
from app.integrations.rerank import FakeRerankerClient
from app.integrations.search import FakeWebSearchClient
from app.models.chat import ChatConversation, ChatMessage
from app.models.copilot import MemoryKind
from app.rag.retriever import RagRetriever
from app.rag.schema import RetrievedChunk
from app.schemas.copilot import CopilotRequest
from app.services.copilot import CopilotMemoryService
from app.services.document import DocumentService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.note import NoteService
from tests.fakes import (
    FakeConflictJudge,
    FakeCopilotEventRepository,
    FakeCopilotMemoryRepository,
    FakeDocumentRepository,
    FakeFileStore,
    FakeKnowledgeBaseRepository,
    FakeNoteRepository,
    FakeOutputReviewer,
    make_gateway,
)


class ScriptedAgentModel(FakeMessagesListChatModel):
    """脚本化模型：忽略 bind_tools，按 responses 顺序回放。"""

    def bind_tools(self, tools: object, **kwargs: object) -> "ScriptedAgentModel":
        return self


class _EmptyRetrievalRepo:
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


class FakeChatRepository:
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
        self, kb_id: uuid.UUID | None, kind: str | None, *, limit: int, offset: int
    ) -> tuple[list[ChatConversation], int]:
        return [], 0

    async def delete_conversation(self, conversation: ChatConversation) -> None:
        self._conversations.pop(conversation.id, None)

    async def add_message(self, message: ChatMessage) -> ChatMessage:
        message.id = message.id or uuid.uuid4()
        message.created_at = datetime.now(UTC)
        self._messages.setdefault(message.conversation_id, []).append(message)
        return message

    async def list_messages(self, conversation_id: uuid.UUID) -> list[ChatMessage]:
        return list(self._messages.get(conversation_id, []))


def make_service(
    tmp_path: Path,
    model: ScriptedAgentModel,
    reviewer: FakeOutputReviewer,
    review_max_attempts: int = 2,
) -> tuple[
    CopilotService,
    FakeCopilotEventRepository,
    FakeChatRepository,
    FakeCopilotMemoryRepository,
]:
    kb_repo = FakeKnowledgeBaseRepository()
    kb_service = KnowledgeBaseService(kb_repo)
    note_service = NoteService(FakeNoteRepository(), kb_repo)
    document_service = DocumentService(FakeDocumentRepository(), kb_repo, FakeFileStore())
    embedder = FakeEmbeddingClient(dimension=8)
    gateway = make_gateway(embedder=embedder, reranker=FakeRerankerClient())
    retriever = RagRetriever(
        repository=_EmptyRetrievalRepo(), gateway=gateway
    )
    memory_repo = FakeCopilotMemoryRepository()
    memory_service = CopilotMemoryService(
        repository=memory_repo,
        gateway=gateway,
        judge=FakeConflictJudge(),
        capacity=200,
        episodic_ttl_days=30,
        recall_floor=0.05,
        recency_window_days=7,
        conflict_top_k=10,
    )
    event_repo = FakeCopilotEventRepository()
    chat_repo = FakeChatRepository()
    service = CopilotService(
        model=model,
        gateway=gateway,
        checkpointer=None,
        tracer=None,
        rag_retriever=retriever,
        kb_service=kb_service,
        note_service=note_service,
        document_service=document_service,
        web_search=FakeWebSearchClient(),
        memory_service=memory_service,
        memory_store=FileMemoryStore(tmp_path),
        chat_repository=chat_repo,
        event_repository=event_repo,
        tuning=CopilotTuning(max_result_chars=4000, review_max_attempts=review_max_attempts),
        reviewer=reviewer,
    )
    return service, event_repo, chat_repo, memory_repo


async def _assistant_message(
    chat_repo: FakeChatRepository,
) -> ChatMessage:
    conversation = list(chat_repo._conversations.values())[0]
    messages = await chat_repo.list_messages(conversation.id)
    return next(m for m in messages if m.role.value == "assistant")


async def test_review_repairs_missing_write(tmp_path: Path) -> None:
    model = ScriptedAgentModel(
        responses=[
            AIMessage(content="记好啦，已经把兴趣写进记忆了。"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_memory",
                        "args": {
                            "kind": "semantic",
                            "content": "喜欢编程",
                            "entity_id": "user:interests",
                        },
                        "id": "call_1",
                    }
                ],
            ),
            AIMessage(content="这次真的写好了。"),
        ]
    )
    reviewer = FakeOutputReviewer(
        results=[
            ReviewResult(
                verdict=ReviewVerdict.MISMATCH,
                issues=[
                    ReviewIssue(
                        claim="已把兴趣写进记忆", tool="write_memory", evidence="no_tool_call"
                    )
                ],
            ),
            ReviewResult(verdict=ReviewVerdict.OK),
        ]
    )
    service, event_repo, chat_repo, memory_repo = make_service(tmp_path, model, reviewer)

    events = [event async for event in service.run(CopilotRequest(question="记住我喜欢编程"))]

    review_verdicts = [e.verdict for e in events if isinstance(e, CopilotReviewEvent)]
    assert review_verdicts == ["mismatch", "repaired"]

    # 修复轮真的调用了 write_memory 并写入记忆
    assert await memory_repo.count_active(MemoryKind.SEMANTIC) == 1
    tool_calls = [e.payload.get("tool_name") for e in event_repo.events if e.type == "tool_call"]
    assert "write_memory" in tool_calls

    assistant = await _assistant_message(chat_repo)
    review_step = next(s for s in assistant.steps or [] if s["tool_name"] == "review")
    assert review_step["args"]["verdict"] == "repaired"
    assert "记好啦" in assistant.content
    assert "这次真的写好了" in assistant.content


async def test_review_passes_no_repair(tmp_path: Path) -> None:
    model = ScriptedAgentModel(responses=[AIMessage(content="这是普通回答。")])
    reviewer = FakeOutputReviewer(results=[ReviewResult(verdict=ReviewVerdict.OK)])
    service, event_repo, chat_repo, _ = make_service(tmp_path, model, reviewer)

    events = [event async for event in service.run(CopilotRequest(question="你好"))]

    review_verdicts = [e.verdict for e in events if isinstance(e, CopilotReviewEvent)]
    assert review_verdicts == ["ok"]
    assert len(reviewer.calls) == 1

    assistant = await _assistant_message(chat_repo)
    assert assistant.content == "这是普通回答。"
    assert [s["tool_name"] for s in (assistant.steps or [])] == ["review"]


async def test_review_corrects_when_repair_fails(tmp_path: Path) -> None:
    model = ScriptedAgentModel(
        responses=[
            AIMessage(content="记好啦。"),
            AIMessage(content="我做不到了。"),
        ]
    )
    reviewer = FakeOutputReviewer(
        results=[
            ReviewResult(
                verdict=ReviewVerdict.MISMATCH,
                issues=[
                    ReviewIssue(claim="已写入记忆", tool="write_memory", evidence="no_tool_call")
                ],
            ),
            ReviewResult(
                verdict=ReviewVerdict.MISMATCH,
                issues=[
                    ReviewIssue(claim="已写入记忆", tool="write_memory", evidence="no_tool_call")
                ],
            ),
        ]
    )
    service, event_repo, chat_repo, memory_repo = make_service(
        tmp_path, model, reviewer, review_max_attempts=1
    )

    events = [event async for event in service.run(CopilotRequest(question="记住它"))]

    review_verdicts = [e.verdict for e in events if isinstance(e, CopilotReviewEvent)]
    assert review_verdicts == ["mismatch", "corrected"]

    assistant = await _assistant_message(chat_repo)
    assert "自检更正" in assistant.content
    # 没真的写入（修复也失败）
    assert await memory_repo.count_active(MemoryKind.SEMANTIC) == 0
