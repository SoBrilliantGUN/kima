"""FastAPI 依赖注入：模块 6（Copilot Agent 运行时）。

核心/模块 1–5 的依赖在 ``deps_core.py``；本模块只放 Copilot 相关的仓库 / 服务 / 网关 /
规划器 / 审查器等创建函数，依赖 ``deps_core`` 的少量 getter（单向依赖，无环）。
"""

import asyncio
from typing import Annotated, cast

from fastapi import Depends, Request
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.approval import ApprovalPolicy
from app.agent.compose import CopilotRuntime, build_runtime
from app.agent.gateway import LLMGateway
from app.agent.guardrail.injection import DEFAULT_INJECTION_POLICY
from app.agent.guardrail.review import LLMOutputReviewer
from app.agent.memory_classifier import LLMMemoryClassifier
from app.agent.resilience.security_breaker import SecurityBreaker
from app.agent.runtime.budget import HardBudget
from app.agent.runtime.config import RuntimeConfig
from app.agent.runtime.context import ContextConfig
from app.agent.runtime.loop_guard import LoopGuard
from app.agent.runtime.planner import LLMPlanner
from app.agent.side_effect import DbSideEffectVerifier
from app.api.deps_core import (
    get_chat_repository,
    get_document_service,
    get_kb_service,
    get_llm_gateway,
    get_note_service,
    get_settings_dep,
    get_web_search_client_dep,
)
from app.core.config import Settings
from app.core.db import async_session_factory, get_db_session
from app.core.memory_store import FileMemoryStore, MemoryFileStore
from app.core.skill_store import FileSkillStore, SkillFileStore
from app.integrations.agent_llm import get_agent_model
from app.integrations.search import WebSearchClient
from app.integrations.tracing import get_langfuse_handler
from app.rag.repository import SqlAlchemyRetrievalRepository
from app.rag.retriever import RagRetriever
from app.repositories.approval import ApprovalStore, SqlAlchemyApprovalStore
from app.repositories.chat import ChatRepository
from app.repositories.copilot import (
    CopilotEventRepository,
    CopilotMemoryRepository,
    SqlAlchemyCopilotEventRepository,
    SqlAlchemyCopilotMemoryRepository,
)
from app.repositories.idempotency import IdempotencyStore, SqlAlchemyIdempotencyStore
from app.services.conflict import LLMConflictJudge
from app.services.copilot import CopilotMemoryService
from app.services.document import DocumentService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.note import NoteService


def get_copilot_memory_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CopilotMemoryRepository:
    """用当前数据库会话构造 Copilot 记忆仓库的 SQLAlchemy 实现。"""
    return SqlAlchemyCopilotMemoryRepository(session)


def get_copilot_event_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CopilotEventRepository:
    """用当前数据库会话构造 Copilot 事件日志仓库的 SQLAlchemy 实现。"""
    return SqlAlchemyCopilotEventRepository(session)


def get_approval_store() -> ApprovalStore:
    """HITL 审批单仓库（独立会话工厂，跨请求持久，支撑「找回挂起审批」+ 超时 fail-close）。"""
    return SqlAlchemyApprovalStore(async_session_factory)


def get_idempotency_store(
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> IdempotencyStore:
    """写工具幂等去重仓库（独立会话工厂，跨请求持久，支撑崩溃续跑命中缓存）。"""
    return SqlAlchemyIdempotencyStore(
        async_session_factory, ttl_seconds=settings.copilot_idempotency_ttl_seconds
    )


def _approval_policy_for(settings: Settings) -> ApprovalPolicy:
    """把审批模式翻译成策略（审批闸恒开，无「关闭」选项）。

    graded → 仅 HIGH 审批；strict → 全写审批。
    """
    if settings.copilot_approval_mode == "strict":
        return ApprovalPolicy.strict()
    return ApprovalPolicy.graded()


def get_memory_file_store() -> MemoryFileStore:
    """Soul/User 记忆文件存储（本地磁盘实现，默认 data/memory）。"""
    return FileMemoryStore()


def get_skill_file_store() -> SkillFileStore:
    """自定义 Skill 文件存储（本地磁盘实现，默认 data/skills，只读）。"""
    return FileSkillStore()


def get_security_breaker(request: Request) -> SecurityBreaker:
    """安全熔断器（app 级单例，lifespan 恒装配，见 main.py；恒在场，缺失即报错）。"""
    breaker = getattr(request.app.state, "copilot_security_breaker", None)
    if breaker is None:
        raise RuntimeError("安全熔断器未装配（copilot_security_breaker 必须在场）")
    return cast(SecurityBreaker, breaker)


def get_conflict_judge(
    request: Request,
    gateway: Annotated[LLMGateway, Depends(get_llm_gateway)],
) -> LLMConflictJudge:
    """记忆冲突判定器（经网关统一门禁/记账/快照）。"""
    return LLMConflictJudge(gateway)


def get_output_reviewer(
    request: Request,
    gateway: Annotated[LLMGateway, Depends(get_llm_gateway)],
) -> LLMOutputReviewer:
    """输出审查器：对账「最终回答 vs 工具轨迹」，抓虚假完成（经网关统一门禁/记账/快照）。"""
    return LLMOutputReviewer(gateway)


def get_planner(
    gateway: Annotated[LLMGateway, Depends(get_llm_gateway)],
) -> LLMPlanner:
    """任务规划器：生成步骤 DAG + 失败 replan（长程多步任务，经网关统一门禁/记账/快照）。"""
    return LLMPlanner(gateway)


def get_copilot_memory_service(
    request: Request,
    repository: Annotated[CopilotMemoryRepository, Depends(get_copilot_memory_repository)],
    judge: Annotated[LLMConflictJudge, Depends(get_conflict_judge)],
    gateway: Annotated[LLMGateway, Depends(get_llm_gateway)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> CopilotMemoryService:
    """构造 Copilot 记忆服务，注入仓库、冲突判定、分类器与记忆参数。

    分类器**恒在场**（不可关闭）——约束类记忆不能靠 Agent 随手挑 kind，必须在落库前
    确定性分类兜底（防线一）。分类器/冲突判定/写·召回 embedding 共用同一网关
    （统一门禁/记账/快照，无裸 embedder）。
    """
    classifier = LLMMemoryClassifier(gateway)
    return CopilotMemoryService(
        repository=repository,
        gateway=gateway,
        judge=judge,
        classifier=classifier,
        episodic_ttl_days=settings.memory_episodic_ttl_days,
        recency_window_days=settings.memory_recency_window_days,
        conflict_top_k=settings.memory_conflict_top_k,
        superseded_window_days=settings.memory_superseded_window_days,
        revival_similarity=settings.memory_revival_similarity,
    )


def get_agent_model_dep(
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> BaseChatModel:
    """Agent 用 LangChain ChatModel（deepseek → ChatDeepSeek；fake → 脚本化假模型）。"""
    return get_agent_model(settings)


def get_langfuse_handler_dep(
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> BaseCallbackHandler:
    """LangFuse callback（必须在场）；未配置 LangFuse 直接报错（可观测不可静默关闭）。"""
    handler = get_langfuse_handler(settings)
    if handler is None:
        raise RuntimeError(
            "LangFuse 未配置（可观测必须在场）：请设 langfuse_provider=cloud 及对应 key"
        )
    return handler


def get_copilot_rag_retriever(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    gateway: Annotated[LLMGateway, Depends(get_llm_gateway)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> RagRetriever:
    """Copilot 检索工具用的混合检索器（复用模块 5 检索层，嵌入/精排经网关）。"""
    return RagRetriever(
        repository=SqlAlchemyRetrievalRepository(session),
        gateway=gateway,
        rerank_min_score=settings.rerank_min_score,
    )


def get_copilot_runtime(
    request: Request,
    model: Annotated[BaseChatModel, Depends(get_agent_model_dep)],
    tracer: Annotated[BaseCallbackHandler, Depends(get_langfuse_handler_dep)],
    rag_retriever: Annotated[RagRetriever, Depends(get_copilot_rag_retriever)],
    kb_service: Annotated[KnowledgeBaseService, Depends(get_kb_service)],
    note_service: Annotated[NoteService, Depends(get_note_service)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
    web_search: Annotated[WebSearchClient, Depends(get_web_search_client_dep)],
    memory_service: Annotated[CopilotMemoryService, Depends(get_copilot_memory_service)],
    memory_store: Annotated[MemoryFileStore, Depends(get_memory_file_store)],
    skill_store: Annotated[SkillFileStore, Depends(get_skill_file_store)],
    chat_repository: Annotated[ChatRepository, Depends(get_chat_repository)],
    event_repository: Annotated[CopilotEventRepository, Depends(get_copilot_event_repository)],
    reviewer: Annotated[LLMOutputReviewer, Depends(get_output_reviewer)],
    planner: Annotated[LLMPlanner, Depends(get_planner)],
    gateway: Annotated[LLMGateway, Depends(get_llm_gateway)],
    approval_store: Annotated[ApprovalStore, Depends(get_approval_store)],
    idempotency_store: Annotated[IdempotencyStore, Depends(get_idempotency_store)],
    security_breaker: Annotated[SecurityBreaker, Depends(get_security_breaker)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> CopilotRuntime:
    """装配 Copilot 运行时（每请求一实例，工具闭包捕获请求作用域服务）。"""
    # 串行化共享 AsyncSession 的访问（工具并发执行 / 事件落库 / 副作用回查共用一把锁）。
    db_lock = asyncio.Lock()
    verifier = DbSideEffectVerifier(note_service, memory_service, db_lock)
    return build_runtime(
        model=model,
        gateway=gateway,
        checkpointer=request.app.state.copilot_checkpointer,
        tracer=tracer,
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
        runtime=RuntimeConfig(
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
            daily_budget=request.app.state.copilot_daily_budget,
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
        ),
        planner=planner,
        breaker=request.app.state.copilot_breaker,
        security_breaker=security_breaker,
        verifier=verifier,
        approval_store=approval_store,
        idempotency_store=idempotency_store,
        db_lock=db_lock,
    )
