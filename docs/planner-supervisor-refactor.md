# Planner 生产级重构方案（supervisor-worker 执行引擎 + ChainOptimizer 审查层）

> 状态：**已实现**（P1–P5 全部落地，2026-10-01）。
> 定案日期：2026-10-01。
> 上游基线：`docs/module-6-copilot.md`（§4.14 planner / §4.13 契约交互）。

---

## 0. 背景与目标

当前 planner（`plan_runner.py` + `runtime/executor.py`）是「graph 外、串行、无审批、无计划审查」的残缺执行器，相对 reactive 存在五处真实差距：

| # | 维度 | 现状 | 目标 |
|---|---|---|---|
| 1 | 并行 | `get_parallel_ready()` 已支持，`executor.py:46` 只取 `ready[0]` 串行 | 真正扇出 |
| 2 | HITL | `_plan_tool_names` 把 `REQUIRE_APPROVAL` 静默剔除 | interrupt 暂停、拒绝 → replan 降级 |
| 3 | 审查 | 无 | 关键路径 + 四维报告 + 分级熔断 |
| 4 | 缩短链路 | `planner._SYSTEM` 无约束 | 最长串行链 ≤4、强制并行 |
| 5 | 宿主 | graph 外 async 迭代器 | LangGraph supervisor-worker |

**目标**：把 planner 重构为**迁入 LangGraph 的 supervisor-worker 引擎**，补齐并行扇出、HITL 暂停审批、计划审查（关键路径 / 分级熔断 / 缩短链路），并**复用 reactive 已沉淀的防线纯函数**，删掉 `run_tool` / `_review_plan_answer` / `_plan_tool_names` 三处手写复制。

---

## 1. 已定决策（定案，不再讨论）

| # | 决策 | 结论 |
|---|---|---|
| D1 | 首要目标 | 完善 planner（现有 planner 不完善） |
| D2 | 范围 | 执行引擎（并行 + HITL）+ 审查层（ChainOptimizer） |
| D3 | 执行宿主 | **迁 LangGraph supervisor-worker**（静态图 + 动态 `Plan` 状态） |
| D4 | 复用方式 | **抽纯函数**：worker 与 reactive 共用防线纯函数，不套 messages 语义 |
| D5 | 审查层深度 | **完整**：关键路径体检 + 四维报告 + 分级熔断三档（Safe/Warn/**Emergency 本轮实现**）+ 设计层 Prompt 约束 |
| D6 | 状态序列化 | `plan` 用 **dict**（节点边界 `from_dict`/`to_dict` 转换）；`trace` 用 `list[dict]`；`final_answer` 用 `str` |
| D7 | 轨迹统一 | **执行时收集统一 trace**（`{"tool","args","result","ok"}`），review 读 `trace` + `final_answer`，删 `trace_from_messages` 与 `_last_answer` |
| D8 | HITL 语义 | interrupt 暂停 → approve 继续 / deny → 步骤 FAILED → replan 换降级步骤（**不再剔除工具**） |
| D9 | 并发 | 实现按「多 worker 真并发安全」写（状态隔离、无共享可变、`depends_on` 保证拓扑不打架）；**当前实际串行，未来解锁** |
| D10 | 崩溃恢复 | 统一走 LangGraph checkpointer；`plan_store` 退役；`copilot_events` 事件溯源保留 |
| D11 | 并行审批 | 支持一个 run 多张并行 pending 审批单；前端错开叠放（文档弹窗式）；`approval_store.get_pending` 由「单张」改「多张」 |

---

## 2. 目标架构：静态图 + 动态 Plan 状态

一张**静态图**，动态性全部在 `Plan` 状态数据里：

```
START → plan_node → supervisor_node ⇄ worker_node（Send 扇出） → synthesize_node → review_node → END
```

- **`plan_node`**：`planner.generate` 生成 Plan + ChainOptimizer 审查 → 写 Plan 进状态。
- **`supervisor_node`**：确定性路由。读 `Plan.get_parallel_ready()`，返回 `[Send("worker", {step}) …]` 扇出；全部完成则 goto synthesize；检测 FAILED 则内部 `await planner.replan` + `plan.merge` 后继续。
- **`worker_node`**：执行单个 step，复用 reactive 防线纯函数 + `interrupt()` 审批。
- **`synthesize_node`**：复用现有 `_synthesize_plan_answer` 逻辑，产出 `final_answer` 写进状态。
- **`review_node`**：复用 `build_review_node`，读 `trace` + `final_answer`。

**为什么 replan 与 interrupt 不再打架**：图结构全程不变，replan 只改 `Plan` 数据、interrupt 只冻结在 worker 节点内部，两者正交（区别于「每次重编译子图」方案的 checkpoint 张力）。

---

## 3. 状态设计（`runtime/planner_state.py`，新建）

```python
class PlannerState(TypedDict):
    plan: dict            # Plan.to_dict()，节点边界 from_dict/to_dict 转换
    task: str
    system_prompt: str    # L0（synthesize / review 共用）
    memory_block: str     # L2 召回约束
    results: dict         # step_id → output_ref
    trace: list[dict]     # 统一执行轨迹 {"tool","args","result","ok"}
    final_answer: str     # synthesize 产出，review 对账用
    verdict: str          # 审查 verdict（ok / warn / critical）
    last_error: dict | None  # 崩溃现场，与 AgentState 同字段语义
```

**为什么 `plan` 用 dict 而非裸 `Plan` 对象**（决策 D6 的根因）：

1. **可变对象破坏 checkpoint 值语义（致命）**：`Plan` 是可变对象，`mark_downstream_obsolete` / `merge` / 改 `step.status` 全是原地 mutate。LangGraph 的 state 更新模型是「节点**返回** partial dict → merge → 整个 state 序列化进 checkpoint」，它靠「返回的新值」感知变化；裸可变对象原地改、不返回新值，checkpoint 无法正确 diff，且序列化器若按引用落盘，历史 checkpoint 会被后续 mutate 污染——直接打穿事件溯源与检查点这道防线。
2. **序列化兼容**：checkpointer 默认 JSON 系 serializer 不识别自定义 dataclass，需额外注册；dict 开箱即用。
3. **与现有序列化同源**：`plan_store.to_dict/from_dict` 已存在，事件溯源共用。

代价：每次节点边界一次 from/to 转换，plan 规模（几十步）开销可忽略。

---

## 4. 节点设计（复用映射）

| 新节点 | 复用来源 | 复用方式 |
|---|---|---|
| `worker_node` | reactive `tool_node` 防线 | **抽纯函数**：`validate_param_contract` / `inject_idempotency_keys` / `evaluate_tool_results`(sanitize+output_contract) / `_resolve_approvals`(interrupt) |
| `review_node` | `build_review_node` | **整节点复用**，输入改为 `trace` + `final_answer`（D7） |
| `plan_node` / `supervisor` 的 LLM 出口 | `agent_node` 装配层 | 复用 `run_budget` + `gateway.invoke_model`（已在用） |
| 审查层 | 新增 `runtime/chain_optimizer.py` | 新写，无复用 |

**轨迹统一（D7）落地**：
- state 新增 `trace: list[dict]`，统一结构 `{"tool","args","result","ok"}`。
- reactive `tool_node` 在 `evaluate_tool_results` 之后追加 trace；planner `worker_node` 追加**同一种结构**。
- `review_node` 改为读 `trace` + `final_answer`，删除 `trace_from_messages`（靠 `tool_call_id` 配对的脆弱代码）与 `_last_answer`。
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

## 6. 复用与清理清单

**新增**：
- `runtime/planner_state.py` — 状态 schema
- `runtime/plan_graph.py` — 图装配（五节点）
- `runtime/chain_optimizer.py` — 审查层

**抽取纯函数**（P1）：从 `reactive.py` 抽出 `_resolve_approvals`（interrupt）与 `evaluate_tool_results` 到独立 helper，worker 与 reactive 共用。

**删除**：`plan_runner.py` 的 `run_tool` / `_review_plan_answer` / `_plan_tool_names` / `_save_checkpoint`；`executor.py` 的 `iter_plan_execution`；`review_node.py` 的 `trace_from_messages` / `_last_answer`。

**废弃/迁移**：`plan_store` 检查点被 LangGraph checkpointer 取代（`resume_plan` 并入 `resume_after_crash`）；`approval_store` 从「串行单 pending」改「并行多 pending」（D11）；`copilot_events` 事件溯源**保留**（事件溯源是崩溃可回溯的基础，不删）。

---

## 7. 分阶段迁移（每阶段可独立验证、不破坏现有 reactive）

| Phase | 内容 | 验收 |
|---|---|---|
| **P1 抽纯函数 + 统一 trace** | 抽 tool_node 防线纯函数，reactive 改调纯函数（零行为变化）；`tool_node` 加 trace 收集，`review_node` 改读 `trace`+`final_answer` | reactive 回归全绿，行为等价 |
| **P2 图骨架 + 并行** | `plan_graph.py` 五节点 + Send 扇出，替换 `iter_plan_execution`；先串行后并行 | 多步任务并行跑通，产物正确 |
| **P3 HITL** | worker 内 `interrupt`，deny → FAILED → replan 降级；`resume` 复用 | 需审批写工具走审批，拒绝换降级步骤 |
| **P4 审查层** | `chain_optimizer` + Prompt 约束 + Safe/Warn/Emergency | 19 步链压到 ≤4 串行，关键路径报告正确 |
| **P5 收尾** | 删 `plan_runner`/`executor` 冗余，`plan_store` 退役，事件溯源保持 | 旧代码删除，崩溃恢复走 checkpointer |

---

## 8. 完成定义（Definition of Done）

1. planner 能**并行**执行无依赖步骤（`get_parallel_ready` 被真正利用）。
2. `REQUIRE_APPROVAL` 工具能**暂停审批**，拒绝后触发 replan 换降级步骤（不再被剔除）。
3. 计划执行前有**关键路径报告 + verdict**，超长串行链被 Prompt 约束压短；分级熔断 Safe/Warn/Emergency 三档可用。
4. `run_tool` / `_review_plan_answer` / `_plan_tool_names` 三处手写逻辑删除，reactive 与 planner 共用同一批防线纯函数。
5. 崩溃恢复统一走 LangGraph checkpointer，`plan_store` 退役，事件溯源保留。
6. `backend` `ruff` + `mypy(strict)` + `pytest` 全绿；测试不起真库/真网/真 LLM。

---

## 9. 风险与已知限制

1. **并发副作用竞态**：当前工具实际串行，无真实竞态；实现按并发安全写，`depends_on` 保证拓扑隔离。跨步骤共享状态（「A 改配置 / B 读旧配置重启」类）登记为已知限制，未来引入真并发时再补锁/版本校验。
2. **并行 interrupt 的多审批并发**（已实现 D11）：`approval_store.get_pending` 返回多张、`stream_plan_graph` 遍历全部 `__interrupt__` 落多张审批单、`resume` 用 `Command(resume=[decision] * n)` 原子 resume。已知限制：本端点一次一个 `decision` **统一裁决**所有 pending（非逐张独立裁决）；逐张独立裁决需改 `/approve` 接收 `[{approval_id, decision}]` 列表 + 前端累积裁决后再批量发。
3. **`plan` 状态序列化**：dict 方案已规避可变对象问题，P2 仍需验证 checkpointer 对 `Plan.to_dict()` 产物的 msgpack/JSON 兼容性。
