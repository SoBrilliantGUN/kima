"""文档摄取流水线：parse → parent 切分 → child 切分 → embed child → 落库 → 置态。

由后台 worker 调用；失败按 `retry_count` / `MAX_RETRIES` 排期重试或置 error。
"""

import random
import uuid
from datetime import UTC, datetime, timedelta

from app.chunking import ParentChunk, chunk_document, estimate_tokens
from app.core.storage import FileStore
from app.integrations.embedding import EmbeddingClient
from app.integrations.parser import DocumentParser, ParsedDocument, ParserError, SourceType
from app.models.document import MAX_RETRIES, Document, DocumentChunk, DocumentStatus, DocumentType
from app.repositories.document import DocumentRepository

EMBED_BATCH_SIZE = 32  # 向量化批大小：控制单次 embed API 调用的文本条数
RETRY_BASE_DELAY = timedelta(seconds=60)  # 重试退避基数：1min → 2min → 4min（指数）


class IngestService:
    """解析并归档单个文档：只向量化 child，parent 不向量化。"""

    def __init__(
        self,
        *,
        repository: DocumentRepository,
        parser: DocumentParser,
        embedder: EmbeddingClient,
        file_store: FileStore,
        base_delay: timedelta = RETRY_BASE_DELAY,
    ) -> None:
        self._repository = repository
        self._parser = parser
        self._embedder = embedder
        self._file_store = file_store
        self._base_delay = base_delay

    async def ingest(self, document_id: uuid.UUID) -> None:
        """解析并归档单个文档：解析 → 分块 → 向量化 → 落库 → 置 done；失败排期重试。

        开头先 delete_chunks 是为了重试时清掉上一轮的旧 chunk，避免残留脏数据。
        """
        document = await self._repository.get(document_id)
        if document is None:
            return
        try:
            await self._repository.delete_chunks(document_id)
            parsed = await self._parse(document)

            chunks = self._build_chunks(document, chunk_document(parsed.markdown))
            await self._embed_children(chunks)
            await self._repository.add_chunks(chunks)

            # 文档字段统一在 add_chunks 之后一次性改，交由 update 提交；
            # 若放在 add_chunks 之前，其内部 commit 会把内容字段顺带刷库（顺序耦合）。
            document.content_markdown = parsed.markdown
            if document.source_type == DocumentType.URL and parsed.title:
                document.title = parsed.title
            if parsed.metadata:
                document.doc_metadata = parsed.metadata
            document.status = DocumentStatus.DONE
            document.error_message = None
            document.next_retry_at = None
            await self._repository.update(document)
        except Exception as exc:
            await self._fail(document, exc)

    async def _parse(self, document: Document) -> ParsedDocument:
        """把 Document 翻译成解析器入参：URL 直接透传 source_url；文件类型先从 FileStore 读字节。

        这里的 if 负责「准备输入」（读文件 vs 传 url），类型到窄解析器的路由由 DispatchDocumentParser 完成。
        """
        if document.source_type == DocumentType.URL:
            return await self._parser.parse(source_type=SourceType.URL, url=document.source_url)
        if document.source_type in (DocumentType.PDF, DocumentType.WORD):
            if not document.file_path:
                raise ParserError("缺少文件路径")
            content = await self._file_store.open(document.file_path)
            return await self._parser.parse(
                source_type=SourceType(document.source_type.value),
                content=content,
                filename=document.filename,
            )
        raise ParserError(f"不支持的来源类型: {document.source_type}")

    def _build_chunks(self, document: Document, parents: list[ParentChunk]) -> list[DocumentChunk]:
        """把 chunk_document 的 ParentChunk 树拍平成 document_chunks 行（small-to-big）。

        parent 行不向量化（embedding=None），child 行以 parent_id 指向父行；chunk_index 各自层级内从 0 计。
        """
        rows: list[DocumentChunk] = []
        for parent_index, parent in enumerate(parents):
            parent_row = DocumentChunk(
                id=uuid.uuid4(),
                document_id=document.id,
                kb_id=document.kb_id,
                parent_id=None,
                chunk_index=parent_index,
                content=parent.content,
                doc_metadata={"heading_path": parent.heading_path} if parent.heading_path else None,
                token_count=estimate_tokens(parent.content),
                embedding=None,
            )
            rows.append(parent_row)
            for child_index, child in enumerate(parent.children):
                rows.append(
                    DocumentChunk(
                        id=uuid.uuid4(),
                        document_id=document.id,
                        kb_id=document.kb_id,
                        parent_id=parent_row.id,
                        chunk_index=child_index,
                        content=child.content,
                        doc_metadata=child.metadata,
                        token_count=estimate_tokens(child.content),
                        embedding=None,
                    )
                )
        return rows

    async def _embed_children(self, chunks: list[DocumentChunk]) -> None:
        """只向量化 child（parent 不向量化），按 EMBED_BATCH_SIZE 分批调 embedder 后回填 embedding。"""
        children = [chunk for chunk in chunks if chunk.parent_id is not None]
        if not children:
            return
        texts = [child.content for child in children]
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[start : start + EMBED_BATCH_SIZE]
            vectors.extend(await self._embedder.embed_documents(batch))
        for child, vector in zip(children, vectors, strict=True):
            child.embedding = vector

    async def _fail(self, document: Document, exc: Exception) -> None:
        """失败处理：retry_count 递增，超 MAX_RETRIES 置 error，否则按指数退避 + 抖动排期重试。"""
        document.retry_count += 1
        if document.retry_count > MAX_RETRIES:
            document.status = DocumentStatus.ERROR
            document.next_retry_at = None
        else:
            document.status = DocumentStatus.PENDING
            delay = self._base_delay * (2 ** (document.retry_count - 1))
            # full jitter：0 ~ delay 均匀抖动，避免批量失败同时重试（惊群）
            delay = timedelta(seconds=random.uniform(0, delay.total_seconds()))
            document.next_retry_at = datetime.now(UTC) + delay
        document.error_message = str(exc) or exc.__class__.__name__
        await self._repository.update(document)

    async def close(self) -> None:
        """关闭仓库持有的数据库会话，归还连接池（worker 每文档开一次）。"""
        await self._repository.close()
