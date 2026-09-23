"""Copilot Agent 循环：脚本化模型跑工具回环 + 事件顺序 + 事件日志落库。"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.agent.events import (
    CopilotDeltaEvent,
    CopilotDoneEvent,
    CopilotMetaEvent,
    CopilotStepEvent,
)
from app.agent.runtime.budget import HardBudget
from app.agent.runtime.config import RuntimeConfig
from app.agent.service import CopilotService
from app.agent.tuning import CopilotTuning
from app.core.memory_store import FileMemoryStore
from app.integrations.embedding import FakeEmbeddingClient
from app.integrations.rerank import FakeRerankerClient
from app.integrations.search import FakeWebSearchClient
from app.models.chat import ChatConversation, ChatKind, ChatMessage
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
    tmp_path: Path, model: ScriptedAgentModel, runtime: RuntimeConfig | None = None
) -> tuple[CopilotService, FakeCopilotEventRepository, FakeChatRepository]:
    kb_repo = FakeKnowledgeBaseRepository()
    kb_service = KnowledgeBaseService(kb_repo)
    note_service = NoteService(FakeNoteRepository(), kb_repo)
    document_service = DocumentService(FakeDocumentRepository(), kb_repo, FakeFileStore())
    embedder = FakeEmbeddingClient(dimension=8)
    retriever = RagRetriever(
        repository=_EmptyRetrievalRepo(), embedder=embedder, reranker=FakeRerankerClient()
    )
    memory_service = CopilotMemoryService(
        repository=FakeCopilotMemoryRepository(),
        embedder=embedder,
        judge=FakeConflictJudge(),
        capacity=200,
        episodic_ttl_days=30,
        recall_floor=0.05,
        recency_window_days=7,
        conflict_top_k=10,
    )
    event_repo = FakeCopilotEventRepository()
    chat_repo = FakeChatRepository()
    return CopilotService(
        model=model,
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
        tuning=CopilotTuning(max_result_chars=4000),
        runtime=runtime,
    ), event_repo, chat_repo


async def test_agent_tool_loop_and_event_log(tmp_path: Path) -> None:
    model = ScriptedAgentModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": "list_notes", "args": {}, "id": "call_1"}]),
            AIMessage(content="库里没有笔记。"),
        ]
    )
    service, event_repo, chat_repo = make_service(tmp_path, model)

    events = [event async for event in service.run(CopilotRequest(question="有哪些笔记？"))]

    kinds = [type(e) for e in events]
    assert CopilotMetaEvent in kinds
    assert CopilotStepEvent in kinds
    assert CopilotDeltaEvent in kinds
    assert kinds[-1] is CopilotDoneEvent

    step = next(e for e in events if isinstance(e, CopilotStepEvent))
    assert step.tool_name == "list_notes"

    done = next(e for e in events if isinstance(e, CopilotDoneEvent))
    # 最终回答文本经 delta 拼出
    deltas = [e.text for e in events if isinstance(e, CopilotDeltaEvent)]
    assert "".join(deltas) == "库里没有笔记。"

    # 事件日志：tool_call → tool_result → done 顺序落库
    logged = [e.type for e in event_repo.events]
    assert "tool_call" in logged
    assert "tool_result" in logged
    assert logged[-1] == "done"
    assert all(e.run_id == event_repo.events[0].run_id for e in event_repo.events)

    # 会话建为 copilot 类型，消息含工具轨迹投影 steps
    conversation = list(chat_repo._conversations.values())[0]
    assert conversation.kind == ChatKind.COPILOT.value
    messages = await chat_repo.list_messages(conversation.id)
    assistant = next(m for m in messages if m.role.value == "assistant")
    assert assistant.steps == [{"tool_name": "list_notes", "args": {}}]
    assert done.assistant_message_id == assistant.id


async def test_done_event_records_accounting_and_attribution(tmp_path: Path) -> None:
    """done 事件落「单位任务账本」：intent/model 归因 + 账本快照（成本/token/缓存命中/轮数）。"""
    model = ScriptedAgentModel(responses=[AIMessage(content="你好。")])
    runtime = RuntimeConfig(budget=HardBudget(max_turns=5))
    service, event_repo, _ = make_service(tmp_path, model, runtime=runtime)

    events = [event async for event in service.run(CopilotRequest(question="你好"))]

    assert isinstance(events[-1], CopilotDoneEvent)
    done = event_repo.events[-1]
    assert done.type == "done"
    payload = done.payload
    assert payload["intent"] == "task"
    assert payload["model"] == "unknown"
    accounting = payload["accounting"]
    assert set(accounting) >= {
        "cost_cny",
        "tokens",
        "input_tokens",
        "cache_read_tokens",
        "cache_hit_rate",
        "turn_count",
        "tool_call_count",
    }
    # fake 模型无 usage_metadata → 用量全 0，但账本结构仍在；turn 计了 1 轮
    assert accounting["turn_count"] == 1
    assert accounting["cost_cny"] == 0.0
    assert accounting["cache_hit_rate"] is None
