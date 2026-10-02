"""copilot 知识 Agent 运行时 + 实时计费（0002~0017 合并为单一基线）

Revision ID: 0002_copilot
Revises: 0001_initial
Create Date: 2026-10-02

原 0002~0017 的 16 个增量迁移合并为一个全量基线，净效果等价于逐条应用：
- 记忆：`copilot_memories`（三型记忆 constraint/fact/preference/episodic，向量 + 词法
  检索 + 软删除窗口）、`copilot_events`（append-only 事件日志）
- 预算/快照：`copilot_daily_budget`（日成本/token，cost_cny）、`copilot_llm_snapshots`
  （调用级快照，恢复不复跑）
- 审批/熔断/幂等：`copilot_approvals`（HITL 审批单 + resume 上下文 + interrupt_id）、
  `copilot_breakers`（熔断状态）、`copilot_idempotency`（写工具幂等去重）
- 计费：`pricing_policy` / `pricing_change_log` / `copilot_llm_cost` + 非重叠 exclusion
  约束 + 时段校验/审计触发器（见 docs/pricing.md）
- 回改：`chat_conversations.kind`、`chat_messages.steps`、`notes.content_hash`
  （唯一索引）、`documents.embedding_approved`

合并时消掉的中间往返：
- `copilot_memories.importance`（建后又删）不再出现
- `copilot_plans`（建后又删，planner 迁 LangGraph 走 checkpointer）不再出现
- `notes.content_hash` 直接建唯一索引（跳过非唯一→唯一的往返）
- `copilot_daily_budget` 直接列名 `cost_cny`（跳过 cost_usd→cost_cny 重命名）
"""

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

from alembic import op

revision = "0002_copilot"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

# embedding 维度与 models 的 EMBEDDING_DIM（settings.embedding_dim，默认 1024）一致
EMBEDDING_DIM = 1024


def upgrade() -> None:
    # --- copilot_memories（三型积累记忆，向量 + 词法 + 软删除窗口） ---
    op.create_table(
        "copilot_memories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.String(length=255), nullable=True),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("ttl_days", sa.Integer(), nullable=True),
        sa.Column("access_count", sa.Integer(), nullable=False),
        sa.Column("last_access", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("trigger_conditions", JSONB(), nullable=True),
        sa.Column(
            "tsv",
            TSVECTOR(),
            sa.Computed("to_tsvector('jiebacfg', content)", persisted=True),
            nullable=True,
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by", sa.Uuid(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_copilot_memories"),
    )
    # HNSW 部分索引：只索引「未 superseded」的条目
    op.execute(
        "CREATE INDEX ix_copilot_memories_embedding_hnsw "
        "ON copilot_memories USING hnsw (embedding vector_cosine_ops) "
        "WHERE embedding IS NOT NULL AND NOT superseded"
    )
    op.execute(
        "CREATE INDEX ix_copilot_memories_kind ON copilot_memories (kind) WHERE NOT superseded"
    )
    # semantic/fact 同 entity 覆盖 / 召回按实体锚定
    op.execute(
        "CREATE INDEX ix_copilot_memories_kind_entity "
        "ON copilot_memories (kind, entity_id) WHERE NOT superseded"
    )
    # BM25 词法检索列 GIN 索引（复用 document_chunks 的 pg_jieba 模式）
    op.execute(
        "CREATE INDEX ix_copilot_memories_tsv ON copilot_memories USING gin (tsv) "
        "WHERE NOT superseded"
    )
    # 并发兜底：同 (kind, entity_id) 只允许一条未 superseded 的活跃记忆
    op.execute(
        "CREATE UNIQUE INDEX ux_copilot_memories_kind_entity "
        "ON copilot_memories (kind, entity_id) "
        "WHERE entity_id IS NOT NULL AND NOT superseded"
    )

    # --- copilot_events（append-only 事件日志） ---
    op.execute("CREATE SEQUENCE IF NOT EXISTS copilot_events_seq")
    op.create_table(
        "copilot_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "seq",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("nextval('copilot_events_seq')"),
        ),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_copilot_events"),
    )
    op.create_index("ix_copilot_events_run_id", "copilot_events", ["run_id"])

    # --- copilot_daily_budget（跨 run 成本/token 累计，币种统一人民币 cost_cny） ---
    op.create_table(
        "copilot_daily_budget",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("cost_cny", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("tokens", sa.BigInteger(), nullable=False, server_default="0"),
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
        sa.PrimaryKeyConstraint("day", name="pk_copilot_daily_budget"),
    )

    # --- copilot_llm_snapshots（LLM 调用级快照，恢复复用不复跑） ---
    op.create_table(
        "copilot_llm_snapshots",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("call_key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("output", JSONB(), nullable=False),
        sa.Column("usage", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("run_id", "call_key", name="pk_copilot_llm_snapshots"),
    )
    # TTL 清理按 created_at 扫描，建索引加速
    op.create_index("ix_copilot_llm_snapshots_created_at", "copilot_llm_snapshots", ["created_at"])

    # --- copilot_approvals（HITL 审批单：resume 上下文 + interrupt_id） ---
    op.create_table(
        "copilot_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("tool", sa.String(length=64), nullable=False),
        sa.Column("args", JSONB(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("level", sa.String(length=16), nullable=False, server_default="high"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        # resume 上下文：前端刷新/关闭后凭审批单即可续批，无需内存 meta 事件
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("assistant_message_id", sa.Uuid(), nullable=True),
        # LangGraph 并行多 interrupt 的 resume map 键
        sa.Column("interrupt_id", sa.String(64), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_copilot_approvals"),
    )
    op.create_index("ix_copilot_approvals_run_id", "copilot_approvals", ["run_id"])

    # --- copilot_breakers（工具/资源级熔断状态，崩溃后续读失败计数） ---
    op.create_table(
        "copilot_breakers",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="closed"),
        sa.Column("failures", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name", name="pk_copilot_breakers"),
    )

    # --- copilot_idempotency（写工具幂等去重：业务意图键 + 原子状态转换） ---
    op.create_table(
        "copilot_idempotency",
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("idem_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="processing"),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("tool_name", "idem_key", name="pk_copilot_idempotency"),
    )

    # --- 实时计费（见 docs/pricing.md） ---
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

    # --- chat 回改 ---
    op.add_column(
        "chat_conversations",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="qa"),
    )
    op.add_column("chat_messages", sa.Column("steps", JSONB(), nullable=True))

    # --- notes 回改：Copilot create_note 幂等去重的 content_hash（唯一索引） ---
    op.add_column("notes", sa.Column("content_hash", sa.String(length=64), nullable=True))
    op.create_index("ux_notes_content_hash", "notes", ["content_hash"], unique=True)

    # --- documents 回改：大文档嵌入确认开关 ---
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
    # 计费触发器/函数先拆（依赖 pricing_policy 表）
    op.execute("DROP TRIGGER IF EXISTS pricing_policy_audit ON pricing_policy")
    op.execute("DROP TRIGGER IF EXISTS pricing_policy_schedule_check ON pricing_policy")
    op.execute("DROP FUNCTION IF EXISTS log_pricing_change()")
    op.execute("DROP FUNCTION IF EXISTS check_pricing_schedule()")

    # 回改列
    op.drop_column("documents", "embedding_approved")
    op.drop_index("ux_notes_content_hash", table_name="notes")
    op.drop_column("notes", "content_hash")
    op.drop_column("chat_messages", "steps")
    op.drop_column("chat_conversations", "kind")

    # 计费表
    op.drop_table("copilot_llm_cost")
    op.drop_table("pricing_change_log")
    op.execute("ALTER TABLE pricing_policy DROP CONSTRAINT IF EXISTS pricing_policy_no_overlap")
    op.drop_index("ix_pricing_policy_vendor_model", table_name="pricing_policy")
    op.drop_table("pricing_policy")

    # 其余表（按依赖反序）
    op.drop_table("copilot_idempotency")
    op.drop_table("copilot_breakers")
    op.drop_index("ix_copilot_approvals_run_id", table_name="copilot_approvals")
    op.drop_table("copilot_approvals")
    op.drop_index("ix_copilot_llm_snapshots_created_at", table_name="copilot_llm_snapshots")
    op.drop_table("copilot_llm_snapshots")
    op.drop_table("copilot_daily_budget")
    op.drop_index("ix_copilot_events_run_id", table_name="copilot_events")
    op.drop_table("copilot_events")
    op.execute("DROP SEQUENCE IF EXISTS copilot_events_seq")

    op.execute("DROP INDEX IF EXISTS ux_copilot_memories_kind_entity")
    op.execute("DROP INDEX IF EXISTS ix_copilot_memories_tsv")
    op.execute("DROP INDEX IF EXISTS ix_copilot_memories_kind_entity")
    op.execute("DROP INDEX IF EXISTS ix_copilot_memories_kind")
    op.execute("DROP INDEX IF EXISTS ix_copilot_memories_embedding_hnsw")
    op.drop_table("copilot_memories")
