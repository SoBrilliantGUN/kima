"""笔记服务层。

笔记是全局实体：可以独立存在（不挂知识库），也可以通过
note_knowledge_bases 关联表挂载到任意知识库。笔记为纯 Markdown 空白笔记。
"""

import hashlib
import uuid
from collections.abc import Sequence

from app.core.exceptions import NotFoundError
from app.models.note import DEFAULT_NOTE_TITLE, Note
from app.repositories.knowledge_base import KnowledgeBaseRepository
from app.repositories.note import NoteRepository
from app.schemas.note import NoteCreate, NoteUpdate

# 列表分页的默认值与上限。
LIST_LIMIT_DEFAULT = 50
LIST_LIMIT_MAX = 100


def content_hash_of(content_markdown: str) -> str:
    """正文 sha256 十六进制（Copilot create_note 幂等去重键）。"""
    return hashlib.sha256(content_markdown.encode("utf-8")).hexdigest()


class NoteService:
    """笔记业务逻辑，封装笔记的创建、查询、更新、删除及挂库。"""

    def __init__(
        self,
        repository: NoteRepository,
        kb_repository: KnowledgeBaseRepository,
    ) -> None:
        self._repository = repository
        self._kb_repository = kb_repository

    async def _validate_kb_if_present(self, kb_id: uuid.UUID | None) -> None:
        """若指定了知识库则校验其存在；未指定（None）表示全局笔记，跳过。"""
        if kb_id is None:
            return
        if await self._kb_repository.get(kb_id) is None:
            raise NotFoundError("知识库不存在")

    async def create_blank(self, payload: NoteCreate) -> Note:
        """创建一篇空白的 Markdown 笔记。"""
        await self._validate_kb_if_present(payload.knowledge_base_id)
        # content_hash 留 NULL：空白笔记不参与 Copilot 幂等去重（空正文的哈希相同，
        # 若落唯一索引会互相撞车）。
        note = Note(title=payload.title or DEFAULT_NOTE_TITLE, content_markdown="")
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
            # 手工编辑退出 Copilot 幂等去重域（置 NULL）：避免「手改后的内容撞上别的笔记
            # 的唯一约束」，且手改笔记不应再作为 create_note 的去重锚点。
            note.content_hash = None

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

    async def create_with_content(
        self, title: str, content_markdown: str, kb_id: uuid.UUID | None = None
    ) -> Note:
        """带正文创建笔记（Copilot create_note 工具），按 content hash 幂等去重。

        若已存在同正文的笔记则直接返回（Agent 崩溃重放不重复建笔记）；
        指定 kb_id 时校验其存在并关联。

        并发安全：去重靠「唯一索引 + 原子 `create_unique`」兜底——并发同正文的两次
        调用只落一条（读-判-写的缝隙被 DB 唯一约束填上），不再依赖「先查后插」的竞态。
        """
        await self._validate_kb_if_present(kb_id)
        content_hash = content_hash_of(content_markdown)
        existing = await self._repository.get_by_content_hash(content_hash)
        if existing is not None:
            if kb_id is not None:
                await self._repository.associate(existing.id, kb_id)
            return existing
        note = Note(
            title=title or DEFAULT_NOTE_TITLE,
            content_markdown=content_markdown,
            content_hash=content_hash,
        )
        created = await self._repository.create_unique(note)
        if created is None:
            # 并发同正文：唯一索引兜底，回读已存在的那条
            existing = await self._repository.get_by_content_hash(content_hash)
            if existing is None:  # 理论不可达：冲突必有一条已落库
                raise RuntimeError("content_hash 冲突但回读失败")
            note = existing
        else:
            note = created
        if kb_id is not None:
            await self._repository.associate(note.id, kb_id)
        return note
