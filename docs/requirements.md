# kima — 项目需求与技术方案

> 记录日期：2026-09-10
> 状态：需求已全部锁定，等待从「模块 1：基础设施」开始实现
> 本文档记录本次讨论的完整结论，作为后续逐模块实现的需求基线

---

## 1. 项目概述

复刻腾讯 **ima**（ima.copilot，AI 知识库 / 智能工作台）的同类产品。

核心定位：**可用的个人 AI 知识库工具**（非 UI 演示、非 Mock 数据），前后端分离、各模块高度解耦、逐个讨论并实现。

项目目标：作为**向面试官展示工程能力的作品**，注重生产级实践与工程严谨性（可复现、可扩展、可运维），而非最小可行 demo。

---

## 2. 技术栈

### 2.1 前端（`./frontend`）

| 项目 | 选型 |
|---|---|
| 框架 | React 18 + TypeScript + Vite |
| 路由 | react-router-dom v6 |
| 服务端状态 | @tanstack/react-query |
| 客户端状态 | URL 参数（导航/选中）+ React Context（全局 UI 开关），不引 zustand |
| 编辑器 | TipTap（Markdown 编辑器，非块级编辑器） |
| 样式 | SCSS + CSS Modules（`.module.scss`），设计令牌用 CSS 自定义属性（`:root`） |
| HTTP | 类型化 API client（fetch/axios） |

### 2.2 后端（`./backend`）

| 项目 | 选型 |
|---|---|
| 框架 | Python + FastAPI |
| ORM | SQLAlchemy 2.0（async） |
| 驱动 | asyncpg |
| 迁移 | Alembic |
| 校验 | Pydantic v2 |

### 2.3 存储

- **PostgreSQL + pgvector**（关系数据 + 向量检索一个库搞定）

### 2.4 AI 能力

| 能力 | 选型 |
|---|---|
| 问答 LLM | DeepSeek（OpenAI 兼容） |
| 向量化 Embedding | SiliconFlow 在线（bge-m3） |
| Rerank | SiliconFlow（bge-reranker） |
| RAG 编排 | **v1 Advanced RAG**（查询改写 + 混合检索 + RRF + rerank + 生成引用） |
| 词法检索 | PostgreSQL 中文全文检索扩展（pg_jieba，备选 zhparser），与 pgvector 同库 |
| Agentic 编排 | **LangGraph（后补）**，不使用 LlamaIndex |

### 2.5 文档解析

- **MinerU**，走**托管 API**（后端保持轻量，不本地跑 GB 级模型），可配置渠道：
  - mineru.net（官方托管，免费额度）
  - Gitee AI 模力方舟（0.02 元/页）
  - 自托管 mineru-api（可选回退）
- 抽象为可配置解析客户端，注入 `MINERU_API_BASE_URL` + `MINERU_API_TOKEN`

---

## 3. 用户体系与部署

- **单用户**、无登录鉴权（个人工具）
- **本地开发为主**
- **能 Docker 就 Docker**，保证「在我机子能跑 = 在别人机子能跑」，目标 `docker compose up` 一键跑通

---

## 4. 目录结构（monorepo）

```
kima/
├── frontend/          # React 18 + TS + Vite
├── backend/           # FastAPI
│   ├── app/
│   │   ├── api/           # 路由层（按模块分 router）
│   │   ├── services/      # 业务逻辑层
│   │   ├── repositories/  # 数据访问层
│   │   ├── models/        # SQLAlchemy ORM 模型
│   │   ├── schemas/       # Pydantic 校验/响应模型
│   │   ├── core/          # 配置、日志、依赖注入
│   │   ├── integrations/  # LLM / Embedding / MinerU 抽象适配层
│   │   └── main.py
│   ├── alembic/           # 数据库迁移
│   └── pyproject.toml
├── docs/              # 需求与方案文档
└── docker-compose.yml # 起 PostgreSQL(pgvector)
```

**解耦原则**：后端按 `router → service → repository` 分层，每个功能模块在各自目录自治，只通过接口互调。LLM / Embedding / MinerU 均为可替换 provider 抽象，换供应商不改业务代码。

---

## 5. 核心模块与实现顺序

依赖关系：知识库是地基 → 文档/笔记填内容 → 向量化产出检索能力 → 问答/搜索消费它。

| # | 模块 | 说明 |
|---|---|---|
| 1 | **基础设施** | 前后端脚手架、Docker 起 Postgres(pgvector)、Alembic、LLM/Embedding/MinerU 三个抽象接口、前端布局骨架 |
| 2 | **知识库管理** | 知识库 CRUD + 前端页面 |
| 3 | **笔记/编辑器** | 笔记 CRUD + TipTap Markdown 编辑器 |
| 4 | **文档解析与归档** | 接入 MinerU API：上传 PDF/URL/Word → 解析 → 分块 → 向量化入库 |
| 5 | **AI 智能问答** | Advanced RAG（查询改写 + 混合检索 + RRF + rerank + 生成引用），预留 Agent 接口 |
| 6 | **全局搜索** | 跨知识库/笔记混合检索（复用模块 4 的向量能力） |

### 模块 5 RAG 细节（Advanced RAG）

```
① 查询改写   用户问题 → DeepSeek 改写为检索友好 query
② 混合检索   向量: bge-m3 → pgvector top-k（按知识库过滤）
             词法: pg_jieba/zhparser 中文全文检索 top-k（ts_rank 打分）
③ RRF 融合   两路结果 RRF 融合成候选集
④ 精排       SiliconFlow bge-reranker → top-N
⑤ 生成       组装 context（去重 + 引用元数据）→ DeepSeek 生成 → 回答 + 引用来源
```

- `rag` 服务封装清晰接口（`retrieve()` / `answer()`），五个步骤各为独立可替换组件
- 词法检索选**PostgreSQL 中文全文检索（pg_jieba）**：dense（pgvector）与 lexical（FTS）同库、单一数据源、事务一致；需自建 Postgres 镜像（多阶段构建，编译 pgvector + pg_jieba）；`ts_rank` 做相关度打分（PG 全文检索的 BM25-like 打分，非严格 Okapi BM25）
- 将来上 LangGraph 做 Agentic 时**只在编排层替换**，检索层与业务层不动

---

## 6. 数据模型（草图，单用户无 users 表）

| 表 | 字段要点 |
|---|---|
| `knowledge_bases` | id、名称、描述、图标/颜色、created_at、updated_at |
| `documents` | id、kb_id、标题、来源类型(pdf/url/word)、source_url、file_path、状态(pending/processing/done/error)、正文、metadata |
| `document_chunks` | id、document_id、kb_id、chunk_index、content、embedding(vector)、token_count |
| `notes` | id、kb_id、标题、content_markdown、created_at、updated_at |
| `chat_conversations` | id、kb_id、标题、created_at |
| `chat_messages` | id、conversation_id、role、content、citations(jsonb)、created_at |

---

## 7. 关键决策记录（含理由）

1. **前端视觉**：结构沿用 ima（左导航 + 主区 + 右侧 AI 侧栏），配色/字体/细节做自己的设计语言。
2. **AI 面板形态**：全局右侧侧栏（copilot 形态，任何页面可呼出，可针对当前知识库/文档提问）。
3. **RAG 路线**：v1 直接做 Advanced RAG（查询改写 + 向量/词法混合检索 + RRF + rerank + 生成引用），Agentic 用 LangGraph 后补，不用 LlamaIndex。理由：数据管道自定义，LlamaIndex 价值有限；LangGraph 是 Agentic 正统编排，将来只在编排层替换即可。
4. **MinerU 接入**：走托管 API，避免本地重模型。后端抽象为可配置解析客户端。
5. **编辑器**：Markdown 编辑器（TipTap），不做类 Notion 块级编辑器（工作量大，后续可迭代）。
6. **文档类型**：PDF、网页链接、Word/文本；图片 OCR 可选（MinerU 自带 OCR，顺带处理）。
7. **存储**：PostgreSQL + pgvector（而非 SQLite+ChromaDB），生产级、可平滑演进。
8. **用户体系**：单用户无鉴权，但数据库/接口层预留 user 维度以便后续升级多用户。
9. **客户端状态**：不引 zustand。导航/选中状态走 URL 参数，服务端数据走 react-query，仅 AI 侧栏开关、主题等极少量全局 UI 状态用 React Context。
10. **样式**：SCSS + CSS Modules（`.module.scss`）。设计令牌用 CSS 自定义属性（`:root`）而非 SCSS 变量，以支持将来暗色主题/换肤的运行时切换。
11. **词法检索**：PostgreSQL 中文全文检索扩展（pg_jieba，备选 zhparser），与 pgvector 同库。理由：dense 与 lexical 单一数据源、事务一致，是生产级做法；自建 Postgres 镜像编译扩展更能体现工程能力（面向面试展示）。注意 `ts_rank` 为 PG 全文检索打分，非严格 Okapi BM25。

---

## 8. 待办 / 下一步

**模块 1：基础设施**（下次对话开始）：

1. 前后端脚手架（Vite 项目 + FastAPI 项目结构）
2. `docker-compose.yml`（PostgreSQL + pgvector）
3. 数据模型骨架 + Alembic 迁移
4. LLM（DeepSeek）/ Embedding（SiliconFlow）/ MinerU 三个抽象接口
5. 前端布局骨架（左导航 + 主内容区 + 右侧 AI 侧栏）
6. 健康检查与配置管理（环境变量注入）

> 详细实现方案在开始模块 1 前会先拆细、与用户逐项对齐后再写代码。
