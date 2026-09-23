"""copilot 全局日预算：跨 run 成本/token 累计（重启后从 DB 续读）

Revision ID: 0003_daily_budget
Revises: 0002_copilot
Create Date: 2026-09-22

- 新增 `copilot_daily_budget`（day 主键，单日一行记录 cost_usd/tokens）
"""

import sqlalchemy as sa

from alembic import op

revision = "0003_daily_budget"
down_revision = "0002_copilot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "copilot_daily_budget",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("tokens", sa.BigInteger(), nullable=False, server_default="0"),
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
        sa.PrimaryKeyConstraint("day", name="pk_copilot_daily_budget"),
    )


def downgrade() -> None:
    op.drop_table("copilot_daily_budget")
