"""大文档确认：documents 表加 embedding_approved 列（大文档警告）

Revision ID: 0014_document_approval
Revises: 0013_idempotency
Create Date: 2026-09-25

- 新增 `documents.embedding_approved`（bool，默认 False）：嵌入成本预估超过阈值时置
  `needs_approval`，用户确认后置 True 跳过阈值检查（见 docs/module-4-documents.md）。
"""
import sqlalchemy as sa

from alembic import op

revision = "0014_document_approval"
down_revision = "0013_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "embedding_approved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("documents", "embedding_approved")
