"""HITL 审批单的数据访问层：Protocol + SQLAlchemy + 内存实现。

审批单是第一类实体：把「等审批」从 LangGraph checkpoint 里解耦成可查询、可审计、可
超时的持久化状态（checkpoint 在 dev 是内存版、前端刷新后也拿不回挂起态）。仓库只做
存取原语；超时失效（fail-close）与裁决落库的编排在 service 层。

``create`` 幂等覆盖写语义不适用——一次 run 可先后产生多张审批单（每次高危写的 interrupt
各一张），以自身 UUID 为主键；``get_pending`` 按 ``run_id`` 取该 run 的所有待审单
（并行审批可多张，按创建时间升序）。
"""

import uuid
from datetime import datetime
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.copilot import ApprovalStatus, CopilotApproval


class ApprovalStore(Protocol):
    async def create(self, approval: CopilotApproval) -> CopilotApproval: ...
    async def get_pending(self, run_id: uuid.UUID) -> list[CopilotApproval]: ...
    async def list_pending(self, now: datetime) -> list[CopilotApproval]: ...
    async def decide(self, approval_id: uuid.UUID, decision: str, now: datetime) -> None: ...
    async def expire(self, approval_id: uuid.UUID) -> None: ...


class SqlAlchemyApprovalStore:
    """SQLAlchemy 实现：持有 session factory，每次操作独立会话。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, approval: CopilotApproval) -> CopilotApproval:
        async with self._session_factory() as session:
            session.add(approval)
            await session.commit()
            await session.refresh(approval)
            return approval

    async def get_pending(self, run_id: uuid.UUID) -> list[CopilotApproval]:
        """取该 run 的所有待审单（并行审批可多张，按创建时间升序）。"""
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(CopilotApproval)
                .where(
                    CopilotApproval.run_id == run_id,
                    CopilotApproval.status == ApprovalStatus.PENDING,
                )
                .order_by(CopilotApproval.created_at.asc())
            )
            return list(rows)

    async def list_pending(self, now: datetime) -> list[CopilotApproval]:
        """返回当前仍待审（未过期）的审批单；顺带把已过期的 pending 惰性标记为 expired。

        惰性失效而非后台 sweep：单用户本地应用没有常驻审批 worker，读时把过期单翻转为
        expired 即可保证 fail-close 语义（过期即不可再批）。
        """
        async with self._session_factory() as session:
            await session.execute(
                update(CopilotApproval)
                .where(
                    CopilotApproval.status == ApprovalStatus.PENDING,
                    CopilotApproval.expires_at.is_not(None),
                    CopilotApproval.expires_at < now,
                )
                .values(status=ApprovalStatus.EXPIRED)
            )
            rows = await session.scalars(
                select(CopilotApproval)
                .where(CopilotApproval.status == ApprovalStatus.PENDING)
                .order_by(CopilotApproval.created_at.asc())
            )
            result = list(rows)
            await session.commit()
            return result

    async def decide(self, approval_id: uuid.UUID, decision: str, now: datetime) -> None:
        """落裁决：status 依 decision 置 approved/rejected，记 decided_at。"""
        status = ApprovalStatus.APPROVED if decision == "approve" else ApprovalStatus.REJECTED
        async with self._session_factory() as session:
            await session.execute(
                update(CopilotApproval)
                .where(CopilotApproval.id == approval_id)
                .values(status=status, decision=decision, decided_at=now)
            )
            await session.commit()

    async def expire(self, approval_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(CopilotApproval)
                .where(CopilotApproval.id == approval_id)
                .values(status=ApprovalStatus.EXPIRED)
            )
            await session.commit()


class InMemoryApprovalStore:
    """内存版审批单（测试/无 DB 场景）：list 保存，惰性失效逻辑同 SQLAlchemy 版。"""

    def __init__(self) -> None:
        self._rows: dict[uuid.UUID, CopilotApproval] = {}

    async def create(self, approval: CopilotApproval) -> CopilotApproval:
        # 忠实镜像 ORM 的 ``default=ApprovalStatus.PENDING``（ORM 在 flush 时补，内存版在此补）。
        if approval.status is None:
            approval.status = ApprovalStatus.PENDING
        self._rows[approval.id] = approval
        return approval

    async def get_pending(self, run_id: uuid.UUID) -> list[CopilotApproval]:
        return sorted(
            (
                a
                for a in self._rows.values()
                if a.run_id == run_id and a.status is ApprovalStatus.PENDING
            ),
            key=lambda a: a.created_at or datetime.min,
        )

    async def list_pending(self, now: datetime) -> list[CopilotApproval]:
        for a in self._rows.values():
            if (
                a.status is ApprovalStatus.PENDING
                and a.expires_at is not None
                and a.expires_at < now
            ):
                a.status = ApprovalStatus.EXPIRED
        return sorted(
            (a for a in self._rows.values() if a.status is ApprovalStatus.PENDING),
            key=lambda a: a.created_at or datetime.min,
        )

    async def decide(self, approval_id: uuid.UUID, decision: str, now: datetime) -> None:
        a = self._rows.get(approval_id)
        if a is None:
            return
        a.status = ApprovalStatus.APPROVED if decision == "approve" else ApprovalStatus.REJECTED
        a.decision = decision
        a.decided_at = now

    async def expire(self, approval_id: uuid.UUID) -> None:
        a = self._rows.get(approval_id)
        if a is not None:
            a.status = ApprovalStatus.EXPIRED
