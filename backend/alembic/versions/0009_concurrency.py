"""并发安全：笔记幂等唯一约束 + 记忆实体唯一索引（《隔离优于共享》原子性落库）

Revision ID: 0009_concurrency
Revises: 0008_copilot_plan
Create Date: 2026-09-23

- `notes.content_hash`：非唯一索引 → 唯一索引。Copilot `create_note` 幂等去重的并发兜底——
  并发同正文的两次 `create_with_content` 只落一条（唯一索引 + `ON CONFLICT DO NOTHING`）。
  空白笔记 content_hash 置 NULL（空正文的 sha256 相同，会互相撞唯一约束）。
- `copilot_memories`：`(kind, entity_id)` 加部分唯一索引（`WHERE entity_id IS NOT NULL
  AND NOT superseded`）。同实体的活跃语义记忆至多一条，并发覆盖不会「丢 version/重复建行」。
"""
from alembic import op

revision = "0009_concurrency"
down_revision = "0008_copilot_plan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. notes.content_hash：先把「空白笔记」的 hash 置 NULL（空正文哈希相同，会撞唯一约束）
    op.execute("UPDATE notes SET content_hash = NULL WHERE content_markdown = ''")
    op.drop_index("ix_notes_content_hash", table_name="notes")
    op.create_index("ux_notes_content_hash", "notes", ["content_hash"], unique=True)

    # 2. copilot_memories：同 (kind, entity_id) 只允许一条未 superseded 的活跃记忆
    op.execute(
        "CREATE UNIQUE INDEX ux_copilot_memories_kind_entity "
        "ON copilot_memories (kind, entity_id) "
        "WHERE entity_id IS NOT NULL AND NOT superseded"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_copilot_memories_kind_entity")
    op.drop_index("ux_notes_content_hash", table_name="notes")
    op.create_index("ix_notes_content_hash", "notes", ["content_hash"])
