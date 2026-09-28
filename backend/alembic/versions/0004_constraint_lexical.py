"""copilot 记忆：约束类型 + 词法检索列（分路召回：约束硬召回 + 语义/情节混合召回）

Revision ID: 0004_constraint_lexical
Revises: 0003_daily_budget
Create Date: 2026-09-22

- `copilot_memories` 加 `trigger_conditions`（JSONB，约束触发条件埋点，本轮不消费）
- `copilot_memories` 加 `tsv` 生成列（BM25 词法检索，复用 document_chunks 的 pg_jieba 模式）
- GIN 索引加速词法检索
- `kind` 列新增 `constraint` 值无需改表（String(16)、无 CHECK 约束）
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0004_constraint_lexical"
down_revision = "0003_daily_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("copilot_memories", sa.Column("trigger_conditions", JSONB(), nullable=True))
    # BM25 词法检索列：存量行自动回填（与 document_chunks.tsv 同款生成列）
    op.execute(
        "ALTER TABLE copilot_memories ADD COLUMN tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('jiebacfg', content)) STORED"
    )
    op.execute(
        "CREATE INDEX ix_copilot_memories_tsv ON copilot_memories USING gin (tsv) "
        "WHERE NOT superseded"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_copilot_memories_tsv")
    op.execute("ALTER TABLE copilot_memories DROP COLUMN IF EXISTS tsv")
    op.drop_column("copilot_memories", "trigger_conditions")
