import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app import __version__
from app.agent.gateway import GatewayConfig, LLMGateway
from app.agent.pricing import PricingService
from app.agent.resilience.circuit_breaker import CircuitBreaker
from app.agent.resilience.retry import RetryPolicy
from app.agent.resilience.security_breaker import SecurityBreaker
from app.agent.runtime.budget import DailyBudget
from app.api.routes import api_router, health_router
from app.core.config import Settings, get_settings
from app.core.db import async_session_factory
from app.core.exceptions import DomainError
from app.core.logging import RequestIdMiddleware, setup_logging
from app.core.memory_store import FileMemoryStore
from app.core.middleware import UploadSizeLimitMiddleware
from app.core.storage import LocalFileStore
from app.integrations import (
    get_document_parser,
    get_embedding_client,
    get_llm_client,
    get_reranker_client,
)
from app.integrations.web import start_browser, stop_browser
from app.repositories.breaker import SqlAlchemyBreakerStore
from app.repositories.copilot import SqlAlchemyCopilotMemoryRepository
from app.repositories.daily_budget import SqlAlchemyDailyBudgetRepository
from app.repositories.document import SqlAlchemyDocumentRepository
from app.repositories.knowledge_base import SqlAlchemyKnowledgeBaseRepository
from app.repositories.llm_cost import SqlAlchemyCostStore
from app.repositories.llm_snapshot import SqlAlchemySnapshotStore
from app.repositories.note_chunk import SqlAlchemyNoteChunkRepository
from app.repositories.pricing import SqlAlchemyPricingRepository
from app.services.document import MAX_FILE_SIZE
from app.services.ingest import IngestService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.note_vectorize import NoteVectorizeService
from app.workers.document_worker import DocumentWorker
from app.workers.note_vectorize_worker import NoteVectorizeWorker

logger = logging.getLogger(__name__)


async def seed_default_knowledge_base() -> None:
    """应用启动时幂等预置默认知识库「我的知识库」。"""
    async with async_session_factory() as session:
        service = KnowledgeBaseService(SqlAlchemyKnowledgeBaseRepository(session))
        await service.ensure_default()


def _pricing_targets(settings: Settings) -> list[tuple[str, str]]:
    """非 fake 的 (vendor, model) 三元组（llm/embedding/rerank），供启动计费校验。"""
    targets: list[tuple[str, str]] = []
    if settings.llm_provider not in ("", "fake"):
        targets.append((settings.llm_provider, settings.llm_model))
    if settings.embedding_provider not in ("", "fake"):
        targets.append((settings.embedding_provider, settings.embedding_model))
    if settings.rerank_provider not in ("", "fake"):
        targets.append((settings.rerank_provider, settings.rerank_model))
    return targets


def build_document_worker(
    gateway: LLMGateway, daily_budget: DailyBudget | None = None
) -> DocumentWorker:
    """用真实依赖构造文档处理 worker（每个操作独立 session）。"""
    settings = get_settings()
    parser = get_document_parser(settings)
    file_store = LocalFileStore()

    return DocumentWorker(
        repo_factory=lambda: SqlAlchemyDocumentRepository(async_session_factory()),
        ingest_factory=lambda: IngestService(
            repository=SqlAlchemyDocumentRepository(async_session_factory()),
            parser=parser,
            file_store=file_store,
            gateway=gateway,
            daily_budget=daily_budget,
            warn_tokens=settings.copilot_document_warn_tokens,
        ),
    )


def build_note_vectorize_worker(
    gateway: LLMGateway, daily_budget: DailyBudget | None = None
) -> NoteVectorizeWorker:
    """用真实依赖构造笔记向量化 worker（每个操作独立 session）。"""
    settings = get_settings()
    return NoteVectorizeWorker(
        repo_factory=lambda: SqlAlchemyNoteChunkRepository(async_session_factory()),
        service_factory=lambda: NoteVectorizeService(
            repository=SqlAlchemyNoteChunkRepository(async_session_factory()),
            gateway=gateway,
            daily_budget=daily_budget,
        ),
        idle_seconds=settings.note_revectorize_idle_seconds,
    )


async def _run_snapshot_cleanup(
    store: SqlAlchemySnapshotStore, ttl_days: int, interval_seconds: int = 3600
) -> None:
    """周期清理过期 LLM 调用快照（防表无限膨胀）；清理失败不阻断，下轮再试。"""
    while True:
        try:
            cutoff = datetime.now(UTC) - timedelta(days=ttl_days)
            deleted = await store.delete_older_than(cutoff)
            if deleted:
                logger.info("清理过期 LLM 快照 %d 条", deleted)
        except Exception as exc:  # noqa: BLE001 - 清理是 best-effort
            logger.warning("LLM 快照清理失败：%s", exc)
        await asyncio.sleep(interval_seconds)


async def _run_superseded_cleanup(window_days: int, interval_seconds: int = 3600) -> None:
    """周期硬删除软删除窗口过期的 superseded 记忆（窗口结束后真删、找不回，防表无限膨胀）。"""
    while True:
        try:
            cutoff = datetime.now(UTC) - timedelta(days=window_days)
            async with async_session_factory() as session:
                repository = SqlAlchemyCopilotMemoryRepository(session)
                deleted = await repository.delete_superseded_older_than(cutoff)
            if deleted:
                logger.info("清理过期 superseded 记忆 %d 条", deleted)
        except Exception as exc:  # noqa: BLE001 - 清理是 best-effort
            logger.warning("superseded 记忆清理失败：%s", exc)
        await asyncio.sleep(interval_seconds)


async def _setup_copilot_checkpointer(
    settings: Settings,
) -> tuple[Any, Any]:
    """生产接 `AsyncPostgresSaver`（checkpoint 落库）；失败 fail-fast 拒绝启动。

    HITL 写工具确认（interrupt/resume）依赖 checkpointer，且内存版降级会静默丢失
    checkpoint（重启后挂起审批/续跑全丢），故不再降级——Postgres 连不上直接抛异常，
    逼运维把持久化修好，而不是悄悄退化成「重启即失忆」。
    """
    pg_url = settings.database_url.replace("postgresql+asyncpg", "postgresql")
    cm = AsyncPostgresSaver.from_conn_string(pg_url)
    saver = await cm.__aenter__()
    await saver.setup()
    return cm, saver


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await seed_default_knowledge_base()
    await start_browser()
    settings = get_settings()
    await FileMemoryStore().ensure()
    checkpointer_cm, checkpointer = await _setup_copilot_checkpointer(settings)
    app.state.copilot_checkpointer = checkpointer
    # 工具熔断：失败计数外置到 DB（copilot_breakers），崩溃重启后续读、不归零（动作指纹持久化）。
    breaker = CircuitBreaker(store=SqlAlchemyBreakerStore(async_session_factory))
    try:
        await breaker.load_all()
    except Exception as exc:  # noqa: BLE001 - 续读失败不影响启动，从空计
        logger.warning("Copilot 熔断状态续读失败（本次从空计）：%s", exc)
    app.state.copilot_breaker = breaker
    # 安全熔断（防线④）：app 级单例，手动恢复（无自动 HALF_OPEN），区别于上面的基础设施熔断。
    # 恒在场（非 None）；「关闭」用 enabled 字段表达（未启用时在场但不计数），与运行时
    # 「安全闸恒在场」约定一致。
    app.state.copilot_security_breaker = SecurityBreaker(
        threshold=settings.copilot_security_breaker_threshold,
        enabled=settings.copilot_security_breaker_enabled,
    )
    daily_budget = DailyBudget(
        max_cost_cny=settings.copilot_daily_max_cost_cny,
        max_tokens=settings.copilot_daily_max_tokens,
        store=SqlAlchemyDailyBudgetRepository(async_session_factory),
    )
    try:
        await daily_budget.load()
    except Exception as exc:
        logger.warning("Copilot 日预算续读失败（本次从 0 计）：%s", exc)
    app.state.copilot_daily_budget = daily_budget
    # 实时计费（docs/pricing.md）：价格时变主数据 + 调用级成本审计。记账/审计恒启用（不可关）。
    # 启动校验「未来 3 天连续覆盖」失败则拒绝启动（fail-fast，超出覆盖窗口报错暂停服务）；
    # 全 fake（无真实提供方）时 _pricing_targets 为空，校验空转：fake 厂商按 ¥0 记账、不落成本明细。
    pricing = PricingService(
        SqlAlchemyPricingRepository(async_session_factory),
        cache_ttl_seconds=settings.copilot_pricing_cache_ttl_seconds,
    )
    if settings.copilot_pricing_check_enabled:
        try:
            await pricing.validate_startup(datetime.now(UTC), _pricing_targets(settings))
        except Exception as exc:
            logger.critical("Copilot 定价启动校验失败，拒绝启动：%s", exc)
            raise
    cost_store = SqlAlchemyCostStore(async_session_factory)
    # LLM 网关：所有 LLM 出口的统一门禁/记账/快照（决策 D1/D6/D7，见 docs/llm-gateway.md）。
    # 复用上面的 daily_budget / breaker 作跨 run 日预算与 LLM 熔断。快照恒落（不可关）。
    snapshot_store = SqlAlchemySnapshotStore(async_session_factory)
    app.state.copilot_gateway = LLMGateway(
        config=GatewayConfig(
            timeout=settings.copilot_llm_gateway_timeout_seconds,
            soft_threshold=settings.copilot_llm_gateway_soft_threshold,
            dlp_redact=settings.copilot_llm_dlp_redact,
            llm_vendor=settings.llm_provider,
            llm_model=settings.llm_model,
            embed_vendor=settings.embedding_provider,
            embed_model=settings.embedding_model,
            rerank_vendor=settings.rerank_provider,
            rerank_model=settings.rerank_model,
        ),
        llm=get_llm_client(settings),
        daily_budget=daily_budget,
        breaker=app.state.copilot_breaker,
        retry=RetryPolicy(max_attempts=settings.copilot_llm_gateway_retry_attempts),
        snapshots=snapshot_store,
        embedder=get_embedding_client(settings),
        reranker=get_reranker_client(settings),
        pricing=pricing,
        cost_store=cost_store,
    )
    document_worker_task = asyncio.create_task(
        build_document_worker(app.state.copilot_gateway, daily_budget).run()
    )
    note_worker_task = asyncio.create_task(
        build_note_vectorize_worker(app.state.copilot_gateway, daily_budget).run()
    )
    # 快照 TTL 清理：周期删除过期 LLM 快照（防表无限膨胀）
    snapshot_cleanup_task = asyncio.create_task(
        _run_snapshot_cleanup(snapshot_store, settings.copilot_llm_snapshot_ttl_days)
    )
    # superseded 记忆硬清理：软删除窗口过期后真删（遗忘第三动作的收尾，防表无限膨胀）
    superseded_cleanup_task = asyncio.create_task(
        _run_superseded_cleanup(settings.memory_superseded_window_days)
    )
    yield
    tasks = [document_worker_task, note_worker_task, superseded_cleanup_task, snapshot_cleanup_task]
    for task in tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if checkpointer_cm is not None:
        await checkpointer_cm.__aexit__(None, None, None)
    await stop_browser()


def create_app() -> FastAPI:
    setup_logging()
    settings = get_settings()

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestIdMiddleware)
    # 文档上传大小粗筛：在读 body 前按 Content-Length 直接 413，避免大文件被整份读入内存
    app.add_middleware(
        UploadSizeLimitMiddleware,
        max_bytes=MAX_FILE_SIZE,
        path=f"{settings.api_prefix}/documents",
    )

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": {"code": exc.code, "message": str(exc)}},
        )

    # 健康检查裸挂（探针）；业务路由挂 /api 前缀
    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.api_prefix)

    return app


app = create_app()
