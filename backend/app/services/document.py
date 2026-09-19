"""文档服务：上传 / from-url / 详情 / 删除 / 重试 / 按库列出。"""

import uuid
from collections.abc import Sequence
from pathlib import Path

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.storage import FileStore
from app.models.document import Document, DocumentStatus, DocumentType
from app.repositories.document import DocumentRepository
from app.repositories.knowledge_base import KnowledgeBaseRepository
from app.schemas.document import DocumentCreateFromUrl

MAX_FILE_SIZE = 20 * 1024 * 1024
ALLOWED_EXTENSIONS: dict[str, DocumentType] = {
    ".pdf": DocumentType.PDF,
    ".docx": DocumentType.WORD,
}
class DocumentService:
    def __init__(
        self,
        repository: DocumentRepository,
        kb_repository: KnowledgeBaseRepository,
        file_store: FileStore,
    ) -> None:
        self._repository = repository
        self._kb_repository = kb_repository
        self._file_store = file_store

    async def _require_kb(self, kb_id: uuid.UUID) -> None:
        if await self._kb_repository.get(kb_id) is None:
            raise NotFoundError("知识库不存在")

    async def create_file(
        self,
        kb_id: uuid.UUID,
        *,
        content: bytes,
        filename: str,
    ) -> Document:
        """校验知识库 + 扩展名/大小 → 落盘 → 建 pending 文档。"""
        await self._require_kb(kb_id)
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ValidationError("仅支持 PDF 或 Word(.docx) 文件")
        if len(content) > MAX_FILE_SIZE:
            raise ValidationError("文件大小不能超过 20MB")

        file_path = await self._file_store.save(content, ext.lstrip("."))
        document = Document(
            kb_id=kb_id,
            title=Path(filename).stem or filename,
            source_type=ALLOWED_EXTENSIONS[ext],
            file_path=file_path,
            status=DocumentStatus.PENDING,
            retry_count=0,
        )
        return await self._repository.add(document)

    async def create_from_url(self, payload: DocumentCreateFromUrl) -> Document:
        await self._require_kb(payload.knowledge_base_id)
        document = Document(
            kb_id=payload.knowledge_base_id,
            title=payload.url,
            source_type=DocumentType.URL,
            source_url=payload.url,
            status=DocumentStatus.PENDING,
            retry_count=0,
        )
        return await self._repository.add(document)

    async def get(self, document_id: uuid.UUID) -> Document:
        document = await self._repository.get(document_id)
        if document is None:
            raise NotFoundError("文档不存在")
        return document

    async def get_file(self, document_id: uuid.UUID) -> tuple[bytes, Document]:
        """读取原文件（仅 pdf/word；url 类型 409）。"""
        document = await self.get(document_id)
        if document.source_type == DocumentType.URL:
            raise ConflictError("URL 类型文档无本地文件")
        if not document.file_path:
            raise NotFoundError("文件不存在")
        return await self._file_store.open(document.file_path), document

    async def get_content(self, document_id: uuid.UUID) -> str:
        """返回解析后的 markdown 正文（仅供已完成文档阅读）。"""
        document = await self.get(document_id)
        if document.status != DocumentStatus.DONE:
            raise ConflictError("文档尚未解析完成")
        return document.content_markdown or ""

    async def delete(self, document_id: uuid.UUID) -> None:
        """删除文档：磁盘原文件需手动删；chunks 由 DB 外键 ON DELETE CASCADE 级联清理。"""
        document = await self.get(document_id)
        if document.file_path:
            await self._file_store.delete(document.file_path)
        await self._repository.delete(document)

    async def retry(self, document_id: uuid.UUID) -> Document:
        document = await self.get(document_id)
        if document.status != DocumentStatus.ERROR:
            raise ConflictError("仅失败状态的文档可重试")
        document.status = DocumentStatus.PENDING
        document.retry_count = 0
        document.next_retry_at = None
        document.error_message = None
        return await self._repository.update(document)

    async def list_by_kb(self, kb_id: uuid.UUID) -> Sequence[Document]:
        await self._require_kb(kb_id)
        return await self._repository.list_by_kb(kb_id)
