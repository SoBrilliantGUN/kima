# kima — 项目需求与技术方案

> 记录日期：2026-09-10
> 状态：需求已锁定；模块 1（基础设施）、模块 2（知识库管理）、模块 3（笔记/编辑器）、模块 4（文档解析与归档）已实现，模块 5（AI 智能问答）待实现
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
| 包管理 | uv（uv.lock 锁文件 + 内建虚拟环境，保证可复现） |
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

| # | 模块 | 说明 | 状态 |
|---|---|---|---|
| 1 | **基础设施** | 前后端脚手架、Docker 起 Postgres(pgvector)、Alembic、LLM/Embedding/MinerU 三个抽象接口、前端布局骨架 | ✅ 完成 |
| 2 | **知识库管理** | 知识库 CRUD + 前端页面 | ✅ 完成 |
| 3 | **笔记/编辑器** | 笔记 CRUD + TipTap Markdown 编辑器（空白笔记 + 添加到知识库 + 知识库内容列表） | ✅ 完成 |
| 4 | **文档解析与归档** | 上传 PDF/URL/Word → 解析 → 内容感知父子分块 → 向量化入库（详见 `docs/module-4-documents.md`） | ✅ 完成 |
| 5 | **AI 智能问答** | Advanced RAG（查询改写 + 混合检索 + RRF + rerank + 生成引用），预留 Agent 接口 | 待做 |
| 6 | **全局搜索** | 跨知识库/笔记混合检索（复用模块 4 的向量能力） | 待做 |

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
| `document_chunks` | id、document_id、kb_id、parent_id(自引用，父子切割)、chunk_index、content、metadata、embedding(vector，child 有/parent 无)、token_count |
| `notes` | id、标题、content_markdown、created_at、updated_at |
| `note_knowledge_bases` | note_id(fk→notes)、knowledge_base_id(fk→knowledge_bases)、created_at；唯一(note_id, knowledge_base_id) |
| `chat_conversations` | id、kb_id、标题、created_at |
| `chat_messages` | id、conversation_id、role、content、citations(jsonb)、created_at |

---

## 7. 关键决策记录（含理由）

1. **前端视觉**：界面尽量复刻 ima（窄图标侧栏 + 知识库页「左列表 + 右问答」+ 简洁白蓝视觉），仅删减单用户不适用的功能（共享知识库/知识库广场/微信生态导入/成员权限/多端同步等），不做自己的设计语言。
2. **AI 助手形态**：复刻 ima，无独立「AI」按钮。AI 由两处承载——① `kima` 首页 tab（全局 AI 问答主页，模块 5 实现）；② 知识库页右侧常驻问答面板（针对当前知识库/文档提问）。全局「浮窗 copilot」（上下文感知的 Agent 形态）作为后补、暂不做。理由：ima 的 AI 入口是「ima 首页 tab + 知识库右问答 + 浮窗 copilot」，并不存在顶栏「AI」按钮；原「全局可呼出侧栏」是与 ima 不符的过度设计。
3. **RAG 路线**：v1 直接做 Advanced RAG（查询改写 + 向量/词法混合检索 + RRF + rerank + 生成引用），Agentic 用 LangGraph 后补，不用 LlamaIndex。理由：数据管道自定义，LlamaIndex 价值有限；LangGraph 是 Agentic 正统编排，将来只在编排层替换即可。
4. **MinerU 接入**：走托管 API，避免本地重模型。后端抽象为可配置解析客户端。
5. **编辑器**：Markdown 编辑器（TipTap），不做类 Notion 块级编辑器（工作量大，后续可迭代）。
6. **文档类型**：PDF、网页链接、Word；图片 OCR 可选（MinerU 自带 OCR，顺带处理）。
7. **存储**：PostgreSQL + pgvector（而非 SQLite+ChromaDB），生产级、可平滑演进。
8. **用户体系**：单用户无鉴权，但数据库/接口层预留 user 维度以便后续升级多用户。
9. **客户端状态**：不引 zustand。导航/选中状态走 URL 参数，服务端数据走 react-query；AI 面板是否显示由路由（是否知识库页）推导，无全局 UI 状态（不引 Context）。
10. **样式**：SCSS + CSS Modules（`.module.scss`）。设计令牌用 CSS 自定义属性（`:root`）统一管理（不做暗色主题）。
11. **词法检索**：PostgreSQL 中文全文检索扩展（pg_jieba，备选 zhparser），与 pgvector 同库。理由：dense 与 lexical 单一数据源、事务一致，是生产级做法；自建 Postgres 镜像编译扩展更能体现工程能力（面向面试展示）。注意 `ts_rank` 为 PG 全文检索打分，非严格 Okapi BM25。
12. **Postgres 镜像节奏**：模块 1 先用官方 `pgvector/pgvector` 镜像跑通链路，词法检索的 pg_jieba 自建镜像推迟到模块 5 再编译。理由：降低起步复杂度与调试成本，先验证其余链路，基础设施一次性定型反而拖慢节奏。
13. **Python 包管理**：uv。理由：极快、有锁文件（uv.lock）、内建虚拟环境，最契合「可复现、可运维」的工程展示目标。
14. **集成抽象形态**：LLM / Embedding / MinerU 三接口统一用 **async + Protocol**（结构化鸭子类型）。理由：与 FastAPI / asyncpg 的 async 生态一致，LLM/Embedding 的 IO 调用不阻塞事件循环；Protocol 避免继承耦合。
15. **导航信息架构对齐 ima**：侧栏一级导航复刻 ima 的单用户裁剪版，顺序为 `kima`（首页 = AI 问答主页，默认落地页）/ `知识库` / `笔记`。其中「问答」并入 `kima` 首页、「搜索」降为知识库内检索能力（模块 6 仍做、不占顶层 tab）、「发现」因多用户裁掉、「浏览」因需内嵌浏览器（纯 Web 无法实现）裁掉，其「收藏网页」能力改由知识库的「URL 文档」承载（见 #19）。理由：决策 #1 只复刻了视觉（窄图标栏），未复刻信息架构；ima 侧栏是「产品功能入口」而非「模块清单」，命名/结构应与产品对齐。
16. **知识库页三栏布局**：复刻 ima 的 master-detail 三栏——`[图标导航 64px] [知识库列表 300px] [内容列表 550px] [问答面板 剩余空间]`，三栏同屏。选中知识库走 URL（`/knowledge-bases/:id`，`/knowledge-bases` 重定向到第一个）；内容列表展示当前知识库的内容 = 库内 documents + 关联进来的 notes（见 #18/#19），问答面板针对当前知识库提问。
17. **默认知识库 + 删除规则**：应用启动（lifespan）时幂等预置「我的知识库」（`#5B8DEF`），保证始终至少一个知识库；**不能删除最后一个知识库**（后端 409 `last_knowledge_base` + 前端隐藏删除按钮）。
18. **笔记全局 + 添加到知识库（推翻旧「文档=笔记统一进库」）**：笔记是**全局**内容，不归属任何知识库（`notes` 无 `kb_id`）；知识库内的「文档」（pdf/word/url 归档）与「笔记」是两类东西。笔记可通过「添加到知识库」动作关联进某知识库（`note_knowledge_bases` 多对多，**引用而非复制**，改笔记库里同步变）。

19. **URL 统一归入文档（详见 `docs/module-4-documents.md`）**：URL 不再作为网页笔记，统一归入文档（`documents.source_type=pdf/word/url`）；`notes` 为纯 Markdown 空白笔记（无 `type`/`summary`/`source_url` 三列、无 `from-url` 端点）。文档解析按类型分发——PDF→MinerU、Word→mammoth+markdownify、URL→复用 WebFetcher。父子切割 small-to-big——`document_chunks` 自引用 `parent_id`，parent 大块存上下文不向量化、child 小块向量化，检索命中 child 回 parent。内容感知分块 5 splitter（结构化递归兜底 + 表格 + 代码 AST + 法律条例 + FAQ 问答对）。异步 DB 轮询 worker + 自建重试退避。文档阅读器（PDF 内嵌原文件 / Word 与 URL 渲染解析 markdown），支持下载原文件 / 打开原网页。笔记向量化留模块 5。

20. **模块 4 前端交互**：① 文档阅读由「路由面板」改为「浮动窗口」——点文档在页面上层打开可拖拽/调整大小/关闭的浮动窗口，可同时开多个、同文档去重聚焦；纯内存态，去掉 `/knowledge-bases/:id/documents/:documentId` 路由；右侧问答面板常驻并针对整个知识库，浮动窗口仅阅读参考。② 上传入口拆分——本地文档用大拖拽批量窗口、URL 用单一链接输入弹窗，二者分离。③ 知识库「添加内容」菜单去掉「笔记」项（笔记不再从知识库新建，列表已有笔记保留）。

---

## 8. 待办 / 下一步

- 模块 1（基础设施）✅ 已实现 — 详见 `docs/module-1-infrastructure.md`
- 模块 2（知识库管理）✅ 已实现 — 详见 `docs/module-2-knowledge-bases.md`
- 模块 3（笔记/编辑器）✅ 已实现 — 详见 `docs/module-3-notes.md`
- 模块 4（文档解析与归档）✅ 已实现 — 详见 `docs/module-4-documents.md`：上传 PDF/URL/Word → 解析 → 内容感知父子分块 → 向量化入库；前端交互（浮动阅读窗口 + 上传入口拆分 + 去笔记入口，见决策 #20）已落地
- **下一步：模块 5（AI 智能问答）**
