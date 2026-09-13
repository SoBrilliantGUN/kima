# 模块 3：笔记（网页笔记 slice）— 详细设计

> 日期：2026-09-13
> 状态：已评审定稿，可直接开工
> 上游基线：`docs/requirements.md`（决策 #15/#16/#18/#19）· `docs/module-2-knowledge-bases.md`

本 slice 交付「**采集网页 → 全局笔记 → 阅读态**」的闭环，外加「**添加到知识库**」关联动作。它是模块 3（笔记/编辑器）的**先导切片**：只做「网页」这一种笔记的采集与阅读，**不做 TipTap 编辑器**（编辑正文留到模块 3 其余部分）。同时**裁掉「浏览」tab**（已随本次需求同步删除代码）。

核心数据关系（推翻了模块 2 §8 决策 #11「文档=笔记统一进库」）：**笔记是全局内容**（不挂知识库），通过「添加到知识库」建立 `note ↔ 知识库` 的**多对多关联**（引用而非复制，改笔记库里同步变）。

---

## 1. 目标与验收

目标：`URL → 抓取 → 提取 Markdown 正文 → DeepSeek 摘要 → 存为全局笔记` 全链路走通；笔记列表 + 阅读态（标题/摘要/可折叠正文）+ 添加到知识库 + 删除均可用；后端 `ruff/mypy/pytest`、前端 `eslint/tsc/build` 全绿。

**验收标准（Definition of Done）**

| # | 验收项 |
|---|---|
| 1 | `alembic upgrade head` 成功，新增 `notes`（含 `type` 列）+ `note_knowledge_bases`（复合主键 + 双 `ondelete=CASCADE` 外键） |
| 2 | 五个端点可用：创建（from-url）/ 列表 / 详情 / 删除 / 添加到知识库，`response_model` 齐全 |
| 3 | 抓取失败（超时/反爬/非 HTML）返回 `422 fetch_failed`，**不建笔记**；不存在的笔记/知识库返回 `404`；非法 URL 返回 `422`，错误体统一 `{"detail":{"code","message"}}` |
| 4 | 前端「新建 → 网页 → 贴 URL → 阅读态」闭环可用；阅读态含标题+摘要+可折叠正文（Markdown 渲染）+ 添加到知识库 + 删除 |
| 5 | 后端 `ruff` + `mypy(strict)` + `pytest` 全绿；前端 `eslint` + `tsc --noEmit` + `vite build` 全绿 |
| 6 | 测试无需真库/真网：service 单元 + API 集成均用内存 Fake（repo / web_fetcher / llm） |

---

## 2. 数据模型

### 2.1 `notes` 表 + `note_knowledge_bases` 表（迁移 `0003_notes`）

**`notes`**（全局笔记，**无 `kb_id`**）

| 字段 | 类型 | 约束 / 默认 |
|---|---|---|
| `id` | `UUID`（PG native） | PK，app 端 `uuid.uuid4` |
| `title` | `String(255)` | 非空（网页标题自动取，兜底 URL） |
| `type` | `Enum(NoteType)`，`native_enum=False` 存 VARCHAR(16) | 非空，`markdown`/`url`（本次只写 `url`） |
| `content_markdown` | `Text` | 非空（网页正文转 Markdown） |
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

### 2.2 ORM 模型（`models/note.py`）

```python
import uuid
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, String, Table, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


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


class TrafilaturaWebFetcher:      # 真实实现
    async def fetch(self, url: str) -> FetchedPage: ...


class FakeWebFetcher:             # 测试替身
    async def fetch(self, url: str) -> FetchedPage: ...
```

- `TrafilaturaWebFetcher.fetch`：`httpx.AsyncClient` GET（带 UA、`timeout=30`、`follow_redirects=True`、响应体大小上限如 5MB）→ **`trafilatura.extract(..., output_format="markdown", include_images=True, with_metadata=True)`** 提取正文。
- 标题优先级：`og:title` → `<title>` → 兜底 URL；图片**不抓**，Markdown 内保留原图链接（`![...](https://…)`）。
- `trafilatura.extract` 是 CPU 同步，用 `asyncio.to_thread` 包一层避免阻塞事件循环。
- **失败判定**：网络错误 / 超时 / 非 HTML / 提取正文为空 → 抛 `FetchError`（见 §5）。

### 3.2 摘要 LLM（`integrations/llm.py`，扩展）

模块 1 已有 `LLMClient` Protocol + `FakeLLMClient`。本 slice 补真实 `DeepSeekLLMClient`：

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
schemas/note.py                    # NoteCreateFromUrl / NoteRead / NoteList / NoteAddToKnowledgeBase
repositories/note.py               # NoteRepository(Protocol) + SqlAlchemyNoteRepository
services/note.py                   # NoteService
api/routes/notes.py                # 5 个端点
integrations/web.py                # WebFetcher(Protocol) + Trafilatura + Fake
integrations/llm.py                # + DeepSeekLLMClient
core/exceptions.py                 # + FetchError
```

### 4.1 Repository（`repositories/note.py`）

```python
class NoteRepository(Protocol):
    async def list(self, *, limit: int, offset: int) -> tuple[list[Note], int]: ...
    async def get(self, note_id: uuid.UUID) -> Note | None: ...
    async def add(self, note: Note) -> Note: ...
    async def delete(self, note: Note) -> None: ...
    async def associate(self, note_id: uuid.UUID, kb_id: uuid.UUID) -> bool: ...  # True=新建关联，False=已存在
```

- `SqlAlchemyNoteRepository`：`list` 用 `count(*)` + `order_by(updated_at.desc(), id.asc())` 分页；`add`/`delete` 内 `commit`（`add` 后 `refresh`）。
- `associate`：`insert(note_knowledge_bases).on_conflict_do_nothing()`（幂等），返回是否真正插入。

### 4.2 Service（`services/note.py`）

| 方法 | 规则 |
|---|---|
| `create_from_url(url)` | `web_fetcher.fetch(url)`（失败 `FetchError`）→ 标题兜底 → `_summarize()` → 建 `Note(type=url, source_url=url)` → `repo.add` |
| `_summarize(fetched)` | 调 `llm.chat` 生成摘要；异常降级截正文前 200 字 |
| `list(limit, offset)` | 边界钳制（`limit` 1~100）后委托 repo |
| `get(note_id)` | 查无抛 `NotFoundError("笔记不存在")` |
| `delete(note_id)` | 先 `get`（404）→ `repo.delete` |
| `add_to_kb(note_id, kb_id)` | 先 `get` 笔记（404）→ `kb_repo.get` 校验知识库存在（404「知识库不存在」）→ `repo.associate`（幂等，重复添加静默成功） |

- `NoteService` 依赖 `NoteRepository + KnowledgeBaseRepository + WebFetcher + LLMClient`（注入时 4 个替身皆可换）。

### 4.3 Router（`api/routes/notes.py`，`prefix="/notes"`）

| 方法 | 路径 | 成功 | 失败 |
|---|---|---|---|
| POST | `/api/notes/from-url` | 201 `NoteRead` | 422（URL 非法 / fetch_failed） |
| GET | `/api/notes?limit=&offset=` | 200 `NoteList` | — |
| GET | `/api/notes/{note_id}` | 200 `NoteRead` | 404 |
| DELETE | `/api/notes/{note_id}` | 204 | 404 |
| POST | `/api/notes/{note_id}/knowledge-bases` | 204 | 404 |

- 列表默认 `limit=50, offset=0`，响应 `{"items": [...], "total": N}`。
- 路径参数 `note_id: uuid.UUID`（非法即 422）。

### 4.4 Schema（`schemas/note.py`）

```python
class NoteCreateFromUrl(BaseModel):
    url: str
    # field_validator：strip 后非空；urlparse 校验 scheme ∈ {http, https}，否则 422

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

- URL 校验**不强制域名后缀**（`HttpUrl` 过严），只要求 `http/https` + 非空，与 ima「必须完整 URL」一致。

---

## 5. 领域异常与错误响应

`core/exceptions.py` 新增：

```python
class FetchError(DomainError):
    status_code = 422
    code = "fetch_failed"
```

- `TrafilaturaWebFetcher` 抓取/解析失败直接抛 `FetchError("无法抓取该网页")`，由 `main.py` 已有全局 `DomainError` handler 映射为 `{"detail": {"code": "fetch_failed", "message": "无法抓取该网页"}}`。
- 前端 `ApiError` 已能解析 `detail.message`，直接展示文案。
- 复用既有 `NotFoundError`（404）、Pydantic 校验（422 默认结构）。

---

## 6. 测试策略（少而深，不起真库/真网）

| 层 | 测试 | 方式 |
|---|---|---|
| Service 单元 | `test_services_note.py` | 注入 `FakeNoteRepository` + `FakeKnowledgeBaseRepository` + `FakeWebFetcher` + `FakeLLMClient`；测 create_from_url（成功/抓取失败/摘要降级）、list 钳制、get/delete 404、add_to_kb（成功/重复幂等/笔记不存在/库不存在） |
| API 集成 | `test_api_notes.py` | `app.dependency_overrides` 换 Fake，`AsyncClient` 走 5 端点，断言状态码 + 响应体 + 错误信封 + 分页形状 |
| 集成 Fake | `tests/fakes.py` 增补 | `FakeNoteRepository`（dict 存储、模拟 list 排序 + associate 幂等）、`FakeWebFetcher`（返回固定 `FetchedPage`，可配抛 `FetchError`） |

- `FakeNoteRepository` 与 `SqlAlchemy` 版同签名（满足 Protocol）。
- 关键用例：from-url 成功→列表含 1 条 type=url；抓取失败→422 fetch_failed 且不落库；删除→404；重复 add_to_kb→幂等仍 204；非法 URL→422。

---

## 7. 前端

### 7.1 文件清单

```
api/types.ts                      # Note / NoteCreateFromUrl / NoteListResponse / NoteAddToKnowledgeBase
api/notes.ts                      # createNoteFromUrl / listNotes / getNote / deleteNote / addNoteToKnowledgeBase
hooks/useNotes.ts                 # react-query hooks + 失效重查
pages/Notes/
  index.tsx                       # 两栏外壳（/notes 与 /notes/:id 共用，useParams 取选中）
  components/                     # 页面专用子组件
    NoteListPane.tsx              # 左栏：新建下拉(网页) + 列表 + hover 删除 + 空态
    NoteReadPane.tsx              # 右栏：标题+摘要+可折叠正文(Markdown)+添加到知识库+删除 + 空态
    WebNoteFormModal.tsx          # 新建「网页」弹窗：贴 URL → 确定 → loading → 成功跳阅读态
    AddToKnowledgeBaseModal.tsx   # 选知识库（复用 useKnowledgeBases）
router.tsx                        # + /notes/:id
```

### 7.2 交互与路由

| 路径 | 行为 |
|---|---|
| `/notes` | 左列表 + 右空态（「从左侧选择或新建一条笔记」） |
| `/notes/:id` | 左列表（选中高亮）+ 右阅读态 |

- 选中走 URL（对齐决策 #9「选中状态走 URL」），不设重定向（笔记可为空，与知识库「至少一个」不同）。
- 「新建」按钮 → 下拉「网页」/「空白笔记」（后者置灰「模块 3 稍后」）→ `WebNoteFormModal`：贴 URL，提交调 `useCreateNoteFromUrl`，成功 `navigate('/notes/' + id)`；失败显示 `ApiError.message`（如「无法抓取该网页」）。
- 阅读态：标题 + 摘要 + `source_url`（外链）+ 可折叠正文（**默认折叠，点「展开」渲染全文**）。正文用 **`react-markdown` + `remark-gfm`** 只读渲染（图片链接原样 `<img>`，能加载就加载）。
- 「添加到知识库」→ `AddToKnowledgeBaseModal`（复用 `useKnowledgeBases` 选库）→ `useAddNoteToKnowledgeBase`；重复添加幂等。
- 删除（列表 hover / 阅读态）二次确认 → `useDeleteNote` → 成功后 `navigate('/notes')`。
- 数据请求走 react-query：`useNotes`（无限列表）、`useNote(id)`、`useCreateNoteFromUrl`、`useDeleteNote`、`useAddNoteToKnowledgeBase`（变更后 `invalidateQueries(['notes'])`）。

### 7.3 新增前端依赖

- `react-markdown` + `remark-gfm`（正文只读渲染）。

---

## 8. 实现顺序（task 列表）

| 步骤 | 内容 | 产出 |
|---|---|---|
| T1 | `pyproject.toml` 加 `httpx`（运行时）+ `trafilatura`；`package.json` 加 `react-markdown`/`remark-gfm` | 依赖可装 |
| T2 | `models/note.py` + 迁移 `0003_notes`（手写 up/down）+ `models/__init__.py` 导出 | 可 `upgrade head` 建表 |
| T3 | `core/exceptions.py` 加 `FetchError` | 领域异常 |
| T4 | `integrations/web.py`（WebFetcher 协议 + Trafilatura + Fake）+ `deps.py` 注入 | 采集能力 |
| T5 | `integrations/llm.py` 加 `DeepSeekLLMClient` + `integrations/__init__.py` 注册 deepseek provider | 真实 LLM |
| T6 | `schemas/note.py` | 校验模型 |
| T7 | `repositories/note.py` + `deps.py` 增补 note repo/service | 数据访问层 |
| T8 | `services/note.py` | 业务规则层 |
| T9 | `api/routes/notes.py` + `routes/__init__.py` 注册 | 5 个端点 |
| T10 | 后端测试：`fakes.py` 增补 + service 单元 + API 集成 | pytest 全绿 |
| T11 | 前端 `types.ts`/`notes.ts`/`useNotes.ts` | 数据层 |
| T12 | 前端 `pages/Notes`（外壳 + 4 组件）+ `router.tsx` | 交互闭环 |
| T13 | 全量质量门禁 + 手工验收 | ruff/mypy/pytest/eslint/tsc/build 全绿 |

---

## 9. 已定决策

1. **笔记全局（无 `kb_id`）**：推翻模块 2 §8 决策 #11「文档=笔记统一进库」。
2. **添加到知识库 = 多对多关联**（引用不复制，改笔记库里同步变）；关联表复合主键 + 双 `ondelete=CASCADE`。
3. **采集同步**：`POST /notes/from-url` 同步等抓取+摘要完成，前端 loading + 30s 超时；不做 `pending→done` 状态机（网页抓取秒级，单用户本地）。
4. **正文转 Markdown**：图片不抓、保留原图链接，能加载就加载。
5. **摘要 DeepSeek 生成**：失败降级截正文前 200 字；默认 `llm_provider=fake`，配置 `.env` 后真实生效。
6. **阅读态（可折叠正文）**：正文默认折叠、点展开；TipTap 编辑留模块 3 其余部分。
7. **本次只做 `url` 类型**：`type` 预留 `markdown`，普通空白笔记留模块 3。
8. **抓取失败 → 422 `fetch_failed`**，不建笔记；`FetchError` 复用给模块 4 的 URL 解析。
9. **删除笔记 = 硬删除**，关联随 `ondelete=CASCADE` 清；知识库被删时关联清、笔记本体保留。
10. **URL 校验**：仅要求 `http/https` + 非空（不强校验域名后缀）。
11. **添加到知识库幂等**：重复添加静默成功（204），靠唯一约束兜底。
12. **正文渲染用 `react-markdown` + `remark-gfm`**（只读）。
