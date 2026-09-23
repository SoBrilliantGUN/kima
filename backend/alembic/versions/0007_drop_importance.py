"""copilot 记忆：删除死字段 importance（写入恒 0.5、全库无消费，纯占位）

Revision ID: 0007_drop_importance
Revises: 0006_llm_snapshot
Create Date: 2026-09-23
"""
import sqlalchemy as sa

from alembic import op

revision = "0007_drop_importance"
down_revision = "0006_llm_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("copilot_memories", "importance")


def downgrade() -> None:
    # 回滚需 server_default 回填存量行（NOT NULL 列加回非空表必须给默认值）
    op.add_column(
        "copilot_memories",
        sa.Column("importance", sa.Float(), nullable=False, server_default="0.5"),
    )
