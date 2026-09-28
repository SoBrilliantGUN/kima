"""copilot（知识 Agent）：三型记忆 + 事件日志 + chat 回改 kind/steps

Revision ID: 0002_copilot
Revises: 0001_initial
Create Date: 2026-09-20

- 新增 `copilot_memories`（三型积累记忆，向量条目）
- 新增 `copilot_events`（append-only 思维链事件日志，seq 全局单调）
- 回改 `chat_conversations` +`kind`（qa/copilot）、`chat_messages` +`steps`（工具轨迹投影）
"""

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0002_copilot"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

# embedding 维度与 models 的 EMBEDDING_DIM（settings.embedding_dim，默认 1024）一致
EMBEDDING_DIM = 1024


def upgrade() -> None:
    # --- copilot_memories（三型积累记忆） ---
    op.create_table(
        "copilot_memories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.String(length=255), nullable=True),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("ttl_days", sa.Integer(), nullable=True),
        sa.Column("importance", sa.Float(), nullable=False),
        sa.Column("access_count", sa.Integer(), nullable=False),
        sa.Column("last_access", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_copilot_memories"),
    )
    # HNSW 部分索引：只索引「未 superseded」的条目
    op.execute(
        "CREATE INDEX ix_copilot_memories_embedding_hnsw "
        "ON copilot_memories USING hnsw (embedding vector_cosine_ops) "
        "WHERE embedding IS NOT NULL AND NOT superseded"
    )
    op.execute(
        "CREATE INDEX ix_copilot_memories_kind ON copilot_memories (kind) WHERE NOT superseded"
    )
    # semantic 同 entity 覆盖 / 召回按实体锚定
    op.execute(
        "CREATE INDEX ix_copilot_memories_kind_entity "
        "ON copilot_memories (kind, entity_id) WHERE NOT superseded"
    )

    # --- copilot_events（append-only 事件日志） ---
    op.execute("CREATE SEQUENCE IF NOT EXISTS copilot_events_seq")
    op.create_table(
        "copilot_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "seq",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("nextval('copilot_events_seq')"),
        ),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_copilot_events"),
    )
    op.create_index("ix_copilot_events_run_id", "copilot_events", ["run_id"])

    # --- chat 回改 ---
    op.add_column(
        "chat_conversations",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="qa"),
    )
    op.add_column("chat_messages", sa.Column("steps", JSONB(), nullable=True))

    # --- notes 回改：Copilot create_note 幂等去重的 content_hash ---
    op.add_column("notes", sa.Column("content_hash", sa.String(length=64), nullable=True))
    op.create_index("ix_notes_content_hash", "notes", ["content_hash"])


def downgrade() -> None:
    op.drop_index("ix_notes_content_hash", table_name="notes")
    op.drop_column("notes", "content_hash")
    op.drop_column("chat_messages", "steps")
    op.drop_column("chat_conversations", "kind")
    op.drop_index("ix_copilot_events_run_id", table_name="copilot_events")
    op.drop_table("copilot_events")
    op.execute("DROP SEQUENCE IF EXISTS copilot_events_seq")
    op.execute("DROP INDEX IF EXISTS ix_copilot_memories_kind_entity")
    op.execute("DROP INDEX IF EXISTS ix_copilot_memories_kind")
    op.execute("DROP INDEX IF EXISTS ix_copilot_memories_embedding_hnsw")
    op.drop_table("copilot_memories")
