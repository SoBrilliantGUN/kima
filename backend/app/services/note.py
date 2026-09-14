"""笔记服务层。

笔记是全局实体：可以独立存在（不挂知识库），也可以通过
note_knowledge_bases 关联表挂载到任意知识库。
"""

import uuid
from collections.abc import Sequence

from app.core.exceptions import NotFoundError
from app.integrations.web import WebFetcher
from app.models.note import DEFAULT_NOTE_TITLE, Note, NoteType
from app.repositories.knowledge_base import KnowledgeBaseRepository
from app.repositories.note import NoteRepository
from app.schemas.note import NoteCreate, NoteCreateFromUrl, NoteUpdate
from app.services.llm import Summarizer

# 列表分页的默认值与上限。
LIST_LIMIT_DEFAULT = 50
LIST_LIMIT_MAX = 100


class NoteService:
    """笔记业务逻辑，封装笔记的创建、查询、更新、删除及挂库。"""

    def __init__(
        self,
        repository: NoteRepository,
        kb_repository: KnowledgeBaseRepository,
        web_fetcher: WebFetcher,
        summarizer: Summarizer,
    ) -> None:
        self._repository = repository
        self._kb_repository = kb_repository
        self._web_fetcher = web_fetcher
        self._summarizer = summarizer

    async def _validate_kb_if_present(self, kb_id: uuid.UUID | None) -> None:
        """若指定了知识库则校验其存在；未指定（None）表示全局笔记，跳过。"""
        if kb_id is None:
            return
        if await self._kb_repository.get(kb_id) is None:
            raise NotFoundError("知识库不存在")

    async def create_from_url(self, payload: NoteCreateFromUrl) -> Note:
        """从 URL 抓取网页正文并生成摘要，创建一篇 URL 笔记。"""
        await self._validate_kb_if_present(payload.knowledge_base_id)
        fetched = await self._web_fetcher.fetch(payload.url)
        title = fetched.title or payload.url
        summary = await self._summarizer.summarize(fetched.markdown)
        note = Note(
            title=title,
            type=NoteType.URL,
            content_markdown=fetched.markdown,
            summary=summary,
            source_url=payload.url,
        )
        note = await self._repository.add(note)
        if payload.knowledge_base_id is not None:
            await self._repository.associate(note.id, payload.knowledge_base_id)
        return note

    async def create_blank(self, payload: NoteCreate) -> Note:
        """创建一篇空白的 Markdown 笔记。"""
        await self._validate_kb_if_present(payload.knowledge_base_id)
        note = Note(
            title=payload.title or DEFAULT_NOTE_TITLE,
            type=NoteType.MARKDOWN,
            content_markdown="",
        )
        note = await self._repository.add(note)
        if payload.knowledge_base_id is not None:
            await self._repository.associate(note.id, payload.knowledge_base_id)
        return note

    async def list(self, *, limit: int, offset: int) -> tuple[list[Note], int]:
        """分页列出全局笔记（不区分知识库）。"""
        limit = min(max(limit, 1), LIST_LIMIT_MAX)
        offset = max(offset, 0)
        return await self._repository.list(limit=limit, offset=offset)

    async def get(self, note_id: uuid.UUID) -> Note:
        note = await self._repository.get(note_id)
        if note is None:
            raise NotFoundError("笔记不存在")
        return note

    async def update(self, note_id: uuid.UUID, payload: NoteUpdate) -> Note:
        """只更新 payload 中显式提供的字段。"""
        note = await self.get(note_id)
        provided = payload.model_fields_set

        if "title" in provided:
            title = payload.title
            if title is not None:
                note.title = title

        if "content_markdown" in provided and payload.content_markdown is not None:
            note.content_markdown = payload.content_markdown

        return await self._repository.update(note)

    async def delete(self, note_id: uuid.UUID) -> None:
        note = await self.get(note_id)
        await self._repository.delete(note)

    async def add_to_kb(self, note_id: uuid.UUID, kb_id: uuid.UUID) -> None:
        """把一篇已有笔记关联到指定知识库。"""
        await self.get(note_id)
        if await self._kb_repository.get(kb_id) is None:
            raise NotFoundError("知识库不存在")
        await self._repository.associate(note_id, kb_id)

    async def list_by_kb(self, kb_id: uuid.UUID) -> Sequence[Note]:
        """列出指定知识库下的笔记。"""
        if await self._kb_repository.get(kb_id) is None:
            raise NotFoundError("知识库不存在")
        return await self._repository.list_by_kb(kb_id)
