"""文档摄取流水线：parse → parent 切分 → child 切分 → embed child → 落库 → 置态。

由后台 worker 调用；失败按 `retry_count` / `MAX_RETRIES` 排期重试或置 error。
"""

import random
import uuid
from datetime import UTC, datetime, timedelta

from app.agent.gateway import LLMGateway, run_budget
from app.agent.guardrail.document_guard import PoisonedDocumentError, guard_document_text
from app.agent.runtime.budget import BudgetTracker, DailyBudget
from app.chunking import ParentChunk, chunk_document, estimate_tokens
from app.core.storage import FileStore
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
        gateway: LLMGateway,
        file_store: FileStore,
        base_delay: timedelta = RETRY_BASE_DELAY,
        daily_budget: DailyBudget | None = None,
        warn_tokens: int = 100_000,
    ) -> None:
        self._repository = repository
        self._parser = parser
        self._gateway = gateway
        self._file_store = file_store
        self._base_delay = base_delay
        self._daily_budget = daily_budget
        self._warn_tokens = warn_tokens

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
            guard_document_text(parsed.markdown)  # 写库闸：投毒文档在分块/入库前拦截

            chunks = self._build_chunks(document, chunk_document(parsed.markdown))
            # 大文档警告：嵌入成本（child token 预估）超阈值且未获用户确认 → 置 needs_approval，
            # 本轮不嵌入、不落 chunk；确认后重跑会重新 delete + build + embed，无残留脏数据。
            child_tokens = sum(c.token_count or 0 for c in chunks if c.parent_id is not None)
            if child_tokens > self._warn_tokens and not document.embedding_approved:
                document.status = DocumentStatus.NEEDS_APPROVAL
                document.error_message = (
                    f"文档约 {child_tokens} token（嵌入预估），超过阈值 {self._warn_tokens}，"
                    "需确认后再嵌入"
                )
                document.next_retry_at = None
                await self._repository.update(document)
                return
            await self._embed_children(document.id, chunks)
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
        except PoisonedDocumentError as exc:
            await self._reject(document, exc)
        except Exception as exc:
            await self._fail(document, exc)

    async def _parse(self, document: Document) -> ParsedDocument:
        """把 Document 翻译成解析器入参：URL 直接透传 source_url；文件类型先从 FileStore 读字节。

        这里的 if 负责「准备输入」（读文件 vs 传 url），
        类型到窄解析器的路由由 DispatchDocumentParser 完成。
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

        parent 行不向量化（embedding=None），child 行以 parent_id 指向父行；
        chunk_index 各自层级内从 0 计。
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

    async def _embed_children(self, document_id: uuid.UUID, chunks: list[DocumentChunk]) -> None:
        """只向量化 child（parent 不向量化），按 EMBED_BATCH_SIZE 分批回填 embedding。

        经网关时以 ``ingest:{document_id}`` 为一个 run：每批 embed 都带 run context 记账/快照，
        崩溃重试重跑同一文档时命中快照、不重复调用（at-least-once）。
        """
        children = [chunk for chunk in chunks if chunk.parent_id is not None]
        if not children:
            return
        texts = [child.content for child in children]
        vectors: list[list[float]] = []
        tracker = BudgetTracker(None, sink=self._daily_budget)
        with run_budget(tracker, run_id=f"ingest:{document_id}"):
            for start in range(0, len(texts), EMBED_BATCH_SIZE):
                batch = texts[start : start + EMBED_BATCH_SIZE]
                vectors.extend(await self._gateway.embed("ingest", batch))
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

    async def _reject(self, document: Document, exc: PoisonedDocumentError) -> None:
        """写库闸命中：直接置 error，不重试（毒内容重试也不会变干净）。"""
        document.status = DocumentStatus.ERROR
        document.error_message = str(exc)
        document.next_retry_at = None
        await self._repository.update(document)

    async def close(self) -> None:
        """关闭仓库持有的数据库会话，归还连接池（worker 每文档开一次）。"""
        await self._repository.close()
