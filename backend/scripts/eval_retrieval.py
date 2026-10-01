"""检索评估 CLI：读 golden 集 → 跑真实混合检索 → 出 recall@k / MRR 报告。

用法（在 backend/ 下）：
    .venv/Scripts/python -m scripts.eval_retrieval eval/golden.example.json [--top-k 6]

依赖 `.env` 里的真实 provider（embedding/rerank 用 siliconflow，否则 fake），
并连本地 Postgres（含已向量化入库的 chunk）。golden 集的 `kb_id` 需替换为真实库 ID。
"""

import argparse
import asyncio
import logging
import uuid

from app.agent.gateway import GatewayConfig, LLMGateway, run_budget
from app.agent.pricing import PricingService
from app.agent.resilience.circuit_breaker import CircuitBreaker
from app.agent.resilience.retry import RetryPolicy
from app.agent.runtime.budget import BudgetTracker, DailyBudget
from app.core.config import Settings, get_settings
from app.core.db import async_session_factory
from app.integrations import get_embedding_client, get_llm_client, get_reranker_client
from app.rag.eval_runner import format_report, load_retrieval_cases, run_retrieval_eval
from app.rag.repository import SqlAlchemyRetrievalRepository
from app.rag.retriever import RagRetriever
from app.rag.schema import RetrievedChunk
from app.repositories.breaker import SqlAlchemyBreakerStore
from app.repositories.daily_budget import SqlAlchemyDailyBudgetRepository
from app.repositories.llm_cost import SqlAlchemyCostStore
from app.repositories.llm_snapshot import SqlAlchemySnapshotStore
from app.repositories.pricing import SqlAlchemyPricingRepository

logger = logging.getLogger(__name__)


async def _build_gateway(settings: Settings) -> LLMGateway:
    """按真实 provider 构造 LLM 网关（与 ``app.main`` 同构，供 CLI 独立运行）。

    网关是检索唯一出口（统一门禁/记账/快照），embed/rerank 都必须经它；复用跨 run 的
    ``DailyBudget`` 与 LLM 熔断，快照/成本审计恒落库。
    """
    daily_budget = DailyBudget(
        max_cost_cny=settings.copilot_daily_max_cost_cny,
        max_tokens=settings.copilot_daily_max_tokens,
        store=SqlAlchemyDailyBudgetRepository(async_session_factory),
    )
    try:
        await daily_budget.load()
    except Exception as exc:  # noqa: BLE001 - 续读失败不影响单次评估，从 0 计
        logger.warning("日预算续读失败（本次从 0 计）：%s", exc)

    breaker = CircuitBreaker(store=SqlAlchemyBreakerStore(async_session_factory))
    try:
        await breaker.load_all()
    except Exception as exc:  # noqa: BLE001 - 续读失败不影响单次评估，从空计
        logger.warning("熔断状态续读失败（本次从空计）：%s", exc)

    return LLMGateway(
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
        breaker=breaker,
        retry=RetryPolicy(max_attempts=settings.copilot_llm_gateway_retry_attempts),
        snapshots=SqlAlchemySnapshotStore(async_session_factory),
        embedder=get_embedding_client(settings),
        reranker=get_reranker_client(settings),
        pricing=PricingService(
            SqlAlchemyPricingRepository(async_session_factory),
            cache_ttl_seconds=settings.copilot_pricing_cache_ttl_seconds,
        ),
        cost_store=SqlAlchemyCostStore(async_session_factory),
    )


async def _main(golden_path: str, top_k: int) -> None:
    settings = get_settings()
    gateway = await _build_gateway(settings)
    cases = load_retrieval_cases(golden_path)

    async with async_session_factory() as session:
        retriever = RagRetriever(
            repository=SqlAlchemyRetrievalRepository(session),
            gateway=gateway,
            rerank_min_score=settings.rerank_min_score,
        )

        async def retrieve(query: str, kb_id: str) -> list[RetrievedChunk]:
            return await retriever.retrieve(query, [uuid.UUID(kb_id)])

        # 检索经网关（embed/rerank），必须先进入 run_budget 才可记账/快照。
        with run_budget(BudgetTracker(budget=None), run_id=f"eval_retrieval:{uuid.uuid4()}"):
            report = await run_retrieval_eval(retrieve, cases, top_k=top_k)

    print(format_report(report))


def main() -> None:
    parser = argparse.ArgumentParser(description="检索评估：recall@k / MRR")
    parser.add_argument(
        "golden_path", help="golden 集 JSON 路径（[{query, kb_id, golden_snippet}]）"
    )
    parser.add_argument("--top-k", type=int, default=6, help="top-k 检索条数（默认 6）")
    args = parser.parse_args()
    asyncio.run(_main(args.golden_path, args.top_k))


if __name__ == "__main__":
    main()
