import uuid

import pytest

from app.core.exceptions import FetchError, NotFoundError
from app.integrations.llm import ChatMessage, ChatResult
from app.integrations.web import FakeWebFetcher, FetchedPage
from app.models.knowledge_base import KnowledgeBase
from app.models.note import DEFAULT_NOTE_TITLE, NoteType
from app.schemas.note import NoteCreate, NoteCreateFromUrl, NoteUpdate
from app.services.llm import Summarizer
from app.services.note import NoteService
from tests.fakes import FakeKnowledgeBaseRepository, FakeNoteRepository


class _RaisingLLM:
    """任何 chat 调用都抛异常，用于验证摘要降级。"""

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult:
        raise RuntimeError("llm down")


@pytest.fixture
def note_repo() -> FakeNoteRepository:
    return FakeNoteRepository()


@pytest.fixture
def kb_repo() -> FakeKnowledgeBaseRepository:
    return FakeKnowledgeBaseRepository()


@pytest.fixture
def service(
    note_repo: FakeNoteRepository, kb_repo: FakeKnowledgeBaseRepository
) -> NoteService:
    return NoteService(note_repo, kb_repo, FakeWebFetcher(), Summarizer(_RaisingLLM()))


async def _make_kb(kb_repo: FakeKnowledgeBaseRepository, name: str = "工作") -> KnowledgeBase:
    kb = KnowledgeBase(name=name)
    return await kb_repo.add(kb)


async def test_create_from_url_creates_url_note(service: NoteService) -> None:
    note = await service.create_from_url(NoteCreateFromUrl(url="https://example.com"))
    assert note.type == NoteType.URL
    assert note.source_url == "https://example.com"
    assert note.title == "示例标题"
    # 摘要降级：LLM 抛异常 → 摘要 = 正文前 200 字（本例正文短，无省略号）
    assert note.summary is not None
    assert note.summary.startswith("# 示例正文")

    items, total = await service.list(limit=10, offset=0)
    assert total == 1
    assert items[0].id == note.id


async def test_create_from_url_summary_truncates_long_body(
    note_repo: FakeNoteRepository, kb_repo: FakeKnowledgeBaseRepository
) -> None:
    long_markdown = "x" * 300
    web_fetcher = FakeWebFetcher(FetchedPage(markdown=long_markdown, title="长文"))
    service = NoteService(note_repo, kb_repo, web_fetcher, Summarizer(_RaisingLLM()))
    note = await service.create_from_url(NoteCreateFromUrl(url="https://example.com"))
    assert note.summary == "x" * 200 + "…"


async def test_create_from_url_fetch_failure_creates_nothing(
    note_repo: FakeNoteRepository, kb_repo: FakeKnowledgeBaseRepository
) -> None:
    service = NoteService(note_repo, kb_repo, FakeWebFetcher(fail=True), Summarizer(_RaisingLLM()))
    with pytest.raises(FetchError):
        await service.create_from_url(NoteCreateFromUrl(url="https://example.com"))
    _, total = await note_repo.list(limit=10, offset=0)
    assert total == 0


async def test_create_from_url_associates_kb(
    service: NoteService, note_repo: FakeNoteRepository, kb_repo: FakeKnowledgeBaseRepository
) -> None:
    kb = await _make_kb(kb_repo)
    note = await service.create_from_url(
        NoteCreateFromUrl(url="https://example.com", knowledge_base_id=kb.id)
    )
    assert await note_repo.list_by_kb(kb.id) == [note]


async def test_create_from_url_rejects_missing_kb(
    service: NoteService, note_repo: FakeNoteRepository
) -> None:
    with pytest.raises(NotFoundError):
        await service.create_from_url(
            NoteCreateFromUrl(url="https://example.com", knowledge_base_id=uuid.uuid4())
        )
    _, total = await note_repo.list(limit=10, offset=0)
    assert total == 0


async def test_create_blank_uses_default_title(service: NoteService) -> None:
    note = await service.create_blank(NoteCreate())
    assert note.type == NoteType.MARKDOWN
    assert note.title == DEFAULT_NOTE_TITLE
    assert note.content_markdown == ""


async def test_create_blank_associates_kb(
    service: NoteService, note_repo: FakeNoteRepository, kb_repo: FakeKnowledgeBaseRepository
) -> None:
    kb = await _make_kb(kb_repo)
    note = await service.create_blank(NoteCreate(knowledge_base_id=kb.id))
    assert await note_repo.list_by_kb(kb.id) == [note]


async def test_create_blank_rejects_missing_kb(
    service: NoteService, note_repo: FakeNoteRepository
) -> None:
    with pytest.raises(NotFoundError):
        await service.create_blank(NoteCreate(knowledge_base_id=uuid.uuid4()))
    _, total = await note_repo.list(limit=10, offset=0)
    assert total == 0


async def test_update_title_and_content(service: NoteService) -> None:
    note = await service.create_blank(NoteCreate())
    updated = await service.update(
        note.id, NoteUpdate(title="新标题", content_markdown="# 正文")
    )
    assert updated.title == "新标题"
    assert updated.content_markdown == "# 正文"


async def test_update_missing_raises_not_found(service: NoteService) -> None:
    with pytest.raises(NotFoundError):
        await service.update(uuid.uuid4(), NoteUpdate(title="标题"))


async def test_get_missing_raises_not_found(service: NoteService) -> None:
    with pytest.raises(NotFoundError):
        await service.get(uuid.uuid4())


async def test_delete_removes(service: NoteService) -> None:
    note = await service.create_blank(NoteCreate())
    await service.delete(note.id)
    with pytest.raises(NotFoundError):
        await service.get(note.id)


async def test_delete_missing_raises_not_found(service: NoteService) -> None:
    with pytest.raises(NotFoundError):
        await service.delete(uuid.uuid4())


async def test_add_to_kb_and_idempotent(
    service: NoteService, note_repo: FakeNoteRepository, kb_repo: FakeKnowledgeBaseRepository
) -> None:
    kb = await _make_kb(kb_repo)
    note = await service.create_blank(NoteCreate())
    await service.add_to_kb(note.id, kb.id)
    await service.add_to_kb(note.id, kb.id)  # 幂等，静默成功
    assert await note_repo.list_by_kb(kb.id) == [note]


async def test_add_to_kb_missing_note_raises(
    service: NoteService, kb_repo: FakeKnowledgeBaseRepository
) -> None:
    kb = await _make_kb(kb_repo)
    with pytest.raises(NotFoundError):
        await service.add_to_kb(uuid.uuid4(), kb.id)


async def test_add_to_kb_missing_kb_raises(service: NoteService) -> None:
    note = await service.create_blank(NoteCreate())
    with pytest.raises(NotFoundError):
        await service.add_to_kb(note.id, uuid.uuid4())


async def test_list_by_kb_missing_kb_raises(service: NoteService) -> None:
    with pytest.raises(NotFoundError):
        await service.list_by_kb(uuid.uuid4())


async def test_list_clamps_limit(service: NoteService) -> None:
    for _ in range(3):
        await service.create_blank(NoteCreate())
    items, _ = await service.list(limit=1000, offset=0)
    assert len(items) == 3


def test_note_update_requires_at_least_one_field() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        NoteUpdate()


def test_note_create_from_url_rejects_invalid_url() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        NoteCreateFromUrl(url="not-a-url")
    with pytest.raises(ValidationError):
        NoteCreateFromUrl(url="ftp://example.com")
