"""写工具幂等去重表：业务意图键 + 原子状态转换（见 docs/module-6-copilot.md §4.16）

Revision ID: 0013_idempotency
Revises: 0012_pricing
Create Date: 2026-09-24

- 新增 `copilot_idempotency`：幂等键标识「一次业务意图」（编排层注入的内容派生键），
  主键 (tool_name, idem_key) 原子抢占；processing → succeeded（落结果缓存）/ failed_final
  （落错误）；request_hash 做同键不同参数冲突检测；expires_at 做 processing 残留 TTL 回收。
"""
import sqlalchemy as sa

from alembic import op

revision = "0013_idempotency"
down_revision = "0012_pricing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "copilot_idempotency",
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("idem_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="processing"),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("tool_name", "idem_key", name="pk_copilot_idempotency"),
    )


def downgrade() -> None:
    op.drop_table("copilot_idempotency")
