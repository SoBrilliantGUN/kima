"""契约交互：上行 OutputContract 校验 + 约束显式携带（planner/synthesizer）+ 输出自检。"""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from pydantic import PrivateAttr

from app.agent.compose import CopilotRuntime, build_runtime
from app.agent.guardrail.review import (
    ReviewIssue,
    ReviewResult,
    ReviewVerdict,
    SideEffectVerifier,
)
from app.agent.plan_runner import PlanRunner
from app.agent.runtime.budget import BudgetTracker, HardBudget
from app.agent.runtime.config import RuntimeConfig
from app.agent.runtime.planner import LLMPlanner, Plan, PlanStep, StepStatus
from app.agent.toolmeta import OutputContract, apply_output_contract
from app.core.memory_store import FileMemoryStore
from app.core.skill_store import FileSkillStore
from app.integrations.embedding import FakeEmbeddingClient
from app.integrations.rerank import FakeRerankerClient
from app.integrations.search import FakeWebSearchClient
from app.models.chat import ChatConversation, ChatMessage
from app.rag.retriever import RagRetriever
from app.rag.schema import RetrievedChunk
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
    FakeMemoryClassifier,
    FakeNoteRepository,
    FakeOutputReviewer,
    ScriptedLLM,
    gateway_run,
    make_copilot_defaults,
    make_gateway,
)

# --- OutputContract 上行契约关（纯函数） ---


def test_contract_no_contract_passthrough() -> None:
    assert apply_output_contract("search", "hello", None) == "hello"


def test_contract_max_chars_clips() -> None:
    contract = OutputContract(max_chars=5)
    assert apply_output_contract("search", "hello world", contract) == "hello…"


def test_contract_required_missing_rejects() -> None:
    contract = OutputContract(required={"output": str})
    out = apply_output_contract("search", '{"state": "x"}', contract)
    assert "缺必填字段" in out and "output" in out


def test_contract_type_mismatch_rejects() -> None:
    contract = OutputContract(required={"output": str})
    out = apply_output_contract("search", '{"output": 1}', contract)
    assert "类型不符" in out


def test_contract_whitelist_strips_undeclared() -> None:
    contract = OutputContract(required={"output": str}, optional={"state": str})
    out = apply_output_contract("search", '{"output":"o","state":"s","reasoning":"秘密"}', contract)
    assert "output" in out and "state" in out
    assert "reasoning" not in out


def test_contract_rejects_non_json() -> None:
    contract = OutputContract(required={"output": str})
    out = apply_output_contract("search", "不是 JSON", contract)
    assert "不是合法 JSON" in out


# --- 约束显式携带（planner 下行任务包） ---


async def test_planner_generate_carries_constraints() -> None:
    llm = ScriptedLLM(['{"steps":[]}'])
    planner = LLMPlanner(make_gateway(llm=llm))
    async with gateway_run():
        await planner.generate("总结文档", ["search"], constraints="[MEMORY]\n- 禁止使用 ORM")
    assert len(llm.calls) == 1
    user_content = llm.calls[0][1].content
    assert "禁止使用 ORM" in user_content


# --- 合成回答 / 输出自检（需要完整服务，用录制模型捕获输入消息） ---


class RecordingModel(FakeMessagesListChatModel):
    """记录传入 ainvoke 的消息序列，返回固定回答（合成路径不走 bind_tools）。"""

    _calls: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    def __init__(self, response: AIMessage) -> None:
        super().__init__(responses=[response])

    def bind_tools(self, tools: object, **kwargs: object) -> "RecordingModel":
        return self

    @property
    def calls(self) -> list[list[BaseMessage]]:
        return self._calls

    async def ainvoke(self, input: object, config: object = None, **kwargs: object) -> AIMessage:
        self._calls.append(cast(list[BaseMessage], input))
        return cast(AIMessage, self.responses[0])


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
        conversation.created_at = datetime.now(UTC)
        conversation.updated_at = datetime.now(UTC)
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


def _make_service(
    tmp_path: Path,
    model: RecordingModel,
    reviewer: FakeOutputReviewer | None = None,
    verifier: SideEffectVerifier | None = None,
) -> CopilotRuntime:
    kb_repo = FakeKnowledgeBaseRepository()
    kb_service = KnowledgeBaseService(kb_repo)
    note_service = NoteService(FakeNoteRepository(), kb_repo)
    document_service = DocumentService(FakeDocumentRepository(), kb_repo, FakeFileStore())
    embedder = FakeEmbeddingClient(dimension=8)
    gateway = make_gateway(embedder=embedder, reranker=FakeRerankerClient())
    retriever = RagRetriever(repository=_EmptyRetrievalRepo(), gateway=gateway)
    memory_service = CopilotMemoryService(
        repository=FakeCopilotMemoryRepository(),
        gateway=gateway,
        judge=FakeConflictJudge(),
        classifier=FakeMemoryClassifier(),
        episodic_ttl_days=30,
        recency_window_days=7,
        conflict_top_k=10,
    )
    return build_runtime(
        model=model,
        gateway=gateway,
        rag_retriever=retriever,
        kb_service=kb_service,
        note_service=note_service,
        document_service=document_service,
        web_search=FakeWebSearchClient(),
        memory_service=memory_service,
        memory_store=FileMemoryStore(tmp_path),
        skill_store=FileSkillStore(tmp_path / "skills"),
        chat_repository=FakeChatRepository(),
        event_repository=FakeCopilotEventRepository(),
        reviewer=reviewer or FakeOutputReviewer(),
        runtime=RuntimeConfig(),
        **make_copilot_defaults(note_service, memory_service, verifier=verifier),
    )


async def test_synthesize_carries_constraints(tmp_path: Path) -> None:
    model = RecordingModel(AIMessage(content="答案"))
    rt = _make_service(tmp_path, model)
    plan = Plan(steps=(PlanStep(step_id="1", action="search", params={}, is_terminal=True),))

    answer = await PlanRunner(rt)._synthesize_plan_answer(
        "任务",
        {"1": "r1"},
        BudgetTracker(HardBudget()),
        uuid.uuid4(),
        plan,
        system_prompt="【系统】SYS底线",
        memory_block="[MEMORY]\n- 禁止使用 ORM",
    )
    assert answer == "答案"
    messages = model.calls[0]
    system = next(m for m in messages if isinstance(m, SystemMessage))
    assert "SYS底线" in str(system.content)
    assert any("禁止使用 ORM" in str(m.content) for m in messages)


async def test_plan_review_appends_correction_on_mismatch(tmp_path: Path) -> None:
    reviewer = FakeOutputReviewer(
        results=[
            ReviewResult(
                verdict=ReviewVerdict.MISMATCH,
                issues=[
                    ReviewIssue(claim="已创建笔记", tool="create_note", evidence="no_tool_call")
                ],
            )
        ]
    )
    model = RecordingModel(AIMessage(content="答案"))
    rt = _make_service(tmp_path, model, reviewer=reviewer)
    plan = Plan(
        steps=(
            PlanStep(
                step_id="1",
                action="search",
                params={},
                status=StepStatus.COMPLETED,
                output_ref="r1",
            ),
        )
    )

    answer = await PlanRunner(rt)._review_plan_answer(
        "已创建笔记", plan, {"1": "r1"}, uuid.uuid4(), BudgetTracker(HardBudget())
    )
    assert "自检更正" in answer
    assert len(reviewer.calls) == 1


async def test_plan_review_passes_when_ok(tmp_path: Path) -> None:
    reviewer = FakeOutputReviewer(results=[ReviewResult(verdict=ReviewVerdict.OK)])
    model = RecordingModel(AIMessage(content="答案"))
    rt = _make_service(tmp_path, model, reviewer=reviewer)
    plan = Plan(
        steps=(PlanStep(step_id="1", action="search", params={}, status=StepStatus.COMPLETED),)
    )

    answer = await PlanRunner(rt)._review_plan_answer(
        "普通回答", plan, {"1": "r1"}, uuid.uuid4(), BudgetTracker(HardBudget())
    )
    assert answer == "普通回答"
    assert len(reviewer.calls) == 1


class _FailingVerifier:
    """确定性对账器替身：对任何写工具返回失败原因（模拟「声称成功但副作用未落库」）。"""

    async def verify(self, tool_name: str, args: dict[str, Any], result: str) -> str:
        return f"{tool_name} 副作用缺失"


async def test_plan_review_deterministic_verifier_forces_correction(tmp_path: Path) -> None:
    """写工具步骤副作用未落库 → 确定性对账强制更正，LLM 审查器根本不跑。"""
    reviewer = FakeOutputReviewer(results=[ReviewResult(verdict=ReviewVerdict.OK)])
    verifier = _FailingVerifier()
    model = RecordingModel(AIMessage(content="答案"))
    rt = _make_service(tmp_path, model, reviewer=reviewer, verifier=verifier)
    plan = Plan(
        steps=(
            PlanStep(
                step_id="1",
                action="create_note",
                params={"title": "t", "content": "c"},
                status=StepStatus.COMPLETED,
                output_ref="已创建笔记 00000000-0000-0000-0000-000000000001",
            ),
        )
    )

    answer = await PlanRunner(rt)._review_plan_answer(
        "已创建笔记",
        plan,
        {"1": "已创建笔记 00000000-0000-0000-0000-000000000001"},
        uuid.uuid4(),
        BudgetTracker(HardBudget()),
    )
    assert "自检更正" in answer
    assert len(reviewer.calls) == 0  # 确定性对账先命中，LLM 审查器没跑
