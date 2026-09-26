# 模块 6：Copilot（知识 Agent）— 详细设计

> 日期：2026-09-20（v1 实现完成 2026-09-21；v2 生产级运行时 2026-09-21 起）
> 状态：v1 已实现；**v2（生产级运行时重构）已实现**（见 §4，含 Loop 五宪法加固 §4.9）；**六大契约工具层加固已实现**（见 §5.1）；**分路召回架构已实现**（见 §2.5：约束硬召回 + 混合召回 + 写入分类器，2026-09-22）；**遗忘三动作已实现**（见 §2.4：召回写回 + 跨型冲突 + 软删除复活，2026-09-22）；**LLM 网关已实现**（见 §4.11：统一门禁/记账/调用级快照/json_repair，2026-09-23）；**遗忘收尾已实现**（见 §2.4：duplicate 去重不写 + 窗口过期硬清理 + 复活加激活门槛 + 删死字段 importance，2026-09-23）；**契约交互已实现**（见 §4.13：约束显式携带 + 上行 OutputContract + planner 输出自检，2026-09-23）；**Agent 权限系统四道防线已实现**（见 §4.14：参数契约 + 工具调用量第五轴 + 出站 DLP 脱敏 + 安全熔断，2026-09-23）；**Agent 容错收尾已实现**（见 §4.16：幂等键稳定序号 + 熔断持久化/级联 + last_error 崩溃现场 + reactive 崩溃恢复端点，2026-09-23）；**LLM 网关「全量无裸调用」加固已实现**（见 §4.11：run_budget tracker+run_id 双必填 / current() fail-fast / 历史摘要·记忆 embedding·模块5 检索·worker 全经网关、删除裸 embedder 回退，2026-09-25）
> 上游基线：`docs/requirements.md`（决策 #2/#3）· `docs/module-5-ai-qa.md`（RagService / 检索层 / SSE）· `docs/module-4-documents.md`（文档/笔记向量化 + worker）

本模块交付「**Copilot 知识 Agent**」：把模块 5 的单次被动问答（`RagService.answer()`：改写 → 检索 → 生成）升级为 **LLM 自主调用工具的多步 Agentic 循环**，配**四型记忆**（约束/事实/偏好/情节）与**全局浮窗**形态。核心区别于问答：Copilot 能「办事」（检索/读/写/联网），而不只是「回答」。并在工程上做到四件事：**工具描述工程 + 副作用分层 + 幂等写**、**纯函数式状态落盘（含思维链，宕机可从状态恢复）**、**四型记忆的分路召回（约束硬召回 + 混合召回）/ 激活衰减遗忘 / LLM 冲突判定**、**可观测性（LangFuse trace）**。

**这些功能不做**：知识号发布（多用户）；Skill 广场；记忆面板的手动编辑；文档导入工具（Agent 触发 ingest）；共享知识库；多 Agent 协作 / 分布式 backends 抽象 / MCP / event sourcing / 自动学习闭环（这些重型能力 kima 单用户不需要）。**HITL 审批、熔断、四道闸、四轴预算、评测闭环**等「单 Agent 生产纪律」能力属 v2 范围（§4）。

---

## 1. 目标与验收

目标：全局浮窗 Copilot 能「把库里关于 X 的内容总结成一篇新笔记」「联网搜 A 再结合我库里的 B 写对比报告」；记忆按类型精准调取、越用越懂、越旧越淡化；Agent 全程状态（含思维链）落盘、宕机可从状态恢复；同时现有 document/note worker 从「轮询」改为「事件驱动偷懒」。Q&A 路径（`RagService`/`ChatService`）完全不动，Agent 复用检索层与业务层。

**验收标准（Definition of Done）**

| # | 验收项 |
|---|---|
| 1 | `alembic upgrade head` 成功（`0002_copilot`：`copilot_memories` + `copilot_events` 表 + chat 回改 `kind`/`steps`） |
| 2 | Agent 循环经 `langchain-deepseek`（`ChatDeepSeek`）+ LangGraph `create_agent` 跑通工具回环（fake 下确定性测试） |
| 3 | 9 个工具复用现有服务层，带**副作用分层**（7 只读 / 2 写）+ 描述工程 + 结果截断 |
| 4 | 记忆四型（约束/事实/偏好/情节）：分路召回（约束硬召回 + 混合召回）+ 激活衰减遗忘 + LLM 冲突判定 + `superseded` 留痕；Soul/User 存 MD 文件 |
| 5 | Copilot SSE 流式：`meta` → `step` → `delta` → `done`/`error`；**思维链经 checkpoint（`AsyncPostgresSaver`）+ 事件日志（`copilot_events`）双落库，可断点续跑** |
| 6 | worker 偷懒：无新文档/笔记时 idle 不空转，新增文档/更新笔记后立即唤醒；`recover_stuck` 兜底不丢 |
| 7 | 后端 `ruff` + `mypy(strict)` + `pytest` 全绿；前端 `eslint` + `tsc --noEmit` + `vite build` 全绿；测试不起真库/真网/真 LLM |
| 8 | LangFuse 可观测：配 key 后 agent run / LLM / 工具调用三层 trace 上报；无 key 时 handler=None 不阻塞 |

**v2 追加验收**（随 §4 重构落地）：

| # | 验收项 |
|---|---|
| 9 | 手写 `StateGraph`（reactive：agent ⇄ tools ⇄ review 进图）替换 `create_agent`，现有行为等价（轨迹断言证明） |
| 10 | 意图路由（task/qa/complaint/injection 四类，混合分类）+ complaint 确定性 workflow |
| 11 | 四轴预算（真算 cost + `asyncio.wait_for` 硬熔断）+ 死循环/幽灵循环检测 |
| 12 | 四道闸（入口/检索/工具参数/输出，fail-closed）+ 写工具 HITL（interrupt/resume） |
| 13 | 错误分级重试 + 工具熔断 + 结果 spill |
| 14 | 完整 LLM Planner（DAG 生成 + 失败 replan） |
| 15 | 评测闭环：golden 数据集 + LLM-judge + 轨迹断言 + 回归 runner（fake 模型确定性重放） |
| 16 | LLM 网关：所有 LLM/embedding/rerank 出口必经，统一门禁（80% 软提示 + 100% 硬停）/ 调用级快照（宕机恢复不重跑）/ json_repair 确定性修复 / 重试超时熔断 / 记账（见 §4.11） |

---

## 2. 记忆系统（核心）

> 设计原则：记忆会越来越长，不可能每轮全量注入，且不同类型的记忆「存活」方式不同。据此把积累型记忆分成四型，各配不同的召回、遗忘、冲突策略——约束是「确定域」硬召回、其余是「概率域」混合召回。Soul/User 仍为稳定短文本（文件），全文注入。**三层模型**：L0 身份层（Soul/User）+ L1 积累层（四型向量）+ L2 技能层（自定义 Skill，见 §2.6）。

### 2.1 存储与四型分类

| 记忆 | 形态 | 存储 | 注入 |
|---|---|---|---|
| Soul（人设/说话风格） | 一个 MD 文件 `soul.md` | `data/memory/` | 每轮全文注入 |
| User（档案/背景/偏好） | 一个 MD 文件 `user.md` | `data/memory/` | 每轮全文注入 |
| **约束 constraint** | 向量条目 | `copilot_memories` | **硬召回全量**（不过相似度阈值，红线无条件在场） |
| **偏好 preference** | 向量条目 | `copilot_memories` | **硬召回全量**（偏好/软规则，数量少） |
| **事实 fact** | 向量条目 | `copilot_memories` | **混合召回**（dense 向量 + lexical 词法 RRF）top-k |
| **情节记忆 episodic** | 向量条目 | `copilot_memories` | **混合召回**（dense 向量 + lexical 词法 RRF）top-k |

四型语义（`kind` 枚举）：

| kind | 是什么 | 召回 | 遗忘 | 冲突 |
|---|---|---|---|---|
| `constraint` 约束 | 硬规则/红线/禁令（「禁止联网搜索敏感话题」「严禁 DROP TABLE」），**确定域**——只要任务沾边就无条件在场 | **硬召回全量**（list_active，不过相似度阈值） | **不衰减**（activation 恒 1.0） | 语义去重；contradiction 新覆盖旧 |
| `preference` 偏好 | 偏好/软规则/风格默认（「用户喜欢简洁回答」「回答要 JSON」），稳定、长期有效 | **硬召回全量** | **不衰减**（activation 恒 1.0），仅受容量淘汰 | 语义去重；contradiction 新覆盖旧 |
| `fact` 事实 | 稳定事实/状态（「用户是副总经理」「项目 X 用技术栈 Y」），entity 锚定 | **混合召回**（dense + lexical RRF）top-k | **不衰减**；同 entity 覆盖（version++） | 同 `entity_id` 覆盖 |
| `episodic` 情节记忆 | 具体事件、带时间锚点（「2026-07 PR #4412 移除了 buffer-pool 导致 OOM」） | **混合召回**（dense + lexical RRF）top-k | **TTL 衰减** `exp(-age/ttl)` | LLM 去重 |

### 2.2 注入与按需读

- `CopilotService._assemble_context()` 开始时：读 Soul/User 文件全文 + 召回记忆（constraint/preference 硬召回全量 + fact/episodic 混合 top-N）→ `assemble_system_prompt`（L0 宪法层）+ `format_memory_block`（L2 记忆块，按 token 预算串行合并、约束子预算 ≤40%）。**2026-09-23 起此组装上移到意图路由之后、三种执行模式之前**——planner/qa/reactive 共用同一份，`system_prompt` + `memory_block` 随任务显式携带到 planner/synthesizer/qa（见 §4.13①）。
- **召回审计**：召回后落一条 `recall` 事件（各通道命中条数），事后能查清某条约束当时为什么没被召回。
- **按需读**：`search_memory(query, kind?)` 工具，语义检索命中条目并回写 `access_count`/`last_access`。

### 2.3 写入（非追加，冲突判定 + 分类器兜底）

- **Soul/User**：`update_profile("soul"/"user", content)` 覆盖写 MD 文件（服务层 `MemoryFileStore.write`）。
- **记忆条目**：`write_memory(kind, content, entity_id?)`（服务层 `CopilotMemoryService.write_memory`，kind ∈ constraint/fact/preference/episodic）：
  0. **写入分类器兜底**（`LLMMemoryClassifier`，temperature=0）：对 content 做确定性四型分类，其 kind / entity_id / trigger_conditions **覆盖** Agent 自报值——防止「禁止 ORM」被 Agent 当 fact 写入而走向量 top-k 漏召回。分类器失败回退 `None`（不覆盖，与现状一致）。
  1. embed 新内容 → **跨 kind** cosine top-k 候选（预筛）：constraint 是红线孤岛（只与 constraint 冲突）；事实/偏好/情节三型互为候选——对齐「套餐降级」跨型灾难（情节压偏好）。
  2. **LLM 批量判定**（非纯 cosine 阈值）：`duplicate`（语义等价、去重不写）/ `contradiction`（新的赢）/ 同主题不同事实（都留）/ `none`。
  3. **去重 vs 覆写两条路**：`duplicate`（同型语义等价）→ **去重不写**、touch 旧记忆续命（机制二「旧胜新丢」）；`contradiction` → 写新 + 输家打 `superseded=True` + `superseded_at`/`superseded_by`（软删除窗口留痕，供召回复活守卫）。fact 同 `entity_id` 直接覆盖（`version++`），并清理矛盾的旧偏好/情节。

### 2.4 遗忘（激活衰减 + 召回写回 + 软删除窗口 + 容量硬淘汰）

- **激活值**：
  ```
  activation(mem) = base + log(1 + access_count)·0.2 + recency
  base   = 1.0                                    # constraint / preference / fact（不衰减）
           exp(-age_days / ttl_days)             # episodic（默认 ttl 30 天）
  recency= 0.3 若 last_access 在 7 天内，否则 0
  ```
- **软遗忘（召回时）**：activation < `RECALL_FLOOR`（0.05）的记忆召回不到。
- **召回写回（检索强化）**：`recall()` 命中条目（fact/episodic 上场者）回写 `access_count+1`/`last_access`（`repository.touch`，单条 Core UPDATE 不阻塞召回）——ACT-R 频率/近期增益的活水，不再只靠 `search_memory` 工具。
- **软删除窗口（冲突误判的退路）**：输家 `superseded=True` + `superseded_at`（时间戳）+ `superseded_by`（压它的那条 id）。召回时 `search_recoverable` 找窗口期内（默认 7 天）强命中（余弦 ≥ `memory_revival_similarity`，默认 0.5）的 superseded 软记忆，过两道门槛——**复活守卫**：`superseded_by` 还活着（真冲突仍成立）则不复活，已废弃/删除则放行；**激活门槛**：`activation < RECALL_FLOOR` 的不复活（复活了也上不了场、徒留僵尸 active 行）。守卫查压它的赢家用 `get_many` 批量查（避免逐条 N+1）。放行则翻回 `superseded=False` 并回写 touch。过窗不复活，交给窗口过期硬清理彻底退场。
- **硬淘汰（写入前）**：`count(kind) >= MEMORY_CAPACITY`（默认各 200）时，按 activation 升序淘汰最低分腾空间再写（constraint 不参与淘汰，fact 优先保留高版本）。
- **窗口过期硬清理（后台周期任务）**：软删除窗口（7 天）过期的 superseded 记忆由 lifespan 的 `_run_superseded_cleanup` 周期任务硬删除（`repository.delete_superseded_older_than`），真删、找不回——「遗忘」的收尾闭环、防表无限膨胀。其余（淘汰/复活/召回写回）仍在 `write_memory`/`recall` 内同步完成。

### 2.5 分路召回架构（约束硬召回 + 混合召回，2026-09-22）

> 对照《分路召回架构》一讲：Agent 系统里的信息分「**确定域**」（约束/红线，只要任务沾边就必须在场）与「**概率域**」（事实/偏好/情节，大概相关就够）。把确定域的东西扔进概率域（向量相似度 + 阈值）去检索，就会发生「禁止 ORM」相似度 0.38 < 阈值 0.45、红线被概率淹没。解法 = **分类存、分路召**。

**写入时加固（防线一）**：`MemoryClassifier`（`app/agent/memory_classifier.py`）在落库前用 temperature=0 的轻量 LLM 把记忆确定性分为四型（constraint/fact/preference/episodic），映射到 `kind`（constraint→CONSTRAINT / fact→FACT / preference→PREFERENCE / episodic→EPISODIC），**覆盖** Agent 自报的 kind。正则匹配「禁止/必须」是「用二战雷达拦隐身导弹」——语义交给 LLM。

**读取时硬召回（防线二）**：四阶段并行召回、按优先级串行合并（`services/copilot.py` 的 `recall` + `agent/memory.py` 的 `format_memory_block`）：

| 阶段 | 召回方式 | 目标 | 适用记忆 |
|---|---|---|---|
| 1 规则硬召回 | `list_active(CONSTRAINT)` 全量，不过相似度阈值 | 红线无条件在场 | constraint |
| 2 规则硬召回 | `list_active(PREFERENCE)` 全量 | 偏好/软规则长期在场 | preference |
| 3 混合召回 | dense 向量 + lexical 词法（`tsv` + pg_jieba BM25）RRF 融合 | 语义 + 精确串（表名/错误码）都命中 | fact |
| 4 混合召回 | 同上 | 同上 | episodic |

- **查询并行、合并串行**：四通道同时查，合并时按 约束→偏好→事实→情节 逐条扣 token 预算（`memory_block_max_tokens` 默认 2000），**约束子预算 ≤40%**——红线优先在场、但不独占全部预算。
- **词法通道**：`copilot_memories.tsv`（`to_tsvector('jiebacfg', content)` 生成列）+ GIN 索引，`search_lexical` 补 dense 漏掉的精确串；与 dense RRF 融合（`_fuse_memories`，键=memory.id）。
- **软遗忘只作用于概率域**：constraint/preference 走全量硬召回、不经 activation floor；fact/episodic 混合召回后才过 `_above_floor`。
- **事后审计**：`recall` 事件落库记录各通道命中条数（`copilot_events`，type=`recall`）。

### 2.6 自定义 Skill（L2 技能层，2026-09-26）

> 补齐三层模型的 L2：程序性知识（「怎么做」的经验技巧）不再混进 L1 四型——抽离成**自定义 Skill**，一个 skill = 一个 MD 文件。对应 ima 的 AGENT.md「经验技巧」，但拆成多文件（每个可复用技巧一个）。

- **存储**：`data/skills/*.md`（文件、不入库，与 Soul/User 同构）。**无 DB 表**——skill 就是 Markdown 文本资产。
- **格式**：frontmatter `name`（唯一标识）+ `description`（一句话，供召回匹配）+ 正文（skill 的指令/经验）。**暂不做**模板/程序/沙箱——skill 是「一份 Markdown 说明书」，无可执行体。
- **注入**：按需召回（语义匹配 description/正文）而非全文常驻——skill 数量会增长，不能像 Soul/User 每轮全量注入。
- **增删改查（Agent 工具）**：`list_skills`/`read_skill`（只读）+ `write_skill`（MEDIUM，upsert 覆盖同名）+ `delete_skill`（HIGH，走 HITL 审批；**只能删自定义 skill、官方内置工具不可删**）。文件系统直放仍可用，`write_skill` 按 frontmatter `name` 覆盖既有文件、兼容手工直放。
- **与内置工具的区别**：现有 `GET /api/copilot/skills` 返回**内置工具清单**（name/description/has_side_effect，即「Agent 能调的能力」）；自定义 skill 是「用户沉淀的可复用经验」（知识资产），二者不同、勿混。

---

## 3. 数据模型

### 迁移 `0002_copilot`

**新增表 `copilot_memories`**

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | `UUID` PK | `uuid.uuid4` |
| `kind` | `Enum`(native_enum=False) `constraint`\|`preference`\|`fact`\|`episodic` | 四型 |
| `content` | `Text` | 记忆内容，非空 |
| `entity_id` | `String(255)` 可空 | fact 的稳定实体键（`user:role` 之类）；空则不按实体 |
| `embedding` | `Vector(1024)` | bge-m3（`settings.embedding_dim`） |
| `ttl_days` | `Integer` 可空 | episodic 默认 30；constraint/preference/fact 空（不衰减） |
| `trigger_conditions` | `JSONB` 可空 | constraint 的触发条件（如 `{"type":"domain","value":"database"}`），本轮仅落库埋点、召回暂不消费 |
| `access_count` | `Integer` | 命中次数，默认 0 |
| `last_access` | `DateTime(timezone)` 可空 | 最近命中 |
| `superseded` | `Boolean` | 冲突被覆盖标记（可逆、留痕），默认 false |
| `superseded_at` | `DateTime(timezone)` 可空 | 软删除窗口起点（被 superseded 的时间戳，N 天内可召回复活） |
| `superseded_by` | `Uuid` 可空 | 压它的那条记忆 id（复活守卫：压它的还活着就不复活） |
| `version` | `Integer` | fact 同 entity 覆盖时递增，默认 1 |
| `created_at` / `updated_at` | `DateTime(timezone)` | 继承 `TimestampMixin` |

索引：HNSW 部分索引 on `embedding`（`WHERE NOT superseded`）；`kind` 普通索引；fact 的 `(kind, entity_id)` 唯一/普通索引；`tsv` GIN 索引（词法检索，见迁移 `0004`）。

> **迁移 `0004_constraint_lexical`**（2026-09-22）：`copilot_memories` 加 `trigger_conditions`（JSONB 可空）+ `tsv` 生成列（`GENERATED ALWAYS AS to_tsvector('jiebacfg', content) STORED`，存量自动回填）+ GIN 索引 `ix_copilot_memories_tsv`。`kind` 加 `constraint` 值**无需改表**（`String(16)`、无 CHECK 约束）。

> **迁移 `0005_memory_forgetting`**（2026-09-22）：`copilot_memories` 加 `superseded_at`（`DateTime(timezone)` 可空）+ `superseded_by`（`Uuid` 可空）——软删除窗口（遗忘第三动作）的留痕字段。迁移链：`0001_initial → 0002_copilot → 0003_daily_budget → 0004_constraint_lexical → 0005_memory_forgetting`。

> **迁移 `0006_llm_snapshot`**（2026-09-23）：新增 `copilot_llm_snapshots` 表——LLM 调用级快照（决策 D3/D4/D8，见 §4.11）。字段：`run_id`(String64) + `call_key`(String64，内容哈希) 联合主键 + `kind`(chat/agent/embedding/rerank) + `output`(JSONB) + `usage`(JSONB 可空) + `created_at`。`created_at` 建索引供 TTL 清理。迁移链追加 `→ 0006_llm_snapshot`。

> **迁移 `0007_drop_importance`**（2026-09-23）：`copilot_memories` 删 `importance`（`Float`）——写入恒 0.5、全库无消费的死字段（未接入 activation，纯占位）。迁移链追加 `→ 0007_drop_importance`。

> **迁移 `0008_copilot_plan`**（2026-09-23）：新增 `copilot_plans` 表——planner 计划检查点（Plan-as-Data 第三道防线，见 §4.12）。字段：`run_id`(Uuid PK) + `version`(Integer) + `state`(JSONB，`{task, version, steps[]}` 完整计划状态) + `created_at`/`updated_at`。迁移链追加 `→ 0008_copilot_plan`。

> **迁移 `0010_copilot_approval`**（2026-09-23，down_revision=`0009_concurrency`）：新增 `copilot_approvals` 表——HITL 审批单第一类实体（见 §4.15）。字段：`id`(Uuid PK) + `run_id`(Uuid 索引) + `tool`(String64) + `args`(JSONB) + `summary`(Text，人话摘要) + `level`(String16) + `status`(String16，pending/approved/rejected/expired) + `decision`(String16 可空) + `decided_at`/`expires_at`(DateTime(tz) 可空) + `created_at`/`updated_at`。`run_id` 建普通索引 `ix_copilot_approvals_run_id`。

> **迁移 `0011_breaker_state`**（2026-09-23，down_revision=`0010_copilot_approval`）：新增 `copilot_breakers` 表——工具/资源级熔断状态外置（第三层「动作指纹」持久化，见 §4.16）。字段：`name`(String128 PK，工具名或 `resource:<name>`) + `state`(String16，closed/open/half_open) + `failures`(JSONB，墙钟失败时间戳列表) + `updated_at`。崩溃后据此续读失败计数，避免「恢复后计数归零、永远触发不了熔断」。迁移链追加 `→ 0011_breaker_state`。

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

## 4. Agent 运行时（v2：生产级，手写 StateGraph）

> v1 用 LangGraph 的 prebuilt 回环搭起来跑通；v2 将其重构为**手写 `StateGraph`** 的生产级运行时。原因不是「框架不好」，而是 prebuilt 的固定 `model↔tools` 回环装不下三类控制流——**进入循环前的意图路由**、**循环中的输出自检回环**、**分布在入口/工具/输出三处的注入闸**——这三类正是 LangGraph 定义「该下探到 StateGraph」的场景。工具执行仍用 `ToolNode`、模型仍用 `ChatDeepSeek`、checkpoint 仍用 `AsyncPostgresSaver`：只换编排拓扑，不重造底子。

### 4.1 目标（生产六性）

可审计、崩溃可复原、改进有根据、长程不混乱、可观察、AI 犯错及时阻止。

### 4.2 分层结构

```
app/agent/
  runtime/                  # 执行运行时
    config.py               # RuntimeConfig：打包四轴预算/防循环/注入闸/HITL 安全旋钮
    state.py                # AgentState（可序列化：messages + last_error 崩溃现场 + trust + compression_level）
    budget.py               # 四轴预算 turns/seconds/tokens/cost + 第五轴 max_tool_calls，真算 cost + asyncio.wait_for 硬熔断
    daily_budget.py         # 跨 run 全局日预算（成本/token 硬上限，DailyBudgetStore 外置 DB、按自然日重置）
    loop_guard.py           # 死循环指纹 + 幽灵循环上下文 hash
    reactive.py             # reactive 循环：agent ⇄ tools（含写工具 HITL interrupt）图构建
    reactive_helpers.py     # reactive 模块级纯函数（业务意图幂等键注入 / 工具结果零信任 / 错误格式化）
    planner.py              # LLMPlanner：DAG 生成 + replan（真实实现）
    plan_model.py           # Plan-as-Data 数据模型（PlanStep/Plan 版本化状态机 + parse/validate）
    executor.py             # DAG 执行器：拓扑执行 + 增量重规划（局部作废 + merge）
    workflow.py             # 确定性手写流程（complaint）
    router.py               # 意图路由（supervisor）
  guardrail/
    trust.py                # 零信任评分：红线 + 三维度（内容/来源/行为）加权 + 五档处置
    injection.py            # 红线正则语料 + 祈使句浓度（信任评分的检测原语）
    sensitive.py            # 敏感脱敏：模型输出/审计日志里的密钥/邮箱/手机号打码
    document_guard.py       # KB 写库闸：投毒文档入库前按分拦截（不重试）
    review.py               # 输出审查节点（进图，替代 v1 的 graph 外 while 回环）
  resilience/
    error_classifier.py     # is_retryable + classify_error：异常 → 可重试（黄灯）/ 不重试（红灯）/ 分类标签（崩溃现场持久化）
    result.py               # ToolOutcome（OK/TRANSIENT/PERMANENT）+ ToolFailure（reason/hint/code）
    timeout.py              # with_timeout：per-tool 超时（estimated_latency_ms × 3）
    retry.py                # 退避重试（只重试黄灯）
    circuit_breaker.py      # 工具熔断（CLOSED/OPEN/HALF_OPEN + 级联 resource + store 持久化跨崩溃）+ available() 候选集剔除
    security_breaker.py     # 安全熔断（防线④：安全行为信号驱动，手动恢复，无自动 HALF_OPEN）
    spill.py                # 大工具结果落盘 + read_tool_result
  toolmeta.py               # ToolMeta 元数据注册（SideEffectLevel / ToolRegistry / idempotency_key_for+request_hash_for / OutputContract / ParamContract + apply_output_contract + validate_param_contract）
  gateway.py                # LLM 网关主体（LLMGateway：门禁/快照/重试超时熔断/记账/80% 软提示）
  gateway_context.py        # 网关 run 期上下文注入（ContextVar：当前 tracker + run_id）
  gateway_codec.py          # 网关序列化/指纹/出站 DLP 脱敏纯函数
  snapshot.py               # 调用级快照协议 + 内存实现（Postgres 实现见 repositories/llm_snapshot.py）
  graph.py                  # 总图装配
  tools.py                  # 15 工具闭包（组装 + 六要素描述 + 四层守卫）
  tools_helpers.py          # 工具集模块级 helper + 参数契约
  tools_registry.py         # ToolMeta 注册表（build_registry：工具名 → 元数据单一真源）
  memory.py                 # system prompt 拼装（L0/L2 分层 + 记忆块 token 预算合并）
  memory_classifier.py      # 写入分类器（temperature=0 四型分类，覆盖 Agent 自报 kind）
  events.py                 # Copilot 流式事件类型（SSE 推给前端的结构化事件联合类型）
  side_effect.py            # DbSideEffectVerifier（写工具副作用确定性对账，回查 note/memory）
  helpers.py                # 共享小工具（_bounded_call 预算包裹 / _clip / 历史截断 / _tool_source）
  plan_mode.py              # planner 执行模式（DAG 规划 → 拓扑执行，mixin 挂载）
  plan_answer.py            # planner「合成 + 输出自检」mixin（PlanSynthesisMixin）
  qa_mode.py                # qa 执行模式（RAG 直答，不进工具循环，mixin 挂载）
  service.py                # CopilotService（主流程 + reactive 执行路径，SSE 编排）
  service_resume.py         # HITL 恢复/审批单 mixin（resume/resume_after_crash/_record_approval）

backend/eval/agent/         # 评测闭环
  dataset.py / trajectory.py / runner.py
```

「内容 vs 框架」边界：四型记忆（`services/copilot.py`，召回/写入拆为 `copilot_recall.py`/`copilot_write.py` mixin、共享常量/融合在 `copilot_common.py`；分类器 `memory_classifier.py`）、15 工具、review 的对账逻辑、事件日志、checkpoint 是**内容**，原样组装；新写的是 runtime/guardrail/resilience/evaluation 四层骨架 + 装配。

### 4.3 三个执行模式

| 模式 | 触发 | 实现 |
|---|---|---|
| reactive | 短任务（几轮内搞定） | agent ⇄ tools 循环 + review 节点 + 预算/防循环 |
| planner | 开放式长任务 | LLM 产出带依赖 DAG → executor 逐步执行 → 失败 replan |
| workflow | 确定性流程（complaint） | 手写图，不碰工具/不写记忆，安抚 + 记录 + 升级 |

### 4.4 意图路由（supervisor）

混合分类：规则/关键词第一刀 + LLM 兜底（省成本 + 路由抗注入）。四类：`task`→planner/reactive、`qa`→RAG 直答（不进工具循环）、`complaint`→workflow、`injection`→L1 拒绝（模型不见这条指令）。

### 4.5 零信任评分（三维度 + 五节点 + 分级处置）+ HITL + review

给每份进入上下文的数据打 0-100 可信度分、按分处置，替代一刀切的布尔 veto。

- **红线**：硬正则（忽略/忘记/角色切换/格式逃逸/泄露提示词）命中即**一票毙**（`injection.py`），不进评分。
- **三维度**（`trust.py`）：内容（100 − 20×祈使密度）+ 来源（系统 90 / 用户 70 / 模型 60 / KB 60 / 工具 40 / 网页 20）+ 行为（写 ≥3 次 −25 / 写后回读 −20）。综合分 = 加权平均（内容 0.5 / 来源 0.3 / 行为 0.2，假设值，待标注样本校准）。
- **五节点**：输入 / 检索 / 上下文 / 工具调用 / 输出，各节点重新评估、分数只减不增（`AgentState.trust` min 合并）。
- **分级处置**：80-100 放行 / 60-79 观察（审计）/ 40-59 隔离（`<data>` 标签）/ 20-39 脱敏 / 0-19 阻断。
- **写库闸**：文档入库前按分拦截（`document_guard.py`），红线即拒（直接置 error，不重试）。
- **脱敏**：模型输出与审计日志里的密钥/邮箱/手机号打码（`sensitive.py`）。
- **HITL（分级审批，2026-09-23）**：写工具 `interrupt()` 挂起 → 前端确认 → resume 重放同一调用（不重问 LLM）。不再是一刀切的「所有写都审批」——那是审批疲劳的根源。改为**规则化分级（Policy-as-Code）**：`app/agent/approval.py` 的 `ApprovalPolicy` 把工具 `SideEffectLevel` 映射成三档处置——`LOW` 自动放行、`MEDIUM`（建笔记/写记忆）异步通知 + 事后审计（自动执行，review 节点 + 事件日志对账）、`HIGH`（覆盖人设档案，`update_profile`）同步审批打断人。规则即代码、可 CR，不引入黑盒风险分。配套三件套：**审批单第一类实体**（`copilot_approvals` 表，pending/approved/rejected/expired 状态机，`GET /copilot/approvals/pending` 找回挂起审批）、**超时 fail-close**（`expires_at` 过期默认阻断，`copilot_approval_timeout_seconds` 默认 15 分钟）、**证据包**（审批载荷带 `summary` 人话摘要 + `level` 风险等级 + `args` 原始参数，前端渲染「动的是什么、风险多高」而非裸 JSON）。详见 §4.15。
- **review**：进图，`OK→END / mismatch→回 agent`（attempts 上限）；**fail-closed**——审查器解析失败判 `UNVERIFIED` 追加诚实更正、不回退放行；**确定性副作用对账**（`SideEffectVerifier` 回查 DB 确认写工具声称的 id 真落库）先于 LLM 判定，防「伪造证据骗校验器」。

### 4.6 四轴预算 + 防循环

- **预算**：turns / seconds / tokens（计费=总减 cache）/ cost。每次进模型前 + 工具批后查；`asyncio.wait_for` 硬熔断；cost 用 DeepSeek 定价表真算。**跨 run 全局日预算** `DailyBudget`（成本/token 按自然日累计、`DailyBudgetStore` 外置 DB，重启续读、防绕过单日上限）。**第五轴 `max_tool_calls`（2026-09-23）**：单 run 工具调用次数上限（默认 50，None=不限），`record_tool_calls` 在执行工具前计数——穷举爆破的兜底（见 §4.14 防线②）。
- **80% 软提示（2026-09-23）**：任一轴占用 ≥ `copilot_llm_gateway_soft_threshold`（默认 0.80）时，网关以「尾三明治」把「预算已用 80%、请立即收尾给出最好答案」钉到本次输入末尾；100% 才硬停（`BudgetExceeded`）。软提示是 volatile 的，不参与快照指纹。
- **LLM 用量记账（2026-09-23 起由网关统一）**：所有 LLM/embedding/rerank 调用经网关 `LLMGateway`（§4.11）统一预检 + 记账，不再散落各调用点——早先的「planner `last_usage` / reviewer `sink` / classifier `sink`」手写记账全部收敛到网关。`BudgetExceeded` 在 review 上抛（硬停 run），在 classifier/judge/planner/embed 上 fail-closed（跑在工具内/独立模式/后台，优雅降级更安全）。
- **单位任务账本 + 缓存命中率监控（2026-09-23）**：done 事件随 run 结束落「单位任务成本」——`BudgetTracker.summary()` 导出 `RunAccounting`（cost/tokens/input/output/cache_read/turn/tool_call），service `_done_payload` 拼进 `copilot_events` 的 `done` payload，附带归因维度 `intent`（task/qa/plan/…）+ `model`。归因是「多维度归因」的最小形态（单用户无鉴权，用户维度恒一），把「事后只能看 `DailyBudget` 聚合总账」变成「每个任务一张能拆到 token/cache 的明细」。`cache_hit_rate = cache_read / input` 随 done 落库，输入量足够大（≥2000 token）且命中率低于 `copilot_cache_hit_rate_warn`（默认 0.3）时 `logger.warning` 告警——命中率骤降是前缀缓存被破坏的信号（如往 L0 塞动态时间戳）。**局限**：账本在内存，崩溃续跑（resume/resume_after_crash）只计恢复后用量、不跨崩溃累计（对齐「tracker 不进 checkpoint」的既有取舍）。
- **防循环**：dead loop（工具+参数指纹，去 volatile 键）+ ghost loop（上下文 hash）。窗口存 state → checkpoint 续跑不丢循环记忆。
- **上下文防挤压**：注入会话历史按 token 预算驱动（`assemble_history` 超预算对早期历史 LLM 摘要、保留近期 N 轮 verbatim；无摘要器则 `_truncate_history_tokens` 硬截断），系统区（soul/user/记忆）每轮重注入、不参与压缩。

### 4.7 可靠性

- **错误分级** → 红灯（`ToolFailure` PERMANENT）不重试、黄灯（TRANSIENT / 超时）退避重试；错误带 `reason` + `hint` 给模型可操作引导。
- **熔断**：连续失败 → OPEN → HALF_OPEN 探测；熔断工具从候选集**剔除**（`CircuitBreaker.available()` 在 bind_tools / 传 planner 前过滤），模型/planner 看不到坏工具，而非调用后返回「暂时不可用」字符串空转 token。
- **spill**：大工具结果落盘 + 占位符，不截断丢信息。

### 4.8 评测闭环（核心执行）

golden 数据集 + LLM-judge + 轨迹断言（调了哪些工具/顺序/次数/最终结果）+ 回归 runner，fake 模型确定性重放。只覆盖核心执行；记忆/防注入/plan 靠事件日志观察。

### 4.9 Loop 五宪法（编排层加固，2026-09-22）

> 对照《Loop Engineering 的暗面》5 条宪法补齐宏观编排层缺口。核心命题：**「大模型是油门，Loop 才是刹车系统」**——终止权 / 裁决权 / 恢复权 / 协调权都必须是确定性代码，不交给概率模型。

| 宪法 | 落地（kima） | 代码位置 |
|---|---|---|
| ① 硬边界熔断 + 独立裁决 | 四轴 `HardBudget`（turns/seconds/tokens/cost）+ `asyncio.wait_for` 硬熔断；跨 run 全局日预算 `DailyBudget`（`DailyBudgetStore` 外置 DB 持久化）；死循环指纹 + 幽灵上下文 hash（`loop_guard.py`）；「防幻觉终止」= review 节点独立裁决（Maker/Checker 分离） | `runtime/budget.py` / `runtime/loop_guard.py` / `guardrail/review.py` |
| ② 提议-裁决分离 + 权限门禁 | 模型只 `bind_tools` 出候选，执行前 Loop 裁决：写工具 HITL `interrupt()` 审批、注入闸 `scan_tool_calls` 拦工具参数、plan 模式 `_tool_map` 未知工具抛错；「写代码的」与「查代码的」分离（agent 产出 / review 节点复核） | `runtime/reactive.py` / `runtime/config.py` / `service.py` |
| ③ 显式状态机 + 上下文防挤压 | 手写 `StateGraph` + 可序列化 `AgentState`（无隐式 `while True`）；上下文按 token 预算分层防挤压（`assemble_history` 摘要早期历史、系统区永不压缩） | `runtime/state.py` / `runtime/reactive.py` / `rag/context.py` / `service.py` |
| ④ 持久化可恢复 | 细粒度 checkpoint `AsyncPostgresSaver`（Windows dev 降级 `InMemorySaver`）+ append-only 事件日志 `copilot_events`（思维链可重放） | `graph.py` / `main.py` / `repositories/copilot.py` |
| ⑤ 同步与隔离 | 写操作串行化：`_serialize` 锁串行化共享 AsyncSession 的工具访问（单用户、单进程，无多 Loop 并发踩踏） | `tools.py` |

> 思考题「执行模型伪造证据骗校验模型」的解法即 §4.5 的**确定性副作用对账**——`SideEffectVerifier` 回查 DB 而非信任另一个 LLM 的判定，从根上断了「伪造证据」这条路。

### 4.10 结构化输出：schema 进提示词 + pydantic 严格解析（2026-09-22，2026-09-23 改 json_repair + 统一 schema）

> 对照《格式幻觉》一讲：结构化输出不是「加行 Prompt + `json.loads`」，而是**数据契约的工程实现**。四处 LLM→JSON 契约（planner DAG / review 判定 / conflict 数组 / 记忆分类）原来只停在第一档（Prompt 手写示例），结构/语义层失败被手写 `.get()` 静默吞掉。

**核心缺陷（已修）**：`review` 闸门原本只在 `json.loads` 抛异常时 fail-closed；合法 JSON 但形状错（`{}`、`{"verdict":"maybe"}`、字段名拼错）会默认 `OK` → **闸门打开**——正是「格式完美的幻觉值不报错、直接污染下游」。现在：

- **输入侧：schema 进提示词（2026-09-23）**：每个 LLM→JSON 输出点定义完整 pydantic 模型，字段语义写进 `Field(description=...)`（中文），`model_json_schema()` 直接产出带语义的完整 schema（含嵌套/枚举），由 `format_instructions(model)` 拼进提示词，**取代各点手写 JSON 示例**——schema 一份顶全部，不再「示例与模型两处漂移」。`conflict` 用 `RootModel[list[ConflictVerdict]]` 保持裸数组契约。
- **输出侧：双层防线**：语法层 `loads_json_repair`（`strip_code_fence` → `json.loads` → 失败则 `json_repair` 确定性修复）+ 结构/语义层 Pydantic（`_ReviewOutput.verdict: Literal["ok","mismatch"]`、`_PlanModel`/`_PlanStepModel`、`_MemoryClassOutput`、`_ConflictOutput`）+ 业务层 `validate_plan`（`action ∈ tool_names`、依赖步骤存在、id 唯一、无自依赖）。统一封装成 `parse_json(raw, model)`（`loads_json_repair` + `model_validate`，失败抛 `StructuredParseError`），四个调用点共用，不再各写一遍。任一失败抛 `StructuredParseError`（带精确错误），不再是静默默认值。
- **json_repair 替代自纠错（2026-09-23，决策 D5）**：语法层坏 JSON（缺逗号/尾逗号/未加引号 key 等）用 `json_repair` 库**确定性修复**，不再「喂回 LLM 再烧一次」的自纠错——省掉重复调用 = 省钱。`json_repair` 只修语法、不修语义（枚举越界/字段类型错仍由 Pydantic 严格兜底）。原「`except StructuredParseError → append 错误 → 重试」的自纠错循环已删除，review/planner/classifier/conflict 四处都改成**单次调用 + fail-closed**。
- **全字段严格**：解析统一走严格 `model_validate`——`issues` 从 `list[Any]` 收紧为 `list[_ReviewIssue]`（`claim`/`tool`/`evidence` 全类型化），classifier/conflict 的手写 dict/长度校验也统一成 pydantic；一处字段坏则整体 fail-closed，不再「核心字段严格、附属字段宽松」的双轨。对「` OK `」「` DUPLICATE `」这类细微格式偏差仍做 `strip().lower()` 归一化（field/model validator）。
- **优雅降级（fail-closed）**：解析失败即降级——review 判 `UNVERIFIED`（诚实更正）、planner 空 plan（退化为 reactive / 「抱歉没能规划」）、conflict 全 `none`、classifier 回退 `None`（安全方向：只漏去重/漏分类、不错去重/错分类）。
- **代码位置**：`integrations/llm.py`（`format_instructions`/`parse_json`/`loads_json_repair`/`StructuredParseError`/`first_validation_error`）、`guardrail/review.py`（`_ReviewOutput` + `_ReviewIssue`）、`runtime/plan_model.py`（`_PlanModel` + `_PlanStepModel` + `validate_plan`）、`services/conflict.py`（`_ConflictOutput`）、`agent/memory_classifier.py`（`_MemoryClassOutput`）。

### 4.11 LLM 网关（统一出口，2026-09-23）

> 核心命题：**任何一次 LLM/embedding/rerank 调用都是真金白银，漏一处门禁/记账就是经济损失**。解法 = 所有调用收进一个 `LLMGateway` 统一出口，调用点不再手写「预算预检 / 超时 / 重试 / 记账 / 解析兜底」。

**动机**：模块 6 运行时里 LLM 调用散落在 8 处（agent 主循环 / review / planner / classifier / judge / 上下文摘要 / embedding / rerank），门禁行为严重不一致——只有 agent 主循环有完整四轴预算预检，其余要么「只事后记账不预检」、要么「完全不记账」（上下文摘要）。这正是经济损失的来源。

**网关五段管线**（每次调用统一走）：

```
preflight（熔断 + 预算硬停 + 80% 软提示）→ 快照复用（命中已成功调用直接返回，不重跑）
→ retry/timeout 执行 → 记账（Usage 回写 run tracker + 跨 run 日预算）→ 快照落库
```

- **三套调用形态统一抽象（D6）**：`complete()`（一次性结构化，包 `LLMClient.chat`）+ `invoke_model()`（流式工具调用，包 `BaseChatModel.ainvoke`）+ `stream()`（流式生成，包 `LLMClient.stream`，逐 token yield 同时缓冲全文记账/快照，2026-09-25 补）+ `embed`/`embed_query`/`rerank`（模块 5 纳入）。
- **80% 软提示 / 100% 硬停（D2）**：任一轴（turns/seconds/tokens/cost）占用 ≥ 0.80 → 尾三明治注入「预算已用 80%、立即收尾」；100% → `BudgetExceeded` 硬停。软提示是 volatile 的、不参与快照指纹。
- **调用级快照（D3/D4/D8）**：以「node + 规范化输入」的 sha256 为键（`call_key`），成功后落 `output` 到 `copilot_llm_snapshots`（Postgres，迁移 `0006`）；恢复时命中缓存直接复用、不重跑（纯函数记忆化）。重放语义 **at-least-once**（崩溃窗口内「已成功未落快照」会重放，可接受）。与 LangGraph checkpoint 互补：checkpoint 决定「从哪个节点续跑」，快照决定「节点内已成功的调用是否真发」。`resume_after_crash(run_id)` 提供崩溃续跑入口。
- **重试/超时/熔断统一（D7）**：瞬时异常退避重试 + 秒轴 `asyncio.wait_for` 硬熔断 + LLM 服务级熔断（复用 `CircuitBreaker` key=`"llm"`），调用点不再手写。`stream()` 例外：流式已部分输出、无法重放，故只做秒轴超时（`asyncio.timeout`）+ 失败记熔断、不重试。
- **记账统一（D1）**：所有出口回写 run 四轴 `BudgetTracker`（`count_turn` 区分 agent 轮次 vs 辅助调用）+ 跨 run `DailyBudget`。per-run 的 tracker 经 `ContextVar`（`run_budget(tracker, run_id)`）注入——网关是 app 级单例，tracker 是 per-run 闭包。**2026-09-25 起 `tracker` 与 `run_id` 都是 `run_budget` 必填参数**，`current()` 无 context 直接 `raise RuntimeError`（fail-fast）——网关只能在 run 上下文里跑，没有「非 run 回退」。
- **预算模型（2026-09-25 定稿）**：`BudgetTracker.budget` 改为可选 `HardBudget | None`——**循环调用（agent）必带 `RuntimeConfig.budget`**（全轴硬限：turns/seconds/tokens/cost，`RuntimeConfig.budget` 改为 concrete 默认、不可为 None）；**非循环调用（worker/模块 5 检索）传 `None`**（无 per-run 预算，只记账 + 日预算 sink，时间由网关 per-call 超时 `asyncio.wait_for` 兜底——超时 `GatewayConfig.timeout` 默认 60s、恒生效、不允许 None 关掉）。**删 `HardBudget.unlimited()`**（无上限预算 = 变相无限，不允许存在）。
- **`BudgetExceeded` 语义**：review **上抛**（硬停 run）；classifier/judge/planner/embed **fail-closed**（跑在工具内/独立模式/后台，`handle_tool_errors` 会吞异常、优雅降级更安全）。

**迁移范围（全量，无裸 LLM/embed/rerank）**：所有 LLM/embed/rerank 调用全收进网关——agent 主循环、review、记忆分类、冲突判定、planner、上下文压缩、**历史摘要 `assemble_history`**、**记忆写/召回 embedding**（`memory.write`/`memory.recall`/`memory.search`）、embed/rerank（`RagRetriever` + `ingest`/`note_vectorize` worker，模块 5 检索也经网关）、**模块 5 生成侧**（`RagService.answer` 的改写 `rewrite_query` / 摘要 `_summarize` / 最终 `stream`，2026-09-25 迁入——`answer()` 用 `run_budget(BudgetTracker(None, sink=daily_budget), run_id=f"rag:{uuid}")` 包住整次回答，一次回答=一个 run）。**2026-09-25 起彻底删除「`gateway=None → 退裸 embedder/reranker/LLMClient`」的回退**——`CopilotMemoryService`/`RagRetriever`/`IngestService`/`NoteVectorizeService` 构造只收必填 `gateway`；`get_llm_gateway`/`get_daily_budget` 上移到 `deps_core` 供模块 5/6 共用。**铁律：任何组件要调 LLM/embed/rerank 只能收 `gateway` 并先 `run_budget`，没有裸 embedder 的说法。**

### 4.12 Plan-as-Data 三道防线（计划数据化 + 增量重规划 + 事件溯源/检查点，2026-09-23）

> 对照《蒙眼狂奔的 ReAct》一讲：ReAct 的计划藏在模型临时思维（CoT）里，系统看不见——单步失败后模型只能「重试所有步骤」，把带副作用的工具（重新 Dump）重复执行、放大成生产事故。**计划的本质是系统掌控的执行契约，不是模型的自言自语**。planner 路径此前「LLM 出 DAG + 失败 replan」只有 DAG 的形，缺三件事：运行时状态散在 executor 局部变量（`pending`/`done`）、失败后 replan 提示「depends_on 留空」导致失败步骤下游被孤儿化（必然抛「循环依赖」报错）、无任何崩溃恢复。本节补齐三道防线。

**第一道：计划数据化（`runtime/plan_model.py`）**——`PlanStep` 从 frozen 变为带运行时状态的可寻址对象：`step_id/action/params/depends_on/is_terminal`（LLM 生成）+ `status`(PENDING/RUNNING/COMPLETED/FAILED/OBSOLETE)/`output_ref`/`error`/`version_created`/`replaces_step_id`（系统填写）。`Plan` 从 `tuple` 变为**版本化状态机**：`_steps`(id→step) + `_dependents`(反向邻接)，`get_parallel_ready()` 按拓扑序取就绪步骤（执行由数据结构驱动，模型退化为只出蓝图）。`output_ref` 让下游步骤引用上游产物，不必重跑；`replaces_step_id` 是重规划新步骤的「替代身份证」。`validate_plan` 增环检测（Kahn）；`_parse_plan_strict` 在构造 Plan（dict 去重）之前拦截空/重复 id。

**第二道：增量重规划（`runtime/executor.py`）**——失败时不再推倒重来：失败步骤标 `FAILED`（审计信号）→ `mark_downstream_obsolete()` 顺反向邻接把下游标 `OBSOLETE`（已完成步骤保留、其产物 `output_ref` 仍可复用）→ replan 产出带 `replaces` + 完整 `depends_on` 的替换步骤 → `merge()` 防御性合并（依赖校验 + 环检测 + 撞 id 校验 + 对被替换旧步骤强制打 OBSOLETE 双保险 + `version++`）。**修复了「失败步骤下游孤儿化」的核心 bug**：旧实现 replan 提示「depends_on 留空」+ 不标下游，导致依赖失败步骤的下游永远无法就绪、必然误报「循环依赖」。replan 输出在 `LLMPlanner.replan` 做工具名合法性拦截（幻觉工具边界拒收，不白烧一次执行），依赖/环/撞 id 交 `merge` 校验（失败重试 replan、超限抛 `PlanExecutionError`）。

**第三道：事件溯源 + 检查点**——①**事件溯源**：plan 状态迁移落 append-only `copilot_events`——`plan_created`/`step_started`/`step_completed`/`step_failed`/`plan_replanned`（带 `version`），与既有 `tool_call`/`tool_result` 互补，整条计划 V1→V2 变迁可回放；②**检查点**：`copilot_plans` 表（迁移 `0008`）在每次状态迁移后落 `{task, version, steps[]}` 快照，`Plan.to_dict`/`from_dict` 序列化。planner 路径跑在 graph 外、无 LangGraph checkpointer，靠此快照崩溃后 `resume_plan(run_id)` 加载续跑——已 `COMPLETED` 步骤不重跑、从首个 `PENDING` 断点续（`POST /api/copilot/plan/resume`）。检查点落库 best-effort（失败不阻断本轮执行）。与 §7 的 LangGraph checkpoint（reactive 图级）与 `copilot_llm_snapshots`（LLM 调用级）三层互补：checkpoint 决定「从哪续」、快照决定「调用是否真发」、plan 快照决定「planner 路径从哪续」。

> **边界修订**：决策 #28 曾写「不做 event sourcing」。此处是**planner 路径的轻量事件溯源**（复用既有 `copilot_events` 追加日志 + 一张检查点表），不是全量 CQRS/事件存储重构——不推翻 #28 对 reactive/reactive 全态的边界，仅补上「长程 DAG 任务可回溯可恢复」这一块。

### 4.13 契约交互（约束显式携带 + 上行契约 + planner 输出自检，2026-09-23）

> 对照《09｜Agent 之间的信息传递：从互相投毒到契约交互》一讲。核心命题：多 Agent 系统里「信息跨过边界」这一动作（下发/回传/转交）就是故障源——噪音、幻觉、约束、错误结论都在过境时流动。kima 是单 Agent，但三种执行模式之间同样有「信息过境」：路由层（service）把任务下发 planner、executor 把工具结果回传给 synthesizer。三条补丁把「过境」这道关收口。**明确不做多 Agent 的统一 Crossing/Port 抽象、message_id 全局去重、分布式服务端强制边界**——单 Agent 用不上，属过度设计（对齐决策 #28）。

**① 约束显式携带（文档 failure #3「约束蒸发」）**——此前「绝不能删除数据」这类约束只写在 reactive 的 system prompt 里，planner/qa 分支在召回之前就 `return`，CONSTRAINT 硬召回被整条旁路，约束在交接中蒸发。修复：`_assemble_context(question, run_id)` 把「读 soul/user + `recall()` + 组装 system_prompt/memory_block」抽成单一入口、上移到意图路由之后、三种执行模式**之前**；`_run_plan`/`_run_qa`/`_synthesize_plan_answer`/`resume_plan` 都显式携带 `system_prompt` + `memory_block`；`LLMPlanner.generate` 加 `constraints=""` 参数——规划器下行任务包带约束（对齐文档 `HandoffPacket(constraints=[...])`）。

**② 上行契约（文档 failure #2「8000 token 执行史」）**——工具结果流向 synthesizer（父 Agent）前过一道契约关。`toolmeta.OutputContract(max_chars / required / optional)` + `apply_output_contract()`：只验形状不看内容（`required` 字段缺失/类型不符 → 拒收占位符；`optional` 之外未声明字段 → 白名单剥离；`max_chars` → 结构裁剪）。`ToolMeta` 加 `output_contract` 字段；检索类工具（`search_knowledge_base`/`read_document`/`read_note`/`search_web`/`search_memory`）声明 `max_chars=SYNTHESIS_RESULT_CHARS(2000)`，结果截到结论级、长正文走 spill/`read_tool_result`（「大体积数据只传引用」）。`required`/`optional` 结构化分支已实现并单测，当前无工具声明（工具都返回字符串）、生产走 `max_chars` 分支，是未来结构化工具的接缝。

**③ planner 输出自检（补 `agent-output-review` 铁律在 planner 旁路的缺口）**——`plan_answer._review_plan_answer`：先跑**确定性副作用对账**（写工具步骤回查 DB、`SideEffectVerifier`，防「伪造证据骗校验器」，与 reactive 的 verifier 同源），再跑 `reviewer` 的 LLM 判定（合成回答 vs 计划执行轨迹，每步 action + 状态 + 结果），mismatch 追加诚实更正、unverified fail-closed（复用 `self._reviewer`/`self._verifier`/`_review_enabled`，与 reactive review 同语义）。此前 planner 合成后直接 `_finalize_answer`、无对账，合成 LLM 可能「声称完成某步但该步实际失败/没执行」。

### 4.14 Agent 权限系统（四道防线，2026-09-23）

> 对照《11｜RBAC 挡不住 Agent：如何设计一个企业级 Agent 权限系统》一讲。核心命题：RBAC 只回答「这个角色能不能对这个资源做这个操作」，管不到「一次取多少条 / 参数是否越界 / 数据取走后流向哪里 / 失控了谁拔线」。执行者从人换成不知疲倦的 Agent，同一套权限模型立即暴露：只读权限穷举扫表、单点全合规的组合外泄、任务跑完凭证还活着。文档给出 MCP 网关四道防线——**kima 是单用户（无认证 / 无 MCP / 无 token），防线①（OBO 令牌交换）架构层面不适用**，其余三道落成代码级机制。

| 文档防线 | kima 落地 | 状态 |
|---|---|---|
| ① OBO 令牌交换（消灭凭证残留） | 单用户无 token 概念，**明确不做**；「不要长期残留凭证」原则落为运维建议（`.env` 的 provider key 最小权限化/轮换） | 不适用 |
| ② 参数级校验（遏制穷举爆破） | `ToolMeta.param_contract`（min/max/enum/pattern）+ `validate_param_contract` 在 `tool_node` 执行前统一拦截；`HardBudget.max_tool_calls` 第五轴（单 run 工具调用次数上限） | **已实现** |
| ③ DLP（数据外泄防护） | `sensitive.py` 补中国场景 PII（身份证/银行卡/护照，数字边界断言）；LLM 网关 `complete`/`invoke_model`/`stream` 出站前 `redact_sensitive`（`copilot_llm_dlp_redact`） | **已实现** |
| ④ 运行时熔断（出事拔权限） | `SecurityBreaker`（手动恢复，无自动 HALF_OPEN）与基础设施 `CircuitBreaker`（自动恢复）分离；反复参数契约违规 → 熔断 → `SecurityBreakerTripped` 硬停 run | **已实现** |

**防线② 参数契约 + 工具调用量上限（穷举爆破源头遏制）**——文档场景「Agent 拿只读权限 `user_id` 从 1 遍历到 10000 扫穿整表」在 kima 的等价物是「`list_notes(offset=0,100,200,…)` 分页穷举整库」。此前参数校验散落在各工具体内（`min(max(limit,1),100)` / `_parse_uuid` / `_parse_kind`），且 loop guard 把 limit/offset 当 volatile 键**剔除**（`loop_guard._VOLATILE_KEYS`）——分页穷举完全绕过死循环检测。两层补丁：① `ParamContract` 声明式边界下沉到 `ToolMeta`（`list` 的 `limit∈[1,100]`/`offset≥0`、`kind` 枚举、`document_id`/`note_id` UUID 格式，共 7 个工具声明），`tool_node` 执行前统一校验、违规 fail-closed 拒收（返回「参数校验失败」给模型改）；② `HardBudget` 加第五轴 `max_tool_calls`（默认 50），`record_tool_calls` 在执行工具前计数，堵住「换 query / 交替工具 / 分页」这些单指纹检测抓不到的穷举形态。

**防线③ DLP（数据外泄）**——`sensitive.py` 原正则偏美国场景（SSN）+ API key，缺中文 PII；补身份证（18 位、含日期结构校验）、银行卡（银联 62 开头 16-19 位）、护照（E/G+8 位）。用数字边界断言 `(?<!\d)(?!\d)` 替代 `\b`（Python `\w` 含 Unicode 字母，CJK 与数字相邻时 `\b` 不成立），并把身份证排在手机号之前（否则 `13` 开头身份证被无边界手机号正则截走前 11 位）。**LLM 网关出站脱敏**是文档点名的关键通路——「Agent 调大模型 API 时整个上下文发给模型服务商」：`LLMGateway.complete`/`invoke_model`/`stream` 出站前对消息内容做 `redact_sensitive`（`copilot_llm_dlp_redact` 开关，默认开），快照指纹仍在脱敏前算（缓存键语义稳定）。embed/rerank 不脱敏（检索语义不允许、且是用户自己的数据）。写工具落库前 DLP、`search_web` 出站扫描暂未做（当前无外部写类工具，属可选加固）。

**防线④ 安全熔断 vs 基础设施熔断**——文档强调熔断要针对安全行为信号（反复参数校验失败 / 越权尝试），且**安全熔断不能自动恢复、须人工介入**。既有 `CircuitBreaker` 是基础设施熔断（只对可重试瞬时异常计数、60s 后自动 HALF_OPEN）。新增 `SecurityBreaker`（`resilience/security_breaker.py`）：违规计数达阈值即 `tripped`，只可手动 `reset()`、无自动恢复路径；`tool_node` 已熔断则抛 `SecurityBreakerTripped` 冻结 run（与 `BudgetExceeded`/`InfiniteLoopDetected` 同一条终止路径）。app 级单例注入（`main.py`/`deps.py`），`copilot_security_breaker_enabled` 默认 `False`（激进的熔断默认可选，参数契约校验本身始终生效）。

**代码位置**：`toolmeta.py`（`ParamContract` + `validate_param_contract`）、`tools.py`（7 个工具声明契约）、`runtime/reactive.py`（`tool_node` 统一拦截 + 安全熔断 + 工具调用量计数）、`runtime/budget.py`（`max_tool_calls` 第五轴 + `record_tool_calls`）、`guardrail/sensitive.py`（中国场景 PII）、`gateway_codec.py`（`_redact_chat`/`_redact_langchain`）+ `gateway.py`（`dlp_redact`）、`resilience/security_breaker.py`（新）。测试：`tests/test_copilot_permission.py`（17 用例）。

### 4.15 HITL 分级审批（正确设计 HITL 审批系统，2026-09-23）

> 对照《12｜人审批后事故反而更大：正确设计 HITL 审批系统》一讲。核心命题：同步阻塞地「所有写操作都推给人审批」会制造审批疲劳——红灯不稀缺，人就闭眼点通过，安全阀变橡皮图章。文档给的三板斧：**规则化分级重建稀缺性 + 高信噪比证据包 + 超时阻断（fail-close）**。kima 落地如下（注意：文档的「Kafka 消息队列」「黑盒 AI 风险打分」本模块明确不做——单用户本地应用，前者过度设计，后者文档自己也反对）。

| 文档要点 | kima 落地 | 状态 |
|---|---|---|
| 挂起/恢复（Agent 框架角色） | LangGraph `interrupt()` + checkpoint + `Command(resume=)` 重放同一调用（不重问 LLM） | **已实现（此前）** |
| 规则化分级（重建稀缺性） | `app/agent/approval.py` 的 `ApprovalPolicy`（Policy-as-Code）：`SideEffectLevel` → 三档处置 `ALLOW`/`NOTIFY`/`REQUIRE_APPROVAL`；`graded()` 工厂 = LOW 放行 / MEDIUM 通知 / HIGH 审批，`strict()` = 全写审批（旧布尔语义） | **已实现** |
| 分级赋值 | `update_profile` 定为 `HIGH`（覆盖人设档案：不可逆、无版本历史）；`create_note`/`write_memory` 保持 `MEDIUM`（幂等/去重的增量写） | **已实现** |
| 审批单第一类实体 | `copilot_approvals` 表（`id`/`run_id` 索引/`tool`/`args`/`summary`/`level`/`status`(pending/approved/rejected/expired)/`decided_at`/`expires_at`）；把「等审批」从 checkpoint（dev 是内存版、前端刷新即丢）解耦成可查询/可恢复状态 | **已实现** |
| 超时阻断（fail-close） | `expires_at` + `copilot_approval_timeout_seconds`（默认 900s）；`resume` 前检查，过期即 `expire` 并按拒绝处理（写不执行）；`list_pending` 惰性翻转过期单 | **已实现** |
| 证据包（高信噪比） | `CopilotApprovalEvent` 载荷带 `summary`（`approval_summary` 确定性人话摘要）+ `level` + `args`；前端审批卡展示风险徽章 + 摘要 + 可展开参数 | **已实现** |
| 找回挂起审批 | `GET /copilot/approvals/pending` 列出待审单（前端刷新/关闭后仍可续批） | **已实现** |

**分级裁决是共享单一路径**：`resolve_approval_decision(name, registry, runtime)` 同时供 reactive 工具门禁（`tool_node`）与 planner 候选集剔除（`_plan_tool_names`）消费，避免规则漂移。reactive 里 `REQUIRE_APPROVAL` → `interrupt()`；`NOTIFY`/`ALLOW` → 自动执行（`NOTIFY` 的写仍走 review 节点确定性副作用对账 + `copilot_events` 事件日志，即「事后审计」）。planner 无 interrupt/resume 机制，故只剔除 `REQUIRE_APPROVAL` 工具、`NOTIFY`/`ALLOW` 照常自动执行 + 审计。

**超时 fail-close 的语义**：`interrupt()` 挂起时写操作本就没执行，超时只需让审批单**失效**（不再可批）——「没人批 = 阻断」是 interrupt 设计的固有性质，故无需后台 worker 主动回放拒绝（那是单用户应用外的过度设计）。`resume` 收到过期单的续批请求时，强制 `expire` 并按拒绝处理，兜住「过期后仍被点通过」的竞态。

**配置**：`copilot_require_write_approval`（总开关，默认 `False`）、`copilot_approval_mode`（`graded` 默认 / `strict`）、`copilot_approval_timeout_seconds`（默认 900）。`deps.py` 的 `_approval_policy_for` 把开关翻译成策略；`get_approval_store` 注入 `SqlAlchemyApprovalStore`。

**代码位置**：`agent/approval.py`（新：`ApprovalDecision`/`ApprovalPolicy`/`resolve_approval_decision`/`approval_summary`）、`agent/tools.py`（`update_profile` → HIGH）、`agent/runtime/config.py`（`approval_policy` 字段）、`agent/runtime/reactive.py`（分级门禁 + 证据载荷 + checkpointer 守卫）、`agent/plan_mode.py`（`_plan_tool_names` 共享裁决）、`agent/events.py`（`CopilotApprovalEvent` 带 summary/level）、`agent/service_resume.py`（`_record_approval`/`list_pending_approvals`/`resume` 超时 fail-close）、`models/copilot.py`（`CopilotApproval` + `ApprovalStatus`）、`repositories/approval.py`（新：`ApprovalStore` + SQLAlchemy + InMemory）、`schemas/copilot.py`（`CopilotApprovalRead/List`）、`api/routes/copilot.py`（`GET /approvals/pending`）、`api/deps.py`（`get_approval_store` + `_approval_policy_for`）。迁移 `0010_copilot_approval`。测试：`tests/test_approval_policy.py`（13 用例）+ `tests/test_copilot_approval.py`（分级 MEDIUM 自动执行 / HIGH 证据载荷）。

### 4.16 Agent 容错（状态外置 + exactly-once 幂等键 + 级联熔断，2026-09-23）

> 对照《14｜Agent 挂了，如何不丢状态、不丢钱、不丢数据地拉起来？》一讲。核心命题：**Agent 容错 = 「状态可重建」**——把状态从 Agent 进程里彻底剥离，Agent 变无状态 Reducer，崩溃后从 Checkpoint + Event 恢复。所有容错能力（安全重试 / exactly-once / 熔断 / 预算）都是这一个原则的推论。此前 kima 的六层状态里四层已外置（推理轨迹→checkpoint、会计账本→`DailyBudget`、待审批→`copilot_approvals`、计划锚点→`copilot_plans`），但「动作指纹」（熔断计数）和「崩溃现场」（last_error 分类）仍只在进程内存，且幂等键踩了文章点名的反例。本节补齐。

**六层状态对照**

| 层 | 含义 | kima 落地 |
|---|---|---|
| ① 推理轨迹 | LLM 的记忆（messages/tool_history） | LangGraph checkpoint（reactive）+ `copilot_plans`（planner）+ `copilot_events`（事件日志） |
| ② 会计账本 | 烧了多少钱（turn/token/cost） | `BudgetTracker`（run 内）+ `DailyBudget`（跨 run 落库，重启续读） |
| ③ 动作指纹 | 熔断防线（调用计数） | **本次新增**：`CircuitBreaker` + `copilot_breakers` 持久化（`BreakerStore`），失败计数跨崩溃不归零 |
| ④ 待审批动作 | 暂停键的位置 | `copilot_approvals`（§4.15） |
| ⑤ 崩溃现场 | 判决书（last_error 分类） | **本次新增**：`AgentState.last_error`（reactive）/ `Plan.last_error`（planner），带 transient/permanent 分类 |
| ⑥ 计划锚点 | Plan-DAG + 稳定幂等序号 | `copilot_plans` + **本次修正**：`idempotency_seq` 随检查点持久化 |

**本次改的三件事**

1. **exactly-once 幂等键修正（P0，真实 bug）**。旧实现违反「幂等键必须来自持久化状态里的稳定序号，不能来自运行时变量」：reactive 用**消息内 `enumerate` 下标**做键（`reactive._inject_idempotency_keys`）——跨轮碰撞，第 1 轮 `create_note` 和第 2 轮 `write_memory` 都拿 `{run_id}:0`，`IdempotencyRegistry` 命中缓存后**第二次写被静默吞掉（丢数据）**；planner 用 `_execute_plan` 的局部 `call_index`——崩溃 `resume_plan` 归零重算、键变，外部系统识别不了。修正：reactive 的 `AgentState` 加 `idempotency_seq`（单调递增、随 checkpoint 落库），键 = `{run_id}:{seq}`；planner 的 `Plan` 加 `idempotency_seq`（随 `copilot_plans` 检查点落库），`_execute_plan` 从断点继续递增。崩溃续跑序号不重算、同一步骤拿到同键。
2. **崩溃现场持久化（P1）**。③动作指纹——`CircuitBreaker` 加 `store`（`BreakerStore` + `copilot_breakers`，迁移 `0011`），失败计数外置、崩溃不归零；时间从 `time.monotonic()` 改 `time.time()`（墙钟跨重启才可比）；`load_all()` 启动续读。⑤崩溃现场——`classify_error(exc)` 返回 `transient`/`permanent`，工具失败写 `last_error`（reactive 走 `AgentState.last_error`、planner 走 `Plan.last_error`），随 checkpoint 持久化，恢复后据此判断「别重试」还是「瞬态可重试」。
3. **级联熔断（P2）+ 崩溃恢复端点（P1）**。`ToolMeta` 加 `resource`（`db`/`web`/`file`），`CircuitBreaker` 加 resource 命名空间——共享依赖挂 → 同资源所有工具一起熔断、从候选集剔除（DB 挂不拖累联网搜索）。`POST /api/copilot/resume` 接 `resume_after_crash`（reactive 崩溃续跑入口，LangGraph 从 checkpoint 续跑）。

**明确不做**：降级切备用模型（主模型限流 → 备用，本期不做）；②层 per-run 账本持久化（`BudgetTracker` 仍内存闭包，跨 run 的 `DailyBudget` 已落库，崩溃续跑单 run 预算重置可接受）；③层 LoopGuard 死循环指纹仍 run 内闭包（崩溃后 messages 从 checkpoint 恢复、模型可见调用史，损失较小）；幂等 registry 仍内存态（跨崩溃 exactly-once 靠工具自身幂等 create_note content_hash / write_memory 冲突判定 + 稳定键，不额外落 registry）。

**代码位置**：`runtime/state.py`（`idempotency_seq`/`last_error`）、`runtime/reactive_helpers.py`（`_inject_idempotency_keys` 单调序号）+ `runtime/reactive.py`（`_format_tool_error_tracked` 追踪 last_error）、`runtime/plan_model.py`（`Plan.idempotency_seq`/`last_error` 序列化）、`plan_mode.py`（`plan.idempotency_seq` 断点续增 + `plan.last_error`）、`resilience/circuit_breaker.py`（store + 墙钟 + resource 级联）、`resilience/error_classifier.py`（`classify_error`）、`repositories/breaker.py`（新：`BreakerStore` + `SqlAlchemyBreakerStore` + `InMemoryBreakerStore`）、`models/copilot.py`（`CopilotBreaker`）、`api/routes/copilot.py`（`POST /resume`）。迁移 `0011_breaker_state`。测试：`tests/test_copilot_resilience.py`（+5 用例：幂等键单调不碰撞 / plan 序列化往返 / classify_error / breaker 持久化 / 级联）。

---

### 4.17 写工具幂等性升级：业务意图键 + 持久化幂等表（2026-09-24）

> 对照《Agent Tools 的幂等性》一文。核心命题：**幂等键标识「一次业务意图」，不是「一次 HTTP 请求」，更不是「第几次调用」的位置序号**。§4.16 把位置序号从「消息内下标」修正为「checkpoint 单调序号」，但仍未脱离「位置」语义——review 回环让模型重发同一写指令、或崩溃续跑，拿到新序号 → 新键 → 快路径去重失效，只能靠模糊的业务兜底（`write_memory` 冲突判定）兜住。本节把键升级为「业务意图」内容派生键，并把去重从内存 `IdempotencyRegistry` 升级为 Stripe 式持久化幂等表。

**改的三件事**

1. **幂等键 = 业务意图（内容派生）**。`ToolMeta` 加 `idempotency_key_fields`（`create_note`→`("content",)` 对齐 content_hash 唯一索引；`write_memory`→`("kind","content","entity_id")`），键 = `{run_id}:{tool_name}:sha256(规范化 key_fields)`。同一业务意图跨重试/崩溃/回环重发拿到同一键；删掉 `idempotency_seq`（位置序号成死代码）。
2. **持久化幂等表 `copilot_idempotency`**（迁移 `0013`）。主键 `(tool_name, idem_key)` 原子抢占：`INSERT ON CONFLICT DO NOTHING` 插 `processing` → 成功后转 `succeeded` 落结果缓存、永久失败转 `failed_final` 落错误。`request_hash`（完整参数指纹）做同键不同参数冲突检测（§5.4 拒绝而非静默返回旧结果）；`expires_at` 做 processing 残留 TTL 回收（崩溃在 claim 后 succeed 前留下的孤儿）。§4.16 的「幂等 registry 仍内存态」在此升级。
3. **三态简化**。文章四态里的 `failed_retryable` 由工具层 `with_retry` 在内存兜底（瞬态错误不落表），幂等表只记 `processing/succeeded/failed_final`。

**代码位置**：`repositories/idempotency.py`（`IdempotencyStore` Protocol + `SqlAlchemyIdempotencyStore` + `InMemoryIdempotencyStore` + `IdempotencyClaim`）、`models/copilot.py`（`CopilotIdempotency`/`IdempotencyStatus`）、`toolmeta.py`（`idempotency_key_fields` + `idempotency_key_for`/`request_hash_for`，删 `IdempotencyRegistry`）、`tools.py`（`build_tools` 注入 `idempotency_store` + 两写工具 claim/succeed/fail 三步法）、`reactive_helpers.py`/`plan_mode.py`（内容派生键注入，删 `idempotency_seq`）、`deps_copilot.py`（`get_idempotency_store`）。配置 `copilot_idempotency_ttl_seconds`（默认 86400）。测试 `tests/test_copilot_idempotency.py`（+7）。

**明确不做**：`failed_retryable` 独立状态、TTL 后台 sweep（惰性回收，对齐 approval 惰性失效）、tenant_id（单用户，键含 run_id 划界）、Outbox/Saga、乐观锁版本号（当前无删改/转账类工具，`update_profile` 覆盖写天然幂等）。

---

## 5. 工具集（15 个，六要素描述 + 副作用分层 + 幂等 + 元数据）

| 工具 | 复用 / 行为 | 副作用 |
|---|---|---|
| `search_knowledge_base(query, kb_ids?)` | `RagRetriever.retrieve`（`app/rag/retriever.py:42`） | 只读 |
| `list_knowledge_bases(limit?, offset?)` | `KnowledgeBaseService.list`；超一页提示 `has_more` | 只读 |
| `list_notes(limit?, offset?)` | `NoteService.list`；超一页提示 `has_more` | 只读 |
| `read_document(document_id)` | `DocumentService.get_content`（`services/document.py:87`） | 只读 |
| `read_note(note_id)` | `NoteService.get`（`services/note.py:54`） | 只读 |
| `search_web(query)` | `WebSearchClient.search`（博查） | 只读 |
| `search_memory(query, kind?)` | 语义检索记忆，回写 access_count | 只读 |
| `read_tool_result(path, grep_pattern?)` | 读落盘的工具结果全文（spill 后按需查） | 只读 |
| `create_note(title, content, kb_id?, idempotency_key?)` | `NoteService.create_with_content`；**幂等**：content hash 去重 + 业务意图幂等键（§4.17） | 写（MEDIUM） |
| `write_memory(kind, content, entity_id?, idempotency_key?)` | 写记忆条目（写入分类器兜底 + 冲突判定去重 + 容量淘汰）；kind ∈ constraint/fact/preference/episodic | 写（MEDIUM） |
| `update_profile(kind, content)` | 覆盖写 `soul.md` / `user.md`；kind ∈ soul/user | 写（MEDIUM） |
| `list_skills()` | 列出已安装的自定义 skill（`data/skills/*.md`） | 只读 |
| `read_skill(name)` | 读某个自定义 skill 的正文（markdown） | 只读 |
| `write_skill(name, description, content)` | 创建/覆盖一个自定义 skill（upsert，覆盖同名） | 写（MEDIUM） |
| `delete_skill(name)` | 删除一个自定义 skill（只能删自定义、官方内置不可删） | 写（HIGH） |

**六要素描述**：每个工具 docstring 即 description，含**用途 / 区别（为何选它不选另一个）/ 参数语义 / 约束（副作用与边界）/ 示例**；入参用 pydantic 类型注解自动生成 schema；写工具在描述里声明副作用（「会真的建笔记」）。

**结果 spill**：`read_document`/`read_note`/`search_*` 返回超 `MAX_RESULT_CHARS`（默认 4000）时**落盘**返回占位符（preview + 路径），用 `read_tool_result` 查全文，避免截断丢信息。

### 5.1 六大契约（工具层加固，2026-09-22）

> 对照「把执行权交给概率」一讲，把「工具是什么性质」从自然语言猜测下沉为代码级强类型声明，并把工具生命周期六个环节各自加一道锁。**不做**多步写 Saga（跨工具长事务补偿回滚，属开放问题）。

| 契约 | 落地 | 代码位置 |
|---|---|---|
| ① 设计契约（原子化） | 拒绝瑞士军刀：`write_memory` 拆成 `update_profile`（写 soul/user 文件）+ `write_memory`（写四型记忆 DB），副作用通道在工具名层面就固定，不由模型传参决定写哪 | `tools.py` |
| ② 决策契约（描述工程） | 15 个工具 docstring 全六要素（用途/区别/参数/约束/示例），补上三个检索工具（search_knowledge_base/search_memory/search_web）的「区别」防选错 | `tools.py` |
| ③ 拦截契约（元数据） | `ToolMeta`（`side_effect_level` LOW/MEDIUM/HIGH + `source` + `estimated_latency_ms` + `enforced_idempotent`）→ `ToolRegistry` 单一真源；删掉 `WRITE_TOOL_NAMES`/`_RETRIEVAL_SOURCE`/`_TOOL_SOURCE` 散落字典，`_is_write`/`_tool_source`/`BehaviorTracker`/`build_review_node` 全由 registry 派生 | `toolmeta.py` |
| ④ 执行契约（无状态 + 幂等） | 工具闭包捕获请求作用域服务（无进程内会话状态）；Loop 给 `enforced_idempotent` 写工具注入**持久化稳定序号幂等键** `{run_id}:{seq}`（reactive 用 `AgentState.idempotency_seq` 单调递增、planner 用 `Plan.idempotency_seq` 随检查点落库，见 §4.16），命中 `IdempotencyRegistry` 直接返回缓存不重放副作用；`create_note` 另以 content hash 持久化去重兜底 | `toolmeta.py` / `runtime/reactive.py` / `service.py` |
| ⑤ 反馈契约（超时 + 红绿灯） | `ToolFailure`（`ToolOutcome` OK/TRANSIENT/PERMANENT + reason/hint/code）；工具 DomainError **抛 ToolFailure 而非返回错误串**；`with_timeout`（超时 = estimated_latency_ms × 3，超时抛黄灯）；`is_retryable` 只重试黄灯、`circuit_breaker` 只对可重试（基础设施）异常记失败；`ToolNode(handle_tool_errors=...)` 把红/黄灯格式化成给模型的文本 | `resilience/result.py` / `resilience/timeout.py` / `runtime/reactive.py` |
| ⑥ 资源契约（Token 经济） | 结果 spill（保留全文 + 占位符）不变；`list_knowledge_bases`/`list_notes` 增 `limit`/`offset` + `has_more` 提示行，不再静默截断 | `tools.py` |

> **上行契约（2026-09-23）**：`ToolMeta` 增 `output_contract`（`OutputContract(max_chars/required/optional)`），工具结果流向 synthesizer 前在 `plan_mode.run_tool` 过契约关（结构裁剪/形状校验/白名单剥离）——见 §4.13②。与拦截契约互补：拦截契约管「工具是什么性质」（安全指纹），上行契约管「工具交回什么形状」（交货标准）。

> 报告/汇总不单列工具（= Agent 最终结构化长文回答）；「导入文档进知识库」不在 Agent 工具集内。

### 5.2 工具规模治理（注意力稀释，2026-09-22）

> 对照「注意力稀释」一讲：候选集控制是**架构**问题、不是 Prompt 问题——把确定性交给工程、把概率性留给模型。铁律：单次推理模型可见工具数 ≤ 20（数学约束，超限则注意力稀释 + token 吞噬 + 位置衰减三重叠加、准确率断崖）。

- **构建期硬上限**：`toolmeta.MAX_VISIBLE_TOOLS = 20`，`build_tools` 返回前 `raise ValueError`（让「第 21 个工具」在启动/测试即炸，而非静默降智）。
- **分层埋点**：`ToolMeta.tier`（`l1` 常驻 / `l2` 角色注入 / `l3` 冷检索），当前全 `l1`、无人消费，为未来 L1/L2/L3 懒加载留挂载点。**暂不上**多维加权检索 / 角色路由——需工具涨到 ~20 或出现第二个角色域才值得，避免过度设计。
- **熔断候选集剔除**：`CircuitBreaker.available(names)` 返回未熔断工具子集；`build_reactive_graph` 在 `bind_tools` 前过滤、`service._run_plan` 在传 planner 前过滤——熔断工具从候选集**剔除**（模型/planner 根本看不到它），而非等模型调用后返回「暂时不可用」字符串空转 tool_call token。`with_circuit_breaker` 装饰器保留作 run 内中途故障的兜底。
- **六要素补齐**：`read_tool_result` 补全 docstring 六要素（用途/区别/参数/约束/示例）+「区别」互斥声明，与其余 10 个工具一致。

---

## 6. API 端点（`api/routes/copilot.py`）

### 6.1 `POST /api/copilot/chat`（SSE）

请求 `CopilotRequest{conversation_id?, question}`。

| 事件 | 载荷 | 说明 |
|---|---|---|
| `meta` | `{conversation_id, user_message_id, assistant_message_id}` | 首问自动建会话（`kind='copilot'`、`kb_id=NULL`） |
| `step` | `{tool_name, args}` | 每次工具调用 |
| `delta` | `{text}` | 最终回答逐 token |
| `approval` | `{run_id, tool, args, summary, level}` | 高危写需人工确认（证据包：人话摘要 + 风险等级 + 原始参数，见 §4.15） |
| `done` | `{assistant_message_id}` | 结束，**思维链经 checkpoint + 事件日志落库** |
| `error` | `{code, message}` | 失败 |

### 6.2 记忆 / 技能 / 审批端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/copilot/memory` | `{soul, user, memories:[...]}`（按 kind 分组，面板只读） |
| GET | `/api/copilot/skills` | 内置工具清单（15 工具 name/description/副作用） |
| GET | `/api/copilot/custom-skills` | 自定义 Skill 清单 `{items:[{name, description, content}]}`（L2 技能层，见 §2.6） |
| GET | `/api/copilot/approvals/pending` | 待审审批单列表（找回挂起审批；惰性失效已过期单） |
| POST | `/api/copilot/approve` | HITL 审批回执：`{run_id, decision, conversation_id, assistant_message_id}` 续跑 |
| POST | `/api/copilot/plan/resume` | planner 崩溃恢复：`{run_id, conversation_id, assistant_message_id}` 从计划检查点续跑未完成步骤 |
| POST | `/api/copilot/resume` | reactive 崩溃恢复：按 run_id 从 checkpoint 续跑（无待审批 interrupt 的宕机恢复，见 §4.16） |

### 6.3 会话

`GET /api/conversations` 的 `kind` 查询参数**可选**：缺省返回 qa+copilot 全部（首页共享历史混排）；`kind=qa` 只取问答、`kind=copilot` 只取 Copilot。`ConversationRead` 含 `kind` 字段供前端按图标区分。

---

## 7. 状态落盘（checkpoint + 事件日志，纯函数式可恢复）

> Agent 是纯函数——**给定状态即可恢复**。kima 按生产环境标准做三层：**LangGraph checkpoint**（状态快照 + 断点续跑）、**事件日志**（append-only 思维链，可审计可重放）、**幂等写**（重放不重复副作用）。

- **Checkpoint（状态恢复）**：接 LangGraph 官方 `langgraph-checkpoint-postgres` 的 `AsyncPostgresSaver`，graph 每个 superstep 自动落库（`checkpoints`/`checkpoint_writes` 表，同库）。`thread_id` = `run_id`（每 run 唯一；多轮上下文靠「注入会话历史」而非 checkpoint 续跑）。进程崩溃 / SSE 中断后，用同一 `thread_id` 重放 `graph.astream`，从最近 checkpoint 续跑，**不重执行已完成的工具调用**。这是 LangGraph 的生产级原语，天然实现「把 Agent 当纯函数、宕机只靠状态恢复」。
- **调用级快照（LLM 调用不重跑，2026-09-23）**：checkpoint 是**节点级**——宕机在节点内时，该节点会整体重跑（重发那次 LLM 调用 = 重付一次钱）。网关的 `copilot_llm_snapshots`（§4.11/§3 迁移 `0006`）把每次 LLM 调用当纯函数：以「node + 规范化输入」内容哈希为键，成功后落 output，恢复时命中缓存直接复用、**不重跑已完成的 LLM 调用**。与 checkpoint 互补：checkpoint 决定「从哪个节点续跑」，快照决定「节点内的调用是否真发」。`resume_after_crash(run_id)` 提供崩溃续跑入口（重放语义 at-least-once）。
- **计划检查点（planner 路径崩溃恢复，2026-09-23）**：planner 路径跑在 graph 外、无 LangGraph checkpointer，靠 `copilot_plans`（§4.12/§3 迁移 `0008`）在每次步骤状态迁移后落「版本化 DAG + 运行时状态」快照；`resume_plan(run_id)` 加载续跑、跳过已完成步骤（`POST /api/copilot/plan/resume`）。与上面两层互补：plan 快照决定「planner 路径从哪个步骤续」。
- **事件日志（思维链可观测）**：append-only `copilot_events` 表 `{seq, run_id, type, payload, created_at}`，`type ∈ {tool_call, tool_result, llm_delta, done, error}`，单调 `seq` 可重放。这是不可变的完整思维链，喂前端工具链 UI 与调试/审计。
- **幂等写**：Loop 给写工具注入**业务意图幂等键** `idempotency_key = "{run_id}:{tool_name}:sha256(key_fields)"`（内容派生，见 §4.17），工具执行层走持久化幂等表 `copilot_idempotency` 去重（原子抢占 processing→succeeded，命中缓存不重放副作用，同键不同参数拒绝）；`create_note` 另以 content hash 唯一索引兜底、`write_memory` 走冲突判定去重。崩溃重放不重复产生副作用。
- **`chat_messages`**：存 user/assistant 最终消息（会话历史）+ `steps`（工具轨迹，事件日志的轻量投影，供前端快读）；完整思维链以事件日志 + checkpoint 为准。

---

## 8. 可观测性（LangFuse）

> 运营侧可观测：trace 每次 agent 运行、每个 LLM 调用、每个工具调用，看成本/延迟/错误。与 §7 的 `copilot_events`（领域审计 + 崩溃重放）互补——这是「运营仪表盘」，不是「可重放状态」。接 LangFuse Cloud 免费档，可选、无 key 即关闭。

- 依赖：`langfuse` + `langfuse-langchain`（LangChain/LangGraph 原生 callback，`ChatDeepSeek` + react agent 自动被追踪，无需手写埋点）。
- `app/integrations/tracing.py`：`get_langfuse_handler(settings) -> CallbackHandler | None`——无 `langfuse_public_key`/`langfuse_secret_key` 时返回 `None`（默认关闭，不阻塞本地开发/测试）。
- 接入：`graph.astream(..., config={"callbacks": [handler], "metadata": {"run_id", "conversation_id", "question"}})`，自动产出三层 span：agent run（顶层）→ LLM 调用（model/token/延迟/成本）→ 工具调用（name/args/延迟/错误）。
- 配置：`langfuse_provider`（空/`cloud`）、`langfuse_public_key`/`langfuse_secret_key`/`langfuse_host`；不进 `0002` 迁移（LangFuse 存自己服务端）。
- **成本归因（不依赖 LangFuse，2026-09-23）**：`copilot_events` 的 `done` payload 自带 `intent`/`model`/`accounting`（cost/tokens/cache_hit_rate/turn/tool_call），无 key 也能事后按 run 查「这次任务烧了多少钱、缓存命中多少」。与 LangFuse 的实时 trace 互补——LangFuse 看过程，`done` 事件看单位成本结论。

---

## 9. Worker 偷懒改造

> 现状：`document_worker`/`note_vectorize_worker` 是 `while True: run_once(); sleep(poll_interval)` 空转轮询。改为事件驱动——处理完待处理项就 idle，直到「新增文档 / 新增或更新笔记」才醒。

- `app/core/wake_events.py`：`document_wake_event`/`note_wake_event = asyncio.Event()`（进程内单例）。
- **API 触发**：`documents.py` 的 `create_file`/`create_from_url` 成功后 `document_wake_event.set()`；`notes.py` 的 `create_note`/`update_note` 后 `note_wake_event.set()`。
- **worker 改造**：`run()` 里 `run_once()` 后 `await asyncio.wait_for(wake_event.wait(), timeout=IDLE_FALLBACK_SECONDS)`（默认 300s）——有唤醒立即处理；超时兜底醒来跑 `recover_stuck`（document）后 `clear()` 继续等。兜底只用于卡死恢复，不空转。
- `main.py`：worker 构造注入对应 wake_event（测试可注入可控 `asyncio.Event`）。

---

## 10. 前端

**入口与形态（对齐 ima）**：Copilot 有两个形态——① 首页「我的Copilot」入口（普通对话形态，主区模式切换）；② 由主对话右上角「小窗按钮」弹出的小窗（跨 Tab 常驻）。**无**右下角 Launcher、**无**独立 `/copilot` 页、**无** Sidebar Copilot Tab（Sidebar 仅 kima/知识库/笔记）。

```
src/components/copilot/
  CopilotProvider/       # Context：main/small 两实例 + 弹窗/回到主窗口/关闭的会话转移
  CopilotWindow/         # 小窗（useWindowRect + ResizeHandles），右上角 [齿轮(copilot设置)][新建会话][回到主窗口][关闭]
  CopilotChat/           # 消息 + 输入框（复用 ChatInput），流式步骤条 + 折叠工具调用链
  CopilotSteps/          # 流式期间「正在检索知识库 / 读取文档 / 联网搜索 / 读取记忆…」步骤条
  CopilotTrace/          # 历史消息的「工具调用 N」折叠链
  CopilotAvatar/         # 方形机械机器人脸头像（固定，不可改）
  CopilotSettingsModal/  # 设置弹窗：左导航（记忆管理 / Skills管理）+ 右内容
    components/
      MemoryManage/      # 记忆管理：形象卡片（头像/名字/编号，纯展示）+ 三个纯展示 Tab（copilot设定/用户档案/长期记忆）
      SkillsManage/      # Skills 管理：分「我安装的 skills」（自定义 MD）+「官方内置的 skills」（工具目录）两区
src/pages/Home/components/CopilotPane/   # 主区 Copilot 对话，右上角 [齿轮(copilot设置)][小窗按钮]
```

- **首页结构**：左面板顶部「问问kima」「我的Copilot」两个等宽入口（点击即新建对应模式的空白会话）+ 共享会话历史（qa+copilot 混排，按 `kind` 图标区分）；主区在「问答」/「Copilot」两模式间切换，点历史项按 `kind` 载入对应会话。
- **设置弹窗**：齿轮唤起，左导航两项（记忆管理 / Skills管理）。记忆管理 = 形象卡片（头像/名字/编号，固定不可编辑）+ 三个纯展示 Tab；Skills 管理 = 分「我安装的 skills」+「官方内置的 skills」两区。
- **状态模型**：Copilot 会话是**单实例**，同一时刻只在「主区」或「小窗」之一；`CopilotProvider` 承载 `{main, small, smallOpen}`。弹窗 = 转移 main→small 并让主区回空白；回到主窗口 = 转移 small→main（覆盖主区空白）并关小窗（不在首页则只关小窗、留历史）；关闭 = 只关小窗。
- **数据层**：`api/copilot.ts`（`streamCopilot` + `getCopilotMemory`/`getCopilotSkills`/`getCopilotCustomSkills`/`getCopilotConversation`）；`hooks/useCopilot.ts`（流状态 + steps + `selectConversation`/`hydrate`）；`api/types.ts` 增类型。
- **记忆面板只读**：三个 Tab 纯展示，不能手改（编辑靠对话）；Skills 管理同样只读。

---

## 11. 测试策略（不起真库/真网/真 LLM）

| 层 | 测试 | 方式 |
|---|---|---|
| Agent 循环 | `test_copilot_agent.py` | `FakeMessagesListChatModel` 脚本「先 tool_calls 后答案」，断言工具被调 + 事件顺序 + 事件日志落库 |
| 工具 | `test_copilot_tools.py` | Fake repos/services：search/read/create_note（幂等去重）/write_memory/search_memory；结果截断 |
| 记忆召回 | `test_copilot_memory.py` / `test_copilot_memory_recall.py` | 四型分型召回（constraint/preference 硬召回全量、fact/episodic 混合 top-k）、约束不过相似度阈值、分类器覆盖 kind、激活衰减过滤、`superseded` 跳过、记忆块 token 预算合并 |
| 记忆冲突 | `test_copilot_conflict.py` | 候选预筛 + LLM 判定（duplicate 同型去重不写 / contradiction 新的赢 / 同主题都留）、fact 同 entity 覆盖 version++、跨型冲突（情节压偏好、约束孤岛） |
| 记忆遗忘 | `test_copilot_forgetting.py` | activation 公式、容量硬淘汰、`recall` 回写 access_count、软删除窗口复活（superseded_by 守卫 + 激活门槛）、窗口过期硬清理 |
| 文件 | `test_memory_store.py` | Soul/User 文件读写、幂等初始化 |
| worker 偷懒 | `test_worker.py` 增补 | 事件唤醒、无工作 idle、超时兜底 recover_stuck |
| API | `test_api_copilot.py` | SSE 冒烟 + `GET /copilot/memory` + `kind` 过滤 |
| 意图路由 | `test_copilot_router.py` | 四类意图分派（task/qa/complaint/injection）+ 混合分类 |
| 预算/防循环 | `test_copilot_budget.py` / `test_copilot_loop_guard.py` | 四轴熔断、死循环指纹、幽灵循环 hash |
| 零信任评分/脱敏/写库闸 | `test_copilot_guardrail.py` / `test_worker.py` | 红线、三维度评分、`<data>` 隔离、误报回归语料、敏感脱敏、投毒文档拒绝 |
| HITL | `test_copilot_approval.py` / `test_approval_policy.py` | 写工具 interrupt → resume 重放同一调用；分级裁决（MEDIUM 自动放行 / HIGH 打断 + 证据载荷）、`ApprovalPolicy`/`resolve_approval_decision`、审批单超时 fail-close |
| 可靠性 | `test_copilot_resilience.py` | 错误分级重试、熔断 OPEN/HALF_OPEN、spill |
| planner | `test_copilot_planner.py` | DAG 生成、逐步执行、失败增量重规划（下游不孤儿化）、状态机（merge/环检测/序列化往返）、崩溃续跑跳过已完成 |
| 契约交互 | `test_copilot_contract.py` | 上行契约关（max_chars 截断/required 缺失/类型不符/白名单剥离/非 JSON 拒收）、planner 约束携带、synthesizer 约束注入、plan 输出自检（mismatch 诚实更正/ok 放行） |
| 评测闭环 | `backend/eval/agent/` | golden 数据集 + LLM-judge + 轨迹断言 + 回归 runner |

Fakes 增补：`FakeCopilotMemoryRepository`、脚本化 agent 模型、fake 计划模型（脚本化 DAG 输出）；复用 `FakeKnowledgeBaseRepository`/`FakeNoteRepository`/`FakeEmbeddingClient`。

---

## 12. 实现顺序（v2 分阶段）

> v1 的 T1–T12 已交付（见 v1 记录）；v2 在其上重构，分三阶段。评测从 P0 就位——「改进有根据」要求第一天就能度量。

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 地基 | 手写 `StateGraph`（reactive：agent ⇄ tools ⇄ review）替换 prebuilt 回环；review 进图（删 v1 的 graph 外 `while` 回环）；`AgentState` 结构化（可序列化：messages/steps/metrics/fingerprints/pending）；分层骨架（runtime/guardrail/resilience 目录）；轨迹评测 harness 骨架 | 现有 copilot 测试全绿 + 轨迹断言证明行为等价 |
| P1 运行时安全 | 四轴预算（真算 cost + `asyncio.wait_for`）+ 防循环（死循环指纹/幽灵 hash）+ 错误分级重试 + 意图路由（四类）+ 写工具 HITL + L1/L3 闸 + complaint workflow | 新增对应测试 |
| P2 长程能力 | 完整 LLM Planner（DAG + replan）+ L2/L4 闸 + 工具熔断 + 结果 spill | 评测集 + 手工验收 |

---

## 13. 已定决策

1. **核心定位**：Agent 工具干活 与 记忆越用越懂 **两者平衡**。
2. **编排**：手写 LangGraph `StateGraph`（reactive：agent ⇄ tools ⇄ review 进图）；不用 prebuilt 回环（v1 用，v2 重构）。
3. **LLM**：`langchain-deepseek` 的 `ChatDeepSeek`；复用 `settings.llm_model`。
4. **记忆双轨**：Soul/User = MD 文件（全文注入）；积累型记忆 = 向量条目，**四型分类**（约束/事实/偏好/情节）。
5. **分路召回**：constraint/preference 硬召回全量（不过相似度阈值）；fact/episodic 混合 top-k（dense 向量 + lexical 词法 RRF）。
6. **写入非追加**：LLM 冲突判定（duplicate 同型去重不写 / contradiction 新的赢 / 同主题都留）+ `superseded` 留痕；fact 同 entity 覆盖 version++；Soul/User 覆盖写文件。
7. **遗忘**：激活衰减（episodic TTL + 频率 + recency）+ 召回 floor + 软删除窗口复活（含激活门槛）+ 容量硬淘汰 + 窗口过期硬清理（lifespan 周期任务）。
8. **按需读记忆**：`search_memory` 工具，命中回写 access_count/last_access。
9. **工具工程**：副作用分层（8 只读 / 3 写）、六要素描述、结果截断、写工具幂等（业务意图幂等键 + 持久化幂等表 + content hash）。
10. **状态落盘**：LangGraph checkpoint（`AsyncPostgresSaver`）+ `copilot_events` 事件日志，配幂等写；「Agent 是纯函数，给定状态即可恢复」。
11. **Skill**：不做用户自定义 Skill；Skill = 内置工具，面板只展示。
12. **报告/汇总**：= Agent 最终结构化长文回答，不单列工具。
13. **联网**：要，复用博查 `WebSearchClient`。
14. **双形态**：首页「我的Copilot」普通对话 + 由主对话右上角「小窗按钮」弹出的跨 Tab 小窗（会话单实例、在两者间转移）。
15. **记忆面板只读**：四型分组查看（「长期记忆」tab 内硬约束/事实/偏好/情节分组），不能手改。
16. **会话**：复用 `chat_conversations`（`kind` 区分 qa/copilot），`chat_messages` 加 `steps`（工具轨迹投影）；完整思维链走 checkpoint + 事件日志。
17. **worker 偷懒**：document/note worker 改事件驱动（`asyncio.Event` 唤醒 + 长超时兜底 recover_stuck），并进本模块。
18. **默认值**：容量各 200、episodic ttl 30 天、recall floor 0.05、recency 窗 7 天、结果截断 4000 字、LLM 冲突候选 top-10——初值，实现后可调。
19. **可观测性**：接 LangFuse Cloud 免费档（`langfuse-langchain` callback，无 key 默认关），trace agent run / LLM / 工具调用，与 `copilot_events` 互补。

**v2 决策（生产级运行时，2026-09-21）**

20. **编排拓扑**：手写 `StateGraph`，理由 = prebuilt 固定回环装不下「意图路由 / 自检回环 / 注入闸」三类控制流；工具执行仍 `ToolNode`、模型仍 `ChatDeepSeek`、checkpoint 仍 `AsyncPostgresSaver`。
21. **三执行模式**：reactive（短任务）/ planner（LLM DAG + replan，长任务）/ workflow（确定性流程，complaint）。
22. **意图路由**：混合分类（规则/关键词第一刀 + LLM 兜底）四类 task/qa/complaint/injection；injection 在 L1 即拒（模型不见指令）。
23. **零信任评分**：给每份数据打 0-100 可信度分；红线（硬正则）一票毙，其余三维度（内容/来源/行为）加权平均（0.5/0.3/0.2，假设值）；五节点（输入/检索/上下文/工具调用/输出）重新评估、分数只减不增；按分五档处置（放行/观察/隔离 `<data>`/脱敏/阻断）；写库闸按分拦截、敏感值脱敏。
24. **HITL（分级审批）**：写工具 → `interrupt()` 挂起 → checkpoint → 前端确认 → resume 重放同一调用（不重问 LLM）；写工具判定来自 `ToolRegistry`（`has_side_effect`），非硬编码名单。**2026-09-23 从二元门禁升级为规则化分级**（`ApprovalPolicy`，Policy-as-Code）：`SideEffectLevel` → 三档 `ALLOW`/`NOTIFY`/`REQUIRE_APPROVAL`，`update_profile` 定为 `HIGH`（覆盖人设档案、同步审批）、`create_note`/`write_memory` 保持 `MEDIUM`（自动放行 + 事后审计）；配审批单第一类实体（`copilot_approvals`）+ 超时 fail-close + 证据包。见 §4.15。
25. **四轴预算 + 防循环**：turns/seconds/tokens(计费=总减 cache)/cost 真算，`asyncio.wait_for` 硬熔断；死循环（工具+参数指纹去 volatile 键）+ 幽灵循环（上下文 hash），窗口存 state（checkpoint 续跑不丢循环记忆）。
26. **可靠性**：错误分级（RED 永久/ YELLOW 瞬时/ GREEN）驱动重试；工具熔断 CLOSED→OPEN→HALF_OPEN；大工具结果 spill 落盘 + `read_tool_result`（不截断丢信息）。
27. **评测闭环**：golden 数据集 + LLM-judge + 轨迹断言 + 回归 runner（fake 模型确定性重放）；只覆盖核心执行，记忆/防注入/plan 靠事件日志观察。
28. **边界**：不做多 Agent 协作 / 分布式 backends 抽象 / MCP / event sourcing / 自动学习闭环（单 Agent 用不上）。
29. **工具规模治理**：`MAX_VISIBLE_TOOLS=20` 构建期硬校验（超限 raise，不静默截断）；`ToolMeta.tier` 分层埋点（l1/l2/l3，暂全 l1）；熔断工具从候选集剔除（`CircuitBreaker.available()` 在 bind_tools / 传 planner 前过滤，模型看不到坏工具而非返回字符串）；`read_tool_result` 六要素补齐。

**v2 决策（结构化输出 + 预算记账，2026-09-22）**

30. **结构化输出双层防线 + 自纠错**：三处 LLM→JSON 契约（planner DAG / review 判定 / conflict 数组）收口为「语法层 `json.loads` + 结构/语义层 Pydantic + 业务层 `validate_plan`」，任一失败抛 `StructuredParseError`；fail-closed（review 形状错判 `UNVERIFIED`，不再默认 `OK`）；自纠错 `max_attempts=3`（≤2 次），只回喂 `first_validation_error` 首条字段级摘要；核心字段严格（`verdict`/`action`/`id`）、非核心字段宽松（`issues`/`params`）；`strip_code_fence`/`StructuredParseError`/`first_validation_error` 收敛到 `integrations/llm.py`。
31. **LLM 用量记账**：~~走 `LLMClient` 的三处调用（planner/reviewer/conflict judge）不再记 0——planner 累计 `last_usage` 由 `service._run_plan` 记入 run tracker；reviewer/conflict judge 构造注入 `sink=DailyBudget` 直接回写日预算~~ **→ 已被决策 37 的 LLM 网关取代（2026-09-23）**：所有 LLM 记账统一收敛到 `LLMGateway`，planner 的 `last_usage` / reviewer 的 `sink` 手写记账全部删除。

**v2 决策（分路召回架构，2026-09-22）**

32. **约束第 4 型 `CONSTRAINT`**：用 `MemoryKind.CONSTRAINT` 表达硬规则/红线（而非布尔 flag）——kind 本身就是「硬召回」信号，`list_active(CONSTRAINT)` 复用现有全量注入原语；`kind` 列 `String(16)` 无 CHECK 约束，新增枚举值不改表。对齐《分路召回架构》的 CONSTRAINT/FACT/PREFERENCE/EPISODIC 四型。
33. **写入分类器（防线一）**：`LLMMemoryClassifier`（temperature=0）落库前确定性四型分类，覆盖 Agent 自报 kind/entity_id/trigger_conditions；失败回退 `None`（不覆盖，与现状一致，分类器是额外兜底不是唯一防线）。分类器**恒在场**（不可关闭、无开关）——约束类记忆不能靠 Agent 随手挑 kind。
34. **混合召回（防线二）**：fact/episodic = dense 向量 + lexical 词法（`tsv` + pg_jieba `plainto_tsquery`）RRF 融合（`_fuse_memories`，键=memory.id）；constraint/preference 硬召回全量（`list_active`，不过阈值、不经 activation floor）。
35. **记忆块 token 预算合并**：`format_memory_block(recalled, max_tokens)` 按 约束→偏好→事实→情节 串行填充，约束子预算 ≤40%（`memory_block_max_tokens` 默认 2000）；**召回审计**落 `recall` 事件（各通道命中条数）。`trigger_conditions` 仅落库埋点、暂不消费（domain 路由留待真实多领域需求）。

**v2 决策（遗忘第三动作，2026-09-22）**

36. **遗忘三动作补全**（对照《Agent 记忆的精神错乱》一讲）：①**召回写回**——`recall()` 命中条目回写 `access_count`/`last_access`（`repository.touch`），ACT-R 频率/近期增益不再只靠 `search_memory`；②**跨型冲突**——写入冲突候选从「同 kind」改为「跨 kind」（constraint 红线孤岛、事实/偏好/情节互为参照，fact 同 `entity_id` 覆写后清理矛盾的偏好/情节）；③**软删除窗口**——`superseded` 加 `superseded_at`/`superseded_by`，召回时 `search_recoverable` 窗口期内强命中复活（守卫：`superseded_by` 还活着不复活，防真冲突误复活）。

**v2 决策（LLM 网关，2026-09-23，全文见 §4.11）**

37. **统一 LLM 网关（D1/D6/D7）**：所有 LLM/embedding/rerank 出口收进 `LLMGateway` 唯一必经之路，调用点不再手写「预算预检 / 超时 / 重试 / 记账 / 解析兜底」。三套调用形态统一抽象（`complete` 一次性结构化 + `invoke_model` 流式工具调用 + `stream` 流式生成 + `embed`/`rerank`），重试/超时/熔断全收（`stream` 无重试、只超时）。范围含模块 5 的 embedding/rerank（`RagRetriever` + ingest/note worker）与模块 5 生成侧（`RagService` 改写/摘要/生成，2026-09-25）。
38. **80% 软提示 / 100% 硬停（D2）**：任一轴占用 ≥ 0.80 → 尾三明治注入「预算已用 80%、立即收尾」；100% → `BudgetExceeded` 硬停。软提示 volatile、不参与快照指纹。
39. **调用级快照（D3/D4/D8）**：以「node + 规范化输入」sha256 为键，成功后落 `copilot_llm_snapshots`（迁移 `0006`），恢复时命中缓存不重跑；重放 at-least-once（崩溃窗口可接受）；快照按 `run_id` 划界、`resume_after_crash(run_id)` 提供续跑入口；快照 TTL 清理（`delete_older_than` + lifespan 周期任务）。
40. **json_repair 替代自纠错（D5）**：语法层坏 JSON 用 `json_repair` 确定性修复，删除 review/planner/classifier/conflict 四处「喂回 LLM 自纠错」循环 → 单次调用 + fail-closed（省重复调用 = 省钱）。`BudgetExceeded` 语义：review 上抛（硬停）、classifier/judge/planner/embed fail-closed（工具内/独立模式/后台，优雅降级）。
41. **后端进 Docker（D9）**：后端进程进 Linux 容器（`docker/backend/Dockerfile` + compose `backend` 服务），消除 Windows 裸跑 ProactorEventLoop 下 psycopg async 不可用 → `AsyncPostgresSaver` 降级 `InMemorySaver` 的环境分裂，快照跨重启可恢复。

**v2 决策（遗忘收尾，2026-09-23）**

42. **duplicate 去重不写（机制二「旧胜新丢」）**：把冲突裁决从「落盘后」提到「落盘前」——`write_memory` 先 `search_cross_kind` + `judge`，`duplicate`（且同型）→ 不落盘、`touch` 旧记忆续命、返回旧条目；只有 `contradiction`/`none` 才写新。跨型「语义等价」不去重（情节/事实角色不同，各留一条）。抽 `_supersede_losers` 助手：事实覆写路径 `include_duplicate=True`（旧偏好/情节与新高阶事实冗余也退场），普通写路径 `False`（只处理 contradiction）。
43. **窗口过期硬清理（遗忘收尾）**：软删除窗口（7 天）过期的 superseded 记忆由 lifespan 的 `_run_superseded_cleanup` 周期任务硬删除（`repository.delete_superseded_older_than`）——真删、找不回，防表无限膨胀（HNSW/GIN 已部分索引 `WHERE NOT superseded`，但表行数仍需收敛）。
44. **复活加激活门槛 + 批量守卫**：`_revive_recoverable` 复活前先 `_above_floor`（衰减到地板下的 superseded 情节不复活，防僵尸 active 行）；守卫查压它的赢家改 `get_many` 批量查询（消除逐条 `get(superseded_by)` 的 N+1）。
45. **删死字段 `importance`**：`CopilotMemory.importance` 写入恒 0.5、全库无消费（未接入 activation），纯占位 → 删（迁移 `0007_drop_importance`）。`compute_activation` 明确为**无界排序键、不是概率**（故意不 clamp 到 [0,1]）——clamp 会让 fact 激活值全塌成 1.0、淘汰退化成只看 version。

**v2 决策（Plan-as-Data，2026-09-23，对照《蒙眼狂奔的 ReAct》）**

46. **planner 三道防线**：planner 路径补上 Plan-as-Data 三件事（§4.12）。① `PlanStep`/`Plan` 改为版本化状态机（`status/output_ref/error/version_created/replaces_step_id` + `get_parallel_ready`/`mark_downstream_obsolete`/`merge`），运行时状态从 executor 局部变量迁入 plan 数据；② 失败增量重规划——`mark_downstream_obsolete` + 带 `replaces`/完整 `depends_on` 的替换步骤 + 防御性 `merge`（依赖/环/撞 id 校验 + 被替换旧步骤强制 OBSOLETE），修复「失败步骤下游孤儿化 → 误报循环依赖」的核心 bug；③ 事件溯源 + 检查点——`plan_created/step_*` 事件落 `copilot_events` + `copilot_plans`（迁移 `0008`）快照，`resume_plan`/`POST /api/copilot/plan/resume` 崩溃续跑（跳过已完成步骤）。修订决策 #28 的「不做 event sourcing」边界为「仅 planner 路径做轻量事件溯源（复用既有事件日志 + 单张检查点表），非全量 CQRS」。`is_terminal` 复活为真消费：`_synthesize_plan_answer` 让 terminal 步骤产物优先。

**v2 决策（契约交互，2026-09-23，对照《09｜Agent 之间的信息传递》）**

47. **契约交互三补丁（§4.13）**：① **约束显式携带**——`_assemble_context` 上移到意图路由之后、三种执行模式之前，`system_prompt`+`memory_block` 随任务显式带到 planner/synthesizer/qa（修复「约束蒸发」）；② **上行契约**——`ToolMeta.output_contract`（`OutputContract(max_chars/required/optional)`）+ `apply_output_contract()`，工具结果过 synthesizer 前做结构裁剪/形状校验/白名单剥离（修复「8000 token 执行史」）；③ **planner 输出自检**——`_review_plan_answer` 先确定性副作用对账（写工具步骤回查 DB）再 LLM 判定，mismatch 诚实更正、unverified fail-closed。**明确不做**统一 Crossing/Port 抽象、message_id 全局去重、分布式服务端强制边界——单 Agent 用不上（对齐决策 #28 的边界判断）。

**v2 决策（结构化输出统一 schema，2026-09-23）**

48. **pydantic schema 进提示词（修订决策 #30）**：所有 LLM→JSON 输出点的**输入侧格式说明**统一改为「完整 pydantic 模型 + `Field(description=...)` → `model_json_schema()` → `format_instructions(model)` 拼进提示词」，**取代各点手写 JSON 示例**（消除「示例与解析模型两处漂移」）。输出侧解析统一收敛为 `parse_json(raw, model)`（`loads_json_repair` + `model_validate`），四个调用点（review/planner/classifier/conflict）共用、不再各写一遍。解析由「核心严格 + 附属宽松」双轨改为**全字段严格**——`review.issues` 从 `list[Any]` 收紧为 `list[_ReviewIssue]`（`claim`/`tool`/`evidence` 类型化），classifier/conflict 的手写 dict/长度校验也改成 pydantic（conflict 用 `RootModel[list[ConflictVerdict]]` 保持裸数组契约）；一处字段坏则整体 fail-closed。对「` OK `」「` DUPLICATE `」细微格式偏差仍保留 `strip().lower()` 归一化。

**v2 决策（Agent 权限系统，2026-09-23，对照《11｜RBAC 挡不住 Agent》）**

49. **四道防线落地（§4.14）**：防线① OBO 令牌交换**不做**（单用户无认证 / 无 token，对齐决策 #28 的边界判断）；防线② 参数级校验——`ParamContract` 下沉 `ToolMeta` + `tool_node` 执行前统一拦截（fail-closed 拒收）+ `HardBudget.max_tool_calls` 第五轴（单 run 工具调用次数上限，堵分页穷举绕过死循环检测）；防线③ DLP——`sensitive.py` 补中国场景 PII（身份证/银行卡/护照，数字边界断言）+ LLM 网关出站脱敏（`copilot_llm_dlp_redact`，快照指纹不受影响）；防线④ 运行时熔断——`SecurityBreaker`（手动恢复，无自动 HALF_OPEN）与基础设施 `CircuitBreaker`（自动恢复）分离，反复参数契约违规 → `SecurityBreakerTripped` 冻结 run。安全熔断默认关（`copilot_security_breaker_enabled=False`），参数契约 / 工具调用量上限 / 出站脱敏默认开。

**v2 决策（Agent 容错收尾，2026-09-23，对照《14｜Agent 挂了，如何不丢状态、不丢钱、不丢数据地拉起来？》）**

50. **幂等键 = 持久化稳定序号，不是运行时变量（§4.16）**：修复两处踩在文章点名的反例上——reactive 的 `{run_id}:{idx}`（`idx` 是消息内 `enumerate` 下标）跨轮碰撞、第二次写被第一次结果吞掉（真实丢数据）；planner 的 `{run_id}:{call_index}`（局部变量）崩溃 resume 归零重算、键变。改为 `AgentState.idempotency_seq`（reactive，单调递增随 checkpoint 落库）+ `Plan.idempotency_seq`（planner，随 `copilot_plans` 检查点落库）。跨崩溃 exactly-once 三条件：稳定幂等键 + 工具自身幂等（content hash / 冲突判定）+ Agent 侧指纹去重（`IdempotencyRegistry`，内存态）。
51. **动作指纹 + 崩溃现场外置（§4.16）**：`CircuitBreaker` 加 `store`（`copilot_breakers` 表 + `BreakerStore`，迁移 `0011`），失败计数跨崩溃不归零、`time.time()` 墙钟跨重启可比、`load_all()` 启动续读；`classify_error` 返回 transient/permanent，工具失败写 `last_error`（reactive `AgentState.last_error` / planner `Plan.last_error`）随 checkpoint 持久化——恢复后据此判断「别重试」。
52. **级联熔断（§4.16）**：`ToolMeta.resource`（db/web/file）+ `CircuitBreaker` resource 命名空间——共享依赖挂 → 同资源工具一起熔断、从候选集剔除。**不做**降级切备用模型（主模型限流 → 备用，本期后置）。
53. **reactive 崩溃恢复端点（§4.16）**：`POST /api/copilot/resume` 接 `resume_after_crash`（LangGraph 从 checkpoint 续跑），补上此前只写了方法没接路由的缺口。Docker 跑后端时 checkpoint 落 Postgres（`AsyncPostgresSaver`），Windows 裸跑会降级内存版（见决策 #41）。

**v2 决策（成本账本 + 缓存命中率监控，2026-09-23，对照《15｜Agent 的成本总是悄悄飙升》）**

54. **度量层补「单位任务成本 + 多维度归因」最小形态（§4.6/§8）**：`BudgetTracker.summary()` 导出 `RunAccounting`，done 事件落 `intent`/`model`/`accounting`——把「只能看 `DailyBudget` 聚合总账」补成「每个任务一张能拆到 token/cache/轮数的明细」。归因只落 `intent`（任务类型）+ `model` 两维（单用户无鉴权，用户维度恒一），不落 per-step 归因（reactive 工具轨迹已在 `steps` 投影里，planner 有 `step_*` 事件，交叉可查）。**明确不做**：难度评估/模型路由、预算驱动自动降级（模型/工具降级）、动态预算（里程碑换预算）——都依赖「多模型档 + 损失模型」，单模型 `deepseek-chat`、单用户体量下是过度设计（对齐决策 #28/#52「本期后置」）。单位成本异常检测（成本飙到历史均值 N 倍 → 死循环/提示注入信号）也后置，等 done 账本有真实数据量再上。
55. **防御层补「缓存命中率监控」（§4.6）**：`cache_hit_rate = cache_read / input` 随 done 落库，输入量 ≥2000 token 且命中率低于 `copilot_cache_hit_rate_warn`（默认 0.3）时 `logger.warning` 告警——命中率骤降是前缀缓存被破坏的信号（往 L0 塞动态时间戳一类）。命中率数据源自网关已有的 `extract_usage`（cache_read 明细），纯观察层补丁，无新增调用成本。

**v2 决策（网关协作者必填 + 快照/记账不可关，2026-09-25）**

56. **网关协作者全必填、快照/记账不可关（修订决策 #37/#39）**：`LLMGateway.__init__` 的 9 个协作者（`llm`/`daily_budget`/`breaker`/`retry`/`snapshots`/`embedder`/`reranker`/`pricing`/`cost_store`）从 `Optional` 全改为**必填无默认**——构造期即自洽，下游删掉全部 `is not None` 判断（原「缺哪个功能才运行时炸」收敛为「构造即 fail-fast」）。同时删两个 off 开关：`copilot_llm_snapshot_enabled`（快照恒落，`SqlAlchemySnapshotStore` 恒构造 + 清理 task 恒启动）与「全 fake 无真实提供方 → `pricing`/`cost_store` 为 `None`」分支（记账/审计恒启用）。fake/空厂商在网关内按 ¥0 记账、不落成本明细（`vendor not in ("", "fake")` 门控定价解析与 `copilot_llm_cost` 写入），真实厂商才走定价。`LLMUnavailableError`（原「未配置 XX 客户端」语义已随 Optional 删除而废弃）改名 `LLMCircuitOpenError`，语义只剩「熔断 OPEN」。唯一保留的运行期 `None` 是 `current().tracker`（worker/RAG 非 run 上下文），属运行期状态而非构造协作者。**2026-09-25 续：连这个运行期 `None` 也删除**——`run_budget(tracker, run_id)` 两参必填、`current()` 无 context 直接 `raise RuntimeError`（fail-fast），网关只能在 run 上下文里跑（worker/RAG 检索、历史摘要、记忆 embedding 都先 `run_budget`）。测试统一经 `tests/fakes.make_gateway()` 工厂装配全 fake 默认（`FakePricingService` 恒 ¥0）。

57. **预算模型定稿：无上限预算不允许存在（2026-09-25）**——`HardBudget.unlimited()`（大数 + `inf` 假无限）删除。预算分两类：**循环调用（agent）** 必带 `RuntimeConfig.budget`（`HardBudget` concrete 默认、不可 None，全轴 turns/seconds/tokens/cost 硬限，防失控 loop）；**非循环调用（worker/模块 5 检索）** 的 `BudgetTracker.budget` 传 `None`（无 per-run 预算，只记账 + 日预算 sink；token/cost 是「事后才知道」、且文档大小由大文档警告兜底，故不设 per-unit 硬限，时间由网关 per-call 超时 `asyncio.wait_for` 兜底）。`GatewayConfig.timeout` 从 `float | None` 改为 `float = 60.0`（秒轴硬熔断恒生效，不允许 None 关掉）。**How to apply**：安全类配置旋钮（熔断/超时/预算/快照/记账）必须给 concrete 默认值，`None` 不能当「关闭」语义用；「关闭」只能是显式开关。

58. **记忆四型改名（2026-09-26）**：`MemoryKind` 枚举 `PROCEDURAL→PREFERENCE`、`SEMANTIC→FACT`，对齐文章原生机制词汇 `constraint/fact/preference/episodic`。原「程序记忆」装「偏好」是术语错位（脑科学 procedural=「怎么做/技能」，不是「偏好」）；「语义记忆」实为「事实/状态」。主轴 = **机制驱动**（按怎么召回/遗忘分），不是脑科学认知词、不是 ima 产品词。`kind` 列存字符串值（`native_enum=False`），改名需 Alembic 数据迁移（`procedural→preference`、`semantic→fact`）。

59. **自定义 Skill（L2 技能层，2026-09-26）**：程序性知识（「怎么做」的经验技巧，对应 ima AGENT.md）独立成**自定义 Skill**，一个 skill = 一个 MD 文件（`data/skills/*.md`，frontmatter `name`/`description` + 正文）。**暂不做**模板/程序/沙箱。与内置工具清单（`/api/copilot/skills`）区分：工具是「能调的能力」、skill 是「沉淀的可复用经验」。记忆分三层：L0 身份（Soul/User）+ L1 积累（四型）+ L2 技能（Skill）。
