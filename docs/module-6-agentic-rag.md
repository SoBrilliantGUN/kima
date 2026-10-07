# QA 升级 Agentic RAG：一套实现 + 对等子 Agent

> 状态：已实现。本文记录决策、主/子 Agent 差异与实现要点。
>
> **演进注记（2026-10）**：本方案落地后，Copilot 进一步重构为「多 Agent 运行时」——入口 LLM 意图判断判 plan/qa/task（plan 走 planner 图、按步数放大预算，其余走 reactive），`spawn_rag` 之外新增 `spawn_reactive`（reactive + 全工具）与 `spawn_plan`（plan + 全工具，含 for）。本文的「对等子 Agent」设计（工具白名单 / 共享成本 / 独立 turn）被三个子 agent 全部复用，`RagSubagent` 加 `system_prompt` 参数后同时支撑 `spawn_rag` 与 `spawn_reactive`；「防递归」改为「允许递归派子 agent，靠 max_spawn 计数 + 四轴预算兜底」（rag 子 agent 仍只读）。详见 `docs/module-6-copilot.md` §4.3。

## 1. 背景与目标

原 `qa_mode.py` 是 **naive RAG**：一次 `rag_retriever.retrieve` → 拼 context → 一次
`qa.generate`，**无工具循环、无 review**。Agent 不能自主决定「检索几次、换什么关键词、
精读哪篇文档」。

本次把 QA 意图改为复用 **reactive 主循环**（Agentic RAG），并顺手消除
`build_reactive_graph` 与 `build_rag_subgraph` 两套 `agent ⇄ tools` 循环的重复——
用「一套实现 + `subagent` 标记」统一，子 Agent 与主 Agent 是**对等完整版**。

采用「上下文强隔离 + 工具白名单 + 父显式下传」的子 Agent 设计，而非
另写一套轻量子循环。

## 2. 核心决策

| # | 决策 | 结论 |
|---|---|---|
| 1 | QA 升级方式 | 走 reactive 主循环（Agentic RAG），不再是 naive RAG 直答 |
| 2 | QA 工具集 | 检索五件套 + `spawn_rag`（`tools.QA_TOOL_NAMES`） |
| 3 | 实现形态 | 一套实现（复用 `build_reactive_graph`），不抽两套循环 |
| 4 | 子 Agent 定位 | 对等完整版（含 review / 约束 / 安全闸） |
| 5 | 约束传递 | 父显式下传（强隔离，唯一通道是派发参数） |
| 6 | 子 Agent 工具 | 检索五件套（只读定位，不含 `spawn_rag`） |
| 7 | 子 Agent 预算 | 共享成本 + 独立 turn |
| 8 | 子 Agent 输出 | 内部 review 回环，预算耗尽给「还行结果」或报错，最终只回结论 |
| 9 | QA 预算 | 与 TASK 一致（默认 `HardBudget`） |
| 10 | 下传内容 | 精简（红线/宪法铁律 + constraint 型硬约束） |

## 3. 主 / 子 Agent 差异（全部落在「参数」层）

| 维度 | 主 Agent | 子 Agent（spawn_rag 派发） |
|---|---|---|
| 图结构 | `build_reactive_graph` | **同一个** `build_reactive_graph` |
| 工具集 | 全量 / QA 五件套+spawn_rag | 检索五件套（`_RAG_SUBAGENT_TOOL_NAMES`） |
| `count_turn` | `True`（计入主循环 turn） | `False`（独立 `max_turns=4`） |
| 初始 state | `history + system_prompt + memory_block` | `[task] + system_prompt(角色+精简约束) + 空 memory_block` |
| 输出 | 最终回答（过 `_finalize_answer`） | 截断结论文本（只回结论） |
| review | 有 | 有（复用同一 `OutputReviewer`） |

关键结论：**「对等完整版」让 `build_rag_subgraph` 被直接删除**——子 Agent 和主 Agent 图结构
完全一致，差异只是调用参数（工具集 + 初始 state + `count_turn` + 输出截断）。

## 4. 实现要点

### 4.1 统一循环

`RagSubagent.run` 在 run 时动态构建 `build_reactive_graph`，复用主循环的 tracker/run_id：

- **共享成本**：从 ContextVar `current()` 取主循环 tracker，子 Agent 的 LLM 调用记入同一账本。
- **独立 turn**：`count_turn=False`（`gateway.invoke_model` 透传），不挤占主循环 `max_turns`，
  由子 Agent 自己的 `recursion_limit = max_turns * 2 + 2` 兜底。
- 图不能跨 run 缓存（tracker/run_id 每 run 不同），`spawn_rag` 低频调用，动态构建开销可忽略。

### 4.2 工具白名单

`build_reactive_graph` 新增 `tool_names: Collection[str] | None`，`None` = 全量；传入时同时
过滤 `tools` 与 `registry`，使 `needs_approval` / `write_tool_names` / `bindable_tools` 都基于
过滤后的子集。

```python
# tools.py
_RAG_SUBAGENT_TOOL_NAMES = frozenset(
    {
        "search_knowledge_base",
        "read_document",
        "read_note",
        "search_web",
        "search_memory",
    }
)
QA_TOOL_NAMES = _RAG_SUBAGENT_TOOL_NAMES | {"spawn_rag"}
```

### 4.3 约束显式下传（强隔离）

子 Agent **不自动继承**父上下文的 soul/user 人设或完整记忆块，唯一通道是派发参数：

```python
# memory.py
def format_subagent_constraints(recalled: RecalledMemories) -> str:
    """子 Agent 精简约束块：宪法铁律（红线）+ constraint 型硬约束。"""
    parts = [f"[约束]\n{load_constitution()}"]
    if recalled.constraint:
        lines = ["- " + m.content for m in recalled.constraint]
        parts.append("硬约束（必须遵守）：\n" + "\n".join(lines))
    return "\n\n".join(parts)
```

传递链：`_assemble_context` 每轮写 `self._constraints["constraints"]` →
`build_tools(constraint_holder=...)` → `spawn_rag` 读 holder 拼进子 Agent system prompt。

不传 preference / fact / episodic（检索子任务只需底线）。

### 4.4 预算：共享成本 + 独立 turn

- token / cost 汇入主循环同一个 `BudgetTracker`（一张账本，done 事件归因完整）。
- 子 Agent 的 turn 单独计数（`count_turn=False`），修掉原「子 Agent 挤占主循环 `max_turns`」的缺陷。

### 4.5 只读子 Agent 不递归

rag 子 Agent 工具集 `_RAG_SUBAGENT_TOOL_NAMES`（只读检索五件套）不含 `spawn_rag`，天然不能
派子 Agent——这是**只读定位**，非全局防递归。reactive / plan 子 Agent 可递归派子 Agent，
靠 `max_spawn` 计数 + 四轴预算兜底。

## 5. 改动清单

| 文件 | 改动 |
|---|---|
| `runtime/reactive.py` | 加 `count_turn` / `tool_names` 参数，`agent_node` 透传 |
| `runtime/rag_subagent.py` | 删 `build_rag_subgraph`，`RagSubagent` 复用主循环图 + `run(task, constraints)` |
| `gateway.py` | `invoke_model` 加 `count_turn` 参数（默认 True） |
| `tools.py` | 加 `QA_TOOL_NAMES`，`build_tools` 加 reviewer/runtime/verifier/security_breaker/review_max_attempts/constraint_holder，`spawn_rag` 下传约束 |
| `compose.py` | `graph_builder` 绑 `build_reactive_graph`（`tool_names` 经 `graph_builder` 透传） |
| `run.py` | QA 分支并入主循环（`tool_names=QA_TOOL_NAMES`），删 `_run_qa` 调用；`compose.py` 建 `rt.constraints` holder |
| `memory.py` | 加 `format_subagent_constraints` |
| `qa_mode.py` | **删除**（QA 不再直答） |

## 6. 测试

- `tests/test_copilot_subagent.py`：子 Agent 复用主循环图后仍「多轮检索 + 只回结论 + 截断 + 注入 system prompt」。
- `tests/test_copilot_context.py`：`format_subagent_constraints` 只含红线 + 硬约束，不含偏好/事实/情节。
- `tests/test_copilot_router.py`：QA 走主循环（有 review 事件）；`QA_TOOL_NAMES` 含 `spawn_rag` 而
  `_RAG_SUBAGENT_TOOL_NAMES` 不含（只读定位）；QA 只读无写工具。
