"""copilot 记忆：软删除窗口（遗忘第三动作）

Revision ID: 0005_memory_forgetting
Revises: 0004_constraint_lexical
Create Date: 2026-09-22

- `superseded_at`：被 superseded 的时间戳（软删除窗口起点，N 天内可召回复活）
- `superseded_by`：压它的那条记忆 id（复活守卫：压它的那条还活着就不复活，防真冲突误复活）
"""

import sqlalchemy as sa

from alembic import op

revision = "0005_memory_forgetting"
down_revision = "0004_constraint_lexical"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "copilot_memories", sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("copilot_memories", sa.Column("superseded_by", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("copilot_memories", "superseded_by")
    op.drop_column("copilot_memories", "superseded_at")
