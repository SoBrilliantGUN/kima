"""initial schema（v1 全量基线：合并原 0001–0007）

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-18

第一版无历史数据，把所有增量迁移合并为单一基线。包含：
- 扩展：vector（pgvector）+ pg_jieba（中文全文检索）
- 表：knowledge_bases / notes / note_knowledge_bases / documents / document_chunks
       / note_chunks / chat_conversations / chat_messages
- 父子切割（small-to-big）：document_chunks 与 note_chunks 均自引用 parent_id，
  parent 存上下文不向量化（embedding NULL）、child 向量化
- 检索索引：HNSW 部分索引（只索引 child）+ tsv GIN（pg_jieba 词法检索）
"""
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

# embedding 维度与 models 的 EMBEDDING_DIM（settings.embedding_dim，默认 1024）一致；
# 迁移冻结默认值，维度漂移由 embed 时的维度断言兜底。
EMBEDDING_DIM = 1024


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_jieba")

    # --- knowledge_bases ---
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("color", sa.String(length=7), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_bases"),
        sa.UniqueConstraint("name", name="uq_knowledge_bases_name"),
    )

    # --- notes（纯 Markdown 空白笔记，全局） ---
    op.create_table(
        "notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content_markdown", sa.Text(), nullable=False),
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
        sa.Column("vectorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_notes"),
    )

    # --- note_knowledge_bases（笔记多对多入库关联表） ---
    op.create_table(
        "note_knowledge_bases",
        sa.Column("note_id", sa.Uuid(), nullable=False),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["note_id"],
            ["notes.id"],
            name="fk_note_knowledge_bases_note_id_notes",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            name="fk_note_knowledge_bases_knowledge_base_id_knowledge_bases",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("note_id", "knowledge_base_id", name="pk_note_knowledge_bases"),
    )

    # --- documents（知识库内文档，kb_id 必填） ---
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

    # --- document_chunks（父子切割 small-to-big） ---
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
        sa.Column(
            "tsv",
            TSVECTOR(),
            sa.Computed("to_tsvector('jiebacfg', content)", persisted=True),
            nullable=True,
        ),
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
    # HNSW 部分索引：只索引 child（embedding 非空），parent 不向量化故不索引。
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding_hnsw "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops) "
        "WHERE embedding IS NOT NULL"
    )
    op.execute("CREATE INDEX ix_document_chunks_tsv_gin ON document_chunks USING gin (tsv)")

    # --- note_chunks（笔记分块，父子切割 small-to-big，与文档一致） ---
    op.create_table(
        "note_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("note_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column(
            "tsv",
            TSVECTOR(),
            sa.Computed("to_tsvector('jiebacfg', content)", persisted=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["note_id"],
            ["notes.id"],
            name="fk_note_chunks_note_id_notes",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["note_chunks.id"],
            name="fk_note_chunks_parent_id_note_chunks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_note_chunks"),
    )
    op.execute(
        "CREATE INDEX ix_note_chunks_embedding_hnsw "
        "ON note_chunks USING hnsw (embedding vector_cosine_ops) "
        "WHERE embedding IS NOT NULL"
    )
    op.execute("CREATE INDEX ix_note_chunks_tsv_gin ON note_chunks USING gin (tsv)")

    # --- chat_conversations（会话：首页全局 kb_id 空 / 右面板挂库） ---
    op.create_table(
        "chat_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kb_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
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
            name="fk_chat_conversations_kb_id_knowledge_bases",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_conversations"),
    )

    # --- chat_messages（append-only，仅 created_at） ---
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["chat_conversations.id"],
            name="fk_chat_messages_conversation_id_chat_conversations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_messages"),
    )


def downgrade() -> None:
    # 索引随表删除一并清理；按依赖反序 drop（先子表后父表，自引用表先于其引用方）。
    op.drop_table("chat_messages")
    op.drop_table("chat_conversations")
    op.drop_table("note_chunks")
    op.drop_table("document_chunks")
    op.drop_table("documents")
    op.drop_table("note_knowledge_bases")
    op.drop_table("notes")
    op.drop_table("knowledge_bases")
    op.execute("DROP EXTENSION IF EXISTS pg_jieba")
    op.execute("DROP EXTENSION IF EXISTS vector")
