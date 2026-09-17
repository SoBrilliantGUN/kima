"""笔记 CRUD 路由。"""

import uuid

from fastapi import APIRouter, Response, status

from app.api.deps import NoteServiceDep
from app.schemas.note import (
    NoteAddToKnowledgeBase,
    NoteCreate,
    NoteList,
    NoteRead,
    NoteUpdate,
)
from app.services.note import LIST_LIMIT_DEFAULT

router = APIRouter(prefix="/notes", tags=["notes"])


@router.post("", response_model=NoteRead, status_code=status.HTTP_201_CREATED)
async def create_note(payload: NoteCreate, service: NoteServiceDep) -> NoteRead:
    note = await service.create_blank(payload)
    return NoteRead.model_validate(note)


@router.get("", response_model=NoteList)
async def list_notes(
    service: NoteServiceDep,
    limit: int = LIST_LIMIT_DEFAULT,
    offset: int = 0,
) -> NoteList:
    items, total = await service.list(limit=limit, offset=offset)
    return NoteList(items=[NoteRead.model_validate(note) for note in items], total=total)


@router.get("/{note_id}", response_model=NoteRead)
async def get_note(note_id: uuid.UUID, service: NoteServiceDep) -> NoteRead:
    note = await service.get(note_id)
    return NoteRead.model_validate(note)


@router.patch("/{note_id}", response_model=NoteRead)
async def update_note(
    note_id: uuid.UUID,
    payload: NoteUpdate,
    service: NoteServiceDep,
) -> NoteRead:
    note = await service.update(note_id, payload)
    return NoteRead.model_validate(note)


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(note_id: uuid.UUID, service: NoteServiceDep) -> Response:
    await service.delete(note_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{note_id}/knowledge-bases", status_code=status.HTTP_204_NO_CONTENT)
async def add_note_to_knowledge_base(
    note_id: uuid.UUID,
    payload: NoteAddToKnowledgeBase,
    service: NoteServiceDep,
) -> Response:
    await service.add_to_kb(note_id, payload.knowledge_base_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
