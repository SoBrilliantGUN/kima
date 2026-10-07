"""FastAPI 依赖注入：模块 6（Copilot Agent 运行时）。

核心/模块 1–5 的依赖在 ``deps_core.py``；本模块只放 Copilot 相关的仓库 / 服务 / 网关 /
规划器 / 审查器等创建函数，依赖 ``deps_core`` 的少量 getter（单向依赖，无环）。

运行时装配细节已收敛到 ``runtime/factory.build_copilot_runtime``（DI 与后台任务共用），
本模块的 ``get_copilot_runtime`` 只从 DI 取原料喂给工厂。
"""

from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.compose import CopilotRuntime
from app.agent.gateway import LLMGateway
from app.agent.memory_classifier import LLMMemoryClassifier
from app.agent.resilience.security_breaker import SecurityBreaker
from app.agent.runtime.factory import build_copilot_runtime
from app.api.deps_core import get_llm_gateway, get_settings_dep
from app.core.config import Settings
from app.core.db import get_db_session
from app.core.memory_store import FileMemoryStore, MemoryFileStore
from app.core.skill_store import FileSkillStore, SkillFileStore
from app.repositories.copilot import (
    CopilotMemoryRepository,
    CopilotStreamEventRepository,
    SqlAlchemyCopilotMemoryRepository,
    SqlAlchemyCopilotStreamEventRepository,
)
from app.services.conflict import LLMConflictJudge
from app.services.copilot import CopilotMemoryService


def get_copilot_memory_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CopilotMemoryRepository:
    """用当前数据库会话构造 Copilot 记忆仓库的 SQLAlchemy 实现。"""
    return SqlAlchemyCopilotMemoryRepository(session)


def get_copilot_stream_event_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CopilotStreamEventRepository:
    """用当前数据库会话构造 SSE 前端事件流仓库（回放端点的只读查询用）。"""
    return SqlAlchemyCopilotStreamEventRepository(session)


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


def get_copilot_runtime(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    gateway: Annotated[LLMGateway, Depends(get_llm_gateway)],
    security_breaker: Annotated[SecurityBreaker, Depends(get_security_breaker)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> CopilotRuntime:
    """装配 Copilot 运行时（每请求一实例，工具闭包捕获请求作用域服务）。

    装配细节收敛在 ``runtime/factory.build_copilot_runtime``：本函数只从 DI 里取
    session + app.state 单例 + settings 喂给它，与后台任务（RunManager）共用同一工厂。
    """
    return build_copilot_runtime(
        session=session,
        gateway=gateway,
        checkpointer=request.app.state.copilot_checkpointer,
        breaker=request.app.state.copilot_breaker,
        security_breaker=security_breaker,
        daily_budget=request.app.state.copilot_daily_budget,
        settings=settings,
    )
