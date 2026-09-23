"""planner 计划检查点表（崩溃恢复，Plan-as-Data 第三道防线）

Revision ID: 0008_copilot_plan
Revises: 0007_drop_importance
Create Date: 2026-09-23

- 新增 `copilot_plans`（planner 路径的检查点快照：版本化 DAG + 每步运行时状态）
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0008_copilot_plan"
down_revision = "0007_drop_importance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "copilot_plans",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", JSONB(), nullable=False),
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
        sa.PrimaryKeyConstraint("run_id", name="pk_copilot_plans"),
    )


def downgrade() -> None:
    op.drop_table("copilot_plans")
