# 实时价格计费 + 人民币统一 + 成本审计方案

> 状态：**已实现**（2026-09-23，含「qa.generate 补 run 身份」）
> 关联：`docs/requirements.md`、`docs/module-6-copilot.md`（§4.11 LLM 网关）
> 目标读者：后续维护者（读本文件了解设计意图与已落地实现）

---

## 0. 一句话目标

把 Copilot 的 **token 计费从「代码里写死的 USD 单价」改成「DB 里时变的、可审计的人民币定价目录」**：

1. **价格是时变主数据**——厂商/模型/生效区间/时段都存 DB，改价不碰代码、不发版；
2. **每厂商一个计费函数**——厂商定价策略各不相同（分时、阶梯、缓存命中/写入费），用 strategy 类各自实现，产出**人民币成本（元）**；
3. **每笔调用带价格快照**——落一张调用级成本明细表，任何一笔成本都能精确复算、对账；

动机：**价格会变（分段/活动），且「当时按什么价算的钱」事后必须能查——写死单价既漏审计、又会在价格变化后算错历史成本。**

---

## 1. 已确认决策（勿再问，直接按此执行）

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 价格真源 | **DB 时变主数据**（effective-dated catalog），不是配置文件、不写死 |
| D2 | 币种 | **统一人民币**；内部字段 `usd`→`cny`，单价单位「元/百万 token」 |
| D3 | 价格计算 | **每厂商一个 strategy 类**（代码），纯函数 `compute_cost(usage, prices) -> 元`；只有厂商「计费模式」变才改代码 |
| D4 | 改价入口 | **无 UI/CLI**，只定义表结构 + 写入时 DB 校验，改价手动 SQL/简单脚本 |
| D5 | 时区 | **统一 UTC**（存储时间戳 + 「一天24小时」时段边界都按 UTC） |
| D6 | 审计 | **价格变更流水表** + **调用级成本明细表（带价格快照）** |
| D7 | 启动校验 | 每个(厂商,模型) **向前连续覆盖 3 天**，任一有空洞 → 报错拒绝启动；llm/embedding/rerank **三者都要有价** |
| D8 | 节假日/促销 | 用**策略持续时间分段**承载（如「正常 9-02 止 + 促销 9-03~9-05 + 正常 9-06 起」三段非重叠区间），不加 weekday/holiday 字段 |
| D9 | 快照粒度 | **调用级成本明细表** `copilot_llm_cost`（run_id + call_key + 厂商 + 模型 + 价格快照 + token + 成本元） |

---

## 2. 现状盘点（问题清单）

### 2.1 价格写死 + 币种错位

- `budget.py::TokenPricing`：`input_per_m=0.27 / cache_read_per_m=0.07 / output_per_m=1.10`（**USD**，写死，只适配 DeepSeek 三维）。
- 币种：`HardBudget.max_cost_usd`、`RunAccounting.cost_usd`、`DailyBudget._cost_usd`、`copilot_daily_budget.cost_usd`、配置 `copilot_*_cost_usd`、报错文案 `$`——全链路 USD。
- 问题：① 厂商定价策略各不相同（见 §4.2），写死的三维 schema 覆盖不了；② 价格变化后历史成本算错；③ 审计缺口——「这笔钱当时按什么价算的」无处可查。

### 2.2 记账的两条路径（都要改）

| 路径 | 文件 | 说明 |
|---|---|---|
| **网关路径**（主） | `agent/gateway.py::_record` | 所有 agent 主循环/review/classifier/embed/rerank 走这里，`tracker.record(usage)` |
| **`_bounded_call` 路径** | `agent/helpers.py:49` | planner/qa 三种执行模式（`plan_mode.py` / `qa_mode.py` / `plan_answer.py`）跑在 graph 外，`tracker.record(usage_fn(result))` |

两条路径最终都进 `BudgetTracker.record` → 内部 `compute_cost(usage, self._pricing)`；`DailyBudget.record` 再各自用 `self._pricing` 算一遍（**成本被算两次**）。

---

## 3. 目标架构

```
                     ┌───────────────────────────────────────────────┐
 调用点              │   PricingService（价格解析 + 成本计算）           │
 agent/review/classi-│                                              │
 fier/judge/planner/ │   resolve(vendor, model, now)                 │
 qa/embed/rerank     │     └─ 命中策略区间(valid_from/valid_to)        │
                     │        └─ 命中时段 → 返回 (policy_id, prices)  │
                     │                                              │
                     │   compute_cost(usage, prices)                 │
                     │     └─ 按 vendor 的 strategy → 元(cny)         │
                     └──────────────┬────────────────────────────────┘
                                    │ cost_cny + price_snapshot
                     ┌──────────────▼────────────────────────────────┐
                     │  BudgetTracker / DailyBudget（只累加，不再自算成本）│
                     │  copilot_llm_cost（每笔调用带价格快照，审计主档）    │
                     └───────────────────────────────────────────────┘

  pricing_policy（时变主数据）        pricing_change_log（审计流水）
  ┌───────────────────────┐          ┌────────────────────────┐
  │ vendor, model          │  ────►  │ policy_id, before/after │
  │ valid_from/valid_to    │  触发器  │ changed_by, remark       │
  │ schedule(时段+单价)     │          └────────────────────────┘
  └───────────────────────┘
```

**铁律**：成本**只在 `PricingService` 算一次**，`BudgetTracker`/`DailyBudget` 只负责累加传入的 `cost_cny`，不再各自持有静态 `TokenPricing` 复算。

---

## 4. 核心设计

### 4.1 定价目录（3 张表 + DB 写入校验）

#### `pricing_policy`（策略 = 一个生效区间 + 一天内各时段单价）

```sql
CREATE TABLE pricing_policy (
  id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  vendor      TEXT        NOT NULL,      -- deepseek | siliconflow | ...
  model       TEXT        NOT NULL,      -- deepseek-chat | BAAI/bge-m3 | ...
  valid_from  TIMESTAMPTZ NOT NULL,      -- 生效起点（UTC，含）
  valid_to    TIMESTAMPTZ NOT NULL,      -- 生效终点（UTC，不含）
  schedule    JSONB       NOT NULL,      -- 一天内各时段单价（见下）
  remark      TEXT        NOT NULL DEFAULT '',
  created_by  TEXT        NOT NULL DEFAULT '',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`schedule` 是「每天重复的单一日程」，时段边界用 UTC 的 `HH:MM`，**必须覆盖 00:00–24:00、首尾相接不重叠**。`prices` 是厂商自定义字段，由该厂商 strategy 解释：

```json
[
  {"start": "00:00", "end": "08:00", "prices": {"input": 0.15, "cache_hit": 0.02, "output": 13.5}},
  {"start": "08:00", "end": "24:00", "prices": {"input": 0.30, "cache_hit": 0.04, "output": 27.0}}
]
```

（DeepSeek 三维：`input`/`cache_hit`/`output`；SiliconFlow 单维：`input`。见 §4.2。）

#### `pricing_change_log`（append-only 审计流水）

```sql
CREATE TABLE pricing_change_log (
  id          BIGSERIAL   PRIMARY KEY,
  policy_id   UUID        NOT NULL,
  action      TEXT        NOT NULL,      -- insert | update | delete
  changed_by  TEXT        NOT NULL DEFAULT '',
  before      JSONB,
  after       JSONB,
  remark      TEXT        NOT NULL DEFAULT '',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

由 `pricing_policy` 上的审计触发器自动填充，**手动 SQL 改价也会留痕**。

#### `copilot_llm_cost`（调用级成本明细，审计主档）

```sql
CREATE TABLE copilot_llm_cost (
  id                BIGSERIAL      PRIMARY KEY,
  run_id            TEXT           NOT NULL,   -- thread_id 字符串（与快照一致）
  call_key          TEXT           NOT NULL,   -- node+规范化输入 的 sha256 前缀（与快照一致）
  vendor            TEXT           NOT NULL,
  model             TEXT           NOT NULL,
  pricing_policy_id UUID,
  price_snapshot    JSONB          NOT NULL,   -- 当时解析出的 {policy_id, slot_start, slot_end, prices}
  input_tokens      BIGINT         NOT NULL,
  output_tokens     BIGINT         NOT NULL,
  cache_read_tokens BIGINT         NOT NULL,
  cost_cny          DOUBLE PRECISION NOT NULL, -- 本笔成本（元）
  created_at        TIMESTAMPTZ    NOT NULL DEFAULT now(),
  UNIQUE (run_id, call_key)
);
```

- 与 `CopilotLLMSnapshot` 同 key `(run_id, call_key)`、幂等 upsert，**真实调用才写、缓存命中不写**。
- **不设 TTL**（审计留痕，只涨不删），区别于快照的 7 天 TTL。

#### DB 写入校验（因为 D4 手动 SQL，校验要落在 DB 层）

1. **区间不重叠**：`btree_gist` exclusion constraint。
   ```sql
   CREATE EXTENSION IF NOT EXISTS btree_gist;
   ALTER TABLE pricing_policy ADD CONSTRAINT pricing_policy_no_overlap
     EXCLUDE USING gist (
       vendor WITH =, model WITH =,
       tstzrange(valid_from, valid_to, '[)') WITH &&
     );
   ```
2. **时段不重叠 + 覆盖 24h**：`BEFORE INSERT OR UPDATE` 触发器 `check_pricing_schedule()`（PL/pgSQL），对 `schedule` 逐条校验：每段 `start < end`；按 `start` 排序后首段从 `00:00` 起、段段首尾相接、末段止于 `24:00`。
3. **vendor 键齐全**：由 strategy 在计费时严格校验（缺键即抛 `PricingError` fail-closed），并在启动校验里兜底（§4.5）。

> 若 `btree_gist` 在镜像里不可用，退路：用 app 层 `PricingService` 的写入前校验 + 一个导入脚本（D4 允许「简单脚本」），但优先 DB 约束。

### 4.2 每厂商 strategy 注册表 + PricingService

```python
# app/agent/pricing/strategy.py（新）
class PricingStrategy(Protocol):
    def compute_cost(self, usage: Usage, prices: dict[str, Any]) -> float: ...  # 元

class DeepSeekStrategy:
    # prices = {"input": 元/百万, "cache_hit": 元/百万, "output": 元/百万}
    def compute_cost(self, usage, prices):
        billable_input = usage.input_tokens - usage.cache_read_tokens
        return (
            billable_input * prices["input"]
            + usage.cache_read_tokens * prices["cache_hit"]
            + usage.output_tokens * prices["output"]
        ) / 1_000_000

class SiliconFlowStrategy:
    # embedding/rerank 只按 input 计（prices = {"input": 元/百万}）
    def compute_cost(self, usage, prices):
        return usage.input_tokens * prices["input"] / 1_000_000

VENDOR_STRATEGIES: dict[str, PricingStrategy] = {
    "deepseek": DeepSeekStrategy(),
    "siliconflow": SiliconFlowStrategy(),
}
```

> 厂商定价策略的差异（来自调研，作为 strategy 实现的依据）：
> - **DeepSeek**：输入(缓存命中/未命中) + 输出，且官方有分时（空闲/高峰）；本方案用「时段」承接分时。
> - **通义千问**：按请求输入 token 长度**阶梯计费**（≤32K / 32K–128K / …）——将来接入时写一个带阶梯的 strategy。
> - **智谱 GLM**：输入长度阶梯 + 缓存命中 + 缓存存储费。
> - **Kimi**：输入(命中/未命中) + 输出 + 缓存写入费（按 TTL）。
> 每接入一个厂商，若其「计费模式」无法用现有 strategy 表达，就新增一个 strategy 类（D3）。

```python
# app/agent/pricing/service.py（新）
class PricingError(DomainError): code = "pricing_error"

@dataclass(frozen=True)
class PriceQuote:
    policy_id: uuid.UUID | None
    slot_start: str      # "HH:MM"
    slot_end: str
    prices: dict[str, Any]

class PricingService:
    def __init__(self, repo: PricingRepository, *, cache_ttl_seconds: int = 60): ...
    async def resolve(self, vendor: str, model: str, now: datetime) -> PriceQuote: ...
    def compute_cost(self, vendor: str, usage: Usage, quote: PriceQuote) -> float: ...
    async def validate_startup(self, now: datetime, targets: list[tuple[str, str]]) -> None: ...
```

- `resolve`：查 `pricing_policy` 中 `valid_from <= now < valid_to` 且 `vendor/model` 命中的行 → 用 UTC 的 `now.time()` 命中时段 → 返回 `PriceQuote`。**找不到生效策略即抛 `PricingError`**（运行时 fail-closed，见 §4.5）。
- `resolve` 带**短 TTL 内存缓存**（默认 60s，跨时段边界最多晚 1 分钟切价，可接受），避免每笔调用都打 DB。
- `compute_cost`：按 `vendor` 取 strategy，`strategy.compute_cost(usage, quote.prices)`；vendor 未注册或缺键 → 抛 `PricingError`。

### 4.3 计费链路改造（网关 + `_bounded_call` + 预算）

**成本在调用点算一次，向下传 `cost_cny`；`record` 不再自算。**

- `BudgetTracker.record(usage, *, cost_cny, count_turn=True)`：删掉 `self._pricing`/`compute_cost`，累加 `_cost_cny += cost_cny`，并 `sink.record(usage, cost_cny=cost_cny)`。
- `DailyBudget.record(usage, *, cost_cny)`：删掉 `self._pricing`，累加 `_cost_cny += cost_cny` + `billable_tokens`。
- `gateway._invoke`（已 async）：拿到 `usage` 后 → 按 `kind` 定 `(vendor, model)`（chat/agent→llm、embedding/embed_query→embedding、rerank→rerank）→ `quote = await pricing.resolve(...)` → `cost_cny = pricing.compute_cost(...)` → `_record(usage, cost_cny, quote)` → 写 `copilot_llm_cost`。
- `qa.generate`（`qa_mode.py`）：**补 run 身份**——原旁路 `_bounded_call`（无 run_id、不写成本明细），现改走 `run_budget + gateway.invoke_model("qa.generate", ...)`，与 synthesize/review 同路径。
- `_bounded_call`（`helpers.py`）：退化为只包**非 LLM 的 awaitable**（工具调用/检索，`Usage()=0`），`record` 成本记 0（不计价）。
- `budget.py::compute_cost` / `TokenPricing` 删除；`daily_budget.py` 不再 import 它们。

**币种全量重命名**（`usd`→`cny`）：`budget.py`（`max_cost_cny`/`_cost_cny`/`RunAccounting.cost_cny`/`to_dict["cost_cny"]`）、`daily_budget.py`（`_cost_cny`/`_unflushed_cost_cny`/`cost_cny`）、`repositories/daily_budget.py`、`models/copilot.py::CopilotDailyBudget.cost_cny`、`config.py`（`copilot_budget_max_cost_cny`/`copilot_daily_max_cost_cny`）、报错文案 `$`→`¥`。

### 4.4 调用级成本明细（审计主档）

- 新增 `CostStore` 协议 + `SqlAlchemyCostStore` + `InMemoryCostStore`（测试用），`put(run_id, call_key, ..., cost_cny, price_snapshot)` 幂等 upsert。
- 写入点：`gateway._invoke` 在 `_record` 之后、与快照写同处；`_bounded_call` 路径暂只累加预算、**不写明细**（planner/qa 跑在 graph 外、无 run_id/call_key 语义，作为开放点 §8）。

### 4.5 启动校验 + 运行时兜底

- **启动**（`main.py::lifespan`）：构造 `PricingService` 后调 `validate_startup(now, targets)`，`targets` = settings 里 **provider != 'fake'** 的 `(vendor, model)` 三元组（llm / embedding / rerank）。校验：
  1. 对每个 target，`[now, now+3d]` 被 `pricing_policy` 的区间**连续覆盖无空洞**；
  2. 每个生效策略的 `schedule` 对应该厂商 strategy 的键齐全、时段覆盖 24h。
  任一不满足 → **抛错拒绝启动**（fail-fast）。
- **运行时**：`resolve` 找不到当前生效价 → 抛 `PricingError` → 网关 fail-closed 停掉本次 run（即「超出覆盖窗口仍无新策略 → 报错暂停服务」）。
- `fake` 提供方（dev/test）跳过校验；另加配置 `copilot_pricing_check_enabled`（默认 True）可整体关闭。

---

## 5. 配置项（`core/config.py` + `.env`）

```python
# 实时计费（见 docs/pricing.md）
copilot_pricing_check_enabled: bool = True   # 启动 3 天覆盖校验开关
copilot_pricing_cache_ttl_seconds: int = 60  # resolve 内存缓存 TTL
# 重命名（币种统一人民币）
copilot_budget_max_cost_cny: float = 1.0     # 原 copilot_budget_max_cost_usd
copilot_daily_max_cost_cny: float = 10.0     # 原 copilot_daily_max_cost_usd
```

---

## 6. 迁移路径（分阶段，可独立提交）

### Phase 1 — 定价目录 + 模型 + 迁移
- 新增 `app/models/pricing.py`（`PricingPolicy` / `PricingChangeLog` / `CopilotLLMCost`），注册进 `models/__init__.py`。
- 迁移 `0012_pricing.py`：建 3 张表 + exclusion 约束 + 审计触发器 + 时段校验触发器；`ALTER copilot_daily_budget RENAME cost_usd TO cost_cny`。
- 单测：校验触发器（重叠区间拒绝、时段不覆盖 24h 拒绝）。

### Phase 2 — strategy + PricingService + 仓库
- 新增 `app/agent/pricing/{strategy,service}.py`、`app/repositories/pricing.py`（SQLAlchemy + InMemory）。
- 单测：`resolve`（命中区间/时段/找不到抛 `PricingError`）、`compute_cost`（DeepSeek 三维 / SiliconFlow 单维）、`validate_startup`（3 天覆盖/空洞报错）。

### Phase 3 — 计费链路改造（币种重命名 + 成本下传）
- `budget.py`/`daily_budget.py` 删 `TokenPricing`/`compute_cost`，`record` 改收 `cost_cny`；全量 `usd`→`cny` 重命名（含 config/文案 `$`→`¥`）。
- 网关 `_invoke` + `_bounded_call` 接入 `PricingService`，成本算一次下传。
- 回归：`test_copilot_budget` / `test_agent_guardrails` / `test_llm_gateway` 等全套更新。

### Phase 4 — 成本明细 + 快照
- 新增 `app/repositories/llm_cost.py`（`SqlAlchemyCostStore` + `InMemoryCostStore`）。
- 网关写 `copilot_llm_cost`（与快照同处、幂等）。
- 单测：真实调用落明细（含 `price_snapshot`/`cost_cny`）、缓存命中不落。

### Phase 5 — 启动校验 + 两个 bug 修复
- `main.py::lifespan` 装配 `PricingService` + `validate_startup`（fail-fast）。
- Bug #2：`store` 必填；Bug #3：`_rollover` 缓冲区。
- 单测：启动校验通过/报错、`_rollover` 跨天增量被 flush 到旧 day。

### Phase 6 — 收尾
- 全量测试 + `ruff` + `mypy`；更新 `docs/pricing.md` 状态为「已完成」。

---

## 7. 改动点清单（文件级）

**新增**
- `app/agent/pricing/__init__.py`、`strategy.py`、`service.py` —— strategy 注册表 + 解析/计算/校验
- `app/models/pricing.py` —— 3 张表模型
- `app/repositories/pricing.py` —— 价格仓库（SQLAlchemy + InMemory）
- `app/repositories/llm_cost.py` —— 成本明细仓库（SQLAlchemy + InMemory）
- `backend/alembic/versions/0012_pricing.py` —— 建表 + 约束 + 触发器 + `cost_usd`→`cost_cny`
- `backend/tests/test_pricing.py`、`test_llm_cost.py`

**修改**
- `app/agent/runtime/budget.py` —— 删 `TokenPricing`/`compute_cost`；`record` 收 `cost_cny`；`usd`→`cny`
- `app/agent/runtime/daily_budget.py` —— 删 `pricing`；`store` 必填；`_rollover` 缓冲区；`usd`→`cny`
- `app/agent/gateway.py` —— 注入 `PricingService` + 三组 `(vendor, model)`；`_invoke` 算成本、写明细
- `app/agent/helpers.py` —— `_bounded_call` 只包非 LLM awaitable，`record` 成本记 0
- `app/agent/qa_mode.py` —— `qa.generate` 改走网关（补 run 身份）
- `app/agent/plan_answer.py` / `runtime/reactive.py` —— 降级兜底 `record` 成本记 0
- `app/agent/service.py` —— `RunAccounting`/`flush` 的 `cny` 适配
- `app/repositories/daily_budget.py` —— `cost_usd`→`cost_cny`
- `app/models/copilot.py` —— `CopilotDailyBudget.cost_cny`
- `app/models/__init__.py` —— 注册新模型
- `app/main.py` —— 装配 `PricingService` + 启动校验
- `app/core/config.py` + `backend/.env.example` —— 新配置项 + 重命名
- `backend/tests/*` —— `cny` 重命名、store 必填、pricing 相关

**风险/回滚重点**
- `reactive.py` 的 agent 主循环是热路径，改 `record` 签名要同步更新 `helpers.py` 与三条执行模式。
- `copilot_daily_budget.cost_usd→cost_cny` 列重命名，迁移要幂等（先建后改、避免破坏现有数据）。

---

## 8. 风险与开放点

1. **（已解决）`qa.generate` 补 run 身份**：原 `qa.generate` 旁路 `_bounded_call`、无 run 身份，不写成本明细。现改走网关（`run_budget + gateway.invoke_model`），与 synthesize/review 同路径，逐笔成本明细已覆盖。剩余 `_bounded_call` 只包非 LLM 的 awaitable（工具/检索，零 token），无需计价。
2. **resolve 缓存 TTL**：60s 缓存意味着跨时段边界最多晚 1 分钟切价；若对秒级精确有要求，把 TTL 调小或用「到下一时段边界即失效」的缓存键。
3. **`btree_gist` 可用性**：若镜像缺 `btree_gist`，exclusion 约束退化为 app 层校验（D4 允许简单脚本）。
4. **节假日细粒度**：D8 用「策略分段」承载节假日/促销，一段区间内**每天同一时刻同价**；若某厂商需要「工作日/节假日」双轨（如 DeepSeek 官方口径），需另加 `days_of_week` 字段，本轮不做。
5. **SiliconFlow 单维计费**：embedding/rerank 的 token 目前是 `_estimate_text_tokens` 估算值（见 `gateway.py`），单价照录、量是估算，与供应商对账后修正。
6. **成本明细表增长**：不设 TTL，长期看会持续膨胀；若成问题再评估归档/分区，先不提前优化。

---

## 9. 测试策略

- **strategy/解析**（`FakePricingRepository` 注入）：
  - `resolve`：命中区间+时段 → 正确 `PriceQuote`；`now` 在区间外/无策略 → 抛 `PricingError`；
  - `compute_cost`：DeepSeek 三维、SiliconFlow 单维算元正确；缺键 → `PricingError`。
- **启动校验**：3 天连续覆盖通过；中间有空洞 → 报错；`fake` provider 跳过。
- **DB 校验**：区间重叠被 exclusion 拒绝；时段不覆盖 24h 被触发器拒绝；审计触发器记录 insert/update。
- **成本下传**：`tracker.record(usage, cost_cny=...)` 累加正确、`sink` 收到同一 `cost_cny`（不重算）。
- **成本明细**：真实调用落 `copilot_llm_cost`（含价格快照 + 成本）；缓存命中不落；幂等 upsert。
- **Bug 回归**：`DailyBudget` 不带 store 构造抛错；`_rollover` 跨天增量 flush 到旧 day、新 day 从 0 计。
- **币种回归**：全链路无 `usd` 残留、文案用 `¥`。

---

## 10. 下次开工清单（给 Claude 的指令）

1. 先读 `docs/pricing.md`（本文件）+ 决策表 D1–D9。
2. 按 §6 分阶段实施，**每阶段独立提交**；阶段间跑通测试再进下一阶段。
3. 改 `record` 签名时，用 `rg "tracker.record|\.record\(usage|sink.record"` 找全调用点，别漏 `helpers.py`/三条执行模式。
4. 币种重命名用 `rg "usd"` 全量扫，确保 `app/` 与 `tests/` 无残留。
5. 遵守 §3 铁律：成本只在 `PricingService` 算一次，`BudgetTracker`/`DailyBudget` 不持有静态价格。
