"""跨 run 的全局日预算：单日成本 / token 硬上限（决策 #25 的跨 run 场景）。

累计经 ``DailyBudgetStore`` 外置到 DB，``load()`` 启动续读、``flush()`` 每次 run 落库，
重启不清零（避免绕过单日上限）。成本由调用方（网关）经 ``PricingService`` 算好后以
``cost_cny`` 传入，本类只累加、不再自算（见 ``docs/pricing.md``）。
"""

from datetime import UTC, date, datetime
from typing import Protocol

from app.agent.runtime.budget import BudgetExceeded, Usage


class DailyBudgetStore(Protocol):
    """日预算的持久化原语（可注入内存版 Fake 做确定性测试）。

    `load(day)` 返回该日的 (cost_cny, tokens) 或 None；`add(day, ...)` 原子累加**增量**——
    多进程/多 run 并发 flush 时靠 DB 侧 ``ON CONFLICT DO UPDATE ... = ... + EXCLUDED``
    累加，而不是覆盖写绝对值，避免「竞态覆盖」丢更新。
    """

    async def load(self, day: date) -> tuple[float, int] | None: ...
    async def add(self, day: date, cost_cny: float, tokens: int) -> None: ...


class DailyBudget:
    """跨 run 的全局日预算：单日成本 / token 硬上限（累计经 store 外置、按自然日重置）。

    run 开始前 `check()` 预检，run 内每次模型用量经 `BudgetTracker` 的 sink 回写。
    只做成本/token 两轴（turn/seconds 是单 run 轴，不跨 run）。``store`` 必填——无 store
    的纯进程内累计会「重启清零」绕过单日上限，故直接拒构造（Bug #2）。
    """

    def __init__(
        self,
        max_cost_cny: float,
        *,
        store: DailyBudgetStore,
        max_tokens: int | None = None,
    ) -> None:
        self._max_cost_cny = max_cost_cny
        self._max_tokens = max_tokens
        self._store = store
        self._cost_cny = 0.0
        self._tokens = 0
        # 自上次 flush 以来的累计增量（flush 时投递、清零；计数器用原子累加而非覆盖写）
        self._unflushed_cost_cny = 0.0
        self._unflushed_tokens = 0
        # 跨天未 flush 的增量（旧 day → 增量），flush 时排空到旧 day，避免 _rollover 丢增量
        self._pending_rollover: list[tuple[date, float, int]] = []
        self._day: date = datetime.now(UTC).date()

    @property
    def cost_cny(self) -> float:
        """当前日累计成本（可观测/审计用）。"""
        return self._cost_cny

    @property
    def tokens(self) -> int:
        """当前日累计 billable token（可观测/审计用）。"""
        return self._tokens

    async def load(self) -> None:
        """启动时从 store 续读当日累计。"""
        self._rollover()
        record = await self._store.load(self._day)
        if record is not None:
            self._cost_cny, self._tokens = record
        self._unflushed_cost_cny = 0.0
        self._unflushed_tokens = 0

    async def flush(self) -> None:
        """把未落库的增量原子累加到 store（run 结束调用，供重启续读）。

        先排空「跨天待清账」缓冲区（旧 day 增量，修复 _rollover 丢增量），再落当天增量。
        只投递增量、不覆盖绝对值：并发进程各自 flush 时，DB 侧用原子累加合并，
        不会出现「后写覆盖先写」的竞态丢更新。
        """
        for day, cost_cny, tokens in self._pending_rollover:
            await self._store.add(day, cost_cny, tokens)
        self._pending_rollover.clear()
        await self._store.add(self._day, self._unflushed_cost_cny, self._unflushed_tokens)
        self._unflushed_cost_cny = 0.0
        self._unflushed_tokens = 0

    def _rollover(self) -> None:
        """跨天重置当日累计；未 flush 的旧日增量先转入待清账缓冲区（不丢）。"""
        today = datetime.now(UTC).date()
        if today != self._day:
            if self._unflushed_cost_cny or self._unflushed_tokens:
                self._pending_rollover.append(
                    (self._day, self._unflushed_cost_cny, self._unflushed_tokens)
                )
            self._day = today
            self._cost_cny = 0.0
            self._tokens = 0
            self._unflushed_cost_cny = 0.0
            self._unflushed_tokens = 0

    def check(self) -> None:
        """run 开始前预检全局日预算；超限抛 BudgetExceeded。"""
        self._rollover()
        if self._cost_cny >= self._max_cost_cny:
            raise BudgetExceeded(f"全局日成本超限：¥{self._cost_cny:.4f}/¥{self._max_cost_cny}")
        if self._max_tokens is not None and self._tokens >= self._max_tokens:
            raise BudgetExceeded(f"全局日 token 超限：{self._tokens}/{self._max_tokens}")

    def usage_ratio(self) -> float:
        """成本/token 两轴占用比例的最大值（≥0），供 80% 软提示判定。"""
        ratios = [
            self._cost_cny / self._max_cost_cny if self._max_cost_cny > 0 else 0.0,
            self._tokens / self._max_tokens if self._max_tokens else 0.0,
        ]
        return max(ratios)

    def record(self, usage: Usage, *, cost_cny: float) -> None:
        """一次模型用量回写全局累计（成本 + token），并计入未落库增量。

        ``cost_cny`` 由调用方算好传入，本类不再自算。
        """
        self._rollover()
        self._cost_cny += cost_cny
        self._tokens += usage.billable_tokens
        self._unflushed_cost_cny += cost_cny
        self._unflushed_tokens += usage.billable_tokens
