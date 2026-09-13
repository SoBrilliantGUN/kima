"""create knowledge_bases table

Revision ID: 0002_knowledge_bases
Revises: 0001_baseline
Create Date: 2026-09-12

"""
import sqlalchemy as sa

from alembic import op

revision = "0002_knowledge_bases"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_table("knowledge_bases")
