# 模块 3：笔记/编辑器 — 详细设计

> 日期：2026-09-14
> 状态：已实现
> 上游基线：`docs/requirements.md`（决策 #15/#16/#18）· `docs/module-2-knowledge-bases.md`

本模块交付「**笔记**」完整能力：

- **空白笔记**：新建，默认标题「无标题笔记」
- **编辑器**：TipTap 所见即所得 + Markdown 快捷输入，**打开即编辑**（顶部工具栏 + 标题 + 写作区，自动保存）
- **关联与闭环**：添加到知识库 + 知识库内容列表展示关联笔记 + 知识库内「添加内容」新建笔记

核心数据关系（推翻模块 2 §8 决策 #11「文档=笔记统一进库」）：**笔记是全局内容**（不挂知识库），通过「添加到知识库」建立 `note ↔ 知识库` 的**多对多关联**（引用而非复制，改笔记库里同步变）。

**明确后置（不在本模块）**：网页采集统一归入文档（模块 4，`documents.source_type=url`）；AI 帮写（续写/扩写/润色）、联动知识库写作 → 模块 5 之后；笔记内搜索/标签/目录 → 后续；笔记向量化 → 模块 5（笔记可变需编辑重向量化）。

---

## 1. 目标与验收

目标：空白笔记新建 + TipTap 编辑/保存可用；笔记列表 + 编辑器（打开即编辑）+ 添加到知识库 + 删除均可用；知识库内容列表正确展示关联笔记、知识库内「添加内容」可新建笔记并自动关联；后端 `ruff/mypy/pytest`、前端 `eslint/tsc/build` 全绿。

**验收标准（Definition of Done）**

| # | 验收项 |
|---|---|
| 1 | `alembic upgrade head` 成功，新增 `notes`（`title` + `content_markdown`）+ `note_knowledge_bases`（复合主键 + 双 `ondelete=CASCADE` 外键） |
| 2 | 六个端点可用：创建（空白）/ 列表 / 详情 / 更新 / 删除 / 添加到知识库，`response_model` 齐全；创建端点支持可选 `knowledge_base_id`（传了即原子建关联） |
| 3 | 知识库内容列表端点返回异构条目（`note` + `document`），「添加到知识库」/「知识库内新建」后该库内容列表可见此笔记 |
| 4 | 不存在的笔记/知识库返回 `404`；空 update body 返回 `422`，错误体统一 `{"detail":{"code","message"}}` |
| 5 | 前端「新建 → 空白笔记 → 打开编辑态」闭环可用；编辑器含顶部工具栏 + 标题 + 写作区 + 自动保存 |
| 6 | 编辑器：TipTap 所见即所得 + Markdown 快捷输入（`# `→标题、`- `→列表、`**x**`→加粗等）生效；打开笔记正文正确回填；保存后刷新仍一致 |
| 7 | 保存：正文自动保存（debounce）+ 标题失焦保存 |
| 8 | 后端 `ruff` + `mypy(strict)` + `pytest` 全绿；前端 `eslint` + `tsc --noEmit` + `vite build` 全绿 |
| 9 | 测试无需真库：service 单元 + API 集成均用内存 Fake（repo） |

---

## 2. 数据模型

### 2.1 `notes` 表 + `note_knowledge_bases` 表（迁移 `0003_notes`）

**`notes`**（全局笔记，**无 `kb_id`**，纯 Markdown）

| 字段 | 类型 | 约束 / 默认 |
|---|---|---|
| `id` | `UUID`（PG native） | PK，app 端 `uuid.uuid4` |
| `title` | `String(255)` | 非空，默认「无标题笔记」 |
| `content_markdown` | `Text` | 非空，默认空串 |
| `created_at` / `updated_at` | `DateTime(timezone)` | 继承 `TimestampMixin` |

**`note_knowledge_bases`**（关联表，多对多）

| 字段 | 类型 | 约束 |
|---|---|---|
| `note_id` | `UUID` | PK 之一，`FK → notes.id ON DELETE CASCADE` |
| `knowledge_base_id` | `UUID` | PK 之一，`FK → knowledge_bases.id ON DELETE CASCADE` |
| `created_at` | `DateTime(timezone)` | `server_default now()` |

- 复合主键 `(note_id, knowledge_base_id)` 即唯一约束，天然幂等。
- 级联方向：删笔记 → 清其关联；删知识库 → 清其关联（**笔记本体保留**，因为笔记全局）。
- 无 `type`/`summary`/`source_url`：笔记是纯 Markdown 空白笔记，网页采集统一归入文档（模块 4，`documents.source_type=url`）。

### 2.2 ORM 模型（`models/note.py`）

```python
import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, Table, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

DEFAULT_NOTE_TITLE = "无标题笔记"


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
    """笔记实体（全局，不挂知识库）。纯 Markdown 空白笔记。"""

    __tablename__ = "notes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")
```

- 关联表用 `Table`（非 ORM 实体），`models/__init__.py` 导出 `Note`、`note_knowledge_bases`。

### 2.3 迁移 `0003_notes`（手写，`down_revision = "0002_knowledge_bases"`）

- `upgrade`：`op.create_table("notes", …)` → `op.create_table("note_knowledge_bases", …)`，FK 名走 naming convention：`fk_note_knowledge_bases_note_id_notes`、`fk_note_knowledge_bases_knowledge_base_id_knowledge_bases`。
- `downgrade`：先 `drop_table("note_knowledge_bases")` 再 `drop_table("notes")`。

---

## 3. 后端分层与接口

沿 `router → service → repository` 三层，依赖注入走 `api/deps.py`（`Depends` + 类型别名，同模块 2）。

```
models/note.py                     # Note + note_knowledge_bases
schemas/note.py                    # NoteCreate / NoteUpdate / NoteRead / NoteList / NoteAddToKnowledgeBase
schemas/knowledge_base.py          # + ContentItem / ContentList（KB 内容列表异构条目）
repositories/note.py               # NoteRepository(Protocol) + SqlAlchemyNoteRepository
services/note.py                   # NoteService
api/routes/notes.py                # 6 个笔记端点
api/routes/knowledge_bases.py      # + GET /knowledge-bases/{kb_id}/contents
```

### 3.1 Repository（`repositories/note.py`）

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

### 3.2 Service（`services/note.py`）

| 方法 | 规则 |
|---|---|
| `create_blank(payload)` | 若 kb_id：先 `kb_repo.get` 校验（404）→ 建 `Note(title=payload.title 或 DEFAULT_NOTE_TITLE, content_markdown="")` → `repo.add` → 若 kb_id：`repo.associate` |
| `list(limit, offset)` | 边界钳制（`limit` 1~100）后委托 repo |
| `get(note_id)` | 查无抛 `NotFoundError("笔记不存在")` |
| `update(note_id, title?, content_markdown?)` | 先 `get`（404）→ 非 None 字段 trim 后赋值 → `repo.update` |
| `delete(note_id)` | 先 `get`（404）→ `repo.delete` |
| `add_to_kb(note_id, kb_id)` | 先 `get` 笔记（404）→ `kb_repo.get` 校验知识库存在（404）→ `repo.associate`（幂等，重复添加静默成功） |
| `list_by_kb(kb_id)` | 先 `kb_repo.get` 校验知识库存在（404）→ `repo.list_by_kb` |

- `NoteService` 依赖 `NoteRepository + KnowledgeBaseRepository`（注入时 2 个替身皆可换）。
- **kb_id 校验前置**：创建前先校验知识库存在，避免「建了笔记但关联失败」的孤儿笔记。

### 3.3 Router（`api/routes/notes.py`，`prefix="/notes"`）

| 方法 | 路径 | 成功 | 失败 |
|---|---|---|---|
| POST | `/api/notes` | 201 `NoteRead` | 422 · 404（kb_id 不存在） |
| GET | `/api/notes?limit=&offset=` | 200 `NoteList` | — |
| GET | `/api/notes/{note_id}` | 200 `NoteRead` | 404 |
| PATCH | `/api/notes/{note_id}` | 200 `NoteRead` | 404 / 422 |
| DELETE | `/api/notes/{note_id}` | 204 | 404 |
| POST | `/api/notes/{note_id}/knowledge-bases` | 204 | 404 |

- 列表默认 `limit=50, offset=0`，响应 `{"items": [...], "total": N}`。
- 路径参数 `note_id: uuid.UUID`（非法即 422）。

### 3.4 知识库内容列表（`api/routes/knowledge_bases.py` 新增）

| 方法 | 路径 | 成功 | 失败 |
|---|---|---|---|
| GET | `/api/knowledge-bases/{kb_id}/contents` | 200 `ContentList` | 404 |

- **异构列表**：`ContentList.items` 为带 `type` 判别字段的联合条目，含 `type="note"` 与 `type="document"`（模块 4），前端据此区分渲染。
- 模块 2 时 `ContentListPane` 只是空态占位，本模块**新建此端点**（而非单独 `notes-by-kb` 端点），模块 4 文档接入时同端点扩展。

### 3.5 Schema（`schemas/note.py` + `schemas/knowledge_base.py`）

```python
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
    content_markdown: str
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
    type: Literal["note", "document"]
    note: NoteRead | None = None
    document: DocumentRead | None = None   # 模块 4

class ContentList(BaseModel):
    items: list[ContentItem]
    total: int
```

- `NoteUpdate` 用 `model_validator` 保证非空更新，避免无意义 PATCH。

---

## 4. 编辑器选型（前端核心）

### 4.1 选型与双向转换

- **TipTap**（`@tiptap/react` + `@tiptap/starter-kit`）：所见即所得编辑器。
- **Markdown 快捷输入**：StarterKit 自带 `# `→标题、`- `→无序列表、`> `→引用、`` ` ``→代码块、`---`→分割线等 InputRule；Bold/Italic/Strike（`**x**`/`*x*`/`~~x~~`）的输入规则也在 StarterKit 内。
- **`tiptap-markdown`**（aguingand）：负责 TipTap 文档 ↔ Markdown 双向序列化，这是「底层存 Markdown」的关键：
  - 打开笔记：`editor.commands.setContent(note.content_markdown)`（parse Markdown → ProseMirror 文档）
  - 保存正文：`editor.getMarkdown()`（serialize → Markdown）→ PATCH
- **界面参考 ima**（打开即编辑，无读态）：编辑器 = **顶部工具栏 + 标题 + 写作区**。
- **顶部工具栏**：固定工具栏（对齐 ima），按钮集 = 撤销/重做、加粗、斜体、删除线、H1/H2/H3、无序列表、有序列表、引用、代码块、行内代码。工具栏按钮与 Markdown 快捷输入并存。

### 4.2 页面布局（打开即编辑）

- **笔记 tab**：左「笔记列表」+ 右「编辑器」，点开笔记**直接进入编辑器**（无读态/编辑态分离，无 `?edit=1` 路由）。
- **编辑器从上到下**：顶部工具栏 → 标题输入框（失焦保存）→ 写作区（TipTap 正文，直接可编辑）。
- **列表项预览**：标题 + 正文前 N 字截断。

### 4.3 保存（混合，自动保存）

| 目标 | 触发 | 调用 |
|---|---|---|
| 正文 `content_markdown` | `onUpdate` debounce（~800ms）自动保存 | `PATCH {content_markdown}` |
| 标题 `title` | `onBlur` 失焦（或 Enter） | `PATCH {title}` |
| 离开编辑页 | 路由切换前 | flush 未保存的 debounce + 保存脏标题 |

- 两者共用 `PATCH /notes/{id}` 部分更新，互不干扰（对齐 ima「自动保存，无需 Ctrl+S」）。
- 前端用 react-query 的 `useUpdateNote` mutation，成功后 `invalidateQueries(['notes'])` + 更新 `['notes', id]` 缓存。

---

## 5. 前端

### 5.1 文件清单

```
api/types.ts                      # Note / NoteCreate / NoteUpdate / NoteListResponse / NoteAddToKnowledgeBase / ContentItem / ContentListResponse
api/notes.ts                      # createNote / listNotes / getNote / updateNote / deleteNote / addNoteToKnowledgeBase
api/knowledgeBases.ts             # + listKbContents
hooks/useNotes.ts                 # react-query hooks + 失效重查
pages/Notes/
  index.tsx                       # 两栏外壳（/notes 与 /notes/:id 共用，useParams 取选中）
  components/                     # 页面专用子组件
    NoteListPane.tsx              # 左栏：新建空白笔记 + 列表 + hover 删除 + 空态
    NoteEditorPane.tsx            # 右栏编辑器：工具栏 + 标题 + TipTap 写作区
    NoteEditorToolbar.tsx         # 工具栏按钮组
    AddToKnowledgeBaseModal.tsx   # 选知识库（复用 useKnowledgeBases）
pages/KnowledgeBasePage/
  components/
    ContentListPane.tsx           # 中栏内容列表：合并 documents + 关联 notes + 「添加内容」下拉
    AddContentMenu.tsx            # 「添加内容」下拉：本地文档 / URL 文档（笔记不再从知识库新建）
router.tsx                        # + /notes/:id（打开即编辑，无 ?edit=1）
```

### 5.2 交互与路由

| 路径 | 行为 |
|---|---|
| `/notes` | 左列表 + 右空态（「从左侧选择或新建一条笔记」） |
| `/notes/:id` | 左列表（选中高亮）+ 右编辑器（打开即编辑） |

- 选中走 URL（对齐决策 #9「选中状态走 URL」）；**打开即编辑，无读态/编辑态切换、无 `?edit=1`**。不设重定向（笔记可为空，与知识库「至少一个」不同）。
- 笔记 tab「新建」→ 空白笔记 → `useCreateNote` → `navigate('/notes/' + id)`（直接编辑态）。
- 编辑器：顶部工具栏（加粗/斜体/标题/列表/引用/代码等）+ 标题（失焦保存）+ TipTap 写作区（debounce 自动保存）。
- 「添加到知识库」（编辑器内）→ `AddToKnowledgeBaseModal`（复用 `useKnowledgeBases` 选库）→ `useAddNoteToKnowledgeBase`；重复添加幂等。
- 删除（列表 hover / 编辑器内）二次确认 → `useDeleteNote` → 成功后 `navigate('/notes')`。
- 知识库页内容列表：`useKbContents(kb_id)` 拉 `ContentList`，渲染关联笔记（带 `type` 判别，与 documents 混合），点击跳 `/notes/:id`。
- 数据请求走 react-query：`useNotes`（无限列表）、`useNote(id)`、`useCreateNote`、`useUpdateNote`、`useDeleteNote`、`useAddNoteToKnowledgeBase`（变更后 `invalidateQueries(['notes', 'knowledge-bases'])`）、`useKbContents`。

### 5.3 新增前端依赖

- `@tiptap/react` + `@tiptap/starter-kit` + `@tiptap/extension-image` + `@tiptap/extension-placeholder` + `tiptap-markdown`。
- `@tiptap/extension-image`：让 TipTap 渲染/编辑 `![...](url)` 图片节点（正文中的 Markdown 图片链接）。
- **不引入** `react-markdown`/`remark-gfm`（正文由 TipTap 渲染/编辑，无只读 Markdown 展示场景）。

---

## 6. 测试策略（少而深，不起真库）

| 层 | 测试 | 方式 |
|---|---|---|
| Service 单元 | `test_services_note.py` | 注入 `FakeNoteRepository` + `FakeKnowledgeBaseRepository`；测 create_blank（默认标题/带 kb_id 关联/kb 不存在 404）、update（成功/404/空 body）、list 钳制、get/delete 404、add_to_kb（成功/重复幂等/笔记不存在/库不存在）、list_by_kb（库不存在 404） |
| API 集成 | `test_api_notes.py` | `app.dependency_overrides` 换 Fake，`AsyncClient` 走 6 端点 + `GET /knowledge-bases/{kb_id}/contents`，断言状态码 + 响应体 + 错误信封 + 分页形状 |
| 集成 Fake | `tests/fakes.py` 增补 | `FakeNoteRepository`（dict 存储、模拟 list 排序 + associate 幂等 + update + list_by_kb） |

- `FakeNoteRepository` 与 `SqlAlchemy` 版同签名（满足 Protocol）。
- 关键用例：create_blank→201 标题「无标题笔记」；create_blank 带 kb_id→关联建立；create_blank 带不存在的 kb_id→404 且不建笔记；PATCH 标题/正文→200 且 updated_at 变；空 update body→422；删除→404；重复 add_to_kb→幂等仍 204；KB 内容列表→含已关联笔记、未关联不含。

---

## 7. 实现顺序（task 列表）

| 步骤 | 内容 | 产出 |
|---|---|---|
| T1 | `package.json` 加 TipTap 系依赖 | 依赖可装 |
| T2 | `models/note.py` + 迁移 `0003_notes`（手写 up/down）+ `models/__init__.py` 导出 | 可 `upgrade head` 建表 |
| T3 | `schemas/note.py`（NoteCreate/NoteUpdate + knowledge_base_id）+ `schemas/knowledge_base.py` 加 ContentItem/ContentList | 校验模型 |
| T4 | `repositories/note.py`（含 update/list_by_kb）+ `deps.py` 增补 note repo/service | 数据访问层 |
| T5 | `services/note.py`（create_blank/update/list_by_kb/kb_id 关联） | 业务规则层 |
| T6 | `api/routes/notes.py`（6 端点）+ `api/routes/knowledge_bases.py` 加 contents 端点 + `routes/__init__.py` 注册 | 接口层 |
| T7 | 后端测试：`fakes.py` 增补 + service 单元 + API 集成 | pytest 全绿 |
| T8 | 前端 `types.ts`/`notes.ts`/`knowledgeBases.ts`/`useNotes.ts` | 数据层 |
| T9 | 前端 `pages/Notes`（外壳 + 列表 + 删除/添加 modal） | 创建闭环 |
| T10 | 前端 `NoteEditorPane`（工具栏 + 标题 + TipTap + 自动保存） | 编辑闭环 |
| T11 | 前端知识库「添加内容」下拉 + `ContentListPane` 展示关联笔记 | 知识库侧收口 |
| T12 | 全量质量门禁 + 手工验收 | ruff/mypy/pytest/eslint/tsc/build 全绿 |

---

## 8. 已定决策

1. **笔记全局（无 `kb_id`）**：推翻模块 2 §8 决策 #11「文档=笔记统一进库」。
2. **添加到知识库 = 多对多关联**（引用不复制，改笔记库里同步变）；关联表复合主键 + 双 `ondelete=CASCADE`。
3. **纯 Markdown 空白笔记**：无 `type`/`summary`/`source_url`；网页采集统一归入文档（模块 4，`documents.source_type=url`）。
4. **编辑器形态**：TipTap 所见即所得 + Markdown 快捷输入；底层存 Markdown（`tiptap-markdown` 双向序列化）。
5. **打开即编辑（无读态）**：对齐 ima「列表 + 工具栏 + 写作区」，点开笔记直接编辑，无读态/编辑态分离、无 `?edit=1` 路由。
6. **编辑器界面参考 ima**：顶部工具栏（加粗/斜体/标题/列表/引用/代码等）+ 标题 + 写作区。
7. **保存（混合自动）**：正文 debounce 自动保存 + 标题失焦保存（对齐 ima「无需 Ctrl+S」）；离开前 flush。
8. **空白笔记默认标题**：落库「无标题笔记」（`title` 保持非空，schema 不变）。
9. **知识库内新建**：知识库页「添加内容」下拉 = 本地文档 / URL 文档（模块 4）；后端给笔记创建端点加可选 `knowledge_base_id`，原子建「笔记 + 关联」，kb 校验前置避免孤儿笔记。
10. **知识库内容列表展示关联笔记**：本模块收口（新建 `GET /knowledge-bases/{kb_id}/contents` 异构端点，含 notes + documents）。
11. **后置项**：AI 帮写 / 联动知识库写作 → 模块 5 之后；笔记内搜索/标签/目录 → 后续；笔记向量化 → 模块 5；本地文档导入 → 模块 4。
12. **删除笔记 = 硬删除**，关联随 `ondelete=CASCADE` 清；知识库被删时关联清、笔记本体保留。
13. **正文由 TipTap 渲染/编辑**：不引入 `react-markdown`/`remark-gfm`。
