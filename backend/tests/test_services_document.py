"""文档服务单元测试：上传 / from-url / 详情 / 删除 / 重试。"""

import uuid

import pytest

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.document import DocumentStatus, DocumentType
from app.models.knowledge_base import KnowledgeBase
from app.schemas.document import DocumentCreateFromUrl
from app.services.document import DocumentService
from tests.fakes import FakeDocumentRepository, FakeFileStore, FakeKnowledgeBaseRepository


@pytest.fixture
def doc_repo() -> FakeDocumentRepository:
    return FakeDocumentRepository()


@pytest.fixture
def kb_repo() -> FakeKnowledgeBaseRepository:
    return FakeKnowledgeBaseRepository()


@pytest.fixture
def file_store() -> FakeFileStore:
    return FakeFileStore()


@pytest.fixture
def service(
    doc_repo: FakeDocumentRepository,
    kb_repo: FakeKnowledgeBaseRepository,
    file_store: FakeFileStore,
) -> DocumentService:
    return DocumentService(doc_repo, kb_repo, file_store)


async def _make_kb(kb_repo: FakeKnowledgeBaseRepository) -> KnowledgeBase:
    return await kb_repo.add(KnowledgeBase(name="工作"))


async def test_create_file_pdf_pending(
    service: DocumentService, kb_repo: FakeKnowledgeBaseRepository,
) -> None:
    kb = await _make_kb(kb_repo)
    doc = await service.create_file(kb.id, content=b"%PDF-1.4", filename="报告.pdf")
    assert doc.status == DocumentStatus.PENDING
    assert doc.source_type == DocumentType.PDF
    assert doc.title == "报告"
    assert doc.file_path is not None


async def test_document_filename_reconstructs_title_and_ext(
    service: DocumentService, kb_repo: FakeKnowledgeBaseRepository,
) -> None:
    kb = await _make_kb(kb_repo)
    pdf = await service.create_file(kb.id, content=b"%PDF-1.4", filename="我的报告.pdf")
    word = await service.create_file(kb.id, content=b"x", filename="会议纪要.docx")
    assert pdf.filename == "我的报告.pdf"
    assert word.filename == "会议纪要.docx"


async def test_create_file_rejects_unsupported_ext(
    service: DocumentService, kb_repo: FakeKnowledgeBaseRepository,
) -> None:
    kb = await _make_kb(kb_repo)
    with pytest.raises(ValidationError):
        await service.create_file(kb.id, content=b"x", filename="笔记.doc")


async def test_create_file_rejects_oversized(
    service: DocumentService, kb_repo: FakeKnowledgeBaseRepository,
) -> None:
    kb = await _make_kb(kb_repo)
    with pytest.raises(ValidationError):
        await service.create_file(kb.id, content=b"x" * (20 * 1024 * 1024 + 1), filename="大.pdf")


async def test_create_file_missing_kb_404(service: DocumentService) -> None:
    with pytest.raises(NotFoundError):
        await service.create_file(uuid.uuid4(), content=b"x", filename="a.pdf")


async def test_create_from_url_pending(
    service: DocumentService, kb_repo: FakeKnowledgeBaseRepository,
) -> None:
    kb = await _make_kb(kb_repo)
    doc = await service.create_from_url(
        DocumentCreateFromUrl(url="https://example.com", knowledge_base_id=kb.id)
    )
    assert doc.source_type == DocumentType.URL
    assert doc.source_url == "https://example.com"
    assert doc.status == DocumentStatus.PENDING


async def test_get_missing_404(service: DocumentService) -> None:
    with pytest.raises(NotFoundError):
        await service.get(uuid.uuid4())


async def test_delete_removes_file_and_doc(
    service: DocumentService, kb_repo: FakeKnowledgeBaseRepository, file_store: FakeFileStore
) -> None:
    kb = await _make_kb(kb_repo)
    doc = await service.create_file(kb.id, content=b"x", filename="a.pdf")
    path = doc.file_path
    assert path in file_store.files

    await service.delete(doc.id)
    assert path not in file_store.files
    with pytest.raises(NotFoundError):
        await service.get(doc.id)


async def test_retry_error_resets(
    service: DocumentService, kb_repo: FakeKnowledgeBaseRepository,
) -> None:
    kb = await _make_kb(kb_repo)
    doc = await service.create_file(kb.id, content=b"x", filename="a.pdf")
    doc.status = DocumentStatus.ERROR
    doc.retry_count = 3
    doc.error_message = "失败"

    retried = await service.retry(doc.id)
    assert retried.status == DocumentStatus.PENDING
    assert retried.retry_count == 0
    assert retried.error_message is None
    assert retried.next_retry_at is None


async def test_retry_non_error_409(
    service: DocumentService, kb_repo: FakeKnowledgeBaseRepository,
) -> None:
    kb = await _make_kb(kb_repo)
    doc = await service.create_file(kb.id, content=b"x", filename="a.pdf")
    with pytest.raises(ConflictError):
        await service.retry(doc.id)


async def test_list_by_kb(service: DocumentService, kb_repo: FakeKnowledgeBaseRepository) -> None:
    kb = await _make_kb(kb_repo)
    await service.create_file(kb.id, content=b"x", filename="a.pdf")
    await service.create_from_url(
        DocumentCreateFromUrl(url="https://example.com", knowledge_base_id=kb.id)
    )
    docs = await service.list_by_kb(kb.id)
    assert len(docs) == 2


async def test_get_file_url_type_409(
    service: DocumentService, kb_repo: FakeKnowledgeBaseRepository,
) -> None:
    kb = await _make_kb(kb_repo)
    doc = await service.create_from_url(
        DocumentCreateFromUrl(url="https://example.com", knowledge_base_id=kb.id)
    )
    with pytest.raises(ConflictError):
        await service.get_file(doc.id)


async def test_get_content_done(
    service: DocumentService, kb_repo: FakeKnowledgeBaseRepository,
) -> None:
    kb = await _make_kb(kb_repo)
    doc = await service.create_file(kb.id, content=b"%PDF-1.4", filename="a.pdf")
    doc.status = DocumentStatus.DONE
    doc.content_markdown = "# 正文"

    assert await service.get_content(doc.id) == "# 正文"


async def test_get_content_pending_409(
    service: DocumentService, kb_repo: FakeKnowledgeBaseRepository,
) -> None:
    kb = await _make_kb(kb_repo)
    doc = await service.create_file(kb.id, content=b"%PDF-1.4", filename="a.pdf")

    with pytest.raises(ConflictError):
        await service.get_content(doc.id)
