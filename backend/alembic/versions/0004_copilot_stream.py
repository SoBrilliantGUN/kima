"""SSE 前端事件流：刷新回放 / 续订

Revision ID: 0004_copilot_stream
Revises: 0003_document_guard_approval
Create Date: 2026-10-07

新增 `copilot_stream_events`：存推给前端的原始 SSE 事件（meta/step/delta/review/
approval/done/error），用于「刷新后续上没收到的内容」——订阅端点先按 seq 回放、再 tail
新事件。run 到终态（completed/failed/interrupted）后整段删除（最终内容已固化进
chat_messages）。与 `copilot_events`（思维链审计日志，append-only 不删）职责分离。
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0004_copilot_stream"
down_revision = "0003_document_guard_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS copilot_stream_events_seq")
    op.create_table(
        "copilot_stream_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "seq",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("nextval('copilot_stream_events_seq')"),
        ),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("assistant_message_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_copilot_stream_events"),
    )
    op.create_index("ix_copilot_stream_events_run_id", "copilot_stream_events", ["run_id"])
    op.create_index(
        "ix_copilot_stream_events_assistant_message_id",
        "copilot_stream_events",
        ["assistant_message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_copilot_stream_events_assistant_message_id", table_name="copilot_stream_events")
    op.drop_index("ix_copilot_stream_events_run_id", table_name="copilot_stream_events")
    op.drop_table("copilot_stream_events")
    op.execute("DROP SEQUENCE IF EXISTS copilot_stream_events_seq")
