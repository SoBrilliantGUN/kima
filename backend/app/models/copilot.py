"""Copilot 记忆与事件日志模型（模块 6）。

- `CopilotMemory`：四型积累记忆（约束/事实/偏好/情节）的向量条目。约束是硬规则/红线，
  硬召回不过相似度阈值；冲突用 `superseded` 留痕而非硬删；fact 同 `entity_id` 覆盖时
  `version++`。
- `CopilotEvent`：append-only 事件日志（思维链），`seq` 全局单调递增可重放。
"""

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.models.base import Base, TimestampMixin

# embedding 维度唯一真源 = settings.embedding_dim（默认 1024，bge-m3）
EMBEDDING_DIM = get_settings().embedding_dim

# copilot_events.seq 用数据库序列保证全局单调递增（跨 run 重放排序）
EVENT_SEQ = "copilot_events_seq"

# copilot_stream_events.seq 同理：SSE 前端事件流（刷新回放 + 续订）按全局 seq 游标重放
STREAM_EVENT_SEQ = "copilot_stream_events_seq"


class MemoryKind(StrEnum):
    """四型记忆：约束（硬规则/红线）/事实（稳定事实）/偏好（软规则）/情节（具体事件）。

    约束是「确定域」——只要任务沾边就无条件在场，不靠余弦相似度；其余三型是「概率域」，
    靠混合召回（向量 + 词法）。
    """

    CONSTRAINT = "constraint"
    FACT = "fact"
    PREFERENCE = "preference"
    EPISODIC = "episodic"


class CopilotMemory(Base, TimestampMixin):
    """积累型记忆条目（向量化，四型分类）。"""

    __tablename__ = "copilot_memories"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kind: Mapped[MemoryKind] = mapped_column(
        Enum(MemoryKind, native_enum=False, length=16), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # fact 的稳定实体键（如 "user:role"）；空则不按实体覆盖
    entity_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    # episodic 默认 30 天；preference/fact/constraint 空（不衰减）
    ttl_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # constraint 的触发条件（如 {"type": "domain", "value": "database"}），本轮仅落库埋点，
    # 召回暂不消费——留给未来 domain 路由用，避免现在过度设计。
    trigger_conditions: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_access: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 冲突被覆盖标记（可逆、留痕，保留溯源），召回时跳过
    superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 软删除窗口（遗忘第三动作）：被 superseded 的时间戳 + 压它的那条记忆 id。
    # superseded_at 是窗口起点（N 天内可召回复活）；superseded_by 是复活守卫——
    # 压它的那条还活着就不复活，防「真冲突」被召回误复活。
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # fact 同 entity 覆盖时递增
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class CopilotEvent(Base):
    """append-only 事件日志（思维链）：`type ∈ {tool_call,tool_result,llm_delta,done,error}`。

    只写不更新，`seq` 由数据库序列赋值，按 run 内 seq 顺序重放。
    """

    __tablename__ = "copilot_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text(f"nextval('{EVENT_SEQ}')")
    )
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CopilotStreamEvent(Base):
    """SSE 前端事件流（刷新回放 / 续订）：``type ∈ {meta,step,delta,review,approval,done,error}``。

    与 `CopilotEvent`（思维链审计日志）不同：本表存的是**推给前端的原始 SSE 事件**，用于
    「刷新后续上没收到的内容」——订阅端点先按 ``seq`` 回放、再 tail 新事件。run 到终态
    （completed/failed/interrupted）后整段删除（最终内容已固化进 ``chat_messages``）。
    ``assistant_message_id`` 是回放键（前端从 meta/POST 响应即有），``run_id`` 仅作审计/join。
    """

    __tablename__ = "copilot_stream_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text(f"nextval('{STREAM_EVENT_SEQ}')")
    )
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    assistant_message_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CopilotDailyBudget(Base, TimestampMixin):
    """Copilot 全局日预算（跨 run 成本/token 累计，重启后从 DB 续读，按自然日一行）。"""

    __tablename__ = "copilot_daily_budget"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    cost_cny: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class CopilotLLMSnapshot(Base):
    """LLM 调用级快照：把每次 LLM 调用当纯函数，成功后落 output，恢复时按键复用。

    PK 为 (run_id, call_key)：``call_key`` 是「node + 规范化输入」的 sha256 前缀，同 run
    内重放时输入一致 → 命中缓存、不重跑（决策 D3/D4）。``run_id`` 是 thread_id 字符串，
    划界「本 run 的重放缓存」，避免跨 run 的 temperature>0 输出被错误复用。
    """

    __tablename__ = "copilot_llm_snapshots"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    call_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    # chat | agent | embedding | rerank
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IdempotencyStatus(StrEnum):
    """幂等记录状态：processing 执行中 / succeeded 成功（可回放缓存）/ failed_final 永久失败。

    文章四态里的 failed_retryable 由工具层 ``with_retry`` 在内存兜底（瞬态错误不落表、
    直接退避重试），幂等表只记录三态——这是对齐本库既有重试层的有意简化。
    """

    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED_FINAL = "failed_final"


class CopilotIdempotency(Base, TimestampMixin):
    """写工具幂等去重记录（Stripe 式）。

    幂等键标识「一次业务意图」（编排层注入的内容派生键，而非位置序号），主键
    ``(tool_name, idem_key)`` 原子抢占：先插 ``processing``，成功后转 ``succeeded`` 落结果
    缓存，永久失败转 ``failed_final`` 落错误。跨重试/崩溃复用同一个键——命中 succeeded
    直接回放、命中 processing 让调用方稍后重试、同键不同 ``request_hash`` 拒绝。

    ``expires_at`` 是 TTL：processing 残留回收（崩溃在 claim 后 succeed 前留下的孤儿，
    过期后 claim 可惰性回收重执行）依赖它；succeeded 记录无 sweep、长期保留（结果缓存
    永久化，与业务唯一约束互补）。
    """

    __tablename__ = "copilot_idempotency"

    tool_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    idem_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[IdempotencyStatus] = mapped_column(
        Enum(IdempotencyStatus, native_enum=False, length=16),
        nullable=False,
        default=IdempotencyStatus.PROCESSING,
    )
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CopilotBreaker(Base):
    """熔断器状态（第三层「动作指纹」的外置持久化）：每工具 / 每资源一行，崩溃后据此
    续读失败计数，避免「恢复后计数归零、永远触发不了熔断」。

    ``name`` 为工具名或 ``resource:<name>``（级联共享依赖，见 resilience/circuit_breaker）；
    ``state`` ∈ closed/open/half_open，``failures`` 是墙钟失败时间戳列表（窗口内）。
    """

    __tablename__ = "copilot_breakers"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="closed")
    failures: Mapped[list[float]] = mapped_column(JSONB, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ApprovalStatus(StrEnum):
    """审批单状态：pending 待审 / approved 已批准 / rejected 已拒绝 / expired 超时失效。

    超时失效是 fail-close 的落地——审批过期默认阻断（写操作本就没执行，过期后不再可批）。
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class CopilotApproval(Base, TimestampMixin):
    """HITL 审批单（第一类实体）：把「等审批」从 checkpoint 里解耦成可查询、可审计、
    可超时的持久化状态。一次 run 可先后产生多张审批单（每张对应一次高危写的 interrupt），
    故以自身 UUID 为主键、``run_id`` 建索引；``resume`` 按 ``run_id`` 定位当前待审单。

    证据包（``summary`` + ``level`` + ``args``）：把冷参数翻译成业务风险，让人一眼看清
    「动的是什么、风险多高」，而非甩一个裸 JSON。``args`` 保留原始参数供展开查看与审计。
    """

    __tablename__ = "copilot_approvals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    # LangGraph interrupt id（resume map 的键）：并行多 interrupt 时 LangGraph 1.x 要求
    # `Command(resume={interrupt_id: value})`，故把 interrupt.id 在挂起那一刻固化为本列，
    # 续批时据此把「每张单的裁决」精确路由到对应的 interrupt，而非按位置盲猜。
    interrupt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # resume 上下文：把「续批时往哪回填」一并落单，前端刷新后从 `/approvals/pending` 找回
    # 即可直接调 `/approve`（无需再凭内存里的 meta 事件）。挂起时 assistant 消息尚未落库，
    # 故 assistant_message_id 只能由落单这一刻固化。
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    assistant_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="high")
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, native_enum=False, length=16),
        nullable=False,
        default=ApprovalStatus.PENDING,
    )
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
