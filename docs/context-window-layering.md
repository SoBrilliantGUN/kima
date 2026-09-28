# 上下文窗口分层整改计划（L0–L5）

> 状态：**已完成（四阶段全部落地，2026-09-28）**。本文档是本次整改的唯一真相源（single source of truth）——
> 中途会话中断 / 执行故障后，按本文档从头重跑即可，不依赖对话上下文。
> 定案日期：2026-09-28。

---

## 0. 背景与目标

kima 现在的上下文装配是「L0 system prompt + L2 记忆块 + 历史」这套模糊分层，存在三个根本问题：

1. **分层编号错位**：`memory.py` 里写「L0 宪法 / L2 记忆块」、`context.py` 里写「L0 system / L3 工具结果」、记忆 `copilot-memory-taxonomy` 里写「L0 身份 / L1 积累 / L2 技能」——三套编号各说各话，语义对不上。
2. **压缩对象错**：`context.py` 的 `ContextManager` 把**所有 messages**（含 system、memory、history）一起压，而正确做法是**只压 history（L5）**，其余层是固定成本、不可压缩。
3. **缺层**：没有 L1 状态快照、没有 L3 skills 全文、没有宪法文件、reminder 用 SystemMessage 而非 user 角色带内标记。

**目标**：把上下文窗口整改为六块分层，每层独立预算，压缩只打 L5，SubAgent 跑 RAG 护前缀，宪法文件化。

---

## 1. 已定决策（定案，不再讨论）

| # | 决策 | 结论 |
|---|---|---|
| D1 | 范围 | 一步到位全落地（含 SubAgent RAG + skills 全文） |
| D2 | 分层主轴 | 按「来源 + 生命周期」六块分层（见 §2） |
| D3 | SubAgent RAG 形态 | **真正的嵌套 Agent，用 LangGraph 子图**（自主多步「检索→阅读→再检索→综合」，独立上下文窗口，只回结论） |
| D4 | 宪法文件位置 | `backend/data/constitution.md`（数据文件，可像 soul/user 一样编辑） |
| D5 | 宪法内容边界 | 只装「红线 + 身份 + 行为铁律」（现 `_BASE_INSTRUCTIONS` 核心 + `CONSTRAINT_REMINDER` 约束部分）；`_MEMORY_GUIDANCE`/`render_tool_hint` 派生的工具提示操作引导留 L0 常规、不进宪法 |
| D6 | soul/user 归属 | **只留 L0 常规**（不进宪法、不尾部重放）；reminder 只重放「红线 + 行为铁律」 |
| D7 | window 值 | **1,048,576（1M）**，对齐 DeepSeek V4 `deepseek-flash` 官方上下文窗口（见 §4） |
| D8 | skills | 官方 skills = Tools（走 `bind_tools` schema）；自定义 skills = `data/skills/*.md`，渐进式加载（L0 列表 + L3 全文） |
| D9 | 带内标记 | 所有注入段 user 角色，`[MEMORY]`/`[STATE]`/`[INVOKED SKILLS]`/`[REMINDER]`/`[HISTORY SUMMARY]`/`[TOPIC SUMMARY]`/`<spilled>` |

---

## 2. 目标架构：六层上下文窗口

### 2.1 六层定义

| 层 | 内容 | 角色 | 预算 | 超限处置 |
|---|---|---|---|---|
| **L0** | system prompt = 常规指令 + **宪法** + soul/user + **skills 列表** | system（独立于 messages） | 8% | 仅告警 |
| **L1** | `[STATE]` 状态快照（Turn / State / Failures / Last action） | user | 15% | 仅告警 |
| **L2** | `[MEMORY]` 记忆（含 constraint）+ **SubAgent 的 RAG 结论** | user | 35% | 裁剪 |
| **L3** | `[INVOKED SKILLS]` 已加载 skills 全文 | user | 独立 25k | 截断 |
| **L4** | `[REMINDER]` 宪法尾部重放（首尾三明治） | user | 无 | — |
| **L5** | 历史对话 + 工具日志 | user/assistant/tool | window − 其余 − margin | 五级压缩 |

### 2.2 基于 window=1048576 的预算绝对值

| 层 | 公式 | 阈值 |
|---|---|---|
| L0 | 1048576 × 8% | 83,886 |
| L1 | 1048576 × 15% | 157,286 |
| L2 | 1048576 × 35% | 367,002 |
| L3 | 固定 | 25,000 |
| L4 | 无 | — |
| L5 | window − L0−L1−L2−L3−L4 − margin(500) | ≈ 414,902 |

### 2.3 消息顺序与带内标记

- **L0 system prompt 单独**，不进 messages（保 Prompt Cache 前缀稳定）。
- messages 内顺序固定：**history → memory → skills → state → reminder**。
- 所有注入段（memory/skills/state/reminder）都是 **user 角色**，带内标记前缀。
- 带内标记双作用：① 给 LLM 分界；② 给代码当锚点——消息落盘/跨轮次恢复后结构化引用丢失，代码靠 `startswith("[HISTORY SUMMARY]")` 从纯文本回捞特定条目。

---

## 3. 核心机制

### 3.1 宪法文件化

- 新建 `backend/data/constitution.md`，内容 = 红线 + 身份 + 行为铁律。
- 同一份内容：拼进 L0 system prompt 头部 + 尾部 L4 `[REMINDER]` 重放（首尾三明治，对抗 Lost in the Middle）。
- 现有 `memory.py` 内容归属：
  - `_BASE_INSTRUCTIONS` 核心 + `CONSTRAINT_REMINDER` 约束部分 → 进宪法。
  - `_MEMORY_GUIDANCE` / `render_tool_hint` 派生的工具提示 → 留 L0 常规，不进宪法、不进 reminder。

### 3.2 带内标记

- 新增 `[STATE]`、`[INVOKED SKILLS]`、`[REMINDER]`；保留 `[MEMORY]`、`[HISTORY SUMMARY]`、`[TOPIC SUMMARY]`、`<spilled>`。

### 3.3 压缩只打 L5

重写 `app/agent/runtime/context.py` 的 `ContextManager`：

```
ratio        = (L0+L1+L2+L3+L4+L5 实际占用) / window
history_budget = window − L0 − L1 − L2 − L3 − L4 − safety_margin(500)
```

五级压缩**只作用于 `run.messages`（L5）**：

| ratio | 级别 | L5 表现 |
|---|---|---|
| < 0.25 | NONE | 只 fit_budget 丢最老（保持 tool 配对） |
| 0.25–0.70 | TOOL_COMPRESS | 工具结果内联压缩（规则式，无 LLM） |
| 0.70–0.85 | HISTORY_SUMMARY | 保留最近 6 条，更早 LLM 摘要成 `[HISTORY SUMMARY]` |
| 0.85–0.92 | TOPIC_SUMMARY | 保留最近 4 条，更早 LLM 摘要成 `[TOPIC SUMMARY]` |
| ≥ 0.92 | EMERGENCY | 只留最后 2 条 + 最近一条摘要 |

### 3.4 SubAgent RAG（LangGraph 子图）

- 新增 `spawn_rag` 工具：主 Agent 传 **task + 引用**（不传内容）。
- 子 Agent = LangGraph 子图，**独立上下文窗口**，工具集只含只读检索（search_knowledge_base / read_document / read_note），自主多步「检索→阅读→再检索→综合」。
- 子图结果过契约白名单 + `handoff_output_max_chars` 截断，只把**结论 + 记账标量（turns/cost/tokens）**回主 Agent 的 L2。
- 复用现有 `LLMGateway`（子图的 LLM 调用也走网关记账/快照）+ 现有四轴 `HardBudget`（子图独立 budget）。

### 3.5 skills 渐进式加载

- L0 注入自定义 skills 的 **name + description 列表**（来自 `FileSkillStore.list_skills()`）。
- 新增 `get_skill(name)` 工具 → 命中后全文进 `_invoked` 缓存 → 拼成 L3 `[INVOKED SKILLS]` 块，**跨轮次持久到 run 结束**。
- 官方 skills = Tools，仍走 `bind_tools` schema，不进 L3。

---

## 4. window 值

- DeepSeek V4（`deepseek-flash`）官方上下文窗口：**1M tokens（1,048,576）**，较上一代 128K 大幅提升。
- 本次定 **window = 1,048,576**，配置项 `copilot_context_max_tokens` 由 32000 提到 1048576。
- ratio 保持 8%/15%/35%、L3 固定 25k、margin 500 不变。

---

## 5. 实施步骤（四阶段，每阶段测试全绿再进下一步）

### 阶段 1 — 分层骨架 + 宪法 + 预算 + 压缩只打 L5（不含 SubAgent/skills）

1. 新建 `backend/data/constitution.md`。
2. 重写 `app/agent/runtime/context.py` 为六层装配器：
   - `ContextConfig` 增各层 ratio（`l0_ratio/l1_ratio/l2_ratio/l3_ratio`、`skills_budget`、`safety_margin`）。
   - `ContextManager.prepare(...)`：六层装配（L0 system / L1 state / L2 memory / L3 skills / L4 reminder / L5 history）。
   - `_compress_history`：`ratio = 所有层之和 / window`，只压 `run.messages`（L5），`history_budget = window − 其余 − margin`。
   - 带内标记统一。
3. `app/agent/memory.py` 拆分：
   - 新增 `load_constitution()`（读 `data/constitution.md`）。
   - `assemble_system_prompt` 改六段：常规指令 + 宪法 + soul/user + skills 列表。
   - 保留 `format_memory_block`（L2 `[MEMORY]` 块）。
4. `app/agent/runtime/state.py`：新增 `format_state(run)`（Turn/State/Failures/Last action），来源字段映射现有 `AgentState`。
5. `app/agent/service.py`：`_assemble_context` 改造成六层装配入口；`_build_input_messages` 的历史摘要逻辑并入 L5 压缩。
6. `app/agent/runtime/reactive.py`：reminder 从 `SystemMessage` 改 user 角色 `[REMINDER]`；compress 节点接新 `ContextManager`。
7. `app/core/config.py`：新增各层 ratio 配置 + `copilot_context_max_tokens` 改 1048576。
8. 迁移 `tests/test_copilot_context.py`、`tests/test_copilot_compression.py`。

**验收**：六层装配正确、压缩只动 L5、ratio 分母含所有层；阶段 1 相关测试全绿。

### 阶段 2 — skills 渐进式加载

1. `app/agent/tools.py`：新增 `get_skill(name)` 工具（复用 `FileSkillStore.read_skill`）。
2. 装配器新增 `[INVOKED SKILLS]` 块 + `_invoked` 缓存（跨轮次持久）。
3. L0 注入 skills 列表（name + description）。
4. 迁移 `tests/test_copilot_skill.py`。

**验收**：L0 有列表、get_skill 加载后全文进 L3 并跨轮次保持。

### 阶段 3 — SubAgent RAG

1. 新建 RAG 子图（独立窗口 + 只读检索工具集 + 独立 budget）。
2. `app/agent/tools.py`：新增 `spawn_rag` 工具（子图执行 + 契约截断回结论）。
3. L2 注入 SubAgent RAG 结论。
4. 新增 `tests/test_copilot_subagent.py`。

**验收**：主 Agent 调 spawn_rag → 子图独立跑 → 只回结论进 L2，主 Agent 前缀不被检索结果污染。

### 阶段 4 — 配置收敛 + 文档 + 全量回归

1. 各层 ratio 配置进 `config.py` / `.env.example`。
2. `docs/module-6-copilot.md` 同步本次分层改动。
3. `ruff app/ tests/` + `mypy app/ tests/` + 全量 pytest 全绿。

**验收**：全量测试 + lint + type 全绿。

---

## 6. 涉及文件清单

**新建**：
- `backend/data/constitution.md`
- `backend/app/agent/runtime/rag_subagent.py`（RAG 子图）
- `backend/tests/test_copilot_subagent.py`

**重写**：
- `backend/app/agent/runtime/context.py`（六层装配器 + 压缩只打 L5）

**修改**：
- `backend/app/agent/memory.py`（宪法加载 + L0 组装 + L2 记忆块）
- `backend/app/agent/runtime/state.py`（L1 快照 `format_state`）
- `backend/app/agent/service.py`（六层装配入口 + 历史并入 L5）
- `backend/app/agent/runtime/reactive.py`（reminder 改 user 角色 + compress 接新装配）
- `backend/app/agent/tools.py`（`get_skill` + `spawn_rag`）
- `backend/app/core/config.py` + `.env.example`（各层 ratio + window 值）
- `backend/tests/test_copilot_context.py` / `test_copilot_compression.py` / `test_copilot_skill.py`

---

## 7. 风险与注意

1. **window 提到 1048576 是配置改动，非模型升级**——DeepSeek V4 官方 1M，但上线前仍应核对真实 API 的窗口上限。
2. **SubAgent 子图必须复用 LLMGateway**——否则子图 LLM 调用旁路网关、漏记账/漏快照（违反网关铁律）。
3. **reminder 从 SystemMessage 改 user 角色**——影响 Prompt Cache 前缀？reminder 在尾部、不进 state/checkpoint、每轮复制注入，改 user 角色后仍需保证「不进永不压缩的 L0」。
4. **`_build_input_messages` 历史摘要与 L5 压缩合并**——现有 `assemble_history`（service 层）与新 L5 五级压缩（context 层）职责重叠，阶段 1 需明确合并归属，避免两套历史压缩并存。
5. **带内标记的 `startswith` 脆弱性**——回捞依赖纯文本前缀，摘要文本若恰好以 `[HISTORY SUMMARY]` 开头会误判；沿用 prodagent 约定即可，暂不引入结构化消息类型。

