# kima

个人 AI 知识库 / 智能工作台：知识库、笔记、文档、AI 问答与 Copilot 知识 Agent 一体化。前后端分离、Docker 一键部署，目标是「生产级、可复现、可扩展」的工程实践。

## 功能特性

- **知识库管理**：CRUD + 预置默认「我的知识库」；内容 = 库内文档 + 关联笔记。
- **笔记**：TipTap Markdown 编辑器，空白笔记自动保存，可「添加到知识库」（引用而非复制）。
- **文档解析归档**：上传 PDF / Word / Markdown / URL → 内容感知父子分块（small-to-big）→ 向量化入库；后台异步 worker + 重试退避。
- **AI 智能问答**：Advanced RAG——查询改写 + 向量/词法混合检索 + RRF 融合 + rerank 精排 + 生成引用。
- **Copilot 知识 Agent**：LangGraph 多 Agent 运行时（意图分流 plan/reactive + 子 agent 交棒），工具调用（检索/读/写/联网/记忆），四型记忆（约束/事实/偏好/情节），HITL 人工审批，崩溃恢复，多轴预算控制。
- **写入闸**：高风险文档入库前「红线即拒 / 人工确认 + 低信任入库」，检索侧隔离。
- **LLM 网关**：统一门禁、调用级快照恢复、`json_repair`、出站 DLP 脱敏、LangFuse 可观测、实时计费/成本审计。
- **上下文窗口分层**：L0–L5 六层上下文编排，超窗按预算裁剪。

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | React 18 + TypeScript + Vite + React Router + React Query + SCSS Modules + TipTap |
| 后端 | FastAPI + SQLAlchemy 2.0 (async) + asyncpg + Alembic + Pydantic v2 + LangGraph |
| 存储 | PostgreSQL + pgvector + pg_jieba（中文全文检索，自编译镜像） |
| AI | DeepSeek（LLM）、SiliconFlow（Embedding bge-m3 / Rerank bge-reranker）、博查（Web Search）、MinerU（文档解析）——均为可替换 provider，默认 `fake` 可空跑 |
| 可观测 | LangFuse（可选，网关层埋点） |

## 目录结构

```
kima/
├── frontend/          # React 18 + TS + Vite
├── backend/           # FastAPI
│   ├── app/
│   │   ├── api/           # 路由层（routes/，按资源分 router）
│   │   ├── agent/         # Copilot Agent 运行时（guardrail / resilience / runtime / pricing / evaluation）
│   │   ├── chunking/      # 文档内容感知分块
│   │   ├── core/          # 配置 / 日志 / DB / 中间件 / 记忆与技能存储
│   │   ├── integrations/  # LLM / Embedding / Rerank / Search / 文档解析 抽象适配层
│   │   ├── models/        # SQLAlchemy ORM
│   │   ├── rag/           # Advanced RAG
│   │   ├── repositories/  # 数据访问层
│   │   ├── schemas/       # Pydantic 校验/响应模型
│   │   ├── services/      # 业务逻辑层
│   │   └── workers/       # 后台 worker（文档处理 / 笔记向量化）
│   ├── alembic/           # 数据库迁移
│   └── tests/
├── docker/            # backend / frontend / pgvector-jieba 镜像
├── docs/              # 需求与设计文档
└── docker-compose.yml
```

后端分层 `router → service → repository`，LLM / Embedding / Rerank / 文档解析均为可替换 provider 抽象，换供应商不改业务代码。

## 快速开始（Docker）

前置：Docker。

```bash
docker compose up -d --build
```

一键构建并启动全部服务：

| 服务 | 说明 | 端口 |
|---|---|---|
| `db` | PostgreSQL + pgvector + pg_jieba（自编译镜像，首次构建需能访问 GitHub 拉取 pg_jieba 源码） | `127.0.0.1:5432` |
| `backend` | FastAPI，entrypoint 自动 `alembic upgrade head` + `uvicorn` | `127.0.0.1:8000` |
| `frontend` | nginx 托管生产构建产物，反代 `/api`、`/health` 到 backend | `0.0.0.0:8080` |

启动后：

- **前端应用**：http://localhost:8080
- **健康检查**：`GET /health/live`（存活）、`GET /health/ready`（DB 就绪）

> 首次构建较慢：backend 镜像需安装 Playwright Chromium（约 +200MB，用于 SPA 抓取），db 镜像需编译 pg_jieba。之后仅源码改动才重 build，依赖层命中缓存。

### 本地开发（可选）

后端测试 / 静态检查可在宿主机跑（无需 Docker 起服务）：

```bash
cd backend
uv sync --group dev
uv run pytest
uv run ruff check .
uv run mypy app tests
```

前端热更新开发（`pnpm@12.3.4`）：

```bash
cd frontend
pnpm install
pnpm dev   # http://localhost:5173
```

## 配置

后端配置经环境变量注入（pydantic-settings），默认全 `fake` provider，可无密钥空跑。核心项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://kima:kima@localhost:5432/kima` | 数据库连接 |
| `LLM_PROVIDER` / `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | `fake` / 空 / DeepSeek | 问答与 Agent 的 LLM |
| `EMBEDDING_PROVIDER` / `EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` | `fake` / 空 / SiliconFlow / bge-m3 | 向量化 |
| `RERANK_PROVIDER` / `RERANK_API_KEY` / `RERANK_BASE_URL` / `RERANK_MODEL` | `fake` / 空 / SiliconFlow / bge-reranker | 精排 |
| `WEB_SEARCH_PROVIDER` / `WEB_SEARCH_API_KEY` / `WEB_SEARCH_BASE_URL` | `fake` / 空 / 博查 | 联网检索 |
| `MINERU_API_BASE_URL` / `MINERU_API_TOKEN` | — | 文档解析（PDF） |
| `CORS_ORIGINS` | `http://localhost:5173` | 允许的来源 |

> 生产部署在 `docker-compose.yml` 里通过环境变量注入真实密钥；后端端口仅绑 `127.0.0.1`，对外统一走前端 nginx 反代。

## API 概览

所有业务路由挂 `/api` 前缀；健康检查裸挂。

| 资源 | 主要端点 |
|---|---|
| 知识库 | `GET/POST /api/knowledge-bases`、`GET/PATCH/DELETE /api/knowledge-bases/{id}`、`GET /api/knowledge-bases/{id}/contents` |
| 笔记 | `GET/POST /api/notes`、`GET/PATCH/DELETE /api/notes/{id}`、`POST /api/notes/{id}/knowledge-bases` |
| 文档 | `POST /api/documents`（multipart）、`POST /api/documents/from-url`、`GET /api/documents/{id}`、`GET .../{id}/file`、`GET .../{id}/content`、`POST .../{id}/retry`、`POST .../{id}/approve`、`DELETE .../{id}` |
| 会话 | `GET/POST /api/conversations`、`GET/DELETE /api/conversations/{id}` |
| 问答 | `POST /api/chat`（SSE：`meta → delta* → citations → done`） |
| Copilot | `POST /api/copilot/chat`（返回 run id，异步后台跑）、`GET /api/copilot/runs/{id}/stream`（SSE 回放/续订）、`POST /api/copilot/approve`、`POST /api/copilot/runs/{id}/cancel`、`GET /api/copilot/approvals/pending`、`GET /api/copilot/memory`、`GET /api/copilot/skills`、`GET /api/copilot/custom-skills` |
| 健康 | `GET /health/live`、`GET /health/ready` |

> Copilot 的 run 与 HTTP 请求解耦：`POST /chat` 同步准备并返回四元组 id 后即返回，后台任务驱动 run，SSE 事件落库；前端经 `/runs/{id}/stream` 回放 + 续订，刷新页面不中断 Agent。

## 测试与质量门禁

```bash
# 后端
cd backend && uv run ruff check . && uv run mypy app tests && uv run pytest

# 前端
cd frontend && pnpm run typecheck && pnpm run lint && pnpm run build
```

CI（`.github/workflows/ci.yml`）在 push / PR 时自动跑以上检查 + 数据库迁移冒烟。

## 文档

设计文档见 `docs/`：

- `requirements.md` — 需求与技术方案
- `module-1-infrastructure.md` ~ `module-6-copilot.md` — 各模块设计
- `pricing.md` — 实时计费 / 成本审计
- `context-window-layering.md` — 上下文窗口 L0–L5 分层
- `planner-supervisor-refactor.md` — planner 与 supervisor 重构
