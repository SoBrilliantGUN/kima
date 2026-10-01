"""删 planner 计划检查点表（planner 迁入 LangGraph，崩溃恢复改走 checkpointer）

Revision ID: 0016_drop_copilot_plan
Revises: 0015_rename_memory_kinds
Create Date: 2026-10-01

``copilot_plans`` 是 planner 跑在 graph 外时的检查点快照；planner 迁入 LangGraph 后
崩溃恢复统一走 checkpointer（``plan_graph`` 的 ``compile(checkpointer=...)``），此表退役。
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0016_drop_copilot_plan"
down_revision = "0015_rename_memory_kinds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("copilot_plans")


def downgrade() -> None:
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
