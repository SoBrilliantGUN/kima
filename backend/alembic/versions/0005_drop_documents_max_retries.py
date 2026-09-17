"""drop documents.max_retries（重试上限改为常量 MAX_RETRIES）

Revision ID: 0005_drop_documents_max_retries
Revises: 0004_documents
Create Date: 2026-09-17

重试上限不再按文档存储（不随数据而异），改为 `models.document.MAX_RETRIES` 常量。
0004 原样建了该列，本迁移删掉；dev 库已有值一并丢弃。
"""
import sqlalchemy as sa

from alembic import op

revision = "0005_drop_documents_max_retries"
down_revision = "0004_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("documents", "max_retries")


def downgrade() -> None:
    # 回加列需补 server_default 以满足 NOT NULL（原值来自 ORM 默认 3）
    op.add_column(
        "documents",
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="3"),
    )
