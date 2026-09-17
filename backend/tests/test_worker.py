"""worker 单元测试：驱动一轮循环（拾取 / done / 重试过滤 / 超时兜底）。"""

import uuid
from datetime import UTC, datetime, timedelta

from app.integrations.embedding import FakeEmbeddingClient
from app.models.document import MAX_RETRIES, Document, DocumentStatus, DocumentType
from app.services.ingest import IngestService
from app.workers.document_worker import DocumentWorker
from tests.fakes import FakeDocumentParser, FakeDocumentRepository, FakeFileStore


async def _make_url_doc(
    repo: FakeDocumentRepository,
    *,
    status: DocumentStatus = DocumentStatus.PENDING,
) -> Document:
    return await repo.add(
        Document(
            kb_id=uuid.uuid4(),
            title="https://example.com",
            source_type=DocumentType.URL,
            source_url="https://example.com",
            status=status,
            retry_count=0,
        )
    )


def _make_worker(
    repo: FakeDocumentRepository,
    parser: FakeDocumentParser,
    *,
    base_delay: timedelta = timedelta(seconds=0),
) -> DocumentWorker:
    return DocumentWorker(
        repo_factory=lambda: repo,
        ingest_factory=lambda: IngestService(
            repository=repo,
            parser=parser,
            embedder=FakeEmbeddingClient(1024),
            file_store=FakeFileStore(),
            base_delay=base_delay,
        ),
        limit=10,
        concurrency=2,
    )


async def test_worker_claims_pending_and_marks_done() -> None:
    repo = FakeDocumentRepository()
    doc = await _make_url_doc(repo)
    worker = _make_worker(repo, FakeDocumentParser())

    assert await worker.run_once() == 1
    assert doc.status == DocumentStatus.DONE


async def test_worker_skips_future_retry() -> None:
    repo = FakeDocumentRepository()
    doc = await _make_url_doc(repo)
    doc.next_retry_at = datetime.now(UTC) + timedelta(hours=1)

    worker = _make_worker(repo, FakeDocumentParser())
    assert await worker.run_once() == 0
    assert doc.status == DocumentStatus.PENDING  # 未被拾取


async def test_worker_recover_stuck_processing() -> None:
    repo = FakeDocumentRepository()
    doc = await _make_url_doc(repo, status=DocumentStatus.PROCESSING)
    doc.updated_at = datetime.now(UTC) - timedelta(hours=2)

    worker = _make_worker(repo, FakeDocumentParser())
    await worker.run_once()
    # 超时兜底重置后又被拾取处理 → done
    assert doc.status == DocumentStatus.DONE


async def test_worker_failure_retries_then_errors() -> None:
    repo = FakeDocumentRepository()
    doc = await _make_url_doc(repo)
    worker = _make_worker(repo, FakeDocumentParser(fail=True))

    # 前 MAX_RETRIES 轮失败仍是 pending，第 MAX_RETRIES+1 轮才置 error
    for _ in range(MAX_RETRIES):
        await worker.run_once()
        assert doc.status == DocumentStatus.PENDING

    await worker.run_once()
    refreshed = await repo.get(doc.id)
    assert refreshed is not None
    assert refreshed.status == DocumentStatus.ERROR
