"""planner 执行图：supervisor-worker 并行执行 + replan 端到端。"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.agent.compose import CopilotRuntime, build_runtime
from app.agent.events import CopilotDoneEvent, CopilotStepEvent
from app.agent.orchestrate import make_config, make_tracker
from app.agent.runtime.plan_graph import (
    _aggregate_for,
    _expand_for,
    build_plan_graph,
    stream_plan_graph,
)
from app.agent.runtime.plan_model import Plan, PlanStep, StepStatus
from app.agent.runtime.planner import LLMPlanner
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
    make_copilot_defaults,
    make_gateway,
    make_runtime_config,
)


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


class _FakeChatRepository:
    async def add_conversation(self, conversation: ChatConversation) -> ChatConversation:
        conversation.id = conversation.id or uuid.uuid4()
        return conversation

    async def get_conversation(self, conversation_id: uuid.UUID) -> ChatConversation | None:
        return None

    async def list_conversations(
        self, kb_id: uuid.UUID | None, kind: str | None, *, limit: int, offset: int
    ) -> tuple[list[ChatConversation], int]:
        return [], 0

    async def delete_conversation(self, conversation: ChatConversation) -> None:
        return None

    async def add_message(self, message: ChatMessage) -> ChatMessage:
        message.id = message.id or uuid.uuid4()
        message.created_at = datetime.now(UTC)
        return message

    async def list_messages(self, conversation_id: uuid.UUID) -> list[ChatMessage]:
        return []


def make_plan_runtime(tmp_path: Path, plan_json: str, answer: str) -> CopilotRuntime:
    """构造带脚本化 planner（LLMPlanner + ScriptedLLM 生成固定 plan）的 runtime。"""
    kb_repo = FakeKnowledgeBaseRepository()
    kb_service = KnowledgeBaseService(kb_repo)
    note_service = NoteService(FakeNoteRepository(), kb_repo)
    document_service = DocumentService(FakeDocumentRepository(), kb_repo, FakeFileStore())
    gateway = make_gateway(
        embedder=FakeEmbeddingClient(dimension=8),
        reranker=FakeRerankerClient(),
        llm=ScriptedLLM([plan_json]),
    )
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
    defaults = make_copilot_defaults(note_service, memory_service)
    defaults["planner"] = LLMPlanner(gateway)
    return build_runtime(
        model=FakeMessagesListChatModel(responses=[AIMessage(content=answer)]),
        gateway=gateway,
        rag_retriever=retriever,
        kb_service=kb_service,
        note_service=note_service,
        document_service=document_service,
        web_search=FakeWebSearchClient(),
        memory_service=memory_service,
        memory_store=FileMemoryStore(tmp_path),
        skill_store=FileSkillStore(tmp_path / "skills"),
        chat_repository=_FakeChatRepository(),
        event_repository=FakeCopilotEventRepository(),
        reviewer=FakeOutputReviewer(),
        runtime=make_runtime_config(),
        **defaults,
    )


def _initial_state(task: str) -> dict[str, Any]:
    return {
        "task": task,
        "system_prompt": "",
        "memory_block": "",
        "skills_block": "",
        "plan": {},
        "results": {},
        "failures": {},
        "trace": [],
        "completed_steps": [],
        "final_answer": "",
        "plan_error": "",
        "step_id": "",
        "last_error": None,
    }


async def test_plan_graph_parallel_steps(tmp_path: Path) -> None:
    """无依赖步骤应被 Send 扇出并行执行，results 收集全部产物。"""
    plan = {
        "steps": [
            {"id": "1", "action": "list_notes", "params": {}},
            {"id": "2", "action": "list_notes", "params": {}},
            {"id": "3", "action": "list_notes", "params": {}},
            {
                "id": "4",
                "action": "finalize_answer",
                "params": {},
                "depends_on": ["1", "2", "3"],
                "terminal": True,
            },
        ]
    }
    rt = make_plan_runtime(tmp_path, json.dumps(plan), "三份笔记都列好了。")
    graph = build_plan_graph(rt, make_tracker(rt))
    run_id = uuid.uuid4()
    config = make_config(rt, run_id, uuid.uuid4(), "列出所有笔记")
    final = await graph.ainvoke(_initial_state("列出所有笔记"), config=config)
    assert set(final["results"].keys()) == {"1", "2", "3", "4"}
    assert final["final_answer"] == "三份笔记都列好了。"


async def test_plan_graph_replans_on_failure(tmp_path: Path) -> None:
    """失败步骤触发 replan；replan 拿不出替换步骤时 plan_error 落定、合成输出失败。"""
    plan = {"steps": [{"id": "1", "action": "read_note", "params": {"id": "missing"}}]}
    rt = make_plan_runtime(tmp_path, json.dumps(plan), "读不到。")
    graph = build_plan_graph(rt, make_tracker(rt))
    run_id = uuid.uuid4()
    config = make_config(rt, run_id, uuid.uuid4(), "读一篇笔记")
    final = await graph.ainvoke(_initial_state("读一篇笔记"), config=config)
    # read_note 读不到 → 失败 → replan 返回空（ScriptedLLM 用尽）→ plan_error 落定
    assert final["plan_error"]
    assert "失败" in final["final_answer"]


async def test_stream_plan_graph_streams_events(tmp_path: Path) -> None:
    """回归：stream_plan_graph 必须能走完 astream 并产出 step/done 事件。

    之前 ``graph.astream(stream_mode="updates")`` 传的是单个字符串，LangGraph 在单 mode 下
    只 yield payload（dict）而非 ``(mode, payload)`` 元组，``_mode, payload`` 解包报
    ``not enough values to unpack (expected 2, got 1)``。改为列表后应正常流式产出。
    """
    plan = {
        "steps": [
            {"id": "1", "action": "list_notes", "params": {}},
            {
                "id": "2",
                "action": "finalize_answer",
                "params": {},
                "depends_on": ["1"],
                "terminal": True,
            },
        ]
    }
    rt = make_plan_runtime(tmp_path, json.dumps(plan), "列好了。")
    tracker = make_tracker(rt)
    graph = build_plan_graph(rt, tracker)
    run_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    assistant_message_id = uuid.uuid4()
    config = make_config(rt, run_id, conversation_id, "列出所有笔记")
    events = [
        e
        async for e in stream_plan_graph(
            rt,
            graph,
            config,
            run_id,
            _initial_state("列出所有笔记"),
            conversation_id,
            assistant_message_id,
            tracker,
        )
    ]
    assert any(isinstance(e, CopilotStepEvent) for e in events)
    assert any(isinstance(e, CopilotDoneEvent) for e in events)


def _plan(*steps: PlanStep) -> Plan:
    return Plan(steps=list(steps))


def _completed(plan: Plan, step_id: str, output_ref: Any) -> None:
    step = plan.get_step(step_id)
    assert step is not None
    step.status = StepStatus.COMPLETED
    step.output_ref = output_ref


def _step(plan: Plan, step_id: str) -> PlanStep:
    step = plan.get_step(step_id)
    assert step is not None
    return step


def test_resolve_params_field() -> None:
    """参数链：{{step_id.output.field}} 取上游产物的单个顶层字段。"""
    plan = _plan(
        PlanStep(step_id="s1", action="list_notes"),
        PlanStep(
            step_id="s2", action="read_note", params={"note_id": "{{s1.output.id}}"},
            depends_on=("s1",),
        ),
    )
    _completed(plan, "s1", {"id": "note-1", "title": "标题"})
    assert plan.resolve_params(_step(plan, "s2")) == {"note_id": "note-1"}


def test_resolve_params_whole_output() -> None:
    """{{step_id}} / {{step_id.output}} 引用整个产物，保持原始类型（list/dict）。"""
    plan = _plan(
        PlanStep(step_id="s1", action="list_documents"),
        PlanStep(
            step_id="s2", action="for", params={"items": "{{s1.output}}"},
            depends_on=("s1",),
        ),
    )
    _completed(plan, "s1", {"summary": "…", "items": [{"id": "a"}, {"id": "b"}]})
    assert plan.resolve_params(_step(plan, "s2")) == {
        "items": {"summary": "…", "items": [{"id": "a"}, {"id": "b"}]}
    }


def test_resolve_params_invalid_template() -> None:
    """非法模板（数组索引）fail-closed 抛 ValueError。"""
    plan = _plan(
        PlanStep(step_id="s1", action="list_notes"),
        PlanStep(
            step_id="s2", action="read_note", params={"id": "{{s1.output[0]}}"},
            depends_on=("s1",),
        ),
    )
    _completed(plan, "s1", {"output": [1]})
    with pytest.raises(ValueError):
        plan.resolve_params(_step(plan, "s2"))


def test_resolve_params_uncompleted_dep() -> None:
    """引用未完成的步骤抛 ValueError。"""
    plan = _plan(
        PlanStep(step_id="s1", action="list_notes"),
        PlanStep(
            step_id="s2", action="read_note", params={"id": "{{s1.output.id}}"},
            depends_on=("s1",),
        ),
    )
    with pytest.raises(ValueError):
        plan.resolve_params(_step(plan, "s2"))


def test_expand_for() -> None:
    """for 展开：解析 items 生成 N 个虚拟步骤，item 经 item_params 映射注入 body 参数。"""
    plan = _plan(
        PlanStep(step_id="s1", action="list_documents"),
        PlanStep(
            step_id="loop",
            action="for",
            params={
                "items": "{{s1.output.items}}",
                "body": "read_document",
                "item_params": {"id": "document_id"},
                "extra_params": {"full_text": True},
            },
            depends_on=("s1",),
        ),
    )
    _completed(
        plan,
        "s1",
        {"summary": "…", "items": [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]},
    )
    _expand_for(plan, _step(plan, "loop"))
    loop = _step(plan, "loop")
    assert loop.status is StepStatus.RUNNING
    children = [s for s in plan.steps if s.step_id.startswith("loop@")]
    assert [c.step_id for c in children] == ["loop@0", "loop@1"]
    assert children[0].action == "read_document"
    assert children[0].params == {"document_id": "a", "full_text": True}
    assert children[1].params == {"document_id": "b", "full_text": True}


def test_aggregate_for() -> None:
    """for 聚合：所有虚拟步骤完成后聚合成功结果，失败项被过滤。"""
    plan = _plan(
        PlanStep(
            step_id="loop", action="for",
            params={"items": [], "body": "read_document", "item_params": {}},
        )
    )
    plan.add_step(PlanStep(step_id="loop@0", action="read_document", params={}))
    plan.add_step(PlanStep(step_id="loop@1", action="read_document", params={}))
    _step(plan, "loop").status = StepStatus.RUNNING
    c0 = _step(plan, "loop@0")
    c0.status = StepStatus.COMPLETED
    c0.output_ref = {"id": "a"}
    c1 = _step(plan, "loop@1")
    c1.status = StepStatus.FAILED
    _aggregate_for(plan, _step(plan, "loop"))
    loop = _step(plan, "loop")
    assert loop.status is StepStatus.COMPLETED
    assert loop.output_ref == [{"id": "a"}]
    assert all(s.status is StepStatus.OBSOLETE for s in plan.steps if s.step_id.startswith("loop@"))
