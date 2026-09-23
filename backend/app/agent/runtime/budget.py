"""四轴预算：turns / seconds / tokens / cost 的硬终止边界（决策 #25）。

预算 = 硬上限而非软建议：每次进模型前预检四轴，任一超限即抛 `BudgetExceeded` 终止；
时间轴额外在 reactive 节点用 `asyncio.wait_for` 硬熔断（超时即终止，不等模型返回）。

cost 轴：成本**只在网关侧经 `PricingService` 算一次**（按厂商 strategy、人民币元），
以 `cost_cny` 传入 `record()`；本模块只累加、不再持有静态定价表（见 `docs/pricing.md`）。

除单 run 的 `HardBudget` 外，`DailyBudget` 提供跨 run 的全局日上限（成本/token）——
见 `daily_budget.py`（累计经 `DailyBudgetStore` 外置到 DB、按自然日重置，重启不清零）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.core.exceptions import DomainError


class BudgetExceeded(DomainError):
    """预算超限：终止本次 run。"""

    code = "budget_exceeded"


@dataclass(frozen=True)
class HardBudget:
    """四轴硬上限（初值，实现后可调）。

    ``max_tool_calls`` 是第五轴「工具调用次数」上限（None = 不限）：与 turns 不同，
    turns 计的是「进模型」的轮次，而 tool_calls 计的是「执行工具」的次数——Agent 单轮可
    并行发出多个工具调用，穷举爆破（防线②）靠这一轴显式兜底（每轮上限之外的总调用量）。
    """

    max_turns: int = 20
    max_seconds: float = 120.0
    max_tokens: int = 100_000
    max_cost_cny: float = 1.0
    max_tool_calls: int | None = None


@dataclass(frozen=True)
class Usage:
    """一次模型调用的 token 用量。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def billable_tokens(self) -> int:
        """计费 token = 总 - cache 命中（cache 命中按更低单价计）。"""
        return self.input_tokens + self.output_tokens - self.cache_read_tokens


def add_usage(a: Usage, b: Usage) -> Usage:
    """累加两次模型用量（LLMClient 自纠错多次调用求和用）。"""
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
    )


def extract_usage(usage_metadata: Any, raw_usage: Any = None) -> Usage:
    """从 usage_metadata 提取 token 用量；缺失则全 0。

    缓存命中 ``cache_read_tokens`` 优先读 LangChain 归一化字段
    ``input_token_details.cache_read``；DeepSeek 的缓存命中是 usage 顶层的
    ``prompt_cache_hit_tokens``（非 OpenAI 的 ``prompt_tokens_details.cached_tokens``），
    langchain_openai 不会把它归一化进 ``usage_metadata``，故需从原始 ``token_usage``
    （``response_metadata.token_usage``）兜底读，否则缓存命中恒 0、按全价计费。
    """
    usage: dict[str, Any] = dict(usage_metadata or {})
    details = usage.get("input_token_details")
    cache_read = details.get("cache_read", 0) if isinstance(details, dict) else 0
    if not cache_read:
        raw = dict(raw_usage or {})
        cache_read = raw.get("prompt_cache_hit_tokens", 0) or 0
    return Usage(
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        cache_read_tokens=int(cache_read),
    )


@dataclass(frozen=True)
class RunAccounting:
    """一次 run 结束时的账本快照（done 事件落库 + 缓存命中率监控）。

    从 ``BudgetTracker`` 的四轴累计导出：字段是「进模型」的累计用量（含 cache 明细），
    供归因（intent/model 维度由 service 层拼进 done 事件）与命中率告警。这是「单位任务
    成本」的最小形态——每个任务结束时都有一张能拆解到 token/cache 的明细，而非只有
    全局日预算那个聚合总账。
    """

    cost_cny: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    turn_count: int
    tool_call_count: int

    @property
    def billable_tokens(self) -> int:
        """计费 token = 总 - cache 命中（与 ``Usage.billable_tokens`` 同义）。"""
        return self.input_tokens + self.output_tokens - self.cache_read_tokens

    @property
    def cache_hit_rate(self) -> float | None:
        """前缀缓存命中率 = cache_read / input（无输入则 None，避免除零误报）。"""
        if self.input_tokens <= 0:
            return None
        return self.cache_read_tokens / self.input_tokens

    def to_dict(self) -> dict[str, Any]:
        """序列化成 done 事件的 JSON payload（成本保留 6 位小数、命中率 4 位）。"""
        hit_rate = self.cache_hit_rate
        return {
            "cost_cny": round(self.cost_cny, 6),
            "tokens": self.billable_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
            "turn_count": self.turn_count,
            "tool_call_count": self.tool_call_count,
        }


class BudgetTracker:
    """一次 run 的四轴累计器（闭包捕获，不进 LangGraph state，避免序列化 start_time）。

    `sink` 为可选全局日预算：`record()` 时把用量同步回写，使跨 run 的日上限生效。
    cost 不再自算——由调用方（网关）经 `PricingService` 算好后以 `cost_cny` 传入。
    """

    def __init__(
        self,
        budget: HardBudget,
        sink: DailyBudget | None = None,
    ) -> None:
        self._budget = budget
        self._sink = sink
        self._turn_count = 0
        self._tool_call_count = 0
        self._usage = Usage()
        self._cost_cny = 0.0
        self._start = time.monotonic()

    @property
    def turn_count(self) -> int:
        return self._turn_count

    @property
    def tool_call_count(self) -> int:
        return self._tool_call_count

    def record_tool_calls(self, n: int) -> None:
        """执行工具前计数 + 预检第五轴（超限抛 BudgetExceeded，工具不执行）。

        只查 tool_calls 这一轴，不碰 turns/seconds/tokens/cost：turns 轴语义是「进模型的
        轮次」，其预检应发生在下一轮 agent_node 进模型前，而非工具执行中途——否则首轮
        agent 已计 1 turn 后、执行工具时 turns 轴被提前触发，导致本应执行的工具被误杀。
        """
        self._tool_call_count += n
        if (
            self._budget.max_tool_calls is not None
            and self._tool_call_count > self._budget.max_tool_calls
        ):
            raise BudgetExceeded(
                f"tool_calls 超限：{self._tool_call_count}/{self._budget.max_tool_calls}"
            )

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def remaining_seconds(self) -> float:
        return self._budget.max_seconds - self.elapsed()

    def usage_ratio(self) -> float:
        """四轴占用比例的最大值（≥0），供 80% 软提示判定（<100% 时软性施压收尾）。"""
        b = self._budget
        ratios = [
            self._turn_count / b.max_turns if b.max_turns > 0 else 0.0,
            self.elapsed() / b.max_seconds if b.max_seconds > 0 else 0.0,
            self._usage.billable_tokens / b.max_tokens if b.max_tokens > 0 else 0.0,
            self._cost_cny / b.max_cost_cny if b.max_cost_cny > 0 else 0.0,
            self._tool_call_count / b.max_tool_calls if b.max_tool_calls else 0.0,
        ]
        return max(ratios)

    def check(self) -> None:
        """进模型前预检四轴；超限抛 BudgetExceeded。"""
        if self._turn_count >= self._budget.max_turns:
            raise BudgetExceeded(f"turns 超限：{self._turn_count}/{self._budget.max_turns}")
        # 时间使用asyncio.wait_for硬熔断
        if self._usage.billable_tokens >= self._budget.max_tokens:
            raise BudgetExceeded(
                f"tokens 超限：{self._usage.billable_tokens}/{self._budget.max_tokens}"
            )
        if self._cost_cny >= self._budget.max_cost_cny:
            raise BudgetExceeded(f"cost 超限：¥{self._cost_cny:.4f}/¥{self._budget.max_cost_cny}")
        if (
            self._budget.max_tool_calls is not None
            and self._tool_call_count >= self._budget.max_tool_calls
        ):
            raise BudgetExceeded(
                f"tool_calls 超限：{self._tool_call_count}/{self._budget.max_tool_calls}"
            )

    def record(self, usage: Usage, *, cost_cny: float, count_turn: bool = True) -> None:
        """一次模型调用后累计 turns / tokens / cost，并回写全局 sink。

        ``cost_cny`` 是调用方（网关）经 ``PricingService`` 算好的本笔成本（元），本模块
        不再自算。``count_turn``：本次调用是否计作一个 turn。仅 agent 主循环计 turn（防
        死循环的 ``max_turns`` 轴）；review / planner / 分类器等辅助 LLM 调用只计 token/
        cost，不计 turn，避免挤占 turns 上限。默认 ``True`` 保持原「仅 agent 调用 record」
        的语义。
        """
        if count_turn:
            self._turn_count += 1
        self._usage = Usage(
            input_tokens=self._usage.input_tokens + usage.input_tokens,
            output_tokens=self._usage.output_tokens + usage.output_tokens,
            cache_read_tokens=self._usage.cache_read_tokens + usage.cache_read_tokens,
        )
        self._cost_cny += cost_cny
        if self._sink is not None:
            self._sink.record(usage, cost_cny=cost_cny)

    def summary(self) -> RunAccounting:
        """导出本 run 的账本快照（done 事件落库用；纯读、不改变累计状态）。"""
        return RunAccounting(
            cost_cny=self._cost_cny,
            input_tokens=self._usage.input_tokens,
            output_tokens=self._usage.output_tokens,
            cache_read_tokens=self._usage.cache_read_tokens,
            turn_count=self._turn_count,
            tool_call_count=self._tool_call_count,
        )


# 重导出：放在模块末尾——``daily_budget.py`` 依赖本模块上方的成本/token 原语，
# 若在顶部 import 会与它形成 import 环（partial initialization）。
from app.agent.runtime.daily_budget import DailyBudget as DailyBudget  # noqa: E402
from app.agent.runtime.daily_budget import DailyBudgetStore as DailyBudgetStore  # noqa: E402
