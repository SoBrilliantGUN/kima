"""ingest 流水线单元测试：done 落父子 chunk / parse 失败 / embed 失败 / 重试排期。"""

import uuid

from app.integrations.embedding import FakeEmbeddingClient
from app.models.document import MAX_RETRIES, Document, DocumentStatus, DocumentType
from app.services.ingest import IngestService
from tests.fakes import FakeDocumentParser, FakeDocumentRepository, FakeFileStore, make_gateway


class _RaisingEmbedder:
    """任何 embed 调用都抛异常，用于验证 embed 失败 → 文档 error。"""

    @property
    def dimension(self) -> int:
        return 1024

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embed down")

    async def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("embed down")


async def _make_pdf(repo: FakeDocumentRepository, file_store: FakeFileStore) -> Document:
    file_path = await file_store.save(b"%PDF-1.4", "pdf")
    return await repo.add(
        Document(
            kb_id=uuid.uuid4(),
            title="测试文档",
            source_type=DocumentType.PDF,
            file_path=file_path,
            status=DocumentStatus.PENDING,
            retry_count=0,
        )
    )


async def _make_markdown(repo: FakeDocumentRepository, file_store: FakeFileStore) -> Document:
    file_path = await file_store.save("# 标题\n\n正文".encode(), "md")
    return await repo.add(
        Document(
            kb_id=uuid.uuid4(),
            title="笔记",
            source_type=DocumentType.MARKDOWN,
            file_path=file_path,
            status=DocumentStatus.PENDING,
            retry_count=0,
        )
    )


async def test_ingest_done_writes_parent_child_chunks() -> None:
    repo = FakeDocumentRepository()
    file_store = FakeFileStore()
    body = "这是内容。" * 300  # 每节约 1275 token，避免合并
    markdown = f"# 概述\n\n{body}\n\n# 细节\n\n{body}\n"
    parser = FakeDocumentParser(markdown=markdown)
    ingest = IngestService(
        repository=repo,
        parser=parser,
        gateway=make_gateway(embedder=FakeEmbeddingClient(1024)),
        file_store=file_store,
    )

    doc = await _make_pdf(repo, file_store)
    await ingest.ingest(doc.id)

    assert doc.status == DocumentStatus.DONE
    assert doc.content_markdown == markdown

    chunks = repo.chunks_of(doc.id)
    parents = [c for c in chunks if c.parent_id is None]
    children = [c for c in chunks if c.parent_id is not None]
    assert len(parents) >= 2
    assert len(children) >= 2
    # parent 不向量化，child 向量化
    assert all(c.embedding is None for c in parents)
    assert all(c.embedding is not None and len(c.embedding) == 1024 for c in children)
    # child 的 parent_id 指向某个 parent
    parent_ids = {p.id for p in parents}
    assert all(c.parent_id in parent_ids for c in children)


async def test_ingest_markdown_document_done() -> None:
    """markdown 文档走完整管线：FileStore 读字节 → 解析 → 分块 → 向量化 → done。"""
    repo = FakeDocumentRepository()
    file_store = FakeFileStore()
    body = "这是内容。" * 300
    markdown = f"# 概述\n\n{body}\n\n# 细节\n\n{body}\n"
    parser = FakeDocumentParser(markdown=markdown)
    ingest = IngestService(
        repository=repo,
        parser=parser,
        gateway=make_gateway(embedder=FakeEmbeddingClient(1024)),
        file_store=file_store,
    )

    doc = await _make_markdown(repo, file_store)
    await ingest.ingest(doc.id)

    assert doc.status == DocumentStatus.DONE
    assert doc.content_markdown == markdown
    assert repo.chunks_of(doc.id)  # 已分块向量化落库


async def test_ingest_parse_failure_schedules_retry() -> None:
    repo = FakeDocumentRepository()
    file_store = FakeFileStore()
    ingest = IngestService(
        repository=repo,
        parser=FakeDocumentParser(fail=True),
        gateway=make_gateway(embedder=FakeEmbeddingClient(1024)),
        file_store=file_store,
    )

    doc = await _make_pdf(repo, file_store)
    await ingest.ingest(doc.id)

    assert doc.status == DocumentStatus.PENDING
    assert doc.retry_count == 1
    assert doc.next_retry_at is not None
    assert doc.error_message


async def test_ingest_retry_exhausted_becomes_error() -> None:
    repo = FakeDocumentRepository()
    file_store = FakeFileStore()
    ingest = IngestService(
        repository=repo,
        parser=FakeDocumentParser(fail=True),
        gateway=make_gateway(embedder=FakeEmbeddingClient(1024)),
        file_store=file_store,
    )

    doc = await repo.add(
        Document(
            kb_id=uuid.uuid4(),
            title="测试",
            source_type=DocumentType.PDF,
            file_path="x.pdf",
            status=DocumentStatus.PENDING,
            retry_count=0,
        )
    )
    file_store.files["x.pdf"] = b"x"

    # 前 MAX_RETRIES 次失败仍是 pending，第 MAX_RETRIES+1 次失败才置 error
    for _ in range(MAX_RETRIES):
        await ingest.ingest(doc.id)
        refreshed = await repo.get(doc.id)
        assert refreshed is not None
        assert refreshed.status == DocumentStatus.PENDING

    await ingest.ingest(doc.id)
    refreshed = await repo.get(doc.id)
    assert refreshed is not None
    assert refreshed.status == DocumentStatus.ERROR
    assert refreshed.next_retry_at is None


async def test_ingest_embed_failure_schedules_retry() -> None:
    repo = FakeDocumentRepository()
    file_store = FakeFileStore()
    # 有正文才会产出 child 触发 embed
    parser = FakeDocumentParser(markdown="这是正文。" * 200)
    ingest = IngestService(
        repository=repo,
        parser=parser,
        gateway=make_gateway(embedder=_RaisingEmbedder()),
        file_store=file_store,
    )

    doc = await _make_pdf(repo, file_store)
    await ingest.ingest(doc.id)
    assert doc.status == DocumentStatus.PENDING
    assert doc.retry_count == 1


async def test_ingest_url_updates_title() -> None:
    repo = FakeDocumentRepository()
    file_store = FakeFileStore()
    ingest = IngestService(
        repository=repo,
        parser=FakeDocumentParser(markdown="# 网页正文", title="真标题"),
        gateway=make_gateway(embedder=FakeEmbeddingClient(1024)),
        file_store=file_store,
    )

    doc = await repo.add(
        Document(
            kb_id=uuid.uuid4(),
            title="https://example.com",
            source_type=DocumentType.URL,
            source_url="https://example.com",
            status=DocumentStatus.PENDING,
            retry_count=0,
        )
    )
    await ingest.ingest(doc.id)
    assert doc.status == DocumentStatus.DONE
    assert doc.title == "真标题"


async def test_ingest_clears_old_chunks_on_retry() -> None:
    repo = FakeDocumentRepository()
    file_store = FakeFileStore()
    ingest = IngestService(
        repository=repo,
        parser=FakeDocumentParser(),
        gateway=make_gateway(embedder=FakeEmbeddingClient(1024)),
        file_store=file_store,
    )
    doc = await _make_pdf(repo, file_store)
    await ingest.ingest(doc.id)
    old_ids = {chunk.id for chunk in repo.chunks_of(doc.id)}

    doc.status = DocumentStatus.PENDING
    await ingest.ingest(doc.id)
    new_ids = {chunk.id for chunk in repo.chunks_of(doc.id)}
    assert new_ids
    assert old_ids.isdisjoint(new_ids)  # 旧 chunk 已清理，重写全新 chunk


async def test_ingest_large_document_needs_approval() -> None:
    """大文档警告：child token 预估超阈值 → 置 needs_approval，不嵌入。"""
    repo = FakeDocumentRepository()
    file_store = FakeFileStore()
    parser = FakeDocumentParser(markdown="这是正文。" * 2000)  # 足够长 → 超阈值
    ingest = IngestService(
        repository=repo,
        parser=parser,
        gateway=make_gateway(embedder=FakeEmbeddingClient(1024)),
        file_store=file_store,
        warn_tokens=10,  # 极小阈值 → 必触发
    )

    doc = await _make_pdf(repo, file_store)
    await ingest.ingest(doc.id)

    assert doc.status == DocumentStatus.NEEDS_APPROVAL
    assert repo.chunks_of(doc.id) == []  # 未嵌入、未落 chunk


async def test_ingest_large_document_approved_then_embeds() -> None:
    """确认后重跑：embedding_approved=True → 跳过阈值检查，正常嵌入 done。"""
    repo = FakeDocumentRepository()
    file_store = FakeFileStore()
    parser = FakeDocumentParser(markdown="这是正文。" * 2000)
    ingest = IngestService(
        repository=repo,
        parser=parser,
        gateway=make_gateway(embedder=FakeEmbeddingClient(1024)),
        file_store=file_store,
        warn_tokens=10,
    )

    doc = await _make_pdf(repo, file_store)
    await ingest.ingest(doc.id)
    assert doc.status == DocumentStatus.NEEDS_APPROVAL

    # 模拟用户确认：置 approved + 回 pending，重跑
    doc.embedding_approved = True
    doc.status = DocumentStatus.PENDING
    await ingest.ingest(doc.id)

    assert doc.status == DocumentStatus.DONE
    assert repo.chunks_of(doc.id)  # 已嵌入落 chunk
