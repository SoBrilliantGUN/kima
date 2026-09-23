"""HITL 审批单表（把「等审批」从 checkpoint 解耦为可查询/可超时的第一类实体）

Revision ID: 0010_copilot_approval
Revises: 0009_concurrency
Create Date: 2026-09-23

- 新增 `copilot_approvals`：高危写工具的审批单（run_id 索引、证据包 summary/level/args、
  状态机 pending/approved/rejected/expired、expires_at 支撑超时 fail-close）
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0010_copilot_approval"
down_revision = "0009_concurrency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "copilot_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("tool", sa.String(length=64), nullable=False),
        sa.Column("args", JSONB(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("level", sa.String(length=16), nullable=False, server_default="high"),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_copilot_approvals"),
    )
    op.create_index("ix_copilot_approvals_run_id", "copilot_approvals", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_copilot_approvals_run_id", table_name="copilot_approvals")
    op.drop_table("copilot_approvals")
