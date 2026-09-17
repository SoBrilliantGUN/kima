"""create documents and document_chunks; drop url-note columns

Revision ID: 0004_documents
Revises: 0003_notes
Create Date: 2026-09-14

本迁移：
- 回改 notes：删 type / summary / source_url 三列（dev 库已有 url 笔记一并丢弃，不可逆）
- 建 documents / document_chunks（自引用 parent_id 父子切割）
- 部分 HNSW 索引：只索引 child（embedding IS NOT NULL）
"""
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0004_documents"
down_revision = "0003_notes"
branch_labels = None
depends_on = None

# embedding 维度与 models.document.EMBEDDING_DIM（settings.embedding_dim，默认 1024）一致；
# 迁移冻结默认值，维度漂移由 embed 时的维度断言兜底。
EMBEDDING_DIM = 1024


def upgrade() -> None:
    # --- 回改 notes：彻底删除网页笔记的 url 专属字段 ---
    op.drop_column("notes", "type")
    op.drop_column("notes", "summary")
    op.drop_column("notes", "source_url")

    # --- documents ---
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kb_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("content_markdown", sa.Text(), nullable=True),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["kb_id"],
            ["knowledge_bases.id"],
            name="fk_documents_kb_id_knowledge_bases",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
    )

    # --- document_chunks（父子切割，small-to-big） ---
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("kb_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_document_chunks_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["kb_id"],
            ["knowledge_bases.id"],
            name="fk_document_chunks_kb_id_knowledge_bases",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["document_chunks.id"],
            name="fk_document_chunks_parent_id_document_chunks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_chunks"),
    )

    # 部分 HNSW 索引：只索引 child（embedding 非空），parent 不向量化故不索引。
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding_hnsw "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops) "
        "WHERE embedding IS NOT NULL"
    )


def downgrade() -> None:
    # 索引随表删除一并清理；先删 child 表（自引用 FK 指向自身），再删 documents。
    op.drop_table("document_chunks")
    op.drop_table("documents")

    # 回加 notes 三列（url 数据已在 upgrade 时不可逆丢失，type 补默认值以满足 NOT NULL）
    op.add_column(
        "notes",
        sa.Column("type", sa.String(length=16), nullable=False, server_default="markdown"),
    )
    op.add_column("notes", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("notes", sa.Column("source_url", sa.Text(), nullable=True))
