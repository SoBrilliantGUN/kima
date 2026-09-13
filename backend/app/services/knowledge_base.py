import uuid

from app.core.exceptions import ConflictError, NotFoundError
from app.models.knowledge_base import DEFAULT_KB_COLOR, KnowledgeBase
from app.repositories.knowledge_base import KnowledgeBaseRepository
from app.schemas.knowledge_base import KnowledgeBaseCreate, KnowledgeBaseUpdate

LIST_LIMIT_DEFAULT = 50
LIST_LIMIT_MAX = 100
DEFAULT_KB_NAME = "我的知识库"


class KnowledgeBaseService:
    def __init__(self, repository: KnowledgeBaseRepository) -> None:
        self._repository = repository

    async def ensure_default(self) -> None:
        """幂等预置默认知识库：仅当没有任何知识库时创建「我的知识库」。"""
        _, total = await self._repository.list(limit=1, offset=0)
        if total == 0:
            await self.create(KnowledgeBaseCreate(name=DEFAULT_KB_NAME))

    async def list(self, *, limit: int, offset: int) -> tuple[list[KnowledgeBase], int]:
        limit = min(max(limit, 1), LIST_LIMIT_MAX)
        offset = max(offset, 0)
        return await self._repository.list(limit=limit, offset=offset)

    async def create(self, payload: KnowledgeBaseCreate) -> KnowledgeBase:
        if await self._repository.get_by_name(payload.name) is not None:
            raise ConflictError("知识库名称已存在")
        kb = KnowledgeBase(
            name=payload.name,
            description=payload.description,
            color=payload.color or DEFAULT_KB_COLOR,
        )
        return await self._repository.add(kb)

    async def get(self, kb_id: uuid.UUID) -> KnowledgeBase:
        kb = await self._repository.get(kb_id)
        if kb is None:
            raise NotFoundError("知识库不存在")
        return kb

    async def update(self, kb_id: uuid.UUID, payload: KnowledgeBaseUpdate) -> KnowledgeBase:
        kb = await self.get(kb_id)
        provided = payload.model_fields_set

        if "name" in provided:
            name = payload.name
            if name is not None and name != kb.name:
                if await self._repository.get_by_name(name) is not None:
                    raise ConflictError("知识库名称已存在")
                kb.name = name

        if "description" in provided:
            kb.description = payload.description

        if "color" in provided:
            kb.color = payload.color or DEFAULT_KB_COLOR

        return await self._repository.update(kb)

    async def delete(self, kb_id: uuid.UUID) -> None:
        kb = await self.get(kb_id)
        _, total = await self._repository.list(limit=1, offset=0)
        if total <= 1:
            raise ConflictError("至少保留一个知识库", code="last_knowledge_base")
        await self._repository.delete(kb)
