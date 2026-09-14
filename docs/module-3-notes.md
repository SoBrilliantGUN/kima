# 模块 3：笔记/编辑器 — 详细设计

> 日期：2026-09-14（在「网页笔记 slice」基础上补全为完整模块 3）
> 状态：已评审定稿，可直接开工
> 上游基线：`docs/requirements.md`（决策 #15/#16/#18/#19）· `docs/module-2-knowledge-bases.md`

> ⚠️ **模块 4 修订（2026-09-14）**：本模块的「网页笔记」能力已在模块 4 被推翻——URL 统一归入文档（`documents.source_type=url`），不再有网页笔记；`notes` 表的 `type`/`summary`/`source_url` 三列、`POST /notes/from-url` 端点、`WebNoteFormModal`、「新建→网页」入口、url 摘要块均在模块 4 删除，笔记收敛为纯 Markdown 空白笔记。`WebFetcher` 保留给 URL 文档复用。阅读本模块时以 `docs/module-4-documents.md` §12 为准。

本模块交付「**笔记**」完整能力：

- **网页笔记**：`URL → 抓取 → 提取 Markdown 正文 → DeepSeek 摘要 → 存为全局笔记`
- **空白笔记**（`type=markdown`）：新建、默认标题「无标题笔记」
- **编辑器**：TipTap 所见即所得 + Markdown 快捷输入，**打开即编辑**（顶部工具栏 + 标题 + 写作区，自动保存）
- **关联与闭环**：添加到知识库 + 知识库内容列表展示关联笔记 + 知识库内「添加内容」新建笔记

核心数据关系（推翻了模块 2 §8 决策 #11「文档=笔记统一进库」）：**笔记是全局内容**（不挂知识库），通过「添加到知识库」建立 `note ↔ 知识库` 的**多对多关联**（引用而非复制，改笔记库里同步变）。

**明确后置（不在本模块）**：AI 帮写（续写/扩写/润色）、联动知识库写作（基于 KB RAG 生成）→ 均留到模块 5 之后；笔记内搜索/标签/目录 → 模块 6 或后续；本地文档导入 → 模块 4。

---

## 1. 目标与验收

目标：`URL → 抓取 → 提取 Markdown → DeepSeek 摘要 → 存为全局笔记` 全链路走通；空白笔记新建 + TipTap 编辑/保存可用；笔记列表 + 编辑器（打开即编辑）+ 添加到知识库 + 删除均可用；知识库内容列表正确展示关联笔记、知识库内「添加内容」可新建网页/笔记并自动关联；后端 `ruff/mypy/pytest`、前端 `eslint/tsc/build` 全绿。

**验收标准（Definition of Done）**

| # | 验收项 |
|---|---|
| 1 | `alembic upgrade head` 成功，新增 `notes`（含 `type` 列）+ `note_knowledge_bases`（复合主键 + 双 `ondelete=CASCADE` 外键） |
| 2 | 七个端点可用：创建（from-url）/ 创建（空白）/ 列表 / 详情 / 更新 / 删除 / 添加到知识库，`response_model` 齐全；创建端点支持可选 `knowledge_base_id`（传了即原子建关联） |
| 3 | 知识库内容列表端点返回异构条目（当前含 notes），「添加到知识库」/「知识库内新建」后该库内容列表可见此笔记 |
| 4 | 抓取失败（超时/反爬/非 HTML）返回 `422 fetch_failed`，**不建笔记**；不存在的笔记/知识库返回 `404`；非法 URL / 空 update body 返回 `422`，错误体统一 `{"detail":{"code","message"}}` |
| 5 | 前端「新建 → 网页 → 贴 URL → 打开编辑态」闭环可用；「新建 → 空白笔记 → 打开编辑态」闭环可用；编辑器含顶部工具栏 + 标题 + 写作区 + 自动保存 |
| 6 | 编辑器：TipTap 所见即所得 + Markdown 快捷输入（`# `→标题、`- `→列表、`**x**`→加粗等）生效；打开 url/markdown 笔记正文正确回填；保存后刷新仍一致 |
| 7 | 保存：正文自动保存（debounce）+ 标题失焦保存；url 笔记 `summary`/`source_url` 作为元信息展示（列表预览 + 编辑器顶部摘要块），正文不折叠、直接可编辑 |
| 8 | 后端 `ruff` + `mypy(strict)` + `pytest` 全绿；前端 `eslint` + `tsc --noEmit` + `vite build` 全绿 |
| 9 | 测试无需真库/真网：service 单元 + API 集成均用内存 Fake（repo / web_fetcher / llm） |

---

## 2. 数据模型

### 2.1 `notes` 表 + `note_knowledge_bases` 表（迁移 `0003_notes`）

**`notes`**（全局笔记，**无 `kb_id`**）

| 字段 | 类型 | 约束 / 默认 |
|---|---|---|
| `id` | `UUID`（PG native） | PK，app 端 `uuid.uuid4` |
| `title` | `String(255)` | 非空（网页标题自动取，兜底 URL；空白笔记默认「无标题笔记」） |
| `type` | `Enum(NoteType)`，`native_enum=False` 存 VARCHAR(16) | 非空，`markdown`/`url` |
| `content_markdown` | `Text` | 非空（网页正文转 Markdown；空白笔记初始为空串） |
| `summary` | `Text` | 可空（DeepSeek 生成，失败降级） |
| `source_url` | `Text` | 可空（type=url 时必有，原始 URL） |
| `created_at` / `updated_at` | `DateTime(timezone)` | 继承 `TimestampMixin` |

**`note_knowledge_bases`**（关联表，多对多）

| 字段 | 类型 | 约束 |
|---|---|---|
| `note_id` | `UUID` | PK 之一，`FK → notes.id ON DELETE CASCADE` |
| `knowledge_base_id` | `UUID` | PK 之一，`FK → knowledge_bases.id ON DELETE CASCADE` |
| `created_at` | `DateTime(timezone)` | `server_default now()` |

- 复合主键 `(note_id, knowledge_base_id)` 即唯一约束，天然幂等。
- 级联方向：删笔记 → 清其关联；删知识库 → 清其关联（**笔记本体保留**，因为笔记全局）。
- **本模块无新增列**（`type` 已预留 `markdown`，编辑能力靠「更新端点」而非新字段）。

### 2.2 ORM 模型（`models/note.py`）

```python
import uuid
from enum import StrEnum

from sqlalchemy import Column, DateTime, Enum, ForeignKey, String, Table, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

DEFAULT_NOTE_TITLE = "无标题笔记"


class NoteType(StrEnum):
    MARKDOWN = "markdown"
    URL = "url"


note_knowledge_bases = Table(
    "note_knowledge_bases",
    Base.metadata,
    Column("note_id", Uuid, ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "knowledge_base_id",
        Uuid,
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)


class Note(Base, TimestampMixin):
    __tablename__ = "notes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[NoteType] = mapped_column(
        Enum(NoteType, native_enum=False, length=16), nullable=False, default=NoteType.MARKDOWN
    )
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- `type` 用 `native_enum=False` 存 VARCHAR（免原生 PG enum 的迁移增删值成本），迁移里以 `sa.String(16)` 落列。
- 关联表用 `Table`（非 ORM 实体），`models/__init__.py` 导出 `Note`、`NoteType`、`note_knowledge_bases`。

### 2.3 迁移 `0003_notes`（手写，`down_revision = "0002_knowledge_bases"`）

- `upgrade`：`op.create_table("notes", …)` → `op.create_table("note_knowledge_bases", …)`，FK 名走 naming convention：`fk_note_knowledge_bases_note_id_notes`、`fk_note_knowledge_bases_knowledge_base_id_knowledge_bases`。
- `downgrade`：先 `drop_table("note_knowledge_bases")` 再 `drop_table("notes")`。

---

## 3. 网页采集与摘要（集成）

新增两个集成能力，均为 `async + Protocol` + Fake（对齐模块 1 惯例）。

### 3.1 网页抓取（`integrations/web.py`，新增）

```python
@dataclass(frozen=True)
class FetchedPage:
    markdown: str
    title: str | None = None
    source_url: str | None = None   # 重定向后的最终 URL，可空


class WebFetcher(Protocol):
    async def fetch(self, url: str) -> FetchedPage: ...


class TrafilaturaWebFetcher:      # 静态抓取（httpx + trafilatura）
    async def fetch(self, url: str) -> FetchedPage: ...


class PlaywrightWebFetcher:       # SPA 渲染抓取（无头浏览器 + trafilatura）
    async def fetch(self, url: str) -> FetchedPage: ...


class FallbackWebFetcher:         # 先静态、正文为空时回退浏览器
    async def fetch(self, url: str) -> FetchedPage: ...


class FakeWebFetcher:             # 测试替身
    async def fetch(self, url: str) -> FetchedPage: ...
```

- `TrafilaturaWebFetcher.fetch`：`httpx.AsyncClient` GET（带 UA、`timeout=30`、`follow_redirects=True`、响应体大小上限如 5MB）→ **`trafilatura.extract(..., output_format="markdown", include_images=True, with_metadata=True)`** 提取正文。
- 标题优先级：`og:title` → `<title>` → 兜底 URL；图片**不抓**，Markdown 内保留原图链接（`![...](https://…)`）。
- `trafilatura.extract` 是 CPU 同步，用 `asyncio.to_thread` 包一层避免阻塞事件循环。
- **失败判定**：网络错误 / 超时 / 非 HTML → 抛 `FetchError`（见 §6）；**提取正文为空** → 抛内部信号 `EmptyContentError(FetchError)`，触发回退而非直接失败。

**SPA 回退（`FallbackWebFetcher`）**：`httpx` 只拿静态 HTML、不执行 JS，纯客户端渲染（React/Vue）页面拿到的是空壳，正文抽取为空。因此按「先静态 → 空正文回退浏览器」两段式：

```
FallbackWebFetcher.fetch(url)
  ├─ TrafilaturaWebFetcher.fetch(url)  ── 成功（有正文）→ 返回
  └─ 抛 EmptyContentError              ── PlaywrightWebFetcher.fetch(url) 渲染后再抽取
```

- 只对 `EmptyContentError` 回退；HTTP 错误（404/超时）直接抛 `FetchError`（浏览器渲染也救不回死链，不回退）。
- `PlaywrightWebFetcher.fetch`：无头 Chromium `new_page` → `goto(wait_until="domcontentloaded", timeout=30s)` → `wait_for_load_state("networkidle", timeout=5s)`（超时继续，容忍有持续网络活动的 SPA）→ 额外 `wait_for_timeout(1s)` 缓冲 → `page.content()` 拿渲染后 HTML → 同样交 trafilatura 抽取。
- **浏览器生命周期**：模块级单例，`main.py` 的 lifespan 启动/关闭共享 Chromium（避免每请求重复启动 ~2s 开销）；启动失败记录日志并降级——`get_browser()` 返回 `None`，此时 `FallbackWebFetcher` 的 spa 侧为 `None`，静态抓取仍可用、SPA 抓取不可用，不影响应用启动。
- 部署需在新环境执行 `playwright install chromium`（浏览器二进制不进 git/镜像）。

### 3.2 摘要 LLM（`integrations/llm.py`，扩展）

模块 1 已有 `LLMClient` Protocol + `FakeLLMClient`。本模块补真实 `DeepSeekLLMClient`：

```python
class DeepSeekLLMClient:
    """DeepSeek（OpenAI 兼容）实现，httpx 异步调用 /chat/completions。"""
    def __init__(self, *, base_url: str, api_key: str, model: str) -> None: ...
    async def chat(self, messages, *, temperature=0.7, max_tokens=None) -> ChatResult: ...
```

- `integrations/__init__.py` 的 `get_llm_client(settings)` 扩参：`_llm_client(provider, model, base_url, api_key)`；`provider == "deepseek"` 返回 `DeepSeekLLMClient`，`"fake"` 返回 `FakeLLMClient`（**默认仍 fake**，配置 `.env` 的 `LLM_PROVIDER=deepseek` + `LLM_API_KEY` 后真实生效）。
- 摘要调用（service 内）：`messages = [system("你是摘要助手，用一到两句中文概括网页正文"), user(正文截断 ~4000 字)]`，`temperature=0.3`、`max_tokens≈150`。
- **降级**：`llm.chat` 抛异常时，摘要降级为 `content_markdown[:200] + "…"`（笔记仍保存，不因摘要失败而失败）。

---

## 4. 后端分层与接口

沿 `router → service → repository` 三层，依赖注入走 `api/deps.py`（`Depends` + 类型别名，同模块 2）。

```
models/note.py                     # Note + NoteType + note_knowledge_bases
schemas/note.py                    # NoteCreateFromUrl / NoteCreate / NoteUpdate / NoteRead / NoteList / NoteAddToKnowledgeBase
schemas/knowledge_base.py          # + ContentItem / ContentList（KB 内容列表异构条目）
repositories/note.py               # NoteRepository(Protocol) + SqlAlchemyNoteRepository
services/note.py                   # NoteService
api/routes/notes.py                # 7 个笔记端点
api/routes/knowledge_bases.py      # + GET /knowledge-bases/{kb_id}/contents
integrations/web.py                # WebFetcher(Protocol) + Trafilatura + Playwright + Fallback + Fake
integrations/llm.py                # + DeepSeekLLMClient
core/exceptions.py                 # + FetchError
```

### 4.1 Repository（`repositories/note.py`）

```python
class NoteRepository(Protocol):
    async def list(self, *, limit: int, offset: int) -> tuple[list[Note], int]: ...
    async def get(self, note_id: uuid.UUID) -> Note | None: ...
    async def add(self, note: Note) -> Note: ...
    async def update(self, note: Note) -> Note: ...
    async def delete(self, note: Note) -> None: ...
    async def associate(self, note_id: uuid.UUID, kb_id: uuid.UUID) -> bool: ...  # True=新建关联，False=已存在
    async def list_by_kb(self, kb_id: uuid.UUID) -> list[Note]: ...
```

- `SqlAlchemyNoteRepository`：`list` 用 `count(*)` + `order_by(updated_at.desc(), id.asc())` 分页；`add`/`update`/`delete` 内 `commit`（`add`/`update` 后 `refresh`）。
- `update`：字段已由 service 改好，仅 `commit` + `refresh`（回填 `updated_at`）。
- `associate`：`insert(note_knowledge_bases).on_conflict_do_nothing()`（幂等），返回是否真正插入。
- `list_by_kb`：`select(Note).join(note_knowledge_bases).where(kb_id == …).order_by(note_knowledge_bases.c.created_at.desc())`（按关联时间倒序）。

### 4.2 Service（`services/note.py`）

| 方法 | 规则 |
|---|---|
| `create_from_url(url, kb_id=None)` | `web_fetcher.fetch(url)`（失败 `FetchError`）→ **若 kb_id：先 `kb_repo.get` 校验存在（404「知识库不存在」）** → 标题兜底 → `_summarize()` → 建 `Note(type=url, source_url=url)` → `repo.add` → **若 kb_id：`repo.associate`** |
| `create_blank(kb_id=None)` | **若 kb_id：先 `kb_repo.get` 校验（404）** → 建 `Note(type=markdown, title=DEFAULT_NOTE_TITLE, content_markdown="")` → `repo.add` → **若 kb_id：`repo.associate`** |
| `_summarize(fetched)` | 调 `llm.chat` 生成摘要；异常降级截正文前 200 字 |
| `list(limit, offset)` | 边界钳制（`limit` 1~100）后委托 repo |
| `get(note_id)` | 查无抛 `NotFoundError("笔记不存在")` |
| `update(note_id, title?, content_markdown?)` | 先 `get`（404）→ 非 None 字段 trim 后赋值 → `repo.update` |
| `delete(note_id)` | 先 `get`（404）→ `repo.delete` |
| `add_to_kb(note_id, kb_id)` | 先 `get` 笔记（404）→ `kb_repo.get` 校验知识库存在（404「知识库不存在」）→ `repo.associate`（幂等，重复添加静默成功） |
| `list_by_kb(kb_id)` | 先 `kb_repo.get` 校验知识库存在（404）→ `repo.list_by_kb` |

- `NoteService` 依赖 `NoteRepository + KnowledgeBaseRepository + WebFetcher + LLMClient`（注入时 4 个替身皆可换）。
- **kb_id 校验前置**：创建前先校验知识库存在，避免「建了笔记但关联失败」的孤儿笔记。

### 4.3 Router（`api/routes/notes.py`，`prefix="/notes"`）

| 方法 | 路径 | 成功 | 失败 |
|---|---|---|---|
| POST | `/api/notes/from-url` | 201 `NoteRead` | 422（URL 非法 / fetch_failed）· 404（kb_id 不存在） |
| POST | `/api/notes` | 201 `NoteRead` | 422 · 404（kb_id 不存在） |
| GET | `/api/notes?limit=&offset=` | 200 `NoteList` | — |
| GET | `/api/notes/{note_id}` | 200 `NoteRead` | 404 |
| PATCH | `/api/notes/{note_id}` | 200 `NoteRead` | 404 / 422 |
| DELETE | `/api/notes/{note_id}` | 204 | 404 |
| POST | `/api/notes/{note_id}/knowledge-bases` | 204 | 404 |

- 列表默认 `limit=50, offset=0`，响应 `{"items": [...], "total": N}`。
- 路径参数 `note_id: uuid.UUID`（非法即 422）。

### 4.4 知识库内容列表（`api/routes/knowledge_bases.py` 新增）

| 方法 | 路径 | 成功 | 失败 |
|---|---|---|---|
| GET | `/api/knowledge-bases/{kb_id}/contents` | 200 `ContentList` | 404 |

- **异构列表**：`ContentList.items` 为带 `type` 判别字段的联合条目，当前只含 `type="note"`（模块 4 增 `"document"`），前端据此区分渲染。
- 模块 2 时 `ContentListPane` 只是空态占位，本模块**新建此端点**（而非单独 `notes-by-kb` 端点），模块 4 文档接入时同端点扩展、前端数据层不动。

### 4.5 Schema（`schemas/note.py` + `schemas/knowledge_base.py`）

```python
class NoteCreateFromUrl(BaseModel):
    url: str
    knowledge_base_id: uuid.UUID | None = None   # 传了即创建后原子关联
    # field_validator：url strip 后非空；urlparse 校验 scheme ∈ {http, https}，否则 422

class NoteCreate(BaseModel):          # 空白笔记
    title: str | None = None          # None → service 补 DEFAULT_NOTE_TITLE
    knowledge_base_id: uuid.UUID | None = None

class NoteUpdate(BaseModel):
    title: str | None = None
    content_markdown: str | None = None
    # @model_validator(mode="after") 保证至少一个字段非 None

class NoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str
    type: NoteType
    content_markdown: str
    summary: str | None
    source_url: str | None
    created_at: datetime
    updated_at: datetime

class NoteList(BaseModel):
    items: list[NoteRead]
    total: int

class NoteAddToKnowledgeBase(BaseModel):
    knowledge_base_id: uuid.UUID
```

```python
# schemas/knowledge_base.py 新增
class ContentItem(BaseModel):
    type: Literal["note"]               # 模块 4 增 "document"
    note: NoteRead | None = None
    # document: DocumentRead | None = None   # 模块 4

class ContentList(BaseModel):
    items: list[ContentItem]
    total: int
```

- URL 校验**不强制域名后缀**（`HttpUrl` 过严），只要求 `http/https` + 非空，与 ima「必须完整 URL」一致。
- `NoteUpdate` 用 `model_validator` 保证非空更新，避免无意义 PATCH。

---

## 5. 编辑器选型（前端核心）

### 5.1 选型与双向转换

- **TipTap**（`@tiptap/react` + `@tiptap/starter-kit`）：所见即所得编辑器。
- **Markdown 快捷输入**：StarterKit 自带 `# `→标题、`- `→无序列表、`> `→引用、`` ` ``→代码块、`---`→分割线等 InputRule；Bold/Italic/Strike（`**x**`/`*x*`/`~~x~~`）的输入规则也在 StarterKit 内。
- **`tiptap-markdown`**（aguingand）：负责 TipTap 文档 ↔ Markdown 双向序列化，这是「底层存 Markdown」的关键：
  - 打开笔记：`editor.commands.setContent(note.content_markdown)`（parse Markdown → ProseMirror 文档）
  - 保存正文：`editor.getMarkdown()`（serialize → Markdown）→ PATCH
- **界面参考 ima**（打开即编辑，无读态）：编辑器 = **顶部工具栏 + 标题 + 写作区**。
- **顶部工具栏**：固定工具栏（对齐 ima），按钮集 = 撤销/重做、加粗、斜体、删除线、H1/H2/H3、无序列表、有序列表、引用、代码块、行内代码。工具栏按钮与 Markdown 快捷输入并存。

### 5.2 页面布局（打开即编辑）

- **笔记 tab**：左「笔记列表」+ 右「编辑器」，点开笔记**直接进入编辑器**（无读态/编辑态分离，无 `?edit=1` 路由）。
- **编辑器从上到下**：顶部工具栏 → 标题输入框（失焦保存）→ **（url 笔记专属）摘要块**：只读展示 `summary` + `source_url` 外链 → 写作区（TipTap 正文，直接可编辑、不折叠）。
- **markdown 笔记**：无摘要块，仅工具栏 + 标题 + 写作区。
- **列表项预览**：标题 + 摘要/正文前 N 字（url 笔记显示 `summary`，markdown 笔记显示正文截断）。

### 5.3 保存（混合，自动保存）

| 目标 | 触发 | 调用 |
|---|---|---|
| 正文 `content_markdown` | `onUpdate` debounce（~800ms）自动保存 | `PATCH {content_markdown}` |
| 标题 `title` | `onBlur` 失焦（或 Enter） | `PATCH {title}` |
| 离开编辑页 | 路由切换前 | flush 未保存的 debounce + 保存脏标题 |

- 两者共用 `PATCH /notes/{id}` 部分更新，互不干扰（对齐 ima「自动保存，无需 Ctrl+S」）。
- 前端用 react-query 的 `useUpdateNote` mutation，成功后 `invalidateQueries(['notes'])` + 更新 `['notes', id]` 缓存。

---

## 6. 领域异常与错误响应

`core/exceptions.py` 新增：

```python
class FetchError(DomainError):
    status_code = 422
    code = "fetch_failed"
```

- `TrafilaturaWebFetcher` / `PlaywrightWebFetcher` 抓取/解析失败直接抛 `FetchError("无法抓取该网页")`，由 `main.py` 已有全局 `DomainError` handler 映射为 `{"detail": {"code": "fetch_failed", "message": "无法抓取该网页"}}`。
- `EmptyContentError(FetchError)` 是**内部回退信号**（静态抓取空正文），被 `FallbackWebFetcher` 消化、不外泄；只有回退也失败（或 spa 不可用）才转成 `FetchError` 暴露给 API 层。
- 前端 `ApiError` 已能解析 `detail.message`，直接展示文案。
- 复用既有 `NotFoundError`（404）、Pydantic 校验（422 默认结构）。

---

## 7. 前端

### 7.1 文件清单

```
api/types.ts                      # Note / NoteCreateFromUrl / NoteCreate / NoteUpdate / NoteListResponse / NoteAddToKnowledgeBase / ContentItem / ContentListResponse
api/notes.ts                      # createNoteFromUrl / createNote / listNotes / getNote / updateNote / deleteNote / addNoteToKnowledgeBase
api/knowledgeBases.ts             # + listKbContents
hooks/useNotes.ts                 # react-query hooks + 失效重查
pages/Notes/
  index.tsx                       # 两栏外壳（/notes 与 /notes/:id 共用，useParams 取选中）
  components/                     # 页面专用子组件
    NoteListPane.tsx              # 左栏：新建下拉(网页/空白笔记) + 列表 + hover 删除 + 空态
    NoteEditorPane.tsx            # 右栏编辑器：工具栏 + 标题 + (url 摘要块) + TipTap 写作区
    NoteEditorToolbar.tsx         # 工具栏按钮组
    WebNoteFormModal.tsx          # 新建「网页」弹窗：贴 URL → 确定 → loading → 成功打开编辑态（可带 kb 上下文）
    AddToKnowledgeBaseModal.tsx   # 选知识库（复用 useKnowledgeBases）
pages/KnowledgeBasePage/
  components/
    ContentListPane.tsx           # 中栏内容列表：合并 documents(后补) + 关联 notes + 「添加内容」下拉
    AddContentMenu.tsx            # 「添加内容」下拉：网页 / 笔记 / 本地文档(置灰)
router.tsx                        # + /notes/:id（打开即编辑，无 ?edit=1）
```

### 7.2 交互与路由

| 路径 | 行为 |
|---|---|
| `/notes` | 左列表 + 右空态（「从左侧选择或新建一条笔记」） |
| `/notes/:id` | 左列表（选中高亮）+ 右编辑器（打开即编辑） |

- 选中走 URL（对齐决策 #9「选中状态走 URL」）；**打开即编辑，无读态/编辑态切换、无 `?edit=1`**。不设重定向（笔记可为空，与知识库「至少一个」不同）。
- 笔记 tab「新建」下拉 →「网页」/「空白笔记」：
  - 网页 → `WebNoteFormModal`（无 kb 上下文）→ `useCreateNoteFromUrl` → `navigate('/notes/' + id)`。
  - 空白笔记 → `useCreateNote` → `navigate('/notes/' + id)`（直接编辑态）。
- 知识库页「添加内容」下拉（`AddContentMenu`）→「网页」/「笔记」/「本地文档(置灰，模块 4)」：
  - 网页 → `WebNoteFormModal`（**带当前 kb 上下文**，`knowledge_base_id`）→ `useCreateNoteFromUrl` → 成功后该笔记已自动关联当前库。
  - 笔记 → `useCreateNote({ knowledge_base_id })` → 空白笔记自动关联当前库 → `navigate('/notes/' + id)`。
- 编辑器：顶部工具栏（加粗/斜体/标题/列表/引用/代码等）+ 标题（失焦保存）+ url 摘要块（只读 `summary` + 外链）+ TipTap 写作区（debounce 自动保存）。
- 「添加到知识库」（编辑器内）→ `AddToKnowledgeBaseModal`（复用 `useKnowledgeBases` 选库）→ `useAddNoteToKnowledgeBase`；重复添加幂等。
- 删除（列表 hover / 编辑器内）二次确认 → `useDeleteNote` → 成功后 `navigate('/notes')`。
- 知识库页内容列表：`useKbContents(kb_id)` 拉 `ContentList`，渲染关联笔记（带 `type` 判别，documents 占位留模块 4），点击跳 `/notes/:id`。
- 数据请求走 react-query：`useNotes`（无限列表）、`useNote(id)`、`useCreateNoteFromUrl`、`useCreateNote`、`useUpdateNote`、`useDeleteNote`、`useAddNoteToKnowledgeBase`（变更后 `invalidateQueries(['notes', 'knowledge-bases'])`）、`useKbContents`。

### 7.3 新增前端依赖

- `@tiptap/react` + `@tiptap/starter-kit` + `@tiptap/extension-image` + `@tiptap/extension-placeholder` + `tiptap-markdown`。
- `@tiptap/extension-image`：让 TipTap 渲染/编辑 `![...](url)` 图片节点，兑现「图片不抓、保留原图链接」。
- **不引入** `react-markdown`/`remark-gfm`（正文由 TipTap 渲染/编辑，无只读 Markdown 展示场景）。

---

## 8. 测试策略（少而深，不起真库/真网）

| 层 | 测试 | 方式 |
|---|---|---|
| Service 单元 | `test_services_note.py` | 注入 `FakeNoteRepository` + `FakeKnowledgeBaseRepository` + `FakeWebFetcher` + `FakeLLMClient`；测 create_from_url（成功/抓取失败/摘要降级/带 kb_id 关联）、create_blank（默认标题/带 kb_id 关联/kb 不存在 404）、update（成功/404/空 body）、list 钳制、get/delete 404、add_to_kb（成功/重复幂等/笔记不存在/库不存在）、list_by_kb（库不存在 404） |
| API 集成 | `test_api_notes.py` | `app.dependency_overrides` 换 Fake，`AsyncClient` 走 7 端点 + `GET /knowledge-bases/{kb_id}/contents`，断言状态码 + 响应体 + 错误信封 + 分页形状 |
| 集成 Fake | `tests/fakes.py` 增补 | `FakeNoteRepository`（dict 存储、模拟 list 排序 + associate 幂等 + update + list_by_kb）、`FakeWebFetcher`（返回固定 `FetchedPage`，可配抛 `FetchError`） |
| Web 抓取单元 | `test_web.py` | 用可控 `_Fetcher` 假实现测 `FallbackWebFetcher` 回退分支（静态成功不回退 / 空正文回退 / HTTP 错误不回退 / spa 不可用抛错）；`PlaywrightWebFetcher` 用 `MagicMock` 假 Browser/Page + monkeypatch `extract_markdown`/`extract_title` 测渲染与抽取（不起真浏览器） |

- `FakeNoteRepository` 与 `SqlAlchemy` 版同签名（满足 Protocol）。
- 关键用例：from-url 成功→列表含 1 条 type=url；from-url 带 kb_id→关联建立；抓取失败→422 fetch_failed 且不落库；create_blank→201 type=markdown 标题「无标题笔记」；create_blank 带不存在的 kb_id→404 且不建笔记；PATCH 标题/正文→200 且 updated_at 变；空 update body→422；删除→404；重复 add_to_kb→幂等仍 204；非法 URL→422；KB 内容列表→含已关联笔记、未关联不含。

---

## 9. 实现顺序（task 列表）

| 步骤 | 内容 | 产出 |
|---|---|---|
| T1 | `pyproject.toml` 加 `httpx`（运行时）+ `trafilatura`；`package.json` 加 TipTap 系依赖 | 依赖可装 |
| T2 | `models/note.py` + 迁移 `0003_notes`（手写 up/down）+ `models/__init__.py` 导出 | 可 `upgrade head` 建表 |
| T3 | `core/exceptions.py` 加 `FetchError` | 领域异常 |
| T4 | `integrations/web.py`（WebFetcher 协议 + Trafilatura + Fake）+ `deps.py` 注入 | 采集能力 |
| T5 | `integrations/llm.py` 加 `DeepSeekLLMClient` + `integrations/__init__.py` 注册 deepseek provider | 真实 LLM |
| T6 | `schemas/note.py`（含 NoteCreate/NoteUpdate + knowledge_base_id）+ `schemas/knowledge_base.py` 加 ContentItem/ContentList | 校验模型 |
| T7 | `repositories/note.py`（含 update/list_by_kb）+ `deps.py` 增补 note repo/service | 数据访问层 |
| T8 | `services/note.py`（含 create_blank/update/list_by_kb/kb_id 关联） | 业务规则层 |
| T9 | `api/routes/notes.py`（7 端点）+ `api/routes/knowledge_bases.py` 加 contents 端点 + `routes/__init__.py` 注册 | 接口层 |
| T10 | 后端测试：`fakes.py` 增补 + service 单元 + API 集成 | pytest 全绿 |
| T11 | 前端 `types.ts`/`notes.ts`/`knowledgeBases.ts`/`useNotes.ts` | 数据层 |
| T12 | 前端 `pages/Notes`（外壳 + 列表 + 网页/删除/添加 modal） | 采集闭环 |
| T13 | 前端 `NoteEditorPane`（工具栏 + 标题 + TipTap + 自动保存） | 编辑闭环 |
| T14 | 前端知识库「添加内容」下拉 + `ContentListPane` 展示关联笔记 | 知识库侧收口 |
| T15 | 全量质量门禁 + 手工验收 | ruff/mypy/pytest/eslint/tsc/build 全绿 |

---

## 10. 已定决策

1. **笔记全局（无 `kb_id`）**：推翻模块 2 §8 决策 #11「文档=笔记统一进库」。
2. **添加到知识库 = 多对多关联**（引用不复制，改笔记库里同步变）；关联表复合主键 + 双 `ondelete=CASCADE`。
3. **采集同步**：`POST /notes/from-url` 同步等抓取+摘要完成，前端 loading + 30s 超时；不做 `pending→done` 状态机（网页抓取秒级，单用户本地）。
4. **正文转 Markdown**：图片不抓、保留原图链接，能加载就加载。
5. **摘要 DeepSeek 生成**：失败降级截正文前 200 字；默认 `llm_provider=fake`，配置 `.env` 后真实生效。
6. **两种笔记都能编辑**：url 笔记也能进 TipTap 改正文，`summary`/`source_url` 元信息保留、编辑正文不影响。
7. **编辑器形态**：TipTap 所见即所得 + Markdown 快捷输入；底层存 Markdown（`tiptap-markdown` 双向序列化）。
8. **打开即编辑（无读态）**：对齐 ima「列表 + 工具栏 + 写作区」，点开笔记直接编辑，无读态/编辑态分离、无 `?edit=1` 路由。
9. **编辑器界面参考 ima**：顶部工具栏（加粗/斜体/标题/列表/引用/代码等）+ 标题 + 写作区；url 笔记摘要块（只读 summary + 外链）在标题与正文之间。
10. **保存（混合自动）**：正文 debounce 自动保存 + 标题失焦保存（对齐 ima「无需 Ctrl+S」）；离开前 flush。
11. **空白笔记默认标题**：落库「无标题笔记」（`title` 保持非空，schema 不变）。
12. **知识库内新建**：知识库页「添加内容」下拉 = 网页 / 笔记（都自动关联当前库）/ 本地文档（置灰，模块 4）；后端给创建端点加可选 `knowledge_base_id`，原子建「笔记 + 关联」，kb 校验前置避免孤儿笔记。
13. **知识库内容列表展示关联笔记**：本模块收口（新建 `GET /knowledge-bases/{kb_id}/contents` 异构端点，当前含 notes，模块 4 增 documents）。
14. **后置项**：AI 帮写 / 联动知识库写作 → 模块 5 之后；笔记内搜索/标签/目录 → 后续；本地文档导入 → 模块 4。
15. **抓取失败 → 422 `fetch_failed`**，不建笔记；`FetchError` 复用给模块 4 的 URL 解析。
16. **删除笔记 = 硬删除**，关联随 `ondelete=CASCADE` 清；知识库被删时关联清、笔记本体保留。
17. **URL 校验**：仅要求 `http/https` + 非空（不强校验域名后缀）。
18. **正文由 TipTap 渲染/编辑**：不引入 `react-markdown`/`remark-gfm`。
19. **网页抓取支持 SPA 回退**：`httpx` 不执行 JS，纯客户端渲染页面抽到空正文，故按「先静态 trafilatura → 空正文回退 Playwright 无头浏览器」两段式；仅对空正文（`EmptyContentError`）回退，HTTP 错误不回退；浏览器由 lifespan 启动/关闭的单例共享，启动失败降级为仅静态抓取。
