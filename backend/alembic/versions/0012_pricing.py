"""实时计费：定价目录 + 变更审计 + 调用级成本明细 + 币种重命名（见 docs/pricing.md）

Revision ID: 0012_pricing
Revises: 0011_breaker_state
Create Date: 2026-09-23

- 新增 `pricing_policy`（策略 = 生效区间 + 一天各时段单价）、`pricing_change_log`（审计流水）、
  `copilot_llm_cost`（调用级成本明细，带价格快照）。
- `pricing_policy` 上加：非重叠 exclusion 约束（btree_gist）、时段覆盖校验触发器、审计触发器。
- `copilot_daily_budget.cost_usd` 重命名为 `cost_cny`（币种统一人民币）。
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0012_pricing"
down_revision = "0011_breaker_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 币种统一人民币：日预算表列重命名（表新建未久，直接改名，不迁移历史值）
    op.alter_column("copilot_daily_budget", "cost_usd", new_column_name="cost_cny")

    op.create_table(
        "pricing_policy",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vendor", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schedule", JSONB(), nullable=False),
        sa.Column("remark", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default=""),
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
        sa.PrimaryKeyConstraint("id", name="pk_pricing_policy"),
    )
    op.create_index("ix_pricing_policy_vendor_model", "pricing_policy", ["vendor", "model"])

    op.create_table(
        "pricing_change_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("policy_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("changed_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("before", JSONB(), nullable=True),
        sa.Column("after", JSONB(), nullable=True),
        sa.Column("remark", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pricing_change_log"),
    )

    op.create_table(
        "copilot_llm_cost",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("call_key", sa.String(length=64), nullable=False),
        sa.Column("vendor", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("pricing_policy_id", sa.Uuid(), nullable=True),
        sa.Column("price_snapshot", JSONB(), nullable=False, server_default="{}"),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_cny", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("run_id", "call_key", name="pk_copilot_llm_cost"),
    )

    # 非重叠 exclusion 约束：同一 (vendor, model) 生效区间不得重叠（需 btree_gist）
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute(
        """
        ALTER TABLE pricing_policy ADD CONSTRAINT pricing_policy_no_overlap
        EXCLUDE USING gist (
            vendor WITH =,
            model WITH =,
            tstzrange(valid_from, valid_to, '[)') WITH &&
        )
        """
    )

    # 时段校验触发器：schedule 每段 start<end，排序后首尾相接覆盖 00:00-24:00（UTC）
    op.execute(
        """
        CREATE OR REPLACE FUNCTION check_pricing_schedule() RETURNS trigger AS $$
        DECLARE
            v record;
        BEGIN
            IF jsonb_array_length(NEW.schedule) = 0 THEN
                RAISE EXCEPTION 'pricing_policy.schedule: 不能为空';
            END IF;
            IF EXISTS (
                SELECT 1 FROM jsonb_array_elements(NEW.schedule) s
                WHERE s->>'start' IS NULL OR s->>'end' IS NULL
                   OR (s->>'start') >= (s->>'end')
            ) THEN
                RAISE EXCEPTION 'pricing_policy.schedule: 存在 start >= end 的时段';
            END IF;

            SELECT
                bool_and(st = prev_en) AS chained,
                (array_agg(st ORDER BY st))[1] = '00:00' AS starts_at_zero,
                (array_agg(en ORDER BY st))[array_length(array_agg(en ORDER BY st), 1)] = '24:00'
                    AS ends_at_full
            INTO v
            FROM (
                SELECT
                    s->>'start' AS st,
                    s->>'end' AS en,
                    lag(s->>'end') OVER (ORDER BY s->>'start') AS prev_en
                FROM jsonb_array_elements(NEW.schedule) s
            ) t;

            IF v.chained IS FALSE OR v.starts_at_zero IS FALSE OR v.ends_at_full IS FALSE THEN
                RAISE EXCEPTION 'pricing_policy.schedule: 时段必须不重叠且覆盖 00:00-24:00';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    # asyncpg 的 prepared statement 不接受一条 execute 里多个命令，函数与触发器分开执行
    op.execute(
        """
        CREATE TRIGGER pricing_policy_schedule_check
        BEFORE INSERT OR UPDATE ON pricing_policy
        FOR EACH ROW EXECUTE FUNCTION check_pricing_schedule();
        """
    )

    # 审计触发器：insert/update/delete 自动记 pricing_change_log（手动 SQL 改价也留痕）
    op.execute(
        """
        CREATE OR REPLACE FUNCTION log_pricing_change() RETURNS trigger AS $$
        DECLARE
            act text;
        BEGIN
            IF TG_OP = 'INSERT' THEN act := 'insert';
            ELSIF TG_OP = 'UPDATE' THEN act := 'update';
            ELSE act := 'delete';
            END IF;
            INSERT INTO pricing_change_log (policy_id, action, changed_by, before, after, remark)
            VALUES (
                COALESCE(NEW.id, OLD.id),
                act,
                COALESCE(NEW.created_by, OLD.created_by, ''),
                CASE WHEN TG_OP IN ('UPDATE','DELETE') THEN to_jsonb(OLD) ELSE NULL END,
                CASE WHEN TG_OP IN ('INSERT','UPDATE') THEN to_jsonb(NEW) ELSE NULL END,
                ''
            );
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    # asyncpg 的 prepared statement 不接受一条 execute 里多个命令，函数与触发器分开执行
    op.execute(
        """
        CREATE TRIGGER pricing_policy_audit
        AFTER INSERT OR UPDATE OR DELETE ON pricing_policy
        FOR EACH ROW EXECUTE FUNCTION log_pricing_change();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS pricing_policy_audit ON pricing_policy")
    op.execute("DROP TRIGGER IF EXISTS pricing_policy_schedule_check ON pricing_policy")
    op.execute("DROP FUNCTION IF EXISTS log_pricing_change()")
    op.execute("DROP FUNCTION IF EXISTS check_pricing_schedule()")
    op.drop_table("copilot_llm_cost")
    op.drop_table("pricing_change_log")
    op.execute("ALTER TABLE pricing_policy DROP CONSTRAINT IF EXISTS pricing_policy_no_overlap")
    op.drop_index("ix_pricing_policy_vendor_model", table_name="pricing_policy")
    op.drop_table("pricing_policy")
    op.alter_column("copilot_daily_budget", "cost_cny", new_column_name="cost_usd")
