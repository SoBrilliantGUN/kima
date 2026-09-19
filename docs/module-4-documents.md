# 模块 4：文档解析与归档 — 详细设计

> 日期：2026-09-14
> 状态：已实现
> 上游基线：`docs/requirements.md`（决策 #4/#6/#12/#14）· `docs/module-1-infrastructure.md`（DocumentParser / EmbeddingClient Protocol）· `docs/module-3-notes.md`（WebFetcher / ContentList / 知识库三栏）

本模块交付「**文档解析与归档**」完整能力：

- **三种来源**：PDF / Word / 网页(URL)，统一「上传 → 解析为 Markdown → 内容感知父子分块 → 向量化入库」
- **异步处理**：DB 轮询式 worker + 自建重试退避，状态落库、重启可恢复
- **父子切割（small-to-big）**：child 小块向量化做精确检索，parent 大块存上下文，命中 child 回 parent 出完整上下文
- **文档阅读器**：PDF 内嵌预览原文件；Word 与 URL 渲染解析出的 Markdown（可下载原文件 / 打开原网页）

**本模块同步回改模块 3**：彻底删除「网页笔记」——URL 统一归入文档，笔记收敛为纯 Markdown 空白笔记。

**明确后置（不在本模块）**：笔记向量化（笔记可变、需编辑重向量化 + 索引语义，随模块 5 RAG 消费方一起定）→ 模块 5；AI 问答 → 模块 5。

---

## 1. 目标与验收

目标：`上传 pdf/word 或贴 URL → 后台异步解析 → 内容感知父子分块 → 向量化入库 → 知识库内容列表可见 + 原文件可读` 全链路走通；worker 重启可恢复；失败可重试。

**验收标准（Definition of Done）**

| # | 验收项 |
|---|---|
| 1 | `alembic upgrade head`：新增 `documents` + `document_chunks`（自引用 `parent_id` + `embedding vector(1024)` + 部分 HNSW 索引）；`notes` 表 drop `type`/`summary`/`source_url` 三列 |
| 2 | `POST /api/documents`（文件 pdf/word）+ `POST /api/documents/from-url`（URL）→ 201 `DocumentRead(status=pending)`，文件落盘 / URL 入库 |
| 3 | worker 异步 `pending → processing → done/error`；`done` 时父子 chunk 已落库、child 已向量化（parent 不向量化），embedding 维度 = 1024 |
| 4 | 失败自动重试退避，超 `MAX_RETRIES` 置 `error`；`POST /{id}/retry` 手动重试；worker 重启能重新拾起 `pending` |
| 5 | `GET /api/knowledge-bases/{kb_id}/contents` 返回异构条目（`note` + `document`）；文档条目含状态/来源/错误信息 |
| 6 | 前端：上传/URL 入口（本地文档大拖拽批量窗 + URL 单一链接弹窗，二者分离）+ 列表（状态徽标 + 进度轮询 + 失败重试/删除）+ 浮动阅读窗口（PDF 内嵌 / Word 与 URL 渲染解析 markdown，可拖拽/调整大小/关闭，多开去重聚焦） |
| 7 | 三种来源解析产出有效 markdown；分块走内容感知 3 splitter（markdown-it-py AST）+ 父子切割（small-to-big） |
| 8 | 后端 `ruff` + `mypy(strict)` + `pytest` 全绿；前端 `eslint` + `tsc --noEmit` + `vite build` 全绿；测试不起真库/真网/真 MinerU |

---

## 2. 处理模型：DB 轮询式 worker（核心）

### 2.1 状态机

```
pending ──worker 拾起──▶ processing ──成功──▶ done
   ▲                        │
   │                    失败(可重试)──▶ 排期重试(仍 pending, retry_count+1, next_retry_at)
   │                        │
   └── POST /retry 重置─────┴── 超过 MAX_RETRIES ──▶ error(终态)
```

- **重试退避**：`next_retry_at = now + base_delay * 2^(retry_count-1)`；超过 `MAX_RETRIES` → `error`（终态，靠手动 `POST /retry` 唤醒）。
- **恢复性**：状态全程落库；`processing` 中「卡死」的行（超时兜底）会被重新判为可重试，进程重启后 `pending` 行可被重新拾起。

### 2.2 worker 实现（`app/workers/document_worker.py`）

- `main.py` lifespan 里 `asyncio.create_task(run_worker())`，循环：
  1. 扫 `documents` 里 `status=pending AND next_retry_at<=now`（`FOR UPDATE SKIP LOCKED`，单进程但写法正确、可平滑扩展多实例）
  2. 置 `processing` → 解析 → 分块 → 向量化 → `done` / 失败判定重试
  3. `asyncio.sleep(轮询间隔)`；lifespan shutdown 时 `cancel` 优雅退出
- **并发**：`asyncio.Semaphore(2)` 限制同时处理数（MinerU 免费额度友好 + 不自我打爆）。
- **超时兜底**：`processing` 状态超过阈值（如 30min）视为卡死，重置回 `pending` 并计入 `retry_count`。

---

## 3. 数据模型

### 3.1 `documents` 表（迁移 `0004_documents`，`down_revision="0003_notes"`）

**`documents`**（知识库内文档，`kb_id` 必填——与笔记「全局」相反）

| 字段 | 类型 | 约束 / 默认 |
|---|---|---|
| `id` | `UUID`（PG native） | PK，app 端 `uuid.uuid4` |
| `kb_id` | `UUID` | FK → `knowledge_bases.id ON DELETE CASCADE`，非空 |
| `title` | `String(255)` | 非空（pdf/word=文件名去扩展名；url 初始=URL，done 后=og:title） |
| `source_type` | `Enum(DocumentType)` `native_enum=False` 存 VARCHAR(16) | 非空，`pdf`/`word`/`url` |
| `source_url` | `Text` | 可空（仅 url 类型，点击打开原网页） |
| `file_path` | `Text` | 可空（仅 pdf/word，本地相对路径） |
| `status` | `Enum(DocumentStatus)` `native_enum=False` 存 VARCHAR(16) | 非空，`pending`/`processing`/`done`/`error`，默认 `pending` |
| `content_markdown` | `Text` | 可空（解析产物，供 RAG 分块向量化 + word/url 文档阅读，经 `/content` 端点下发） |
| `metadata` | `JSONB` | 可空（`file_name`/`mime_type`/`page_count` 等解析器回传） |
| `retry_count` | `Integer` | 非空，默认 `0`（重试上限为常量 `MAX_RETRIES=3`，非列） |
| `error_message` | `Text` | 可空（失败原因，前端展示） |
| `next_retry_at` | `DateTime(timezone)` | 可空（排期重试） |
| `created_at` / `updated_at` | `DateTime(timezone)` | 继承 `TimestampMixin` |

### 3.2 `document_chunks` 表（父子切割，small-to-big）

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | `UUID` | PK，app 端 `uuid.uuid4` |
| `document_id` | `UUID` | FK → `documents.id ON DELETE CASCADE` |
| `kb_id` | `UUID` | FK → `knowledge_bases.id ON DELETE CASCADE`（冗余，加速按库过滤） |
| `parent_id` | `UUID` | **自引用 FK → `document_chunks.id`**，可空：`NULL`=parent 行，非空=child 行 |
| `chunk_index` | `Integer` | 块序（parent 内 child 顺序） |
| `content` | `Text` | 非空（parent=整节上下文；child=细粒度语义块） |
| `metadata` | `JSONB` | 可空（`heading_path`/`block_type`，供 RAG 上下文与调试） |
| `token_count` | `Integer` | 可空（估算） |
| `embedding` | `Vector(1024)` | **可空**：child 有向量，parent 为 NULL |

**两级语义（small-to-big）**：

- `parent` = 大块上下文（标题节，目标 ~1000–2000 token，**不向量化**）。
- `child` = 细粒度检索单元（3 splitter 产物，目标 ~300–500 token，**向量化**；标题亦作为可检索 child 落地）。
- 检索（模块 5）：query 向量 → 命中 child → 回 `parent_id` 取 parent 全文做上下文，解决「小 chunk 精确检索、大 chunk 完整上下文」。
- `parent_id` 的 NULL / 非空 即区分 parent / child，无需额外 level 列（固定两级）。

### 3.3 ORM 模型（`models/document.py`）

```python
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.models.base import Base, TimestampMixin

# embedding 维度唯一真源 = settings.embedding_dim（默认 1024，bge-m3）
EMBEDDING_DIM = get_settings().embedding_dim

# 重试上限：worker 级全局策略，不随文档而异，故为常量而非列。
MAX_RETRIES = 3


class DocumentType(StrEnum):
    PDF = "pdf"
    WORD = "word"
    URL = "url"


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    ERROR = "error"


class Document(Base, TimestampMixin):
    """知识库内文档（kb_id 必填，与笔记「全局」相反）。"""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[DocumentType] = mapped_column(
        Enum(DocumentType, native_enum=False, length=16), nullable=False
    )
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, native_enum=False, length=16),
        nullable=False,
        default=DocumentStatus.PENDING,
    )
    content_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    # `metadata` 是 SQLAlchemy 保留名，列名映射为 "metadata"、属性名用 doc_metadata
    doc_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentChunk(Base):
    """父子切割（small-to-big）：单表自引用 parent_id，parent 不向量化、child 向量化。"""

    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    doc_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
```

- 两个 enum 均 `native_enum=False` 存 VARCHAR（对齐模块 3 决策，免原生 PG enum 的迁移增删值成本）。
- `embedding` 维度从 `settings.embedding_dim` 读（唯一真源），模型层用常量 `EMBEDDING_DIM` 承接；迁移层冻结默认值 1024，维度漂移由 embed 时断言兜底。
- `max_retries` 不落库：重试上限是 worker 级全局策略、不随文档而异，故为常量 `MAX_RETRIES=3`（与 `retry_count` 列在判定处比较）。
- `metadata` 是 SQLAlchemy 保留名，两模型列名仍映射为 `"metadata"`、属性名用 `doc_metadata`（避开 `Base.metadata`）。
- `parent_id` 自引用 FK `ondelete=CASCADE`：删 parent → 清其 children；删 document → 清全部分块。
- `Document.filename` 派生属性：`title + 扩展名`（`DOCUMENT_EXTENSIONS` 映射，注意 WORD 扩展名是 `docx` 而非 `source_type.value` 的 `word`），供下载文件名与 MinerU 上传 name 使用。

### 3.4 迁移 `0004_documents`（手写）

- **回改 notes**：`op.drop_column("notes", "type")` / `"summary"` / `"source_url"`（彻底删除网页笔记的 url 专属字段；dev 库已有 url 笔记一并丢弃，写进迁移说明）。
- **建表**：`op.create_table("documents", …)` → `op.create_table("document_chunks", …)`，FK 名走 naming convention（`fk_documents_kb_id_knowledge_bases`、`fk_document_chunks_parent_id_document_chunks` 等）。
- **部分 HNSW 索引**（只索引 child，即 `embedding IS NOT NULL`）：

```python
op.execute(
    "CREATE INDEX ix_document_chunks_embedding_hnsw "
    "ON document_chunks USING hnsw (embedding vector_cosine_ops) "
    "WHERE embedding IS NOT NULL"
)
```

- `CREATE EXTENSION vector` 已在模块 1 基线落好，本迁移不重复建。
- `downgrade`：先 `drop_table("document_chunks")` → `drop_table("documents")` → 回加 notes 三列（`type`/`summary`/`source_url`，url 数据不可逆丢失，注明）。

---

## 4. 解析层：按类型分发（`integrations/parser.py` 扩展）

模块 1 的 `DocumentParser` Protocol + `SourceType` 已定形，本模块落三个真实实现，工厂按 `source_type` 分发：

```
PDF  → MinerUDocumentParser   （托管 API v4：申请上传 → PUT → 轮询 → 下载 zip → markdown）
WORD → WordDocumentParser     （mammoth → HTML → markdownify → markdown，保留 GFM 表格）
URL  → WebDocumentParser      （复用 WebFetcher：trafilatura → Playwright 回退 → markdown + og:title）
```

### 4.1 `MinerUDocumentParser`（PDF）

- 封装 mineru.net 官方云 API v4（`MINERU_API_BASE_URL` 默认 `https://mineru.net` + `MINERU_API_TOKEN` 必配），四步：
  1. `POST /api/v4/file-urls/batch` 申请上传链接 → 得 `batch_id` + `file_urls`（预签名地址）
  2. `PUT` 文件字节到 `file_urls`
  3. 轮询 `GET /api/v4/extract-results/batch/{batch_id}` 至 `state=done`，得 `full_zip_url`
  4. 下载 zip、解出 markdown
- 超时/额度/5xx → 抛可重试异常（由 worker 判重试）。
- `content_list` / 页数等元数据暂不提取（留模块 5 RAG 消费时再取 zip 内 `content_list.json`）。

### 4.2 `WordDocumentParser`（Word）

- `.docx` → `mammoth.convert_to_html`（得到含 `<table>` 的 HTML）→ `markdownify`（HTML→markdown，保留 GFM 表格）。mammoth 的 markdown 输出不支持表格（会丢弃表格结构），故必须经 HTML 中转；`.doc`（老二进制格式）**不在范围**（上传校验拒绝，422）。
- CPU 同步转换用 `asyncio.to_thread` 包一层（对齐模块 3 trafilatura 处理）。

### 4.3 `WebDocumentParser`（URL）

- 复用模块 3 的 `WebFetcher`（`FallbackWebFetcher`：先静态 trafilatura、空正文回退 Playwright），产出 `FetchedPage(markdown, title, source_url)`；trafilatura 抽取时 `with_metadata=False`，正文不带 YAML 元数据头（标题另经 `extract_title` 取）。
- 标题取 `og:title`/`<title>`（worker 完成后回写 `documents.title`）。
- **不生成摘要**（阅读器直接渲染解析正文，摘要无用武之地）。

### 4.4 工厂分发

- `get_document_parser(settings)` 返回 `DispatchDocumentParser`（满足 `DocumentParser` 分发边界协议）：按 `source_type` 路由到 pdf/word/web 三个窄解析器——pdf 满足 `PdfParser`、word 满足 `WordParser`、web 满足 `UrlParser`，业务层无感。`parse` 透传可选 `filename`（原始文件名），仅 PDF（MinerU 上传 name）使用。

---

## 5. 分块层：内容感知 + 父子切割（新包 `app/chunking/`）

这是模块 4 的技术亮点，单独成包、纯函数、强单测。语义块识别基于 **markdown-it-py AST**（不再手写
行首正则），天然正确处理嵌套列表 / GFM 表格 / 代码围栏 / 引用 / Setext 标题，并按「重复出现的最浅
标题层级」切父级节，避免单标题文档塌成一个 parent。

```
app/chunking/
├── base.py          # Chunk/ParentChunk dataclass + token 估算 + 递归切分（含相邻块重叠）
├── block.py         # markdown-it-py AST → 语义块（标题/段落/代码围栏/表格/列表/引用）+ 父级切分
├── registry.py      # 按「块类型」匹配 splitter；无匹配 → 递归兜底；标题即 child
└── splitters/
    ├── recursive.py   # 结构化递归（段落→行→句→词；超长硬切 + 相邻块重叠）——默认兜底
    ├── table.py       # 整表保留；超长按行切 + 重复表头
    └── code_ast.py    # 代码块按函数/类边界；无 AST 语言降级按行
```

### 5.1 管线（两级）

```
markdown
  ├─ ① 父级切分（section segmentation）: 按标题层级切成「节」= parent；无标题文档退化为递归切分(~1500 token)
  │     过小节合并、超大节拆分，目标 ~1000–2000 token/parent
  └─ ② 子级切分（content-aware，3 splitter 逐节处理）: 产出 child(~300–500 token)
        超长块落回「递归字符硬切 + 重叠」兜底
```

- **父级切分**：按「重复出现的最浅标题层级」聚成 parent（单标题文档不塌成一个 parent）；无标题的扁平文档回退递归切分分段。
- **子级切分**：每节内按 `block.py` 识别的异质块（段落/表格/代码/列表/引用），经 `registry` 匹配对应 splitter 产出 child；标题块本身作为 child 落地（可检索）。
- **重叠**：相邻合并块之间保留 ~50 token 重叠（句子边界对齐）；超长不可拆片段落回「递归字符硬切 + 重叠」兜底。
- **元数据**：child 的 `metadata` 带 `block_type` + `heading_path`（所属标题路径），供模块 5 检索时拼上下文；parent 带 `heading_path`。

### 5.2 3 个 splitter

| splitter | 处理对象 | 切分方式 |
|---|---|---|
| `recursive.py` | 标题 + 普通段落/列表/引用 | 段落 → 行 → 句 → 词；超长硬切 + 相邻块重叠（默认兜底） |
| `table.py` | Markdown 表格 | 整表保留；超长按行切 + 重复表头 |
| `code_ast.py` | 代码块 | 按函数/类边界；无 AST 支持的语言降级按行 |

---

## 6. 向量化层（`integrations/embedding.py` 扩展）

- 模块 1 已有 `EmbeddingClient` Protocol + `FakeEmbeddingClient`。本模块补真实 `SiliconFlowEmbeddingClient`（OpenAI 兼容 `/embeddings`，model=bge-m3）。
- `get_embedding_client(settings)` 注册 `siliconflow` provider（`fake` 仍默认，配置 `.env` 后真实生效）。
- **只向量化 child**：chunks 按 `batch_size=32` 调 `embed_documents`；单批失败重试一次，仍失败 → 文档 `error`（worker 重试）。
- 维度断言复用模块 1（返回前 `len(vec) == dimension`，防配置与实现漂移）。

---

## 7. 文件存储（`FileStore` 抽象）

- 本地磁盘 `backend/storage/documents/`（`.gitignore`），`documents.file_path` 存相对路径（如 `{document_id}.pdf`）。
- `FileStore` Protocol：`save(bytes, ext) -> path` / `open(path) -> bytes` / `delete(path)`；本地实现 + 测试用 `FakeFileStore`。
- URL 类型**无本地文件**，只存 `source_url`（即「原文件」指针）。
- 留扩展点：后续换对象存储只改 `FileStore` 实现，业务层不动。

---

## 8. 后端分层与接口

沿 `router → service → repository` 三层（对齐模块 2/3），依赖注入走 `api/deps.py`。

```
models/document.py            # Document / DocumentChunk / DocumentType / DocumentStatus
models/note.py                # 回改：Note 去掉 type/summary/source_url
schemas/document.py           # DocumentCreateFile / DocumentCreateFromUrl / DocumentRead
schemas/note.py               # 回改：NoteRead 去掉 type/summary/source_url
repositories/document.py      # DocumentRepository(Protocol) + SqlAlchemy 版（含父子 chunk 批量写入）
services/document.py          # DocumentService（上传/from-url/详情/删除/重试）
services/ingest.py            # ingest 流水线：parse → parent 切分 → child 切分 → embed child → 落库 → 置态
workers/document_worker.py    # 后台轮询循环
api/routes/documents.py       # 文档端点
api/routes/notes.py           # 回改：删 from-url 端点
api/routes/knowledge_bases.py # ContentItem 增 type="document"
integrations/parser.py        # + MinerU/Word/Web 实现 + 分发工厂
integrations/embedding.py     # + SiliconFlow 实现
chunking/                     # 分块包（§5）
core/storage.py               # FileStore Protocol + 本地实现
```

### 8.1 Repository（`repositories/document.py`）

```python
class DocumentRepository(Protocol):
    async def add(self, doc: Document) -> Document: ...
    async def get(self, doc_id: uuid.UUID) -> Document | None: ...
    async def update(self, doc: Document) -> Document: ...
    async def delete(self, doc: Document) -> None: ...
    async def claim_pending(self, limit: int) -> list[Document]: ...  # worker 用，SKIP LOCKED
    async def add_chunks(self, chunks: list[DocumentChunk]) -> None: ...  # 父子批量写入
    async def delete_chunks(self, doc_id: uuid.UUID) -> None: ...  # 重试前清理旧 chunk
```

- `claim_pending`：`select ... where status=pending and (next_retry_at is null or next_retry_at<=now) order by created_at for update skip locked limit N`，返回后由 worker 置 `processing`。
- `add_chunks`：`session.add_all(chunks)` 批量提交（parent 行 + child 行一次写入）。

### 8.2 Service（`services/document.py` + `services/ingest.py`）

**`DocumentService`**

| 方法 | 规则 |
|---|---|
| `create_file(kb_id, file, filename)` | 先 `kb_repo.get` 校验知识库存在（404）→ 校验扩展名/`mime_type` ∈ pdf/docx、大小 ≤20MB（否则 422）→ `FileStore.save` 落盘 → 建 `Document(source_type=pdf/word, title=文件名去扩展名, status=pending)` → `repo.add` |
| `create_from_url(kb_id, url)` | 先 `kb_repo.get` 校验（404）→ URL 校验 http/https 非空（否则 422）→ 建 `Document(source_type=url, source_url=url, title=url, status=pending)` → `repo.add` |
| `get(doc_id)` | 查无抛 `NotFoundError("文档不存在")` |
| `delete(doc_id)` | 先 `get`（404）→ 删文件（pdf/word 时 `FileStore.delete`）→ `repo.delete`（chunk 随 CASCADE 清） |
| `retry(doc_id)` | 先 `get`（404）→ 仅 `error` 态可重试（否则 409）→ `status=pending`、`retry_count=0`、`next_retry_at=NULL`、`error_message=NULL` → `repo.update` |
| `get_content(doc_id)` | 先 `get`（404）→ 仅 `done` 态可读（否则 409）→ 返回 `content_markdown`（供阅读器渲染） |

**`ingest.py`（worker 调用）**

```python
async def ingest(document_id: uuid.UUID) -> None:
    # 1. 置 processing
    # 2. parse(source_type, content/url) -> markdown
    # 3. content_markdown 落库；url 时回写 title=og:title
    # 4. parent 切分 -> [section]
    # 5. 逐节 child 切分 -> [(parent, [child])]
    # 6. embed child 批量 -> vectors
    # 7. repo.add_chunks(全部 parent+child)
    # 8. 置 done
    # 失败：判定可重试 → 排期重试 / 超 MAX_RETRIES → error + error_message
```

### 8.3 Router（`api/routes/documents.py`，`prefix="/documents"`）

| 方法 | 路径 | 成功 | 失败 |
|---|---|---|---|
| POST | `/api/documents`（multipart: `file` + `kb_id`，pdf/word） | 201 `DocumentRead` | 422（类型/超限）· 404（kb 不存在） |
| POST | `/api/documents/from-url`（json: `url` + `kb_id`） | 201 `DocumentRead` | 422（URL 非法）· 404 |
| GET | `/api/documents/{document_id}` | 200 `DocumentRead` | 404 |
| GET | `/api/documents/{document_id}/file` | 200 原文件流（仅 pdf/word） | 404 / 409（url 类型） |
| GET | `/api/documents/{document_id}/content` | 200 `DocumentContentRead{markdown}`（word/url 阅读正文） | 404 / 409（非 done） |
| POST | `/api/documents/{document_id}/retry` | 200 `DocumentRead` | 404 / 409（非 error） |
| DELETE | `/api/documents/{document_id}` | 204 | 404 |

- `GET /file`：pdf 用 `Content-Disposition: inline`（供 `<iframe>` 内嵌），word 用 `attachment`（触发下载）；文件名用原始 `title + 扩展名`（`Document.filename`），RFC 5987 编码非 ASCII。

### 8.4 知识库内容列表（`api/routes/knowledge_bases.py` 扩展）

- `GET /api/knowledge-bases/{kb_id}/contents` 的 `ContentList.items` 增 `type="document"` 条目（`ContentItem.type ∈ {"note", "document"}`），模块 3 已预留、本模块收口。
- 不单开 documents 列表端点（对齐模块 3 决策 #13）。

### 8.5 Schema（`schemas/document.py` + 回改 `schemas/note.py`）

```python
class DocumentCreateFromUrl(BaseModel):
    url: str
    knowledge_base_id: uuid.UUID
    # field_validator：url strip 后非空；urlparse 校验 scheme ∈ {http, https}

class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kb_id: uuid.UUID
    title: str
    source_type: DocumentType
    source_url: str | None
    status: DocumentStatus
    error_message: str | None
    metadata: dict | None
    created_at: datetime
    updated_at: datetime
    # 不含 content_markdown / file_path（正文走 /content 端点，文件走 /file 端点）


class DocumentContentRead(BaseModel):
    markdown: str  # 解析后的正文（word/url 阅读器渲染）
```

```python
# schemas/knowledge_base.py 扩展
class ContentItem(BaseModel):
    type: Literal["note", "document"]
    note: NoteRead | None = None
    document: DocumentRead | None = None
```

- 回改 `NoteRead`：去掉 `type`/`summary`/`source_url` 三字段。
- `DocumentRead` 不含正文：列表/轮询 body 保持轻量；正文经 `/content`、原文件经 `/file`、URL 原文经 `source_url` 直达。

---

## 9. 前端

### 9.1 文件清单

```
api/types.ts              # Document / DocumentRead / ContentItem(加 document)
api/documents.ts          # uploadDocument / createDocumentFromUrl / getDocument / retryDocument / deleteDocument / documentFileUrl
api/notes.ts             # 回改：删 createNoteFromUrl
hooks/useDocuments.ts     # react-query hooks + 状态轮询（pending/processing 时 refetchInterval）
hooks/useDocumentWindows.ts # 浮动窗口状态：open/close/focus + 同文档去重聚焦 + 切库清空
pages/Notes/
  index.tsx               # 回改：删「新建→网页」入口
  components/
    NoteEditorPane.tsx    # 回改：删 url 摘要块（所有笔记纯 markdown）
    (删 WebNoteFormModal.tsx)
pages/KnowledgeBasePage/
  components/
    ContentListPane.tsx   # document 条目：状态徽标 + 进度 + 失败重试/删除 + 点击开浮动窗口
    AddContentMenu.tsx    # 回改：删「笔记」项，仅剩「本地文档」/「URL 文档」
    DocumentDropzone/     # 本地文档上传：大拖拽窗，批量多文件 + 逐文件进度
    UrlInputModal/        # URL 文档：单一链接输入弹窗
    DocumentWindow/       # 浮动文档阅读窗口（拖拽 / 调整大小 / 关闭）
    (删 DocumentUploadModal.tsx / DocumentReaderPane.tsx)
router.tsx                # 移除 /documents/:documentId 路由（浮动窗口纯内存，不走 URL）
```

### 9.2 交互与阅读器

- **入口**：知识库「添加内容」下拉两项——
  - 「本地文档」→ `DocumentDropzone`（大拖拽窗口，支持批量拖入/多选 pdf/word，逐文件上传并显示进度，全部完成仅刷新列表、不自动打开）；
  - 「URL 文档」→ `UrlInputModal`（单一 http/https 链接输入框，确定后建文档、仅刷新列表）。
- **列表**：document 条目显示标题 + `source_type` 图标 + 状态徽标（`pending`/`processing` 转圈、`done` 正常、`error` 红）；`error` 悬浮可重试/删除；点击打开浮动阅读窗口。
- **进度轮询**：`useDocument(id)` 在 `status ∈ {pending, processing}` 时 `refetchInterval` 轮询，`done/error` 停。
- **浮动阅读窗口（`DocumentWindow`）**：点击文档在页面上层打开可拖拽、可调整大小、可关闭的窗口；同一文档去重、重复点击聚焦已有窗口；可同时开多个；纯内存态，切换知识库或刷新即清空。窗口内容按 source_type 渲染：
  - `pdf`：`<iframe src={documentFileUrl(id)}>` 浏览器原生内嵌预览（不引 PDF.js，最简）+ header「下载原文件」按钮；
  - `word`：渲染解析出的 Markdown（`GET /content` → `react-markdown` + `remark-gfm`）+ header「下载原文件」按钮；
  - `url`：渲染解析出的 Markdown（同上）+ header「打开原网页」按钮（新标签页打开 `source_url`）。
- **问答面板常驻**：右侧 `QaPanel` 始终可见、针对整个知识库提问；浮动文档窗口仅作阅读参考，不切换问答范围。
- **删除**：二次确认 → `deleteDocument` → `invalidateQueries(['knowledge-bases'])`；若该文档窗口正打开则一并关闭。

### 9.3 前端依赖

- 本模块新增前端依赖 `react-markdown` + `remark-gfm`（word/url 的 markdown 阅读器，原生支持 GFM 表格/图片/代码）；PDF 内嵌仍用浏览器原生 `<iframe>`，不引 PDF.js。

---

## 10. 测试策略（少而深，不起真库/真网/真 MinerU）

| 层 | 测试 | 方式 |
|---|---|---|
| 分块单元 | `test_chunking.py` | 3 splitter 各自边界（递归句子切分 + 相邻块重叠 + 超长硬切、表格整表/按行+重复表头、代码函数边界/降级按行）+ AST 语义块（标题/段落/表格/代码/列表/引用 + heading_path）+ parent 切分（标题节/单标题不塌/无标题回退/合并/拆分）+ registry 兜底 |
| ingest 单元 | `test_ingest.py` | 注入 Fake parser/embedding/chunker：done 落父子 chunk（child 有向量、parent NULL）/ parse 失败→error / embed 失败→error / 重试排期正确 |
| service 单元 | `test_services_document.py` | 注入 Fake repo/parser/file_store：上传建 pending、from-url、详情/删除/重试、kb 不存在 404、类型不支持 422、非 error 态重试 409 |
| API 集成 | `test_api_documents.py` | `dependency_overrides` 换 Fake，走 6 端点 + contents 含 document 条目 |
| worker 测试 | `test_worker.py` | 可控 Fake 驱动一轮循环：拾取 pending、done/error 流转、`next_retry_at` 过滤、processing 超时兜底 |
| 回改回归 | `test_notes.py` | notes 无 url 路径、NoteRead 无 type/summary/source_url、from-url 端点移除 |
| 集成 Fake | `tests/fakes.py` 增补 | `FakeDocumentRepository` / `FakeDocumentParser`（假分发器）/ `FakeFileParser` / `FakeUrlParser`（窄解析器替身）/ `FakeFileStore` / `FakeEmbeddingClient` |

- 关键用例：pdf 上传→pending→worker done 后 contents 含 document、child 已向量化；word 上传→mammoth 解析；from-url→WebFetcher 抓取→done 后 title=og:title；解析失败→重试→超限→error→retry 重置；删除→文件与 chunk 皆清。

---

## 11. 实现顺序（task 列表）

| 步骤 | 内容 | 产出 |
|---|---|---|
| T1 | `pyproject.toml` 加 `python-multipart`、`pgvector`、`mammoth`（+ 视需 `tree-sitter`）；`httpx` 移入运行时依赖 | 依赖可装 |
| T2 | `models/document.py` + 迁移 `0004`（建 documents/document_chunks + drop notes 三列 + 部分 HNSW） | 可 `upgrade head` |
| T3 | `app/chunking/` 包：markdown-it-py AST 切分 + registry + 3 splitter + parent 切分 + 单测 | 分块能力（可独立验收） |
| T4 | `integrations/parser.py`（MinerU/Word/Web 三实现 + 分发工厂）+ `integrations/embedding.py`（SiliconFlow） | 解析/向量化真实 provider |
| T5 | `core/storage.py`（FileStore）+ `services/ingest.py`（parent→child→embed 流水线） | 核心业务逻辑 |
| T6 | `repositories/document.py` + `schemas/document.py` | 数据访问/校验层 |
| T7 | `services/document.py` + `api/routes/documents.py` + contents 扩展 + deps 注入 | 接口层 |
| T8 | `workers/document_worker.py` + lifespan 接入 + 重试退避 | 后台异步 |
| T9 | 回改模块 3：notes 删三列/删 from-url/删 WebNoteFormModal/删「新建→网页」/删 url 摘要块 | 网页笔记移除 |
| T10 | 后端测试（fakes + 分块 + ingest + service + API + worker + notes 回归） | pytest 全绿 |
| T11 | 前端数据层 `types.ts`/`documents.ts`/`useDocuments.ts` | 数据层 |
| T12 | 前端 `DocumentDropzone` + `UrlInputModal` + `ContentListPane` 状态/进度/删除 | 上传闭环 |
| T13 | 前端 `DocumentWindow`（浮动窗口，PDF 内嵌 / Word 与 URL markdown 阅读）+ `useDocumentWindows` | 阅读闭环 |
| T14 | 全量质量门禁 + 手工验收 | ruff/mypy/pytest/eslint/tsc/build 全绿 |

---

## 12. 已定决策

1. **异步处理**：DB 轮询式 worker + 自建重试退避（`retry_count`/`next_retry_at` + 常量 `MAX_RETRIES`），状态落库、重启可恢复，不引 Redis/队列。
2. **来源路由按类型分发**：PDF→MinerU、Word→mammoth+markdownify、URL→复用 WebFetcher；pdf/word/web 分别满足 `PdfParser`/`WordParser`/`UrlParser` 窄协议，对外统一由 `DispatchDocumentParser` 按 `source_type` 分发。
3. **URL 只作为文档**：放弃网页笔记；`documents.source_type = pdf/word/url`；解析出 markdown 供阅读器渲染，也可点击打开原网页核对最新内容。
4. **彻底删除网页笔记**：`notes` 删 `type`/`summary`/`source_url` 三列，删 `from-url` 端点、`WebNoteFormModal`、「新建→网页」入口、url 摘要块；`WebFetcher` 保留给 URL 文档复用。
5. **父子切割（small-to-big）**：单表 `document_chunks` 自引用 `parent_id`；parent 大块存上下文不向量化，child 小块向量化；检索命中 child 回 parent 出上下文。
6. **内容感知分块**：markdown-it-py AST 识别异质块 + 3 个可插拔 splitter（结构化递归=兜底 + 表格 + 代码），标题作为可检索 child 落地、相邻块重叠 ~50 token，独立 `app/chunking/` 包。
7. **文档阅读器**：PDF 内嵌预览原文件（浏览器原生 `<iframe>`）；Word 与 URL 渲染解析出的 Markdown（`react-markdown` + `remark-gfm`，支持 GFM 表格），并提供「下载原文件」（pdf/word）或「打开原网页」（url）按钮。
8. **笔记向量化留模块 5**：笔记可变需编辑重向量化 + 索引语义，随 RAG 消费方一起定。
9. **文件存储本地磁盘**：`backend/storage/documents/`（gitignore），`FileStore` 抽象留对象存储扩展点；URL 无本地文件只存 `source_url`。
10. **正文与元数据分离**：`DocumentRead` 不含 `content_markdown`/`file_path`；正文经 `/content` 端点下发（阅读器渲染），原文件经 `/file` 端点流式下发。
11. **文档列表并入知识库 contents**：不单开 documents 列表端点，`ContentItem` 增 `document` 条目（对齐模块 3 决策 #13）。
12. **Word 仅 `.docx`**：`.doc` 老格式上传校验拒绝（422）。
13. **URL 文档不生成摘要**：阅读器直接渲染解析正文，列表只显示标题（og:title）。
14. **文档阅读 = 浮动窗口（非路由面板）**：点文档在页面上层打开可拖拽/调整大小/关闭的浮动窗口，可同时开多个、同文档去重聚焦；纯内存态（去掉 `/knowledge-bases/:id/documents/:documentId` 路由、不持久化，刷新即消失）；右侧 `QaPanel` 常驻并针对整个知识库，文档窗口仅阅读参考。此为「选中走 URL」（决策 #9）在文档阅读场景的例外：知识库/笔记选中仍走 URL，文档阅读改内存浮动窗口。
15. **上传入口拆分 + 去笔记**：`DocumentUploadModal` 拆为 `DocumentDropzone`（本地文档，大拖拽窗 + 批量多文件 + 逐文件进度）与 `UrlInputModal`（URL 文档，单一链接输入）两个独立组件；「添加内容」菜单去掉「笔记」项（笔记仅不再从知识库新建，列表已有笔记保留）。
