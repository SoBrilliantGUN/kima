"""后台文档处理 worker：事件驱动 + 自建重试退避。

循环：唤醒/超时兜底 → 扫描到期 pending 行（FOR UPDATE SKIP LOCKED）→ 置 processing →
调 ingest（解析 → 分块 → 向量化 → done / 失败排期重试）；`asyncio.Semaphore` 限制并发。
无新文档时 idle（`wake_event.wait`），不空转；超时兜底跑 `recover_stuck`。
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.core.wake_events import document_wake_event
from app.models.document import Document
from app.repositories.document import DocumentRepository
from app.services.ingest import IngestService

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 2
DEFAULT_CONCURRENCY = 2
DEFAULT_IDLE_FALLBACK_SECONDS = 300.0
DEFAULT_PROCESSING_TIMEOUT = 30 * 60.0  # 30 分钟，超时视为卡死


class DocumentWorker:
    """按仓库/摄取服务工厂驱动的事件驱动循环（工厂便于测试注入 Fake）。"""

    def __init__(
        self,
        *,
        repo_factory: Callable[[], DocumentRepository],
        ingest_factory: Callable[[], IngestService],
        limit: int = DEFAULT_LIMIT,
        concurrency: int = DEFAULT_CONCURRENCY,
        idle_fallback_seconds: float = DEFAULT_IDLE_FALLBACK_SECONDS,
        processing_timeout: float = DEFAULT_PROCESSING_TIMEOUT,
        wake_event: asyncio.Event | None = None,
    ) -> None:
        self._repo_factory = repo_factory
        self._ingest_factory = ingest_factory
        self._limit = limit
        self._concurrency = concurrency
        self._idle_fallback_seconds = idle_fallback_seconds
        self._processing_timeout = processing_timeout
        self._wake_event = wake_event or document_wake_event

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
            # 有唤醒立即处理；超时兜底醒来跑 recover_stuck（run_once 内做）后继续等，不空转
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=self._idle_fallback_seconds)
            except TimeoutError:
                pass
            self._wake_event.clear()
