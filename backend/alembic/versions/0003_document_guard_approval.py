"""写库闸人工确认：注入红线命中改 needs_approval + 检索侧低信任隔离

Revision ID: 0003_document_guard_approval
Revises: 0002_copilot
Create Date: 2026-10-05

写库闸从「红线命中即拒」改为「红线命中 → needs_approval → 用户确认 → 低信任入库」：
- ``documents.injection_approved``：用户是否已确认该文档的注入红线命中（确认后跳过红线硬停）。
- ``documents.guard_report``：命中违规的结构化详情（pattern/matched/before/after），
  供前端待确认态高亮展示命中片段与前后文。
- ``document_chunks.quarantined``：入库时按 chunk 是否命中红线打标，检索侧据此把
  低信任文档的命中片段包进 ``<data>`` 隔离，而非硬阻断。
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0003_document_guard_approval"
down_revision = "0002_copilot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "injection_approved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column("documents", sa.Column("guard_report", JSONB(), nullable=True))
    op.add_column(
        "document_chunks",
        sa.Column(
            "quarantined",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("document_chunks", "quarantined")
    op.drop_column("documents", "guard_report")
    op.drop_column("documents", "injection_approved")
