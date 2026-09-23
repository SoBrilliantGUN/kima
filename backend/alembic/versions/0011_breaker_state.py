"""熔断器状态表（动作指纹外置持久化）

Revision ID: 0011_breaker_state
Revises: 0010_copilot_approval
Create Date: 2026-09-23

- 新增 `copilot_breakers`：工具/资源级熔断状态（name 主键、state 状态机、failures 墙钟
  失败时间戳 JSONB）。崩溃后据此续读失败计数，避免「恢复后计数归零、永远触发不了熔断」。
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0011_breaker_state"
down_revision = "0010_copilot_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "copilot_breakers",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="closed"),
        sa.Column("failures", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name", name="pk_copilot_breakers"),
    )


def downgrade() -> None:
    op.drop_table("copilot_breakers")
