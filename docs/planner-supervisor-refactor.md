# Planner 执行引擎设计（supervisor-worker + ChainOptimizer 审查层）

> 状态：**已实现**。
> 上游基线：`docs/module-6-copilot.md`（§4.14 planner / §4.13 契约交互）。

---

## 0. 设计目标

planner 是一个 **LangGraph supervisor-worker 执行引擎**，具备五项能力：

| # | 维度 | 设计 |
|---|---|---|
| 1 | 并行 | `get_parallel_ready()` 真正扇出（无依赖步骤并行执行） |
| 2 | HITL | interrupt 暂停、拒绝 → replan 降级 |
| 3 | 审查 | 关键路径 + 四维报告 + 分级熔断 |
| 4 | 缩短链路 | 最长串行链 ≤4、强制并行 |
| 5 | 宿主 | LangGraph supervisor-worker（静态图 + 动态 Plan 状态） |

**设计目标**：并行扇出、HITL 暂停审批、计划审查（关键路径 / 分级熔断 / 缩短链路），并**复用 reactive 的防线纯函数**（不重复实现）。

---

## 1. 设计决策

| 决策 | 结论 |
|---|---|
| 范围 | 执行引擎（并行 + HITL）+ 审查层（ChainOptimizer） |
| 执行宿主 | LangGraph supervisor-worker（静态图 + 动态 `Plan` 状态） |
| 复用方式 | worker 与 reactive 共用防线纯函数，不套 messages 语义 |
| 审查层深度 | 关键路径体检 + 四维报告 + 分级熔断三档（Safe/Warn/Emergency）+ 设计层 Prompt 约束 |
| 状态序列化 | `plan` 用 **dict**（节点边界 `from_dict`/`to_dict` 转换）；`trace` 用 `list[dict]`；`final_answer` 用 `str` |
| 轨迹统一 | 执行时收集统一 trace（`{"tool","args","result","ok"}`），review 读 `trace` + `final_answer` |
| HITL 语义 | interrupt 暂停 → approve 继续 / deny → 步骤 FAILED → replan 换降级步骤 |
| 并发 | 按「多 worker 真并发安全」写（状态隔离、无共享可变、`depends_on` 保证拓扑不打架） |
| 崩溃恢复 | 统一走 LangGraph checkpointer，`copilot_events` 事件溯源 |
| 并行审批 | 支持一个 run 多张并行 pending 审批单，前端错开叠放 |

---

## 2. 目标架构：静态图 + 动态 Plan 状态

一张**静态图**，动态性全部在 `Plan` 状态数据里：

```
START → plan_node → supervisor_node ⇄ worker_node（Send 扇出） → finalize_node → review_node → END
```

- **`plan_node`**：`planner.generate` 生成 Plan + ChainOptimizer 审查 → 写 Plan 进状态。生成前先做**检索式 skill 召回**（embedding 相似度预选 top-K，`orchestrate.recall_relevant_skills`）→ 把选中 skill 全文经 `generate(skills=...)` 显式携带给规划器、并写进 `skills_block` 供合成步骤注入（reactive 走懒加载 `get_skill`→L3，plan 无 agentic 循环故用检索式预选替代）。
- **`supervisor_node`**：确定性路由。读 `Plan.get_parallel_ready()`，返回 `[Send("worker", {step}) …]` 扇出；全部完成则 goto finalize；检测 FAILED 则内部 `await planner.replan` + `plan.merge` 后继续。
- **`worker_node`**：执行单个 step，复用 reactive 防线纯函数 + `interrupt()` 审批。
- **`finalize_node`**：只挑 terminal 合成步骤（`finalize_answer`）的产物写进 `final_answer`——不再汇总所有 `results`、不再调 LLM（合成已在 worker 内完成）。
- **`review_node`**：复用 `build_review_node`，读 `trace` + `final_answer`。

**replan 与 interrupt 的隔离**：图结构全程不变，replan 只改 `Plan` 数据、interrupt 只冻结在 worker 节点内部，两者正交。

---

## 3. 状态设计（`runtime/planner_state.py`，新建）

```python
class PlannerState(TypedDict):
    plan: dict  # Plan.to_dict()，节点边界 from_dict/to_dict 转换
    task: str
    system_prompt: str  # L0（合成步骤 / review 共用）
    memory_block: str  # L2 召回约束
    skills_block: str  # 召回的相关 skill 全文块（plan_node 写入、合成步骤共用）
    results: dict  # step_id → output_ref
    trace: list[dict]  # 统一执行轨迹 {"tool","args","result","ok"}
    final_answer: str  # finalize 挑 terminal 合成产物，review 对账用
    verdict: str  # 审查 verdict（ok / warn / critical）
    last_error: dict | None  # 崩溃现场，与 AgentState 同字段语义
```

**为什么 `plan` 用 dict 而非裸 `Plan` 对象**：

1. **可变对象破坏 checkpoint 值语义（致命）**：`Plan` 是可变对象，`mark_downstream_obsolete` / `merge` / 改 `step.status` 全是原地 mutate。LangGraph 的 state 更新模型是「节点**返回** partial dict → merge → 整个 state 序列化进 checkpoint」，它靠「返回的新值」感知变化；裸可变对象原地改、不返回新值，checkpoint 无法正确 diff，且序列化器若按引用落盘，历史 checkpoint 会被后续 mutate 污染——直接打穿事件溯源与检查点这道防线。
2. **序列化兼容**：checkpointer 默认 JSON 系 serializer 不识别自定义 dataclass，需额外注册；dict 开箱即用。
3. **与现有序列化同源**：`plan_store.to_dict/from_dict` 已存在，事件溯源共用。

代价：每次节点边界一次 from/to 转换，plan 规模（几十步）开销可忽略。

---

## 4. 节点设计（复用映射）

| 新节点 | 复用来源 | 复用方式 |
|---|---|---|
| `worker_node` | reactive `tool_node` 防线 | **抽纯函数**：`validate_param_contract` / `inject_idempotency_keys` / `evaluate_tool_results`(sanitize+output_contract) / `_resolve_approvals`(interrupt) |
| `review_node` | `build_review_node` | **整节点复用**，输入为 `trace` + `final_answer` |
| `plan_node` / `supervisor` 的 LLM 出口 | `agent_node` 装配层 | 复用 `run_budget` + `gateway.invoke_model`（已在用） |
| 审查层 | 新增 `runtime/chain_optimizer.py` | 新写，无复用 |

**合成步骤 + 入口**：
- **合成步骤**：planner 生成的最后一步是 terminal 的 `finalize_answer` 步骤（`depends_on` 所有前置步骤、`params` 留空），`worker_node` 识别后调 LLM 把前置产物整理成最终回答、写入 `results[step_id]`；`finalize_node` 只挑它的产物当 `final_answer`。收益：plan 不再汇总所有 `results` → **results 累积不膨胀 → 无需给 plan 模式加分层压缩**。
- **执行入口（`runtime/entry.py`）**：两张图（reactive / planner）独立演进，`entry.run_plan` 供 `PlanSubagent`（plan 子 agent）复用；主 agent 默认 `run_reactive`（不再按意图选入口），复杂任务经 `spawn_plan` 子 agent 表达。

**轨迹统一**：
- state 新增 `trace: list[dict]`，统一结构 `{"tool","args","result","ok"}`。
- reactive `tool_node` 在 `evaluate_tool_results` 之后追加 trace；planner `worker_node` 追加**同一种结构**。
- `review_node` 读 `trace` + `final_answer`。
- 收益：① 两种模式零适配；② 审查、执行、监控统一在同一处——trace 未来喂 ChainOptimizer 反馈层 / Trace 埋点。

---

## 5. 审查层（`runtime/chain_optimizer.py`，新建）

```python
analyse_plan(plan) -> PlanReport   # 关键路径长度（DAG 最长路径，缓存递归）+ 四维分
# verdict: critical(<50) / warn(50-75) / ok(>75)，结构分低于底线直接拒
```

- **plan_node 内跑审查**：`verdict=critical` → 有界打回重生成（复用 generate 失败回退）或降级 reactive。
- **分级熔断执行（三档本轮全实现）**：
  - `ok` → **Safe**：正常执行，标准审计。
  - `warn` → **Warn**：强制加检查点 + 关键写步骤人工确认。
  - `emergency`（用户强制）→ **Emergency**：每步挂审计钩子、全链路人工确认、免责降级（能追溯能回滚）。
- **设计层 Prompt 约束**：改 `planner._SYSTEM`，写入以下五条约束（最长串行链 ≤4、无依赖只读 `depends_on=[]`、写前校验写后补偿、幂等键、不可逆前 `human_approval`）。这是「缩短链路」最便宜的一招，先行。

---

## 6. 代码结构

- `runtime/planner_state.py` — 状态 schema
- `runtime/plan_graph.py` — 图装配（五节点）
- `runtime/chain_optimizer.py` — 审查层
- `runtime/planner.py` — LLMPlanner（DAG 生成 + replan）
- `runtime/plan_model.py` — Plan-as-Data 数据模型
- `runtime/entry.py` — 执行入口（run_plan 供 PlanSubagent 复用 / run_reactive 主循环）

防线纯函数（`validate_param_contract` / `resolve_approvals` / `inject_idempotency_keys` / `evaluate_tool_results`）由 worker 与 reactive 共用。

---

## 7. 完成定义（Definition of Done）

1. planner 能**并行**执行无依赖步骤（`get_parallel_ready` 被真正利用）。
2. `REQUIRE_APPROVAL` 工具能**暂停审批**，拒绝后触发 replan 换降级步骤。
3. 计划执行前有**关键路径报告 + verdict**，超长串行链被 Prompt 约束压短；分级熔断 Safe/Warn/Emergency 三档可用。
4. reactive 与 planner 共用同一批防线纯函数。
5. 崩溃恢复统一走 LangGraph checkpointer，事件溯源保留。
6. `backend` `ruff` + `mypy(strict)` + `pytest` 全绿；测试不起真库/真网/真 LLM。

---

## 8. 风险与已知限制

1. **并发副作用竞态**：当前工具实际串行，无真实竞态；实现按并发安全写，`depends_on` 保证拓扑隔离。跨步骤共享状态（「A 改配置 / B 读旧配置重启」类）登记为已知限制，未来引入真并发时再补锁/版本校验。
2. **并行 interrupt 的多审批并发**：`approval_store.get_pending` 返回多张、`stream_plan_graph` 遍历全部 `__interrupt__` 落多张审批单。逐张独立裁决：`resolve_approvals` 在 interrupt 载荷里生成 `approval_id`、`record_approval` 固化 LangGraph `interrupt_id`，`resume` 按 `interrupt_id` 用 ID 键 resume map 精确路由（LangGraph 1.x 多 interrupt 要求）；`/approve` 接收 `decisions: [{approval_id, decision}]` 列表，前端逐单 approve/reject 后累积全部裁决一次性提交。
3. **`plan` 状态序列化**：dict 方案已规避可变对象问题，仍需验证 checkpointer 对 `Plan.to_dict()` 产物的 msgpack/JSON 兼容性。
