"""后台文档处理 worker：DB 轮询 + 自建重试退避。

循环：扫描到期 pending 行（FOR UPDATE SKIP LOCKED）→ 置 processing → 调 ingest
（解析 → 分块 → 向量化 → done / 失败排期重试）；`asyncio.Semaphore` 限制并发。
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.models.document import Document
from app.repositories.document import DocumentRepository
from app.services.ingest import IngestService

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 2
DEFAULT_CONCURRENCY = 2
DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_PROCESSING_TIMEOUT = 30 * 60.0  # 30 分钟，超时视为卡死


class DocumentWorker:
    """按仓库/摄取服务工厂驱动的轮询循环（工厂便于测试注入 Fake）。"""

    def __init__(
        self,
        *,
        repo_factory: Callable[[], DocumentRepository],
        ingest_factory: Callable[[], IngestService],
        limit: int = DEFAULT_LIMIT,
        concurrency: int = DEFAULT_CONCURRENCY,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        processing_timeout: float = DEFAULT_PROCESSING_TIMEOUT,
    ) -> None:
        self._repo_factory = repo_factory
        self._ingest_factory = ingest_factory
        self._limit = limit
        self._concurrency = concurrency
        self._poll_interval = poll_interval
        self._processing_timeout = processing_timeout

    async def run_once(self) -> int:
        """驱动一轮：超时兜底 + 拾取 pending + 逐个 ingest。返回处理条数。"""
        repository = self._repo_factory()
        try:
            await repository.recover_stuck(
                datetime.now(UTC), timedelta(seconds=self._processing_timeout)
            )
            claimed = await repository.claim_pending(self._limit)
        finally:
            await repository.close()

        semaphore = asyncio.Semaphore(self._concurrency)

        async def _process(document: Document) -> None:
            async with semaphore:
                ingest = self._ingest_factory()
                try:
                    await ingest.ingest(document.id)
                finally:
                    await ingest.close()

        await asyncio.gather(*(_process(document) for document in claimed))
        return len(claimed)

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("document worker 处理一轮失败")
            await asyncio.sleep(self._poll_interval)
