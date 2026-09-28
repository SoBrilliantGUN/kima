"""copilot 记忆：kind 枚举改名 procedural→preference、semantic→fact（对齐机制词汇）

Revision ID: 0015_rename_memory_kinds
Revises: 0014_document_approval
Create Date: 2026-09-26

`MemoryKind` 原「程序记忆（procedural，实为偏好）」「语义记忆（semantic，实为事实）」是
术语错位，改名对齐文章原生机制词汇 constraint/fact/preference/episodic（见 module-6 §2.1
与决策 #58）。`kind` 列存字符串值（native_enum=False），故需数据迁移改写存量行。
"""

from alembic import op

revision = "0015_rename_memory_kinds"
down_revision = "0014_document_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE copilot_memories SET kind = 'preference' WHERE kind = 'procedural'")
    op.execute("UPDATE copilot_memories SET kind = 'fact' WHERE kind = 'semantic'")


def downgrade() -> None:
    op.execute("UPDATE copilot_memories SET kind = 'procedural' WHERE kind = 'preference'")
    op.execute("UPDATE copilot_memories SET kind = 'semantic' WHERE kind = 'fact'")
