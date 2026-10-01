"""审批分级（Policy-as-Code）+ 审批单内存仓库：分档裁决与超时 fail-close 的单元测试。"""

import uuid
from datetime import UTC, datetime, timedelta

from app.agent.approval import (
    ApprovalDecision,
    ApprovalPolicy,
    approval_summary,
    resolve_approval_decision,
)
from app.agent.toolmeta import SideEffectLevel, ToolMeta
from app.models.copilot import ApprovalStatus, CopilotApproval
from app.repositories.approval import InMemoryApprovalStore
from tests.fakes import make_runtime_config


def _registry(name: str, level: SideEffectLevel) -> dict[str, ToolMeta]:
    return {name: ToolMeta(name, "test", level, "tool_result", 500)}


def test_graded_policy_maps_levels() -> None:
    policy = ApprovalPolicy.graded()
    assert policy.decide(SideEffectLevel.LOW) is ApprovalDecision.ALLOW
    assert policy.decide(SideEffectLevel.MEDIUM) is ApprovalDecision.NOTIFY
    assert policy.decide(SideEffectLevel.HIGH) is ApprovalDecision.REQUIRE_APPROVAL


def test_strict_policy_requires_all_writes() -> None:
    policy = ApprovalPolicy.strict()
    assert policy.decide(SideEffectLevel.MEDIUM) is ApprovalDecision.REQUIRE_APPROVAL
    assert policy.decide(SideEffectLevel.HIGH) is ApprovalDecision.REQUIRE_APPROVAL
    assert policy.decide(SideEffectLevel.LOW) is ApprovalDecision.ALLOW


def test_policy_unknown_level_fail_closed() -> None:
    # level_map 缺省时 fail-closed 到审批（安全优先）
    policy = ApprovalPolicy(level_map={SideEffectLevel.LOW: ApprovalDecision.ALLOW})
    assert policy.decide(SideEffectLevel.HIGH) is ApprovalDecision.REQUIRE_APPROVAL


def test_resolve_readonly_is_allow() -> None:
    runtime = make_runtime_config(approval_policy=ApprovalPolicy.graded())
    assert (
        resolve_approval_decision("read_note", _registry("read_note", SideEffectLevel.LOW), runtime)
        is ApprovalDecision.ALLOW
    )


def test_resolve_graded_medium_is_notify() -> None:
    runtime = make_runtime_config(approval_policy=ApprovalPolicy.graded())
    assert (
        resolve_approval_decision(
            "create_note", _registry("create_note", SideEffectLevel.MEDIUM), runtime
        )
        is ApprovalDecision.NOTIFY
    )


def test_resolve_graded_high_requires_approval() -> None:
    runtime = make_runtime_config(approval_policy=ApprovalPolicy.graded())
    assert (
        resolve_approval_decision(
            "update_profile", _registry("update_profile", SideEffectLevel.HIGH), runtime
        )
        is ApprovalDecision.REQUIRE_APPROVAL
    )


def test_approval_summary_deterministic() -> None:
    assert approval_summary("create_note", {"title": "会议纪要"}) == "新建笔记《会议纪要》"
    assert approval_summary("write_memory", {"kind": "constraint"}) == "写入一条硬约束记忆"
    assert approval_summary("update_profile", {"kind": "soul"}) == "覆盖 soul 人设档案"


async def test_approval_store_create_get_decide() -> None:
    store = InMemoryApprovalStore()
    run_id = uuid.uuid4()
    approval = await store.create(
        CopilotApproval(
            run_id=run_id,
            tool="update_profile",
            args={"kind": "soul"},
            summary="覆盖 soul 人设档案",
            level="high",
        )
    )
    pending = await store.get_pending(run_id)
    assert pending and pending[0].id == approval.id

    now = datetime.now(UTC)
    await store.decide(approval.id, "approve", now)
    assert await store.get_pending(run_id) == []  # 已裁决，不再是 pending


async def test_approval_store_expiry_fail_close() -> None:
    store = InMemoryApprovalStore()
    run_id = uuid.uuid4()
    now = datetime.now(UTC)
    expired = await store.create(
        CopilotApproval(
            run_id=run_id,
            tool="update_profile",
            args={"kind": "soul"},
            summary="覆盖 soul 人设档案",
            level="high",
            expires_at=now - timedelta(seconds=1),
        )
    )
    # list_pending 惰性失效：过期的 pending 不再返回，且状态翻转为 expired
    pending = await store.list_pending(now)
    assert all(a.id != expired.id for a in pending)
    assert expired.status is ApprovalStatus.EXPIRED
