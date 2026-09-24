"""审批单补 resume 上下文（conversation_id / assistant_message_id）

Revision ID: 0013_copilot_approval_resume_ctx
Revises: 0012_pricing
Create Date: 2026-09-24

- `copilot_approvals` 加 `conversation_id` / `assistant_message_id`（可空 Uuid）：
  前端刷新/关闭后从 `/approvals/pending` 找回审批单即可直接调 `/approve`，无需再凭
  内存里 meta 事件持有的会话上下文。挂起时 assistant 消息尚未落库，故 assistant_message_id
  只能在落单这一刻固化。
"""
import sqlalchemy as sa

from alembic import op

revision = "0013_copilot_approval_resume_ctx"
down_revision = "0012_pricing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "copilot_approvals", sa.Column("conversation_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "copilot_approvals", sa.Column("assistant_message_id", sa.Uuid(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("copilot_approvals", "assistant_message_id")
    op.drop_column("copilot_approvals", "conversation_id")
