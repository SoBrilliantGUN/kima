"""LLM 调用级快照表（决策 D3/D4/D8）

Revision ID: 0006_llm_snapshot
Revises: 0005_memory_forgetting
Create Date: 2026-09-23

- 新增 `copilot_llm_snapshots`（每次 LLM 调用的输入内容哈希 + 输出，恢复时复用不复跑）
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0006_llm_snapshot"
down_revision = "0005_memory_forgetting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "copilot_llm_snapshots",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("call_key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("output", JSONB(), nullable=False),
        sa.Column("usage", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("run_id", "call_key", name="pk_copilot_llm_snapshots"),
    )
    # TTL 清理按 created_at 扫描，建索引加速
    op.create_index("ix_copilot_llm_snapshots_created_at", "copilot_llm_snapshots", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_copilot_llm_snapshots_created_at", table_name="copilot_llm_snapshots")
    op.drop_table("copilot_llm_snapshots")
