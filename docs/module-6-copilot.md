# 模块 6：Copilot（知识 Agent）— 详细设计

> 日期：2026-09-20
> 状态：待评审
> 上游基线：`docs/requirements.md`（决策 #2/#3）· `docs/module-5-ai-qa.md`（RagService / 检索层 / SSE）· `docs/module-4-documents.md`（文档/笔记向量化 + worker）

本模块交付「**Copilot 知识 Agent**」：把模块 5 的单次被动问答（`RagService.answer()`：改写 → 检索 → 生成）升级为 **LLM 自主调用工具的多步 Agentic 循环**，配**三型记忆**（情节/语义/程序）与**全局浮窗**形态。核心区别于问答：Copilot 能「办事」（检索/读/写/联网），而不只是「回答」。并在工程上做到四件事：**工具描述工程 + 副作用分层 + 幂等写**、**纯函数式状态落盘（含思维链，宕机可从状态恢复）**、**三型记忆的分型召回 / 激活衰减遗忘 / LLM 冲突判定**、**可观测性（LangFuse trace）**。

**这些功能不做**：用户自定义 Skill / 知识号发布（多用户）；Skill 广场；记忆面板的手动编辑；文档导入工具（Agent 触发 ingest）；共享知识库；多 Agent 协作 / HITL 审批 / 熔断（这些重型能力 kima 单用户不需要）。

---

## 1. 目标与验收

目标：全局浮窗 Copilot 能「把库里关于 X 的内容总结成一篇新笔记」「联网搜 A 再结合我库里的 B 写对比报告」；记忆按类型精准调取、越用越懂、越旧越淡化；Agent 全程状态（含思维链）落盘、宕机可从状态恢复；同时现有 document/note worker 从「轮询」改为「事件驱动偷懒」。Q&A 路径（`RagService`/`ChatService`）完全不动，Agent 复用检索层与业务层。

**验收标准（Definition of Done）**

| # | 验收项 |
|---|---|
| 1 | `alembic upgrade head` 成功（`0002_copilot`：`copilot_memories` + `copilot_events` 表 + chat 回改 `kind`/`steps`） |
| 2 | Agent 循环经 `langchain-deepseek`（`ChatDeepSeek`）+ LangGraph `create_react_agent` 跑通工具回环（fake 下确定性测试） |
| 3 | 9 个工具复用现有服务层，带**副作用分层**（7 只读 / 2 写）+ 描述工程 + 结果截断 |
| 4 | 记忆三型（情节/语义/程序）：分型召回 + 激活衰减遗忘 + LLM 冲突判定 + `superseded` 留痕；Soul/User 存 MD 文件 |
| 5 | Copilot SSE 流式：`meta` → `step` → `delta` → `done`/`error`；**思维链经 checkpoint（`AsyncPostgresSaver`）+ 事件日志（`copilot_events`）双落库，可断点续跑** |
| 6 | worker 偷懒：无新文档/笔记时 idle 不空转，新增文档/更新笔记后立即唤醒；`recover_stuck` 兜底不丢 |
| 7 | 后端 `ruff` + `mypy(strict)` + `pytest` 全绿；前端 `eslint` + `tsc --noEmit` + `vite build` 全绿；测试不起真库/真网/真 LLM |
| 8 | LangFuse 可观测：配 key 后 agent run / LLM / 工具调用三层 trace 上报；无 key 时 handler=None 不阻塞 |

---

## 2. 记忆系统（核心）

> 设计原则：记忆会越来越长，不可能每轮全量注入，且不同类型的记忆「存活」方式不同。据此把积累型记忆分成三型，各配不同的召回、遗忘、冲突策略。Soul/User 仍为稳定短文本（文件），全文注入。

### 2.1 存储与三型分类

| 记忆 | 形态 | 存储 | 注入 |
|---|---|---|---|
| Soul（人设/说话风格） | 一个 MD 文件 `soul.md` | `data/memory/` | 每轮全文注入 |
| User（档案/背景/偏好） | 一个 MD 文件 `user.md` | `data/memory/` | 每轮全文注入 |
| **程序记忆 procedural** | 向量条目 | `copilot_memories` | **force-recall 全量注入**（数量少） |
| **语义记忆 semantic** | 向量条目 | `copilot_memories` | 语义相似 top-k |
| **情节记忆 episodic** | 向量条目 | `copilot_memories` | 语义相似 top-k |

三型语义（`kind` 枚举）：

| kind | 是什么 | 召回 | 遗忘 | 冲突 |
|---|---|---|---|---|
| `procedural` 程序记忆 | 怎么做/规则/偏好（「写周报用这个模板」「用户喜欢简洁回答」），稳定、长期有效 | force-recall 全量 | **不衰减**（activation 恒 1.0），仅受容量淘汰 | 语义去重；contradiction 新覆盖旧 |
| `semantic` 语义记忆 | 稳定事实/知识（「用户是副总经理」「项目 X 用技术栈 Y」），entity 锚定 | 语义相似 top-k | **不衰减**；同 entity 覆盖（version++） | 同 `entity_id` 覆盖 |
| `episodic` 情节记忆 | 具体事件、带时间锚点（「2026-07 PR #4412 移除了 buffer-pool 导致 OOM」） | 语义相似 top-k | **TTL 衰减** `exp(-age/ttl)` | LLM 去重 |

### 2.2 注入与按需读

- `CopilotService.run()` 开始时：读 Soul/User 文件全文 + 召回记忆（procedural 全量 + semantic/episodic 语义 top-N）→ `assemble_system_prompt`。
- **按需读**：`search_memory(query, kind?)` 工具，语义检索命中条目并回写 `access_count`/`last_access`。

### 2.3 写入（非追加，冲突判定）

- **Soul/User**：`write_memory("soul"/"user", content)` 覆盖写 MD 文件。
- **记忆条目**：`write_memory(kind, content, entity_id?)`：
  1. embed 新内容 → 同 kind 内 cosine top-k 候选（预筛）。
  2. **LLM 批量判定**（非纯 cosine 阈值）：`duplicate`（保留更完整的一条）/ `contradiction`（新的赢）/ 同主题不同事实（都留）/ `none`。
  3. **supersede 而非硬删**：输家打 `superseded=True`（可逆、留痕、保留溯源）；semantic 同 `entity_id` 直接覆盖（`version++`）。

### 2.4 遗忘（激活衰减 + 容量硬淘汰）

- **激活值**：
  ```
  activation(mem) = base + log(1 + access_count)·0.2 + recency
  base   = 1.0                                    # procedural / semantic（不衰减）
           exp(-age_days / ttl_days)             # episodic（默认 ttl 30 天）
  recency= 0.3 若 last_access 在 7 天内，否则 0
  ```
- **软遗忘（召回时）**：activation < `RECALL_FLOOR`（0.05）的记忆召回不到。
- **硬淘汰（写入前）**：`count(kind) >= MEMORY_CAPACITY`（默认各 200）时，按 activation 升序淘汰最低分腾空间再写（procedural 不参与淘汰，semantic 优先保留高版本）。
- **无后台 worker**：淘汰在 `write_memory` 内同步完成；`access_count`/`last_access` 由 `search_memory` 命中时回写（异步回写，不阻塞召回）。

---

## 3. 数据模型

### 迁移 `0002_copilot`

**新增表 `copilot_memories`**

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | `UUID` PK | `uuid.uuid4` |
| `kind` | `Enum`(native_enum=False) `procedural`\|`semantic`\|`episodic` | 三型 |
| `content` | `Text` | 记忆内容，非空 |
| `entity_id` | `String(255)` 可空 | semantic 的稳定实体键（`user:role` 之类）；空则不按实体 |
| `embedding` | `Vector(1024)` | bge-m3（`settings.embedding_dim`） |
| `ttl_days` | `Integer` 可空 | episodic 默认 30；procedural/semantic 空（不衰减） |
| `importance` | `Float` | 0~1，默认 0.5（LLM 写入时打分） |
| `access_count` | `Integer` | 命中次数，默认 0 |
| `last_access` | `DateTime(timezone)` 可空 | 最近命中 |
| `superseded` | `Boolean` | 冲突被覆盖标记（可逆、留痕），默认 false |
| `version` | `Integer` | semantic 同 entity 覆盖时递增，默认 1 |
| `created_at` / `updated_at` | `DateTime(timezone)` | 继承 `TimestampMixin` |

索引：HNSW 部分索引 on `embedding`（`WHERE NOT superseded`）；`kind` 普通索引；semantic 的 `(kind, entity_id)` 唯一/普通索引。

**新增表 `copilot_events`**（append-only 事件日志 = 思维链）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | `UUID` PK | `uuid.uuid4` |
| `seq` | `BigInteger` | 单调递增序号（按 run 排序重放） |
| `run_id` | `UUID` | 一次 agent run（≈ 一轮对话的一个 assistant 回答） |
| `type` | `String(32)` | `tool_call`/`tool_result`/`llm_delta`/`done`/`error` |
| `payload` | `JSONB` | 事件载荷（工具名/参数/结果摘要/文本增量） |
| `created_at` | `DateTime(timezone)` | 继承 `TimestampMixin`（append-only，不 update） |

> LangGraph checkpointer 的 `checkpoints`/`checkpoint_writes` 表由 `AsyncPostgresSaver.setup()` 建，不在 `0002` 迁移里手写。

**回改表**

| 表 | 改动 | 说明 |
|---|---|---|
| `chat_conversations` | +`kind`(String16 非空 默认 `qa`) | Copilot 会话 `kb_id=NULL` + `kind='copilot'` |
| `chat_messages` | +`steps`(JSONB 可空) | 工具轨迹 `[{tool_name,args}]`（UI 快读；完整思维链见 `copilot_events`） |

> Soul/User 不入库（文件），不建 `copilot_profile` 表、不建 `copilot_skills` 表。

---

## 4. Agent 编排（LangGraph）

```
app/agent/
├── state.py        # AgentState(TypedDict) = {"messages": Annotated[list, add_messages]}
├── graph.py        # build_copilot_agent(model, tools, system_prompt) → create_react_agent(...)
├── memory.py       # assemble_system_prompt(soul, user, recalled, context) → str
├── tools.py        # build_tools(...) → 9 个 LangChain 工具（闭包捕获请求作用域仓储/服务）
└── service.py      # CopilotService.run() → 注入记忆 → graph.astream → SSE + 事件日志落库
```

- **LLM**：`app/integrations/agent_llm.py` 的 `get_agent_model(settings) -> BaseChatModel`；`deepseek` → `ChatDeepSeek(api_key, model=settings.llm_model, temperature=0.3)`；`fake` → `FakeMessagesListChatModel`。
- **system prompt**：基础指令 + Soul 全文 + User 全文 + 召回记忆片段 + 当前上下文 + 三型分类引导（告诉 Agent 何时写何种记忆）+ 工具使用说明。
- **流式映射**：`graph.astream(stream_mode="messages")` 产出 `AIMessageChunk`——`tool_calls` → `step`、`content` → `delta`。

---

## 5. 工具集（9 个，副作用分层 + 描述工程 + 幂等）

| 工具 | 复用 / 行为 | 副作用 |
|---|---|---|
| `search_knowledge_base(query, kb_ids?)` | `RagRetriever.retrieve`（`app/rag/retriever.py:42`） | 只读 |
| `list_knowledge_bases()` | `KnowledgeBaseService.list` | 只读 |
| `list_notes()` | `NoteService.list` | 只读 |
| `read_document(document_id)` | `DocumentService.get_content`（`services/document.py:87`） | 只读 |
| `read_note(note_id)` | `NoteService.get`（`services/note.py:54`） | 只读 |
| `search_web(query)` | `WebSearchClient.search`（博查） | 只读 |
| `search_memory(query, kind?)` | 语义检索记忆，回写 access_count | 只读 |
| `create_note(title, content, kb_id?)` | 新增 `NoteService.create_with_content`；**幂等**：按 content hash 去重 | 写 |
| `write_memory(kind, content, entity_id?)` | 写记忆条目（去重+淘汰）；`soul`/`user` 覆盖写文件 | 写 |

**描述工程**：每个工具 docstring 即 description，含**触发时机 + 入参语义 + 返回格式**；入参用 pydantic 类型注解自动生成 schema；写工具在描述里声明副作用（「会新建笔记」）。

**结果截断**：`read_document`/`read_note`/`search_*` 返回截断到 `MAX_RESULT_CHARS`（默认 4000），超出尾部标注「结果已截断，可换更精确的查询」，避免大结果灌爆上下文。

> 报告/汇总不单列工具（= Agent 最终结构化长文回答）；「导入文档进知识库」不在 Agent 工具集内。

---

## 6. API 端点（`api/routes/copilot.py`）

### 6.1 `POST /api/copilot/chat`（SSE）

请求 `CopilotRequest{conversation_id?, question, context?: {kb_id?, note_id?, document_id?}}`。

| 事件 | 载荷 | 说明 |
|---|---|---|
| `meta` | `{conversation_id, user_message_id, assistant_message_id}` | 首问自动建会话（`kind='copilot'`、`kb_id=NULL`） |
| `step` | `{tool_name, args}` | 每次工具调用 |
| `delta` | `{text}` | 最终回答逐 token |
| `done` | `{assistant_message_id}` | 结束，**思维链经 checkpoint + 事件日志落库** |
| `error` | `{code, message}` | 失败 |

### 6.2 记忆 / 技能只读端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/copilot/memory` | `{soul, user, memories:[...]}`（按 kind 分组，面板只读） |
| GET | `/api/copilot/skills` | 静态内置技能清单（9 工具名称+描述+副作用） |

### 6.3 会话

`GET /api/conversations` 加 `kind` 查询参数（默认 `qa`）；浮窗传 `kind=copilot`。

---

## 7. 状态落盘（checkpoint + 事件日志，纯函数式可恢复）

> Agent 是纯函数——**给定状态即可恢复**。kima 按生产环境标准做三层：**LangGraph checkpoint**（状态快照 + 断点续跑）、**事件日志**（append-only 思维链，可审计可重放）、**幂等写**（重放不重复副作用）。

- **Checkpoint（状态恢复）**：接 LangGraph 官方 `langgraph-checkpoint-postgres` 的 `AsyncPostgresSaver`，graph 每个 superstep 自动落库（`checkpoints`/`checkpoint_writes` 表，同库）。`thread_id` = 会话 id。进程崩溃 / SSE 中断后，用同一 `thread_id` 重放 `graph.astream`，从最近 checkpoint 续跑，**不重执行已完成的工具调用**。这是 LangGraph 的生产级原语，天然实现「把 Agent 当纯函数、宕机只靠状态恢复」。
- **事件日志（思维链可观测）**：append-only `copilot_events` 表 `{seq, run_id, type, payload, created_at}`，`type ∈ {tool_call, tool_result, llm_delta, done, error}`，单调 `seq` 可重放。这是不可变的完整思维链，喂前端工具链 UI 与调试/审计。
- **幂等写**：写工具注入 `idempotency_key = "{run_id}:{seq}"`（`create_note` 按 key 去重、`write_memory` 冲突判定），崩溃重放不重复产生副作用。
- **`chat_messages`**：存 user/assistant 最终消息（会话历史）+ `steps`（工具轨迹，事件日志的轻量投影，供前端快读）；完整思维链以事件日志 + checkpoint 为准。

---

## 8. 可观测性（LangFuse）

> 运营侧可观测：trace 每次 agent 运行、每个 LLM 调用、每个工具调用，看成本/延迟/错误。与 §7 的 `copilot_events`（领域审计 + 崩溃重放）互补——这是「运营仪表盘」，不是「可重放状态」。接 LangFuse Cloud 免费档，可选、无 key 即关闭。

- 依赖：`langfuse` + `langfuse-langchain`（LangChain/LangGraph 原生 callback，`ChatDeepSeek` + react agent 自动被追踪，无需手写埋点）。
- `app/integrations/tracing.py`：`get_langfuse_handler(settings) -> CallbackHandler | None`——无 `langfuse_public_key`/`langfuse_secret_key` 时返回 `None`（默认关闭，不阻塞本地开发/测试）。
- 接入：`graph.astream(..., config={"callbacks": [handler], "metadata": {"run_id", "conversation_id", "question"}})`，自动产出三层 span：agent run（顶层）→ LLM 调用（model/token/延迟/成本）→ 工具调用（name/args/延迟/错误）。
- 配置：`langfuse_provider`（空/`cloud`）、`langfuse_public_key`/`langfuse_secret_key`/`langfuse_host`；不进 `0002` 迁移（LangFuse 存自己服务端）。

---

## 9. Worker 偷懒改造

> 现状：`document_worker`/`note_vectorize_worker` 是 `while True: run_once(); sleep(poll_interval)` 空转轮询。改为事件驱动——处理完待处理项就 idle，直到「新增文档 / 新增或更新笔记」才醒。

- `app/core/wake_events.py`：`document_wake_event`/`note_wake_event = asyncio.Event()`（进程内单例）。
- **API 触发**：`documents.py` 的 `create_file`/`create_from_url` 成功后 `document_wake_event.set()`；`notes.py` 的 `create_note`/`update_note` 后 `note_wake_event.set()`。
- **worker 改造**：`run()` 里 `run_once()` 后 `await asyncio.wait_for(wake_event.wait(), timeout=IDLE_FALLBACK_SECONDS)`（默认 300s）——有唤醒立即处理；超时兜底醒来跑 `recover_stuck`（document）后 `clear()` 继续等。兜底只用于卡死恢复，不空转。
- `main.py`：worker 构造注入对应 wake_event（测试可注入可控 `asyncio.Event`）。

---

## 10. 前端

```
src/components/copilot/
  CopilotLauncher/      # 右下角悬浮按钮，AppLayout 挂载，切换开合
  CopilotWindow/        # 单实例浮窗（复用 useWindowRect + ResizeHandles + DocumentWindow 样式）
  CopilotChat/          # 复用 components/chat 的 ChatInput + ChatMessageList
  CopilotSteps/         # 把 step 事件渲染成「正在检索知识库 / 读取文档 / 联网搜索 / 读取记忆…」折叠步骤条
  CopilotTrace/         # 展开历史消息的完整工具调用链（读 copilot_events 事件日志）
  CopilotMemoryPanel/   # 只读：Soul/User 文本 + 三型记忆条目（按 kind 分组）+ 内置技能清单
```

- 数据层：`api/copilot.ts`（`streamCopilot` 解析 meta/step/delta/done/error）+ `getCopilotMemory`/`getCopilotSkills`；`hooks/useCopilot.ts`（流状态 + steps）；`api/types.ts` 增类型。
- **上下文感知**：`AppLayout` 用 `useLocation`/`useParams` 推导 `/knowledge-bases/:id`→`{kb_id}`、`/notes/:id`→`{note_id}`，随请求提交。
- **记忆面板只读**：三型分组查看，不能手改（编辑靠对话）。

---

## 11. 测试策略（不起真库/真网/真 LLM）

| 层 | 测试 | 方式 |
|---|---|---|
| Agent 循环 | `test_copilot_agent.py` | `FakeMessagesListChatModel` 脚本「先 tool_calls 后答案」，断言工具被调 + 事件顺序 + 事件日志落库 |
| 工具 | `test_copilot_tools.py` | Fake repos/services：search/read/create_note（幂等去重）/write_memory/search_memory；结果截断 |
| 记忆召回 | `test_copilot_memory.py` | 三型分型召回（procedural 全量、semantic/episodic top-k）、激活衰减过滤、`superseded` 跳过 |
| 记忆冲突 | `test_copilot_conflict.py` | 候选预筛 + LLM 判定（duplicate/contradiction/同主题都留）、semantic 同 entity 覆盖 version++ |
| 记忆遗忘 | `test_copilot_forgetting.py` | activation 公式、容量硬淘汰、`search_memory` 回写 access_count |
| 文件 | `test_memory_store.py` | Soul/User 文件读写、幂等初始化 |
| worker 偷懒 | `test_worker.py` 增补 | 事件唤醒、无工作 idle、超时兜底 recover_stuck |
| API | `test_api_copilot.py` | SSE 冒烟 + `GET /copilot/memory` + `kind` 过滤 |

Fakes 增补：`FakeCopilotMemoryRepository`、脚本化 agent 模型；复用 `FakeKnowledgeBaseRepository`/`FakeNoteRepository`/`FakeEmbeddingClient`。

---

## 12. 实现顺序

| 步骤 | 内容 |
|---|---|
| T1 | pyproject 加 `langgraph`/`langchain-deepseek`/`langgraph-checkpoint-postgres`；迁移 `0002_copilot`（`copilot_memories` + `copilot_events` + chat 回改 `kind`/`steps`） |
| T2 | `integrations/agent_llm.py`（`ChatDeepSeek` + fake）+ `integrations/tracing.py`（LangFuse handler，无 key 返回 None）+ mypy overrides |
| T3 | `core/memory_store.py`（Soul/User 文件）+ `models/copilot.py` + `repositories/copilot.py`（向量检索/supersede/entity 覆盖） |
| T4 | `services/copilot.py`（三型召回/冲突/激活衰减/容量淘汰编排）+ `NoteService.create_with_content`（幂等） |
| T5 | `app/agent/`：state/graph/memory（system prompt 拼装 + 三型引导） |
| T6 | `app/agent/tools.py`（9 工具 + 副作用分层 + 结果截断 + 幂等）+ `app/agent/service.py`（run → SSE + 事件日志落库，graph 挂 `AsyncPostgresSaver` + LangFuse callback） |
| T7 | `api/routes/copilot.py` + 会话 `kind` 过滤 + deps/main 注册 |
| T8 | worker 偷懒改造（`core/wake_events.py` + workers + API 触发 + main 注入） |
| T9 | 后端测试全绿（ruff/mypy/pytest） |
| T10 | 前端 `api/copilot.ts` + `types.ts` + `hooks/useCopilot.ts` |
| T11 | 前端 `CopilotLauncher`/`Window`/`Chat`/`Steps`/`Trace`/`MemoryPanel` + AppLayout + 上下文推导 |
| T12 | 质量门禁（ruff/mypy/pytest/tsc/eslint/build）+ 手工验收 |

---

## 13. 已定决策

1. **核心定位**：Agent 工具干活 与 记忆越用越懂 **两者平衡**。
2. **编排**：LangGraph `create_react_agent`；只在编排层替换（对齐决策 #3）。
3. **LLM**：`langchain-deepseek` 的 `ChatDeepSeek`；复用 `settings.llm_model`。
4. **记忆双轨**：Soul/User = MD 文件（全文注入）；积累型记忆 = 向量条目，**三型分类**（情节/语义/程序）。
5. **分型召回**：procedural force-recall 全量；semantic/episodic 语义 top-k。
6. **写入非追加**：LLM 冲突判定（duplicate/contradiction/同主题都留）+ `superseded` 留痕；semantic 同 entity 覆盖 version++；Soul/User 覆盖写文件。
7. **遗忘**：激活衰减（episodic TTL + 频率 + recency）+ 召回 floor + 容量硬淘汰；无后台 worker。
8. **按需读记忆**：`search_memory` 工具，命中回写 access_count/last_access。
9. **工具工程**：副作用分层（7 只读 / 2 写）、描述工程、结果截断、写工具幂等。
10. **状态落盘**：LangGraph checkpoint（`AsyncPostgresSaver`）+ `copilot_events` 事件日志，配幂等写；「Agent 是纯函数，给定状态即可恢复」。
11. **Skill**：不做用户自定义 Skill；Skill = 内置工具，面板只展示。
12. **报告/汇总**：= Agent 最终结构化长文回答，不单列工具。
13. **联网**：要，复用博查 `WebSearchClient`。
14. **浮窗 + 上下文感知**：单实例全局浮窗，路由推导当前 kb/note 随请求提交。
15. **记忆面板只读**：三型分组查看，不能手改。
16. **会话**：复用 `chat_conversations`（`kind` 区分 qa/copilot），`chat_messages` 加 `steps`（工具轨迹投影）；完整思维链走 checkpoint + 事件日志。
17. **worker 偷懒**：document/note worker 改事件驱动（`asyncio.Event` 唤醒 + 长超时兜底 recover_stuck），并进本模块。
18. **默认值**：容量各 200、episodic ttl 30 天、recall floor 0.05、recency 窗 7 天、结果截断 4000 字、LLM 冲突候选 top-10——初值，实现后可调。
19. **可观测性**：接 LangFuse Cloud 免费档（`langfuse-langchain` callback，无 key 默认关），trace agent run / LLM / 工具调用，与 `copilot_events` 互补。
