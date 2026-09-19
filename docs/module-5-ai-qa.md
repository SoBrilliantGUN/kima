# 模块 5：AI 智能问答 — 详细设计

> 日期：2026-09-18
> 状态：已实现
> 上游基线：`docs/requirements.md`（决策 #2/#3/#11/#12/#16）· `docs/module-4-documents.md`（父子分块 + document_chunks + worker）· `docs/module-3-notes.md`（笔记全局 + note_knowledge_bases）

本模块交付「**AI 智能问答**」完整能力：

- **两处入口**：`kima` 首页（选库 / 全网二选一切换）+ 知识库页右侧常驻问答面板（当前库）
- **Advanced RAG 五步**：查询改写 → 混合检索（向量 + 词法 + RRF）→ rerank 精排 → token 预算组装上下文 → 流式生成 + chunk 级引用
- **词法检索**：pg_jieba 中文全文检索完整落地（自建 Postgres 镜像）
- **笔记向量化**：只覆盖已入库笔记，编辑停手后 idle 异步重向量化
- **全网搜索**：博查 Web Search API，作为可插拔 provider

**明确后置（不在本模块）**：Copilot 浮窗 Agent（决策 #2 已定「后补」）；LangGraph Agentic 编排（只在编排层替换）；「快速/深度」模型档位切换。

---

## 1. 目标与验收

目标：`kima 首页`与`知识库右面板`两处问答走完整 Advanced RAG，支持「选库 / 全网」切换；回答流式输出并附带 chunk 级引用；笔记编辑后自动重向量化。

**验收标准（Definition of Done）**

| # | 验收项 |
|---|---|
| 1 | `alembic upgrade head` 成功（单一基线 `0001_initial` 建全量表 + 扩展 + 索引） |
| 2 | 自建 Postgres 镜像（`docker/` 多阶段，编译 pgvector + pg_jieba），`docker compose up` 起库即带 `vector` + `pg_jieba` 两扩展 |
| 3 | `rag.retrieve()` 返回混合检索候选（dense + lexical → RRF → rerank → 命中 child 回 parent）；文档与笔记统一可检索，按 `kb_id` 过滤正确 |
| 4 | 问答 SSE 流式：先 `meta`（会话/消息 id）→ 逐 token `delta` → `citations` → `done` |
| 5 | 首页「全网 / 知识库」切换：全网走博查、知识库走混合检索，各自生成引用 |
| 6 | 笔记保存后 idle 自动重向量化（删除旧 chunk + 新增），`vectorized_at` 回写；游离笔记跳过 |
| 7 | 后端 `ruff` + `mypy(strict)` + `pytest` 全绿；前端 `eslint` + `tsc --noEmit` + `vite build` 全绿；测试不起真库/真网/真 LLM |

---

## 2. RAG 管线（核心）

```
用户问题
  ├─ ① 查询改写   喂最近 1~2 轮对话 → DeepSeek 改写为检索友好 query（解决指代）
  ├─ ② 混合检索   向量: bge-m3 → pgvector top-k（按 kb_id 过滤）
  │               词法: pg_jieba ts_rank top-k
  ├─ ③ RRF 融合   两路结果 RRF 融合成候选集
  ├─ ④ 精排       SiliconFlow bge-reranker → 过滤低分（RERANK_MIN_SCORE）→ top-N
  └─ ⑤ 生成       组装 context（token 预算 + 历史摘要 + 去重引用）→ DeepSeek 流式 → 回答 + 引用
```

- 检索层（②③④）封装为 `retrieve()`，生成层（⑤）封装为 `answer()`，二者独立可替换；`RagService` 为门面，将来上 LangGraph Agentic 时只在 `answer()` 编排层替换，检索层与业务层不动（对齐决策 #3）。
- 全网档：跳过 ②③④，改走 `WebSearchClient`，复用 ⑤ 的 context 组装与流式生成。

---

## 3. 数据模型

### 3.1 数据模型（迁移已并入 `0001_initial` 基线）

**新增表**

**`note_chunks`**（笔记分块，**父子两级** small-to-big）

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | `UUID` | PK，app 端 `uuid.uuid4` |
| `note_id` | `UUID` | FK → `notes.id ON DELETE CASCADE` |
| `parent_id` | `UUID` | 自引用 FK → `note_chunks.id ON DELETE CASCADE`，可空：`NULL`=parent、非空=child |
| `chunk_index` | `Integer` | 块序 |
| `content` | `Text` | 非空 |
| `metadata` | `JSONB` | 可空（`heading_path`/`block_type`） |
| `token_count` | `Integer` | 可空 |
| `embedding` | `Vector(1024)` | 可空（child 有 / parent 无） |
| `tsv` | `TSVECTOR` | 非空（`to_tsvector('jiebacfg', content)`） |

- **无 `kb_id`、有 `parent_id`**：笔记全局、多对多入库，chunk 不能挂单一库（按库过滤走关联表）；`parent_id` 自引用做 small-to-big（parent 存上下文不向量化、child 向量化），与文档一致。
- **入库判定走关联表**：检索某库时 `note_chunks ⋈ note_knowledge_bases(knowledge_base_id=X)` 过滤；游离笔记无 chunk 天然不参与。

**`chat_conversations`**（会话）

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | `UUID` | PK |
| `kb_id` | `UUID` | 可空 FK → `knowledge_bases.id ON DELETE CASCADE`（首页全局会话为空；右面板挂库） |
| `title` | `String(255)` | 非空（首问截断生成，默认「新对话」） |
| `created_at` / `updated_at` | `DateTime(timezone)` | 继承 `TimestampMixin` |

**`chat_messages`**（消息）

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | `UUID` | PK |
| `conversation_id` | `UUID` | FK → `chat_conversations.id ON DELETE CASCADE` |
| `role` | `Enum(ChatRole)` `native_enum=False` | `user`/`assistant` |
| `content` | `Text` | 非空 |
| `citations` | `JSONB` | 可空（`[{index, source_type, source_id, title, snippet, chunk_id?, url?}]`，仅 assistant） |
| `created_at` | `DateTime(timezone)` | 继承 `TimestampMixin` |

**回改**

- `notes` 加 `vectorized_at`（`DateTime(timezone)` 可空）——笔记向量化 idle 检测基准。
- `document_chunks` 加 `tsv`（`TSVECTOR` 非空）+ GIN 索引（`USING gin (tsv)`）；迁移里一次性回填存量 `tsv = to_tsvector('jiebacfg', content)`。**不重算 `embedding`**（模块 4 已有向量，重算纯浪费）。

### 3.2 ORM 模型

- `models/note_chunk.py`、`models/chat.py`；`models/note.py` 加 `vectorized_at`；`models/document.py` 的 `DocumentChunk` 加 `tsv`。
- `embedding` 维度仍读 `settings.embedding_dim`（唯一真源，1024）；`tsv` 用 SQLAlchemy 的 `TSVECTOR` 列，写时 `func.to_tsvector("jiebacfg", content)`。

---

## 4. 检索层（新包 `app/rag/`）

```
app/rag/
├── schema.py       # RetrievedChunk / RetrievalResult / Citation 数据类
├── dense.py        # 向量检索：embed query → pgvector top-k（按 kb_id 过滤）
├── lexical.py      # 词法检索：pg_jieba ts_rank top-k
├── hybrid.py       # RRF 融合
├── rerank.py       # bge-reranker 精排
├── rewrite.py      # 查询改写（LLM，喂最近 1~2 轮）
├── context.py      # context_assembler：token 预算 + 历史摘要 + 检索上下文 + 引用编号
├── generate.py     # answer()：流式生成
└── service.py      # RagService 门面：retrieve() / answer()（编排层）
```

### 4.1 混合检索

- **dense**：`embed_query(query)` → `pgvector` 余弦 top-20；按 `kb_id` 过滤（document_chunks.kb_id / note_chunks 经关联表）。
- **lexical**：`to_tsquery('jiebacfg', plainto_tsquery(query))` → `tsv @@` → `ts_rank` 排序 top-20。
- **RRF**：`score(d) = Σ 1/(k + rank_i)`（k≈60），两路融合成候选集。
- **rerank**：`RerankerClient`（SiliconFlow bge-reranker）对候选集精排 → 过滤低于 `rerank_min_score`（Settings，默认 0.3）的候选 → top-N（默认 6）。
- **命中回父**：文档与笔记命中 child → 回 `parent_id` 取 parent 全文作上下文（small-to-big）；按 `source_type` 分桶（document/note 各查各表），杜绝跨表 UUID 撞号覆盖与空集合冗余查询。
- **`retrieve()` 语义**：输入 `(query, kb_id | None)`，返回带 `chunk`/`source_type`/`source_id`/`title`/`snippet` 的结构化结果，不绑定问答，便于后续搜索类能力复用。

### 4.2 集成抽象（`integrations/` 扩展）

- `integrations/rerank.py`：`RerankerClient` Protocol + `FakeRerankerClient` + `SiliconFlowRerankerClient`（OpenAI 兼容 `/rerank`，model=bge-reranker）。
- `integrations/llm.py`：`LLMClient` 协议加 `stream()`（`AsyncIterator[str]`），模块 1 刻意留到本模块定形；`FakeLLMClient` 补流式实现。
- 工厂 `get_reranker_client(settings)`、`get_web_search_client(settings)` 对齐现有 LLM/Embedding 工厂（`fake` 默认，配置 `.env` 后生效）。

---

## 5. 笔记向量化（复用文档 worker 基建）

- **范围**：仅「已入库」笔记（`note_knowledge_bases` 至少一条关联）才向量化；游离笔记跳过。
- **触发**：后端 worker 轮询，扫描：

  ```sql
  notes WHERE (vectorized_at IS NULL OR updated_at > vectorized_at)
         AND updated_at < now() - NOTE_REVECTORIZE_IDLE_SECONDS
         AND EXISTS(SELECT 1 FROM note_knowledge_bases WHERE note_id = notes.id)
  ```

  即「内容有变 **且** 已停止编辑超过阈值」才处理；阈值 `NOTE_REVECTORIZE_IDLE_SECONDS` 默认 **120s**，可配置。
- **处理**：删旧 `note_chunks` → 复用文档的两级分块管线（`chunk_document`，父子 small-to-big）→ 批量 embed child → 写 `note_chunks` + 回写 `vectorized_at`。
- **重向量化 = 删除 + 新增**：不做原地 diff（内容变了本来就要重切，diff 对齐复杂度不值当）。
- **worker**：`workers/note_vectorize_worker.py`，与文档 worker 并列，`FOR UPDATE SKIP LOCKED` + `asyncio.Semaphore` 限流；lifespan 接入、优雅退出。前端零耦合，「关闭笔记」只是「停止编辑」的特例，被阈值自然覆盖。

---

## 6. 全网搜索（`integrations/search.py`）

- 新增 `WebSearchClient` Protocol（`search(query) -> list[WebSearchResult]`）+ `FakeWebSearchClient` + `BochaWebSearchClient`；工厂查表（`fake` 默认）。
- `WebSearchResult`：`title` / `url` / `snippet`（博查 Web Search API 返回的结构化结果）。
- 全网档流程：query（可选改写）→ 博查 top-k → 组装 context + 引用（`source_type="web"`，引用 URL/标题/摘要）→ 复用 `context_assembler` 与流式 `generate`。

---

## 7. 上下文组装（`context.py`）

> 面向生产级长会话的成本与稳定性设计，核心是「**固定段先扣 + 历史 token 预算 + 动态降级 recent 轮数**」，而非硬编码「喂几轮」。

### 7.1 预算分配

把一次 LLM 调用的上下文窗口按「固定段 + 弹性段」分配：

| 段 | 内容 | 说明 |
|---|---|---|
| 固定段 | system prompt + 检索上下文（含引用编号）+ 本轮问题 + 安全余量 | 先扣，不压缩 |
| 弹性段 | 对话历史 | 剩余预算，可压缩（见 7.2） |

- 总预算 `CONTEXT_MAX_TOKENS` 可配置；`history_budget = 总预算 − 固定段`，组装时先把固定段（system/context/query + `SAFETY_MARGIN_TOKENS` 默认 200）扣掉，剩余全部留给历史。

### 7.2 历史分级压缩（替代「写死 N 轮」）

- 全部历史在 `history_budget` 内 → 全量 verbatim，不做摘要。
- 超预算 → **动态降级 `recent_turns`**：从配置的最近 N 轮（`HISTORY_RECENT_TURNS`，默认 3）逐轮下调，保留「塞得下预算」的最大 verbatim 轮数；更早的历史用一次 `temperature=0` 的 LLM 调用压缩成一段摘要垫在最前。连 1 轮都塞不下时，recent 降为 0、全量走摘要。
- **摘要硬截断**：摘要生成后按「剩余预算 = `history_budget − recent 占用`」截断，保证 `[摘要, *recent]` 总 token ≤ 预算（`estimate_tokens` 估算口径）。
- **语义损失边界清晰**：损失优先级从旧到新——早期历史先被压缩成摘要（有损）、recent 仅在自身塞不下时逐轮降级、摘要尾部最后被截断；日常（未超预算）对话零损失。

### 7.3 引用编号

- 每条命中 chunk 生成 `[n]`；检索上下文按 `[n]` 标注；生成 prompt 要求模型在正文引用处标 `[n]`；`citations` 存 `n → {source_type, source_id, title, snippet, chunk_id?/url?}`，随消息落库。

---

## 8. 后端分层与接口

沿 `router → service → repository` 三层，依赖注入走 `api/deps.py`。

```
models/note_chunk.py            # NoteChunk
models/chat.py                  # ChatConversation / ChatMessage / ChatRole
models/note.py                  # 回改：+ vectorized_at
models/document.py              # 回改：DocumentChunk + tsv
schemas/chat.py                 # ConversationCreate / ConversationRead / MessageRead / ChatRequest / Citation
repositories/chat.py            # ChatRepository(Protocol) + SqlAlchemy 版
repositories/note_chunk.py      # NoteChunkRepository(批量写/删/按库检索)
services/chat.py                # ChatService（会话 CRUD + 提问入口编排）
services/rag.py                 # RagService（retrieve/answer）
workers/note_vectorize_worker.py
api/routes/chat.py              # 会话 + 问答 SSE 端点
integrations/rerank.py          # + SiliconFlow
integrations/search.py          # + Bocha
integrations/llm.py             # + stream()
rag/                            # 检索/改写/上下文/生成（§4/§7）
docker/                         # 自建 pg_jieba 镜像（§9）
```

### 8.1 会话端点（`api/routes/chat.py`，`prefix="/conversations"`）

| 方法 | 路径 | 成功 | 失败 |
|---|---|---|---|
| GET | `/api/conversations?kb_id=` | 200 `ConversationList`（首页全局 `kb_id` 空 / 右面板按库） | — |
| POST | `/api/conversations` | 201 `ConversationRead`（`kb_id` 可空） | 404（kb 不存在） |
| GET | `/api/conversations/{id}` | 200 `ConversationDetail`（含 messages） | 404 |
| DELETE | `/api/conversations/{id}` | 204 | 404 |

### 8.2 问答端点（`api/routes/chat.py`，`POST /api/chat`，SSE）

请求体 `ChatRequest`：

```python
class ChatRequest(BaseModel):
    mode: Literal["kb", "web"]
    kb_id: uuid.UUID | None = None   # mode=kb 时必填
    conversation_id: uuid.UUID | None = None  # 空则新建会话
    question: str
```

SSE 事件流（`text/event-stream`）：

| 事件 | 载荷 | 说明 |
|---|---|---|
| `meta` | `{conversation_id, user_message_id, assistant_message_id}` | 首问自动建会话，回 id |
| `delta` | `{text}` | 逐 token 打字机 |
| `citations` | `[{index, source_type, source_id, title, snippet, chunk_id?, url?}]` | 生成完成后补齐 |
| `done` | `{assistant_message_id}` | 结束 |
| `error` | `{code, message}` | 失败 |

- `ChatService.ask()`：校验（`mode=kb` 必须有 `kb_id`，`conversation_id` 存在性）→ 建/取会话 → 存 user 消息 → 调 `RagService.answer()` 流式 → 存 assistant 消息（含 `citations`）。
- 流式实现：`StreamingResponse` + `async generator`；`LLMClient.stream()` 逐 token 转发为 `delta` 事件。

---

## 9. pg_jieba 自建镜像

- `docker/pgvector-jieba/Dockerfile`：基于 `pgvector/pgvector:pg16` 多阶段构建——下载 `pg_jieba` 源码 + jieba 分词字典 → cmake/make 编译安装 → 保留 `vector` + `pg_jieba` 两扩展。内含 `ca-certificates`（否则 git 报 `CAfile: none`）+ 浅克隆重试 + `git config url.<镜像>.insteadOf` 让主仓与 submodule 统一走镜像源。
- `docker-compose.yml`：`db` 服务 `build: ./docker/pgvector-jieba`，替换官方镜像。国内直连 github.com 超时，构建用镜像源：`docker compose build --build-arg GIT_MIRROR=https://gh-proxy.com/https://github.com db`。
- 迁移基线 `0001_initial` 前导 `CREATE EXTENSION IF NOT EXISTS pg_jieba`（`vector` 同在前导）。
- 决策 #12 收口：官方镜像仅作模块 1~4 过渡，本模块起用自建镜像。

---

## 10. 前端

### 10.1 首页（`pages/Home/`，参考 ima 首页）

```
pages/Home/
  index.tsx                    # 会话列表 + 问答区 + 模式切换
  components/
    ConversationList.tsx       # 历史会话（新建/切换/删除）
    ChatPanel.tsx              # 消息流 + 输入框 + 模式切换 + 库选择器
    ModeSwitch.tsx             # 「全网 / 知识库」切换（知识库档展开库下拉）
shared/chat/
  StreamingMessage.tsx         # SSE 打字机渲染
  CitationPanel.tsx            # 底部「来源」面板（标题 + 片段 + 跳转）
  MessageBubble.tsx            # user/assistant 气泡 + [1][2] 内联引用
```

- **布局对齐 ima 首页**：左侧历史会话列表 + 主区问答（顶部模式切换「全网/知识库」、知识库档带库选择器、中部消息流、底部输入框）。
- **默认落地页**：`/` 即首页（决策 #2/#15）。
- **模式切换**：`全网` / `知识库`（选库）二选一；无「所有库」档（对齐 ima，检索过滤维度退化为固定 `kb_id` 或全网两种）。
- **Copilot 本版不加**（后补，决策 #2）。

### 10.2 知识库右面板（`pages/KnowledgeBasePage/components/QaPanel.tsx`）

- 右面板常驻、针对当前库提问（`kb_id` 取自当前路由）；按库持久化会话（进库自动载入该库最近会话，可新建）。
- 复用 `StreamingMessage` / `CitationPanel` / `MessageBubble` 组件。

### 10.3 数据层

- `api/chat.ts`：`listConversations` / `createConversation` / `getConversation` / `deleteConversation` / `askQuestion`（`fetch` + `ReadableStream` 解析 SSE）。
- `api/types.ts`：`Conversation` / `ChatMessage` / `Citation` / SSE 事件类型。
- `hooks/useChat.ts` / `useConversations.ts`：react-query hooks + SSE 流状态管理。
- 引用交互：正文 `[1][2]` 可点，聚焦到底部来源面板对应条目；来源条目可点开对应文档（复用模块 4 浮动窗口）或跳转笔记。

---

## 11. 测试策略（少而深，不起真库/真网/真 LLM）

| 层 | 测试 | 方式 |
|---|---|---|
| 检索单测 | `test_rag.py` | RRF 融合、rerank 过滤低分、命中回父（按 source_type 分桶）、按库过滤、笔记经关联表过滤 |
| 上下文单测 | `test_context.py` | 预算分配、历史超预算动态降级 recent 轮数、摘要硬截断、引用编号生成 |
| 服务单测 | `test_rag_service.py` | `answer()` 注入 Fake retrieval/rerank/llm/embedding：五步编排（含改写）+ 流式事件顺序 |
| 评估单测 | `test_rag_eval.py` / `test_rag_eval_runner.py` | 检索指标 recall@k / MRR（纯函数）+ golden 集运行器（Fake retrieve）+ LLM-judge faithfulness / answer_relevancy / context_relevancy（Fake judge） |
| 笔记 worker 单测 | `test_note_vectorize.py` | idle 阈值判定、删除+新增、游离笔记跳过、`vectorized_at` 回写 |
| API 集成 | `test_api_chat.py` | 会话 CRUD + `POST /api/chat` SSE 冒烟（Fake 流）+ 错误信封 |
| 集成 Fake | `tests/fakes.py` 增补 | `FakeWebSearchClient` / `FakeRerankerClient` / 流式 `FakeLLMClient` / `FakeNoteChunkRepository` |

- 关键用例：`mode=kb` 缺 `kb_id` → 422；会话不存在 → 404；SSE 事件顺序 `meta→delta…→citations→done`；笔记 idle 阈值内不触发、超阈值触发且删旧新增。

---

## 12. 实现顺序

| 步骤 | 内容 | 产出 |
|---|---|---|
| T1 | 自建 pg_jieba 镜像 + compose 换镜像 + 迁移前导建扩展 | `docker compose up` 带双扩展 |
| T2 | 迁移基线 `0001_initial`：note_chunks / chat 两表 / notes.vectorized_at / document_chunks.tsv + GIN（生成列自动回填） | 可 `upgrade head` |
| T3 | `app/rag/`：schema + dense + lexical + hybrid(RRF) + rerank + 单测 | 检索能力（可独立验收） |
| T4 | `integrations/`：rerank(SiliconFlow) + search(Bocha) + llm.stream() | 真实 provider |
| T5 | `rag/context.py` + `rag/rewrite.py` + `rag/generate.py` + `rag/service.py` | 生成编排 |
| T6 | `repositories/chat.py` + `schemas/chat.py` + `services/chat.py` + `api/routes/chat.py`（SSE） | 接口层 |
| T7 | `workers/note_vectorize_worker.py` + lifespan 接入 + 阈值配置 | 笔记向量化 |
| T8 | 后端测试（检索/上下文/服务/worker/API 集成 + Fake 增补） | pytest 全绿 |
| T9 | 前端 `api/chat.ts`（SSE client）+ `shared/chat` 组件 + Home 页（会话列表 + 模式切换 + 库选择器 + 流式 + 引用面板） | 首页问答闭环 |
| T10 | 前端 `QaPanel`（按库会话）+ 复用流式/引用组件 | 右面板问答闭环 |
| T11 | 全量质量门禁 + 手工验收 | ruff/mypy/pytest/eslint/tsc/build 全绿 |

---

## 13. 已定决策

1. **两处入口**：`kima` 首页（选库 / 全网二选一）+ 知识库右面板（当前库）；无「所有库聚合」档（对齐 ima，个人场景够用）。
2. **检索范围**：选库模式 = 该库 documents + 该库已关联 notes；游离笔记不向量化、不参与检索。
3. **全网搜索**：完整做，接博查 Web Search API；`WebSearchClient` Protocol 可插拔；全网/知识库二选一、不混合。
4. **流式（SSE）**：`LLMClient` 加 `stream()`；问答走 SSE 打字机 + 引用补齐。
5. **词法检索完整落地**：自建 Postgres 镜像（编译 pgvector + pg_jieba）；混合检索 = 向量 + 词法 + RRF；存量 chunk 只补 `tsv`、不重算 embedding。
6. **笔记向量化**：父子两级分块（与文档一致）、只覆盖已入库笔记；后端 worker idle 触发（`vectorized_at` + 默认 120s 阈值）；重向量化 = 删除旧 chunk + 新增。
7. **上下文管理**：token 预算驱动——固定段（system/检索上下文/问题/安全余量）先扣、剩余给历史；超预算时动态降级 recent 轮数（保留「塞得下」的最大 verbatim 轮）+ 摘要早期历史并硬截断到剩余预算，替代硬编码轮数；各阈值可配置。
8. **引用**：chunk 级 + 底部「来源」面板（标题 + 片段 + 跳转原文档/笔记），正文标 `[n]`。
9. **会话管理**：首页带历史会话列表（`kb_id` 空）；右面板按库持久化（`kb_id` 挂库）。
10. **首页 UI 参考 ima**：会话列表 + 主问答区（模式切换 + 库选择器 + 消息流 + 输入框）；Copilot 浮窗本版不加。
11. **模型**：单 `deepseek-chat`，无「快速/深度」档（后置）；rerank 用 SiliconFlow bge-reranker；改写/摘要 `temperature=0`，生成 `temperature=0.3`。
12. **评估最小集**：检索层 recall@k / MRR（`app/rag/metrics.py` 纯函数 + `app/rag/eval_runner.py` golden 集运行器，驱动 chunking 决策）+ 端到端 LLM-judge faithfulness / answer_relevancy / context_relevancy（`app/rag/eval.py`，复用现有 `LLMClient`，1–5 分归一化）；`scripts/eval_retrieval.py` 跑真实检索出报告，golden 集用「应命中片段」子串标注（`eval/golden.example.json`）；不引 ragas 重包（避免拖入 langchain），judge 用 `temperature=0`。
