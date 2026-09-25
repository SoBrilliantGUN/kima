"""实时计费定价目录模型（见 docs/pricing.md）。

- `PricingPolicy`：策略 = 一个生效时间区间（``valid_from``/``valid_to``，UTC）+ 一天内
  各时段单价（``schedule`` JSONB）。时变主数据——改价只动数据、不碰代码；节假日/促销用
  「把区间切成非重叠几段」承载。
- `PricingChangeLog`：append-only 审计流水，由 ``pricing_policy`` 上的触发器填充，
  手动 SQL 改价也留痕。
- `CopilotLLMCost`：调用级成本明细（审计主档），每笔真实 LLM 调用带「当时解析出的价格
  快照 + 成本（元）」，与 ``CopilotLLMSnapshot`` 同 key 幂等 upsert、不设 TTL。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Float, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PricingPolicy(Base, TimestampMixin):
    """一条定价策略：某厂商某模型在某时间区间内、一天各时段的单价。

    ``schedule`` 是「每天重复的单一日程」，时段边界用 UTC 的 ``HH:MM`` 字符串，必须覆盖
    00:00–24:00、首尾相接不重叠；``prices`` 是厂商自定义字段（DeepSeek 三维、SiliconFlow
    单维），由该厂商的 strategy 解释。
    """

    __tablename__ = "pricing_policy"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    vendor: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schedule: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    remark: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")


class PricingChangeLog(Base):
    """定价变更流水（append-only，审计）。``before``/``after`` 为整行 JSONB 快照。"""

    __tablename__ = "pricing_change_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    policy_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    changed_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    remark: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CopilotLLMCost(Base):
    """调用级成本明细（审计主档）：每笔真实 LLM 调用一行，带价格快照可精确复算。

    PK 为 ``(run_id, call_key)``，与 ``CopilotLLMSnapshot`` 同 key——同一逻辑调用幂等
    upsert、缓存命中不写（不重复计费）。``price_snapshot`` 记录「当时解析出的各维度单价 +
    策略 id + 时段」，使历史成本在价格变化后依然可对账。
    """

    __tablename__ = "copilot_llm_cost"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    call_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    vendor: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    pricing_policy_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    price_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cost_cny: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
