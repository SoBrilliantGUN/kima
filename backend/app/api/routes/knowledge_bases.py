"""知识库 CRUD 路由。"""

import uuid

from fastapi import APIRouter, Response, status

from app.api.deps import DocumentServiceDep, KnowledgeBaseServiceDep, NoteServiceDep
from app.schemas.document import DocumentRead
from app.schemas.knowledge_base import (
    ContentItem,
    ContentList,
    KnowledgeBaseCreate,
    KnowledgeBaseList,
    KnowledgeBaseRead,
    KnowledgeBaseUpdate,
)
from app.schemas.note import NoteRead
from app.services.knowledge_base import LIST_LIMIT_DEFAULT

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-bases"])


@router.get("", response_model=KnowledgeBaseList)
async def list_knowledge_bases(
    service: KnowledgeBaseServiceDep,
    limit: int = LIST_LIMIT_DEFAULT,
    offset: int = 0,
) -> KnowledgeBaseList:
    items, total = await service.list(limit=limit, offset=offset)
    return KnowledgeBaseList(
        items=[KnowledgeBaseRead.model_validate(kb) for kb in items],
        total=total,
    )


@router.post("", response_model=KnowledgeBaseRead, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    service: KnowledgeBaseServiceDep,
) -> KnowledgeBaseRead:
    kb = await service.create(payload)
    return KnowledgeBaseRead.model_validate(kb)


@router.get("/{kb_id}", response_model=KnowledgeBaseRead)
async def get_knowledge_base(
    kb_id: uuid.UUID,
    service: KnowledgeBaseServiceDep,
) -> KnowledgeBaseRead:
    kb = await service.get(kb_id)
    return KnowledgeBaseRead.model_validate(kb)


@router.get("/{kb_id}/contents", response_model=ContentList)
async def list_knowledge_base_contents(
    kb_id: uuid.UUID,
    note_service: NoteServiceDep,
    document_service: DocumentServiceDep,
) -> ContentList:
    notes = await note_service.list_by_kb(kb_id)
    documents = await document_service.list_by_kb(kb_id)
    items = [ContentItem(type="note", note=NoteRead.model_validate(note)) for note in notes]
    items += [
        ContentItem(type="document", document=DocumentRead.model_validate(document))
        for document in documents
    ]
    return ContentList(items=items, total=len(items))


@router.patch("/{kb_id}", response_model=KnowledgeBaseRead)
async def update_knowledge_base(
    kb_id: uuid.UUID,
    payload: KnowledgeBaseUpdate,
    service: KnowledgeBaseServiceDep,
) -> KnowledgeBaseRead:
    kb = await service.update(kb_id, payload)
    return KnowledgeBaseRead.model_validate(kb)


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    kb_id: uuid.UUID,
    service: KnowledgeBaseServiceDep,
) -> Response:
    await service.delete(kb_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
