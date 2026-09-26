"""文档端点：上传（pdf/word）/ from-url / 详情 / 原文件 / 重试 / 删除。"""

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Response, UploadFile, status

from app.api.deps import DocumentServiceDep
from app.core.wake_events import document_wake_event
from app.models.document import DocumentStatus, DocumentType
from app.schemas.document import (
    DocumentApproveRequest,
    DocumentContentRead,
    DocumentCreateFromUrl,
    DocumentRead,
)

router = APIRouter(prefix="/documents", tags=["documents"])

PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@router.post("/from-url", response_model=DocumentRead, status_code=status.HTTP_201_CREATED)
async def create_document_from_url(
    payload: DocumentCreateFromUrl,
    service: DocumentServiceDep,
) -> DocumentRead:
    document = await service.create_from_url(payload)
    document_wake_event.set()
    return DocumentRead.model_validate(document)


@router.post("", response_model=DocumentRead, status_code=status.HTTP_201_CREATED)
async def create_document(
    file: Annotated[UploadFile, File()],
    kb_id: Annotated[uuid.UUID, Form()],
    service: DocumentServiceDep,
) -> DocumentRead:
    content = await file.read()
    document = await service.create_file(kb_id, content=content, filename=file.filename or "")
    document_wake_event.set()
    return DocumentRead.model_validate(document)


@router.get("/{document_id}", response_model=DocumentRead)
async def get_document(document_id: uuid.UUID, service: DocumentServiceDep) -> DocumentRead:
    document = await service.get(document_id)
    return DocumentRead.model_validate(document)


@router.get("/{document_id}/file")
async def get_document_file(document_id: uuid.UUID, service: DocumentServiceDep) -> Response:
    content, document = await service.get_file(document_id)
    if document.source_type == DocumentType.PDF:
        media_type = PDF_MEDIA_TYPE
        disposition = "inline"
    else:
        media_type = DOCX_MEDIA_TYPE
        disposition = "attachment"
    # 用原始文件名（title + 扩展名）而非写死的 document.pdf，方便用户区分；RFC 5987 编码非 ASCII
    filename = quote(document.filename, safe="")
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{filename}"},
    )


@router.get("/{document_id}/content", response_model=DocumentContentRead)
async def get_document_content(
    document_id: uuid.UUID, service: DocumentServiceDep
) -> DocumentContentRead:
    markdown = await service.get_content(document_id)
    return DocumentContentRead(markdown=markdown)


@router.post("/{document_id}/retry", response_model=DocumentRead)
async def retry_document(document_id: uuid.UUID, service: DocumentServiceDep) -> DocumentRead:
    document = await service.retry(document_id)
    return DocumentRead.model_validate(document)


@router.post("/{document_id}/approve", response_model=DocumentRead)
async def approve_document(
    document_id: uuid.UUID,
    payload: DocumentApproveRequest,
    service: DocumentServiceDep,
) -> DocumentRead:
    """大文档确认：approve=True 继续嵌入（回 PENDING 触发 worker），False 拒绝（置 error）。"""
    document = await service.approve(document_id, payload.approve)
    if document.status == DocumentStatus.PENDING:
        document_wake_event.set()
    return DocumentRead.model_validate(document)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: uuid.UUID, service: DocumentServiceDep) -> Response:
    await service.delete(document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
