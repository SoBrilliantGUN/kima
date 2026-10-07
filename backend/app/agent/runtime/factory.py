"""Copilot 运行时工厂：把「请求作用域 DI 装配」与「后台任务装配」收敛到同一处。

`build_copilot_runtime` 只吃三类原料——一个 ``AsyncSession``（所有请求作用域仓库/服务共用）、
``app.state`` 单例（网关/checkpointer/熔断/安全闸/日预算）与 ``settings``——产出成品
``CopilotRuntime``。FastAPI 路径（``deps_copilot.get_copilot_runtime``）与解耦后的后台任务
（``RunManager``）都调它，唯一差别是 session 来源：前者是 `Depends(get_db_session)` 的
请求级会话，后者是 `async_session_factory()` 现开、run 结束后关闭的会话。
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.approval import ApprovalPolicy
from app.agent.compose import CopilotRuntime, build_runtime
from app.agent.gateway import LLMGateway
from app.agent.guardrail.injection import DEFAULT_INJECTION_POLICY
from app.agent.guardrail.review import LLMOutputReviewer
from app.agent.memory_classifier import LLMMemoryClassifier
from app.agent.resilience.circuit_breaker import CircuitBreaker
from app.agent.resilience.security_breaker import SecurityBreaker
from app.agent.runtime.budget import DailyBudget, HardBudget
from app.agent.runtime.config import RuntimeConfig
from app.agent.runtime.context import ContextConfig
from app.agent.runtime.loop_guard import LoopGuard
from app.agent.runtime.planner import LLMPlanner
from app.agent.side_effect import DbSideEffectVerifier
from app.core.config import Settings
from app.core.db import async_session_factory
from app.core.memory_store import FileMemoryStore
from app.core.skill_store import FileSkillStore
from app.core.storage import LocalFileStore
from app.integrations import get_web_search_client
from app.integrations.agent_llm import get_agent_model
from app.integrations.search import WebSearchClient
from app.rag.repository import SqlAlchemyRetrievalRepository
from app.rag.retriever import RagRetriever
from app.repositories.approval import SqlAlchemyApprovalStore
from app.repositories.chat import SqlAlchemyChatRepository
from app.repositories.copilot import (
    SqlAlchemyCopilotEventRepository,
    SqlAlchemyCopilotMemoryRepository,
)
from app.repositories.document import SqlAlchemyDocumentRepository
from app.repositories.idempotency import SqlAlchemyIdempotencyStore
from app.repositories.knowledge_base import SqlAlchemyKnowledgeBaseRepository
from app.repositories.note import SqlAlchemyNoteRepository
from app.services.conflict import LLMConflictJudge
from app.services.copilot import CopilotMemoryService
from app.services.document import DocumentService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.note import NoteService


def _approval_policy_for(settings: Settings) -> ApprovalPolicy:
    """把审批模式翻译成策略（审批闸恒开，无「关闭」选项）。"""
    if settings.copilot_approval_mode == "strict":
        return ApprovalPolicy.strict()
    return ApprovalPolicy.graded()


def build_copilot_runtime(
    *,
    session: AsyncSession,
    gateway: LLMGateway,
    checkpointer: Any,
    breaker: CircuitBreaker,
    security_breaker: SecurityBreaker,
    daily_budget: DailyBudget,
    settings: Settings,
) -> CopilotRuntime:
    """从 session + app.state 单例 + settings 装配一个 `CopilotRuntime`。

    DI 路径与后台任务共用本函数；``session`` 是「请求作用域仓库/服务」共用的那一把，
    串行化仍由 ``build_runtime`` 里的 ``db_lock`` 兜底。
    """
    model = get_agent_model(settings)
    kb_repo = SqlAlchemyKnowledgeBaseRepository(session)
    kb_service = KnowledgeBaseService(kb_repo)
    note_service = NoteService(SqlAlchemyNoteRepository(session), kb_repo)
    document_service = DocumentService(
        SqlAlchemyDocumentRepository(session), kb_repo, LocalFileStore()
    )
    web_search: WebSearchClient = get_web_search_client(settings)
    rag_retriever = RagRetriever(
        repository=SqlAlchemyRetrievalRepository(session),
        gateway=gateway,
        rerank_min_score=settings.rerank_min_score,
    )
    memory_service = CopilotMemoryService(
        repository=SqlAlchemyCopilotMemoryRepository(session),
        gateway=gateway,
        judge=LLMConflictJudge(gateway),
        classifier=LLMMemoryClassifier(gateway),
        episodic_ttl_days=settings.memory_episodic_ttl_days,
        recency_window_days=settings.memory_recency_window_days,
        conflict_top_k=settings.memory_conflict_top_k,
        superseded_window_days=settings.memory_superseded_window_days,
        revival_similarity=settings.memory_revival_similarity,
    )
    memory_store = FileMemoryStore()
    skill_store = FileSkillStore()
    chat_repository = SqlAlchemyChatRepository(session)
    event_repository = SqlAlchemyCopilotEventRepository(session)
    reviewer = LLMOutputReviewer(gateway)
    planner = LLMPlanner(gateway)
    approval_store = SqlAlchemyApprovalStore(async_session_factory)
    idempotency_store = SqlAlchemyIdempotencyStore(
        async_session_factory, ttl_seconds=settings.copilot_idempotency_ttl_seconds
    )
    db_lock = asyncio.Lock()
    verifier = DbSideEffectVerifier(note_service, memory_service, kb_service, db_lock)
    runtime = RuntimeConfig(
        budget=HardBudget(
            max_turns=settings.copilot_budget_max_turns,
            max_seconds=settings.copilot_budget_max_seconds,
            max_tokens=settings.copilot_budget_max_tokens,
            max_cost_cny=settings.copilot_budget_max_cost_cny,
            max_tool_calls=settings.copilot_budget_max_tool_calls,
        ),
        loop_guard=LoopGuard(
            repeat_threshold=settings.copilot_loop_repeat_threshold,
            window_size=settings.copilot_loop_window_size,
            stall_threshold=settings.copilot_loop_stall_threshold,
        ),
        injection_policy=DEFAULT_INJECTION_POLICY,
        daily_budget=daily_budget,
        approval_policy=_approval_policy_for(settings),
        approval_timeout_seconds=settings.copilot_approval_timeout_seconds,
        max_result_chars=settings.copilot_max_result_chars,
        review_max_attempts=settings.copilot_review_max_attempts,
        cache_hit_rate_warn=settings.copilot_cache_hit_rate_warn,
        context=ContextConfig(
            max_tokens=settings.copilot_context_max_tokens,
            l0_ratio=settings.copilot_context_l0_ratio,
            l1_ratio=settings.copilot_context_l1_ratio,
            l2_ratio=settings.copilot_context_l2_ratio,
            skills_budget=settings.copilot_context_skills_budget,
            safety_margin=settings.copilot_context_safety_margin,
        ),
    )
    return build_runtime(
        model=model,
        gateway=gateway,
        checkpointer=checkpointer,
        rag_retriever=rag_retriever,
        kb_service=kb_service,
        note_service=note_service,
        document_service=document_service,
        web_search=web_search,
        memory_service=memory_service,
        memory_store=memory_store,
        skill_store=skill_store,
        chat_repository=chat_repository,
        event_repository=event_repository,
        reviewer=reviewer,
        runtime=runtime,
        planner=planner,
        breaker=breaker,
        security_breaker=security_breaker,
        verifier=verifier,
        approval_store=approval_store,
        idempotency_store=idempotency_store,
        db_lock=db_lock,
    )
