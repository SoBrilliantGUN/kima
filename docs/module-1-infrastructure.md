# 模块 1：基础设施 — 详细设计

> 日期：2026-09-11
> 状态：已评审定稿（面试官严审通过），可直接开工
> 上游基线：`docs/requirements.md`

本模块不交付业务功能，交付一套**可运行、可复现、可扩展**的骨架：前后端脚手架 + 数据库 + 三个可替换集成抽象 + 前端布局 + CI/类型检查质量门禁。后续模块 2~6 在此骨架上填充业务。

---

## 1. 目标与验收

目标：`docker compose up` 一键跑通 Postgres（含 vector 扩展）；后端 `/health/ready` 返回含 DB 连通性的 OK；`alembic upgrade head` 成功；前端 dev server 渲染出「左导航 + 主区 + 右侧 AI 侧栏」布局；CI 全绿。

**验收标准（Definition of Done）**

| # | 验收项 |
|---|---|
| 1 | `docker compose up -d` 拉起 pgvector 容器且 `pg_isready` 通过，`vector` 扩展已建 |
| 2 | `uv sync` 后后端可启动，`GET /health/live` 返回 `200`，`GET /health/ready` 返回 `200` 且 `database: ok` |
| 3 | `uv run alembic upgrade head` 成功（生成 `alembic_version` 表 + `CREATE EXTENSION vector`） |
| 4 | 前端 `pnpm dev` 启动，布局骨架正常渲染（窄图标侧栏 + 主内容区） |
| 5 | 三个集成接口的 fake 实现通过 mypy 静态检查与冒烟测试 |
| 6 | CI 全绿：`ruff` + `mypy` + `pytest`（后端）、`eslint` + `tsc --noEmit`（前端）零告警；`README.md` 记录一键启动步骤 |

---

## 2. 目录结构

### 2.1 后端 `./backend`

```
backend/
├── pyproject.toml           # uv 管理，[project] + [dependency-groups]，含 [tool.mypy] / [tool.ruff] / [tool.pytest]
├── uv.lock                  # 锁文件（生成，提交入库）
├── .python-version          # uv 用的 Python 版本（3.12）
├── .env.example             # 环境变量模板（提交）
├── .env                     # 本地实际值（gitignore，不提交）
├── alembic.ini
├── alembic/
│   ├── env.py               # async 引擎 + 读取 app 配置
│   ├── script.py.mako
│   └── versions/            # 迁移脚本（模块 1 空表基线 + CREATE EXTENSION vector）
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI 应用工厂 + 生命周期 + CORS
│   ├── api/
│   │   ├── __init__.py
│   │   ├── deps.py          # 依赖注入：settings / db_session / 集成客户端
│   │   └── routes/
│   │       ├── __init__.py  # 聚合 router
│   │       └── health.py    # GET /health/live + /health/ready
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py        # pydantic-settings 的 Settings
│   │   ├── logging.py       # JSON 结构化日志 + request-id 中间件
│   │   └── db.py            # async engine + async_sessionmaker
│   ├── models/
│   │   ├── __init__.py
│   │   └── base.py          # DeclarativeBase + 通用字段 Mixin
│   ├── schemas/
│   │   ├── __init__.py
│   │   └── health.py        # HealthResponse
│   ├── repositories/        # 模块 1 占位（__init__.py）
│   ├── services/            # 模块 1 占位（__init__.py）
│   └── integrations/
│       ├── __init__.py      # 导出三个 Protocol 与工厂函数
│       ├── llm.py           # LLMClient Protocol + FakeLLMClient
│       ├── embedding.py     # EmbeddingClient Protocol + FakeEmbeddingClient
│       └── parser.py        # DocumentParser Protocol
└── tests/
    ├── __init__.py
    ├── conftest.py          # 测试 fixtures（settings、db、client）
    ├── test_health.py       # live / ready / degraded（DB 挂 → 503）三条路径
    └── test_integrations.py # fake 实现冒烟测试 + 返回确定性断言
```

### 2.2 前端 `./frontend`

```
frontend/
├── package.json             # pnpm 管理
├── pnpm-lock.yaml
├── vite.config.ts           # @ 别名（→ src）+ dev proxy /health、/api → 后端 8000
├── tsconfig.json            # baseUrl/paths 配 @/* → src/*
├── index.html
└── src/
    ├── main.tsx             # 挂载 + QueryClientProvider + RouterProvider
    ├── App.tsx              # 渲染 AppLayout
    ├── router.tsx           # 路由表
    ├── styles/
    │   ├── tokens.css       # :root 设计令牌（CSS 自定义属性）
    │   └── global.scss      # reset + 全局基础样式
    ├── api/
    │   ├── client.ts        # 类型化 fetch 封装（baseURL、错误处理）
    │   ├── types.ts         # 与后端 schema 对齐的 TS 类型
    │   └── knowledgeBases.ts # 知识库端点封装（模块 2）
    ├── hooks/
    │   ├── useHealth.ts     # react-query 调用 /health/ready
    │   └── useKnowledgeBases.ts # 知识库 hooks（模块 2）
    ├── layout/
    │   ├── AppLayout/
    │   │   ├── index.tsx    # 两段式布局容器（Sidebar + main）
    │   │   └── index.module.scss
    │   └── Sidebar/
    │       ├── index.tsx    # 64px 窄图标侧栏
    │       └── index.module.scss
    └── pages/
        ├── Home/            # kima 首页（AI 问答主页，模块 5 实现）
        ├── Browse/          # 浏览（模块 4/5 实现）
        ├── Notes/           # 笔记（模块 3 实现）
        ├── KnowledgeBasePage/ # 知识库三栏页（模块 2）
        └── KnowledgeBaseRedirect/ # /knowledge-bases 重定向（模块 2）
```

### 2.3 仓库根（新增 CI）

```
.github/workflows/ci.yml     # 质量门禁：后端 lint+type+test，前端 lint+type+build，docker compose 冒烟
```

---

## 3. 依赖清单

### 后端（pyproject.toml）

- **运行时**：`fastapi`、`uvicorn[standard]`、`sqlalchemy[asyncio]`、`asyncpg`、`alembic`、`pydantic-settings`、`pydantic`（v2）
- **开发组**（`[dependency-groups].dev`）：`ruff`、`mypy`、`pytest`、`pytest-asyncio`、`httpx`（FastAPI TestClient 依赖）
- 注意：LLM / Embedding / MinerU 的**真实 SDK 不装**——它们走 OpenAI 兼容 HTTP 或 REST，用 `httpx` 即可。`httpx` 当前仅在 dev 组（TestClient 用）；**模块 4/5 接入真实 provider 时，需将其移入运行时依赖**。

### 前端（package.json）

- **运行时**：`react`、`react-dom`、`react-router-dom`、`@tanstack/react-query`
- **开发组**：`vite`、`typescript`、`@vitejs/plugin-react`、`sass`、`eslint`、`prettier`、类型包（`@types/react` 等）
- 注意：TipTap 留到模块 3（编辑器）再装，模块 1 不引。

---

## 4. 配置与环境变量

后端用 `pydantic-settings` 的 `Settings` 类（`core/config.py`），读 `.env` + 环境变量，字段与类型如下（`.env.example` 同构）：

```bash
# App
APP_NAME=kima
ENVIRONMENT=development          # development | production
DEBUG=true
API_PREFIX=/api                  # 仅作用于业务 router（模块 2+）；/health/* 保持裸挂供探针使用

# Database
DATABASE_URL=postgresql+asyncpg://kima:kima@localhost:5432/kima

# LLM（DeepSeek，OpenAI 兼容）
LLM_PROVIDER=fake                # fake | deepseek
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat

# Embedding（SiliconFlow）
EMBEDDING_PROVIDER=fake          # fake | siliconflow
EMBEDDING_API_KEY=
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DIM=1024

# MinerU
MINERU_API_BASE_URL=
MINERU_API_TOKEN=

# CORS
CORS_ORIGINS=http://localhost:5173
```

- **`API_PREFIX` 语义**：业务路由统一挂 `/api` 前缀，`/health/live` 与 `/health/ready` **不加前缀**、裸挂（供 LB/探针用）。避免「配了没用」的死配置。
- **`.env` 路径**：`SettingsConfigDict(env_file=...)` 用 `Path(__file__)` 定位 `backend/.env`，不依赖 CWD，从仓库根目录执行 `uv run --project backend ...` 也能读到。
- **Provider 选择机制**：`integrations/__init__.py` 提供工厂函数，按 `settings.<provider>` 字段（非直接读环境变量）返回对应实现。模块 1 只注册 `fake`，真实 provider 在模块 4/5 以同样方式注册，业务层不改一行。

---

## 5. 数据库与 Alembic

### 5.1 docker-compose.yml（根目录）

```yaml
services:
  db:
    image: pgvector/pgvector:pg16
    container_name: kima-db
    environment:
      POSTGRES_USER: kima
      POSTGRES_PASSWORD: kima
      POSTGRES_DB: kima
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U kima -d kima"]
      interval: 5s
      timeout: 3s
      retries: 10

volumes:
  pgdata:
```

> **范围说明**：模块 1 的 compose **只容器化 db**；backend/frontend 在宿主机用 `uv` / `pnpm` 运行。backend/frontend 的容器化（含 `depends_on: db: condition: service_healthy`）留到后续模块，README 中明确写出「一键起库 + 两条命令起前后端」的完整步骤，避免「一键跑通」名不副实。
> 词法检索的 pg_jieba 自建镜像推迟到模块 5（见 requirements 决策 #12）。

### 5.2 SQLAlchemy（`core/db.py`）

- `create_async_engine(DATABASE_URL)`，`pool_pre_ping=True`
- `async_sessionmaker(expire_on_commit=False)` 供依赖注入 `get_db_session()`
- ORM 基类 `models/base.py`：
  - `Base(DeclarativeBase)`，命名约定 `metadata`（用于 Alembic autogenerate 正确命名约束）
  - `TimestampMixin`：`created_at`（`server_default=func.now()`）/ `updated_at`（`onupdate=func.now()`）。**注意**：`onupdate` 是客户端侧注入（仅 ORM 路径生效，裸 SQL `UPDATE` 不会更新）；如需服务端强一致，后续可换 DB trigger，模块 1 先接受 ORM 语义。

### 5.3 Alembic

- `alembic init -t async alembic`，`env.py` 指向 `app.core.db` 与 `app.models`（`target_metadata = Base.metadata`）
- **首个迁移 = 空表基线 + 建扩展**：不建任何业务表，但**必须**包含 `op.execute("CREATE EXTENSION IF NOT EXISTS vector")`。理由：官方 `pgvector` 镜像只编译扩展、不自动建，模块 4 建 `vector` 列前必须先落此扩展；把扩展建在模块 1，避免模块 4/5 的迁移各建各的。业务表迁移随模块 2~6 各自添加，保持模块自治。

---

## 6. 三个集成抽象（核心）

三接口统一 `async + Protocol`，位于 `app/integrations/`。协议定义 + 一个 `fake` 实现 + 一个工厂函数。

### 6.1 LLM（`llm.py`）

```python
from typing import Protocol, Literal
from dataclasses import dataclass

Role = Literal["system", "user", "assistant"]

@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str

@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

class LLMClient(Protocol):
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult: ...
```

- `FakeLLMClient.chat` 返回固定文案（回显最后一条 user 内容），供模块 1 打通链路。
- 流式 `stream() -> AsyncIterator[str]` **留到模块 5** 再入协议（RAG 生成时定形），避免过早锁定签名。

### 6.2 Embedding（`embedding.py`）

```python
from typing import Protocol

class EmbeddingClient(Protocol):
    @property
    def dimension(self) -> int: ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...
```

- `dimension` 用于建 pgvector 列时确定维度（bge-m3 = 1024）。**唯一真源是 `settings.embedding_dim`**，实现类从 settings 读，不硬编码。
- `FakeEmbeddingClient` 返回固定维度的确定性伪向量（如对文本 hash 生成），并在返回前**断言 `len(vec) == self.dimension`**，防配置与实现漂移。

### 6.3 文档解析（`parser.py`）

```python
from typing import Protocol
from dataclasses import dataclass, field
from enum import StrEnum

class SourceType(StrEnum):
    PDF = "pdf"
    URL = "url"
    WORD = "word"

@dataclass(frozen=True)
class ParsedDocument:
    markdown: str
    title: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

class DocumentParser(Protocol):
    async def parse(
        self,
        *,
        source_type: SourceType,
        content: bytes | None = None,
        url: str | None = None,
    ) -> ParsedDocument: ...
```

- **入参约定**（协议层宽松，语义用文档约束）：`source_type` 与入参一一对应——`PDF/WORD → content`、`URL → url`。具体 MinerU API 的请求/响应细节到模块 4 接入时再定；此处只锁「输入 pdf/url/word 三选一 + 输出 markdown + 元数据」的语义。

### 6.4 工厂函数（`integrations/__init__.py`）

```python
def get_llm_client(settings: Settings) -> LLMClient: ...
def get_embedding_client(settings: Settings) -> EmbeddingClient: ...
def get_document_parser(settings: Settings) -> DocumentParser: ...
```

- 内部按 `settings.<provider>` 字段查表返回实例；`fake` 为默认。
- **生命周期**：工厂用 `functools.lru_cache(maxsize=1)` 或挂 `app.state` 做**单例**，避免模块 4/5 换真 `httpx.AsyncClient` 后每请求重建连接池。依赖注入层（`api/deps.py`）用 `Depends` 暴露这三个工厂，业务路由 `Depends(get_llm_client)` 拿到的是缓存实例。

---

## 7. 健康检查与日志

### 7.1 健康检查（`api/routes/health.py`）

拆 **liveness / readiness** 两个端点，区分「进程存活」与「依赖就绪」：

| 端点 | 行为 |
|---|---|
| `GET /health/live` | 恒 `200`，仅证明进程活着（不碰 DB），供探针/重启策略用 |
| `GET /health/ready` | 内部 `SELECT 1` 探测 DB；可达 → `200` + `{status: ok, database: ok}`；不可达 → **`503`** + `{status: degraded, database: error}` |

```json
{ "status": "ok", "database": "ok", "version": "0.1.0", "app": "kima" }
```

- `schemas/health.py` 定义 `HealthResponse`（Pydantic v2）。
- `test_health.py` 覆盖三条路径：live、ready（DB 正常）、ready（DB 挂 → 503，通过依赖覆盖模拟）。

### 7.2 日志（`core/logging.py`）

- 标准 `logging` + **JSON 结构化**输出（每行一个 JSON 对象，字段含 `timestamp` / `level` / `logger` / `request_id` / `message`）。
- 中间件：读取/生成 `X-Request-ID`，注入日志上下文（`contextvars` + LogRecord 扩展）并回写响应头。
- 不引 structlog，保持模块 1 轻量，但「结构化」要名副其实——输出 JSON，而非普通格式串。

---

## 8. 前端布局与设计令牌

### 8.1 两段式布局（`layout/AppLayout`）

```
┌──────────┬──────────────────────────┐
│ Sidebar  │   <Outlet/> 主内容区      │
│ 64px 窄  │  （各页面渲染处，布局由   │
│ 图标侧栏  │   页面自己声明）         │
└──────────┴──────────────────────────┘
```

- `AppLayout`：只做「Sidebar + main 内容槽位」，不判断路径、不关心页面样式；每个页面自己声明布局（普通页自带留白滚动，全屏页自管 flex 填满）。
- `Sidebar`：64px 窄图标侧栏，导航项走 `NavLink`（`/`(kima) `/knowledge-bases` `/notes` `/browse`），选中态用 `isActive`（首页 `end` 精确匹配）。
- 无独立 AI 侧栏：AI 由 `kima` 首页 tab 与知识库页右侧问答面板承载（见 requirements 决策 #2）。

### 8.2 路由（`router.tsx`）

| 路径 | 页面 | 说明 |
|---|---|---|
| `/` | Home | kima 首页（AI 问答主页，模块 5 实现） |
| `/knowledge-bases` | KnowledgeBaseRedirect | 重定向到第一个知识库 |
| `/knowledge-bases/:id` | KnowledgeBasePage | 知识库三栏页 |
| `/notes` | Notes | 笔记（模块 3 实现） |
| `/browse` | Browse | 浏览（模块 4/5 实现） |

### 8.3 设计令牌（`styles/tokens.css`）

`:root` 定义 CSS 自定义属性：颜色（`--color-bg` / `--color-surface` / `--color-text` / `--color-primary` 等）、间距刻度（`--space-1..8`）、字号/字重、圆角、阴影。不做暗色主题（见 requirements 决策 #10）。

### 8.4 API client（`api/client.ts`）

- 类型化 `fetch` 封装：`baseURL`（dev 下经 Vite proxy 指向后端）、统一 JSON 解析与错误抛出（`/health/*` 裸路径 + 业务 `/api` 前缀在 client 中统一处理）。
- `types.ts` 与后端 `schemas/` 逐字段对齐（`HealthResponse`、`KnowledgeBase` 等，随模块补充）。

---

## 9. 工具链与 CI（质量门禁）

> 面向「生产级展示」：lint、类型检查、测试、冒烟全部进 CI，作为验收 #6 的载体。

### 9.1 后端静态检查

- `ruff`：`pyproject.toml` 的 `[tool.ruff]` **锁定规则集**（如 `select = ["E", "F", "I", "UP", "B", "ASYNC"]`），保证「零告警」可复现、不随 ruff 版本漂移。
- `mypy`：`[tool.mypy]` 开启 strict 级别（或 `strict = true` 后按需放宽），覆盖 `app/` 与 `tests/`；三个 Protocol 的 fake 实现通过结构化静态检查（验收 #5 的落点）。

### 9.2 前端静态检查

- `eslint` + `prettier`（规则集在配置中锁定）。
- `tsc --noEmit`（`package.json` 的 `typecheck` script），与 eslint 并行作为 CI 门禁。

### 9.3 CI workflow（`.github/workflows/ci.yml`）

```
jobs:
  backend:  uv sync → ruff check → mypy → pytest（含 degraded 路径）
  frontend: pnpm install → eslint → tsc --noEmit → vite build
  smoke:    docker compose up -d → pg_isready + vector 扩展校验 → alembic upgrade head
```

---

## 10. 实现顺序（task 列表）

| 步骤 | 内容 | 产出 |
|---|---|---|
| T1 | 仓库根：`docker-compose.yml`、`.gitignore`、`.env.example`、`README.md`、`.github/workflows/ci.yml` | 根骨架 + CI |
| T2 | 后端脚手架：pyproject（含 ruff/mypy/pytest 配置）+ app 分层 + main.py + config/logging/db | 可 import 的后端包 |
| T3 | Alembic async 初始化 + 空表基线 + `CREATE EXTENSION vector` | 迁移链路 + 扩展可用 |
| T4 | 三个 Protocol（Literal/SourceType 收紧）+ fake 实现 + 单例工厂 | 集成层 |
| T5 | `/health/live` + `/health/ready` + deps + CORS | 可启动的 API |
| T6 | 前端脚手架 + 布局 + 令牌 + 路由 + client + `tsc --noEmit` | 前端骨架 |
| T7 | 冒烟测试（含 degraded）+ ruff/mypy/eslint/tsc 全绿 + README 补全 | 验收全绿 |

---

## 11. 已定决策（原「待确认清单」定稿结论）

1. **接口签名**（第 6 节）：认可，并已收紧——`ChatMessage.role` 用 `Literal`，`metadata` 收为 `dict[str, str]`，`embed_documents/embed_query` 拆分保持不变。
2. **健康检查**：采纳 **liveness/readiness 拆分**，`/health/ready` 在 DB 不可达时返回 **503**（非 200）。
3. **Python 版本**：定 3.12，无异议。
4. **前端包管理器**：pnpm，无异议。
5. **迁移基线**：采纳「空表基线」策略，但**把 `CREATE EXTENSION vector` 放进基线迁移**，业务表由模块 2 起各建各表。
