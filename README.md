# kima

复刻腾讯 ima 的 AI 知识库 / 智能工作台（个人工具）。前后端分离、模块高度解耦、逐模块实现，目标是「生产级、可复现、可扩展」的工程实践。

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | React 18 + TypeScript + Vite + React Router + React Query + SCSS Modules |
| 后端 | FastAPI + SQLAlchemy 2.0 (async) + asyncpg + Alembic + Pydantic v2 |
| 存储 | PostgreSQL + pgvector |
| AI | DeepSeek（LLM）、SiliconFlow（Embedding/Rerank）、MinerU（文档解析）——均为可替换 provider 抽象 |

## 目录结构

```
kima/
├── frontend/          # React 18 + TS + Vite
├── backend/           # FastAPI（app/ 分层：api / services / repositories / models / schemas / core / integrations）
├── docs/              # 需求与模块设计文档
└── docker-compose.yml # PostgreSQL(pgvector)
```

## 快速开始

前置：Docker、uv、pnpm 12、Node 18+。

> pnpm 说明：锁 `pnpm@12.3.4`。pnpm 11+ 的自身配置放在 `frontend/pnpm-workspace.yaml`（已关闭 `minimumReleaseAge` 供应链冷却、放行 esbuild/@parcel/watcher 的 build 脚本），本地与 CI 共用同一份配置。

### 1. 起数据库

```bash
docker compose up -d
```

等待 `pg_isready` 通过：

```bash
docker exec kima-db pg_isready -U kima -d kima
```

### 2. 起后端

```bash
cd backend
uv sync                        # 安装依赖（首次）
cp .env.example .env           # 按需改 DATABASE_URL 等
uv run alembic upgrade head    # 建表（baseline + knowledge_bases + notes + note_knowledge_bases + documents + document_chunks，含 CREATE EXTENSION vector）
uv run uvicorn app.main:app --reload
```

### 3. 起前端

```bash
cd frontend
pnpm install
pnpm dev
```

浏览器打开 http://localhost:5173 （默认显示 kima 首页；知识库在 /knowledge-bases）。

## 健康检查

- `GET /health/live` — 进程存活，恒 200
- `GET /health/ready` — 依赖就绪，DB 可达 200 / 不可达 503

## 知识库 API（模块 2）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/knowledge-bases?limit=&offset=` | 列表（分页） |
| POST | `/api/knowledge-bases` | 新建（名称唯一，重复 409） |
| GET | `/api/knowledge-bases/{id}` | 详情 |
| PATCH | `/api/knowledge-bases/{id}` | 更新 |
| DELETE | `/api/knowledge-bases/{id}` | 删除 |

## 笔记 API（模块 3）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/notes` | 新建空白笔记 |
| GET | `/api/notes?limit=&offset=` | 列表（分页） |
| GET | `/api/notes/{id}` | 详情 |
| PATCH | `/api/notes/{id}` | 更新（标题/正文，自动保存） |
| DELETE | `/api/notes/{id}` | 删除 |
| POST | `/api/notes/{id}/knowledge-bases` | 添加到知识库（幂等） |
| GET | `/api/knowledge-bases/{id}/contents` | 知识库内容列表（笔记 + 文档） |

## 文档 API（模块 4）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/documents`（multipart: `file` + `kb_id`） | 上传 PDF/Word → 201（pending，后台异步解析） |
| POST | `/api/documents/from-url`（json: `url` + `kb_id`） | 抓取网页 URL → 201（pending） |
| GET | `/api/documents/{id}` | 详情（状态/来源/错误信息） |
| GET | `/api/documents/{id}/file` | 原文件流（pdf 内嵌 / word 下载；url 类型 409） |
| POST | `/api/documents/{id}/retry` | 失败重试（仅 error 态） |
| DELETE | `/api/documents/{id}` | 删除 |

## 质量门禁

```bash
# 后端
cd backend && uv run ruff check . && uv run mypy app tests && uv run pytest

# 前端
cd frontend && pnpm run typecheck && pnpm run lint && pnpm run build
```

CI（`.github/workflows/ci.yml`）在 push / PR 时自动跑以上检查 + docker compose 冒烟。

## 模块进度

| 模块 | 内容 | 状态 |
|---|---|---|
| 1 | 基础设施（脚手架 + DB + 三个集成抽象 + 前端布局） | ✅ 完成 |
| 2 | 知识库管理 CRUD | ✅ 完成 |
| 3 | 笔记 / TipTap 编辑器 | ✅ 完成 |
| 4 | 文档解析与归档（PDF/URL/Word → 解析 → 分块 → 向量化） | ✅ 完成 |
| 5 | AI 智能问答（Advanced RAG） | 待做 |
| 6 | 全局搜索 | 待做 |

详细需求见 `docs/requirements.md`；模块 1 设计见 `docs/module-1-infrastructure.md`，模块 2 设计见 `docs/module-2-knowledge-bases.md`，模块 3 设计见 `docs/module-3-notes.md`，模块 4 设计见 `docs/module-4-documents.md`。
