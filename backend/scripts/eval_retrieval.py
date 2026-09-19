"""检索评估 CLI：读 golden 集 → 跑真实混合检索 → 出 recall@k / MRR 报告。

用法（在 backend/ 下）：
    .venv/Scripts/python -m scripts.eval_retrieval eval/golden.example.json [--top-k 6]

依赖 `.env` 里的真实 provider（embedding/rerank 用 siliconflow，否则 fake），
并连本地 Postgres（含已向量化入库的 chunk）。golden 集的 `kb_id` 需替换为真实库 ID。
"""

import argparse
import asyncio
import uuid

from app.core.config import get_settings
from app.core.db import async_session_factory
from app.integrations import get_embedding_client, get_reranker_client
from app.rag.eval_runner import format_report, load_retrieval_cases, run_retrieval_eval
from app.rag.repository import SqlAlchemyRetrievalRepository
from app.rag.retriever import RagRetriever
from app.rag.schema import RetrievedChunk


async def _main(golden_path: str, top_k: int) -> None:
    settings = get_settings()
    embedder = get_embedding_client(settings)
    reranker = get_reranker_client(settings)
    cases = load_retrieval_cases(golden_path)

    async with async_session_factory() as session:
        retriever = RagRetriever(
            repository=SqlAlchemyRetrievalRepository(session),
            embedder=embedder,
            reranker=reranker,
        )

        async def retrieve(query: str, kb_id: str) -> list[RetrievedChunk]:
            return await retriever.retrieve(query, uuid.UUID(kb_id))

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
