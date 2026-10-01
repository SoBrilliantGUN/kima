"""审批单加 interrupt_id（LangGraph 并行多 interrupt 的 resume map 键）

Revision ID: 0017_copilot_approval_interrupt_id
Revises: 0016_drop_copilot_plan
Create Date: 2026-10-02

- `copilot_approvals` 加 `interrupt_id`（可空 String(64)）：LangGraph 1.x 对「并行多
  interrupt」要求 `Command(resume={interrupt_id: value})` 的 ID 键 map（而非按位置的列表）。
  故在挂起那一刻把 `interrupt.id` 固化进审批单，续批时据此把每张单的裁决精确路由到
  对应的 interrupt，支撑「逐单审批」。
"""

import sqlalchemy as sa

from alembic import op

revision = "0017_copilot_approval_interrupt_id"
down_revision = "0016_drop_copilot_plan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("copilot_approvals", sa.Column("interrupt_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("copilot_approvals", "interrupt_id")
