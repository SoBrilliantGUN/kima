# 模块 2：知识库管理 — 详细设计

> 日期：2026-09-12
> 状态：已评审定稿，可直接开工
> 上游基线：`docs/requirements.md` · `docs/module-1-infrastructure.md`

本模块交付知识库的**完整 CRUD**（增删改查 + 列表分页）+ 封面颜色 + 空壳详情页。功能刻意克制（**纯 CRUD，不加置顶/收藏/排序/复制/回收站**），把工程深度做到教科书级：分层、校验、领域异常、测试、迁移、文档每一环都经得起推敲，作为模块 3~6 复用的样板。

---

## 1. 目标与验收

目标：知识库实体在 `router → service → repository` 三层链路下走通完整增删改查；领域异常统一映射为标准错误响应；前端「列表 → 新建 → 编辑 → 详情 → 删除」闭环可用；后端 `ruff/mypy/pytest`、前端 `eslint/tsc/build` 全绿。

**验收标准（Definition of Done）**

| # | 验收项 |
|---|---|
| 1 | `alembic upgrade head` 成功，新增 `knowledge_bases` 表（含 `uq_knowledge_bases_name` 唯一约束） |
| 2 | 五个端点可用：列表（分页）/ 创建 / 详情 / 更新 / 删除，`response_model` 齐全，OpenAPI 文档自动生成 |
| 3 | 名称唯一性冲突返回 `409`，不存在的 id 返回 `404`，非法输入返回 `422`，错误体为统一 `{"detail":{"code","message"}}` |
| 4 | 前端列表/详情/新建/编辑/删除全流程可用，卡片点击进详情，删除二次确认 |
| 5 | 后端 `ruff` + `mypy(strict)` + `pytest` 全绿；前端 `eslint` + `tsc --noEmit` + `vite build` 全绿 |
| 6 | 测试无需真库：service 单元测试 + API 集成测试均用内存 Fake 实现 |

---

## 2. 数据模型

### 2.1 `knowledge_bases` 表（迁移 `0002_knowledge_bases`）

| 字段 | 类型 | 约束 / 默认 |
|---|---|---|
| `id` | `UUID`（PG native） | PK，app 端 `uuid.uuid4` 默认 |
| `name` | `String(255)` | **UNIQUE**、非空 |
| `description` | `Text` | 可空 |
| `color` | `String(7)` | 非空，默认 `#5B8DEF`（`#RRGGBB`） |
| `created_at` / `updated_at` | `DateTime(timezone)` | 继承 `TimestampMixin` |

### 2.2 ORM 模型（`models/knowledge_base.py`）

```python
import uuid

from sqlalchemy import String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

DEFAULT_KB_COLOR = "#5B8DEF"


class KnowledgeBase(Base, TimestampMixin):
    __tablename__ = "knowledge_bases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str] = mapped_column(String(7), nullable=False, default=DEFAULT_KB_COLOR)
```

- `id` 用 `uuid.UUID` + app 端 `default=uuid.uuid4`：**避免自增 id 泄露数量**，将来多用户/分布式无需改造。
- `name` 唯一约束由命名约定生成 `uq_knowledge_bases_name`。
- **默认色真源**：业务层（service）统一赋值，模型 `default=` 兜底（防绕过 service 的裸写缺色）。
- 迁移**手写**（不依赖 autogenerate）：显式声明唯一约束与 uuid 列，`downgrade` 完整（`op.drop_table`），保证可回滚。

### 2.3 级联（预留，不在本模块落 DDL）

本模块 `knowledge_bases` 尚无子表。模块 3/4 建 `notes` / `documents` 时用 `ForeignKey(..., ondelete="CASCADE")` 一次性接上，实现「删库连带清空文档/笔记」的硬删除语义（见 §8 决策 #2）。

---

## 3. 后端分层与接口

沿 `router → service → repository` 三层，每层单一职责；service 依赖 repository 的 **Protocol 接口**（对齐模块 1 的 `async + Protocol` 惯例），可注入测试替身。

```
models/knowledge_base.py
schemas/knowledge_base.py         # Create / Update / Read / List
repositories/knowledge_base.py    # KnowledgeBaseRepository(Protocol) + SqlAlchemyKnowledgeBaseRepository
services/knowledge_base.py        # KnowledgeBaseService
api/routes/knowledge_bases.py     # 5 个端点
core/exceptions.py                # 领域异常 + 全局 handler 注册
```

### 3.1 Repository（`repositories/knowledge_base.py`）

```python
class KnowledgeBaseRepository(Protocol):
    async def list(self, *, limit: int, offset: int) -> tuple[list[KnowledgeBase], int]: ...
    async def get(self, kb_id: uuid.UUID) -> KnowledgeBase | None: ...
    async def get_by_name(self, name: str) -> KnowledgeBase | None: ...
    async def add(self, kb: KnowledgeBase) -> KnowledgeBase: ...
    async def update(self, kb: KnowledgeBase) -> KnowledgeBase: ...
    async def delete(self, kb: KnowledgeBase) -> None: ...
```

- `SqlAlchemyKnowledgeBaseRepository`：持有 `AsyncSession`；`list` 用 `count(*)` + `select().order_by(updated_at.desc(), id.asc())` 做分页与稳定排序（`id` 作 tiebreaker 保证同秒更新不跳序）。
- `add` / `update` 内部 `commit` 并 `refresh` 回填 `created_at`/`updated_at`（`server_default` 由 DB 生成）；`delete` 后 `commit`。
- `get_by_name` 仅供唯一性校验用。

### 3.2 Service（`services/knowledge_base.py`）

业务规则集中于此，**不 import web 层**：

| 方法 | 规则 |
|---|---|
| `list` | 委托 repo；对 `limit/offset` 做边界钳制（`limit` 1~100，`offset ≥ 0`） |
| `create` | 名称 trim 后查重 → 重复抛 `ConflictError`；`color` 缺省补 `DEFAULT_KB_COLOR`；落库 |
| `get` | 查无抛 `NotFoundError` |
| `update` | 先 `get`（不存在 404）；若改 `name` 且与他库撞名 → `ConflictError`；其余字段 trim/校验后落库 |
| `ensure_default` | 幂等预置：若无任何知识库则创建「我的知识库」（应用启动时调用） |
| `delete` | 先 `get`（不存在 404）；若为最后一个知识库抛 `ConflictError(last_knowledge_base)`；否则删除 |

### 3.3 Router（`api/routes/knowledge_bases.py`）

| 方法 | 路径 | 成功 | 失败 |
|---|---|---|---|
| GET | `/api/knowledge-bases?limit=&offset=` | 200 `KnowledgeBaseList` | — |
| POST | `/api/knowledge-bases` | 201 `KnowledgeBaseRead` | 409 / 422 |
| GET | `/api/knowledge-bases/{kb_id}` | 200 `KnowledgeBaseRead` | 404 |
| PATCH | `/api/knowledge-bases/{kb_id}` | 200 `KnowledgeBaseRead` | 404 / 409 / 422 |
| DELETE | `/api/knowledge-bases/{kb_id}` | 204 | 404 / 409 |

- 路径参数 `kb_id: uuid.UUID`（FastAPI 自动校验 uuid 格式，非法即 422）。
- 列表默认 `limit=50, offset=0`，响应 `{"items": [...], "total": N}`。

### 3.4 Schema（`schemas/knowledge_base.py`）

```python
class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")

class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    # @model_validator(mode="after") 保证至少一个字段非 None

class KnowledgeBaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    description: str | None
    color: str
    created_at: datetime
    updated_at: datetime

class KnowledgeBaseList(BaseModel):
    items: list[KnowledgeBaseRead]
    total: int
```

- `name` / `description` 用 `field_validator(mode="before")` 做 `strip`（去除首尾空白），再走长度校验。
- `color` 用 `pattern` 正则约束 `#RRGGBB` 格式，非法即 422。

---

## 4. 领域异常与统一错误响应（核心样板）

需求「解耦原则」要求 service 层不依赖 web 层，故**不用 `HTTPException`**，改抛领域异常，由全局 handler 统一映射。

### 4.1 异常定义（`core/exceptions.py`）

```python
class DomainError(Exception):
    status_code: int = 400
    code: str = "domain_error"

class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"

class ConflictError(DomainError):
    status_code = 409
    code = "conflict"
```

### 4.2 全局 handler（`main.py` 注册）

```python
@app.exception_handler(DomainError)
async def domain_error_handler(_, exc: DomainError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": {"code": exc.code, "message": str(exc)}},
    )
```

统一错误体：`{"detail": {"code": "conflict", "message": "知识库名称已存在"}}`。

- 前端 `ApiError` 解析 `detail.message`，能向用户展示具体文案（如「名称已存在」）。
- Pydantic 校验错误（422）保持 FastAPI 默认结构，本模块不强求统一（避免过度设计）。

### 4.3 依赖注入（`api/deps.py` 增补）

```python
def get_kb_repository(session: DBSessionDep) -> KnowledgeBaseRepository: ...
def get_kb_service(repo: KnowledgeBaseRepositoryDep) -> KnowledgeBaseService: ...
```

测试通过 `dependency_overrides` 替换 `get_kb_repository`（或 `get_kb_service`）注入内存 Fake，**无需真库**。

---

## 5. 测试策略（少而深，不起真库）

分层测试，契合「只审模块 2、不碰基建/CI」：

| 层 | 测试 | 方式 |
|---|---|---|
| Service 单元 | `test_services_knowledge_base.py` | 注入内存 `FakeKnowledgeBaseRepository`（dict 存储），测唯一性冲突、默认色、404/409 判定、update 撞名 |
| API 集成 | `test_api_knowledge_bases.py` | `app.dependency_overrides` 换 Fake repo，`AsyncClient` 走 5 个端点，断言状态码 + 响应体 + 错误信封 + 分页形状 |
| Repository | （薄层，由 API 集成测试覆盖） | 纯 SQLAlchemy 委托，不单独起真库 |

- `FakeKnowledgeBaseRepository` 实现与 `SqlAlchemy` 版同签名（满足 Protocol），存内存 dict、模拟 `list` 排序与 `get_by_name` 查重。
- 关键用例：创建→列表含 1 条；重复名→409；查无→404；PATCH 撞名→409；DELETE 后查无→404；非法 color→422；默认色被补上。

---

## 6. 前端

### 6.1 文件清单

```
api/client.ts                     # 补 post/patch/delete；ApiError 解析响应体 detail.message
api/types.ts                      # KnowledgeBase / Create / Update / ListResponse
api/knowledgeBases.ts             # list/get/create/update/remove 端点封装
hooks/useKnowledgeBases.ts        # react-query hooks + 失效重查
pages/KnowledgeBasePage/
  index.tsx                       # 三栏外壳（/knowledge-bases/:id）
  components/                     # 页面专用子组件（非公共组件，不放 src/components）
    KnowledgeBaseFormModal.tsx    # 新建/编辑共用表单（name/description/color 色板）
    KnowledgeBaseListPane.tsx     # 300px 左栏：知识库列表 + 新建/编辑/删除
    ContentListPane.tsx           # 550px 中栏：知识库头 + 内容空态（新建笔记占位）
    QaPanel.tsx                   # 右栏：问答聊天面板（占位，模块 5 接入）
pages/KnowledgeBaseRedirect/
  index.tsx                       # /knowledge-bases 重定向到第一个知识库
router.tsx                        # /knowledge-bases 重定向 + /knowledge-bases/:id
```

### 6.2 交互与路由

| 路径 | 行为 |
|---|---|
| `/knowledge-bases` | 重定向到第一个知识库的 `/knowledge-bases/:id` |
| `/knowledge-bases/:id` | 三栏同屏：左 300px 知识库列表（选中高亮，hover 编辑/删除，只剩一个库时隐藏删除）｜中 550px 内容列表（知识库头 + 空态「知识库里什么也没有，去这里添加→新建笔记」占位）｜右问答面板（占剩余，模块 5 接入） |

- 新建/编辑共用 `KnowledgeBaseFormModal`：`name` 必填、`description` 选填、`color` 从预设色板选；提交调对应 hook，409 显示「名称已存在」。
- 删除二次确认，详情页删除成功后 `navigate` 回列表。
- 数据请求走 react-query：`useKnowledgeBases`（列表）、`useKnowledgeBase(id)`（详情）、`useCreate/useUpdate/useDeleteKnowledgeBase`（变更后 `invalidateQueries(['knowledge-bases'])`）。
- `client.ts` 补 `post/patch/delete`（现在只有 `get`），`ApiError` 解析后端 `detail`（对象或字符串两种形态）暴露 `message` 字段。

---

## 7. 实现顺序（task 列表）

| 步骤 | 内容 | 产出 |
|---|---|---|
| T1 | 模型 + 迁移 `0002_knowledge_bases`（手写 up/down） | 可 `upgrade head` 建表 |
| T2 | `schemas/knowledge_base.py`（Create/Update/Read/List） | 校验模型 |
| T3 | `core/exceptions.py` + `main.py` 全局 handler | 领域异常 + 统一错误体 |
| T4 | `repositories/knowledge_base.py`（Protocol + SQLAlchemy 实现）+ `deps.py` 增补 | 数据访问层 |
| T5 | `services/knowledge_base.py` | 业务规则层 |
| T6 | `api/routes/knowledge_bases.py` + `routes/__init__.py` 注册 | 5 个端点 |
| T7 | 后端测试：service 单元 + API 集成（内存 Fake） | pytest 全绿 |
| T8 | 前端 `client.ts`/`types.ts`/`knowledgeBases.ts`/hooks | 数据层 |
| T9 | 前端组件 + 两个页面 + 路由 | 交互闭环 |
| T10 | 全量质量门禁 + 手工验收 | ruff/mypy/pytest/eslint/tsc/build 全绿 |

---

## 8. 已定决策

1. **主键**：`UUID v4`，app 端 `uuid.uuid4` 生成（非自增）。
2. **删除语义**：**硬删除 + 级联**；级联 DDL 由模块 3/4 建子表时以 `ondelete="CASCADE"` 落，本模块只做 `DELETE` 本体。
3. **封面**：仅 `color`（hex，默认 `#5B8DEF`），不引入 icon/emoji。
4. **详情页**：本模块建空壳（头部 + 文档/笔记空状态），内容由模块 3/4 填充。
5. **错误处理**：领域异常（`NotFoundError`/`ConflictError`）+ 全局 handler，service 层不依赖 web 层。
6. **功能取舍（少而深）**：纯 CRUD，**不加**置顶/收藏/手动排序/复制/回收站。
7. **测试策略**：内存 Fake repo 做单元 + 集成测试，不起真库、不改 CI。
8. **名称唯一**：`name` UNIQUE 约束 + service 查重，冲突返回 409。
9. **前端视觉（修订 2026-09-13）**：界面复刻 ima——窄图标侧栏 + 知识库页三栏（知识库列表 300px | 内容列表 550px | 问答面板剩余）+ 启动预置「我的知识库」+ 不能删最后一个库；仅删减单用户不适用功能；封面沿用颜色块。
10. **预置与删除下限**：应用启动幂等预置「我的知识库」；删除最后一个知识库被拒（409 `last_knowledge_base`）。
11. **文档 = 笔记（同一类内容）**：不分 tab，笔记即 markdown 文档，与 pdf/html 等统一列表；数据模型在模块 3/4 据此设计。
