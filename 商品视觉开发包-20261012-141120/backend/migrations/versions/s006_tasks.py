"""S6 task/input/idempotency schema."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s006_tasks"
down_revision = "s005_recommendations"
branch_labels = None
depends_on = None


def _clock_timestamp() -> sa.TextClause:
    return sa.text("clock_timestamp()")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "tasks",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_user_id", uuid, nullable=False),
        sa.Column("reference_set_id", uuid, nullable=False),
        sa.Column("recommendation_id", uuid, nullable=False),
        sa.Column("recommendation_revision", sa.Integer(), nullable=False),
        sa.Column("recommendation_snapshot_version", sa.String(120), nullable=False),
        sa.Column("schema_version", sa.SmallInteger(), server_default=sa.text("2"), nullable=False),
        sa.Column("workflow_version", sa.String(80), nullable=False),
        sa.Column("request_digest", sa.LargeBinary(32), nullable=False),
        sa.Column(
            "request_digest_version", sa.SmallInteger(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("request_snapshot", jsonb, nullable=False),
        sa.Column("resolved_preferences", jsonb, nullable=False),
        sa.Column("recommendation_snapshot", jsonb, nullable=False),
        sa.Column("reference_assets_snapshot", jsonb, nullable=False),
        sa.Column("category_snapshot", sa.String(100), nullable=False),
        sa.Column("image_types", jsonb, nullable=False),
        sa.Column("planned_count", sa.SmallInteger(), nullable=False),
        sa.Column(
            "billing_mode", sa.String(16), server_default=sa.text("'disabled'"), nullable=False
        ),
        sa.Column(
            "billing_policy_version",
            sa.String(80),
            server_default=sa.text("'disabled-v1'"),
            nullable=False,
        ),
        sa.Column("billing_snapshot", jsonb, nullable=False),
        sa.Column("status", sa.String(24), server_default=sa.text("'queued'"), nullable=False),
        sa.Column(
            "retention_days", sa.SmallInteger(), server_default=sa.text("60"), nullable=False
        ),
        sa.Column(
            "retention_policy_revision",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("terminal_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'prompting', 'needs_input', 'generating', 'reconciling', "
            "'succeeded', 'partially_succeeded', 'failed')",
            name="status_check",
        ),
        sa.CheckConstraint("schema_version = 2", name="schema_version_check"),
        sa.CheckConstraint("planned_count BETWEEN 1 AND 8", name="planned_count_check"),
        sa.CheckConstraint("billing_mode = 'disabled'", name="billing_mode_check"),
        sa.CheckConstraint("request_digest_version > 0", name="request_digest_version_check"),
        sa.CheckConstraint("retention_days > 0", name="retention_days_check"),
        sa.CheckConstraint("retention_policy_revision > 0", name="retention_policy_revision_check"),
        sa.CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        sa.CheckConstraint(
            "expires_at IS NULL OR terminal_at IS NOT NULL", name="expiration_terminal_check"
        ),
        sa.ForeignKeyConstraint(
            ["reference_set_id", "owner_user_id"],
            ["reference_sets.id", "reference_sets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_tasks_reference_owner",
        ),
        sa.ForeignKeyConstraint(
            ["recommendation_id", "owner_user_id"],
            ["recommendations.id", "recommendations.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_tasks_recommendation_owner",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="RESTRICT", name="fk_tasks_owner"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recommendation_id", name="uq_tasks_initial_recommendation"),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_tasks_id_owner"),
    )
    op.create_index(
        "ix_tasks_owner_status_created", "tasks", ["owner_user_id", "status", "created_at"]
    )
    op.create_index("ix_tasks_expiry", "tasks", ["expires_at"])

    op.create_table(
        "task_items",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("image_type", sa.String(80), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("image_id", uuid),
        sa.Column("preview_ready", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("error_code", sa.String(80)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint("position BETWEEN 1 AND 8", name="position_check"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'saving', 'reconciling', 'succeeded', 'failed')",
            name="status_check",
        ),
        sa.CheckConstraint(
            "(width IS NULL AND height IS NULL) OR (width > 0 AND height > 0)",
            name="dimensions_check",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="CASCADE", name="fk_task_items_task"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "image_type", name="uq_task_items_task_type"),
        sa.UniqueConstraint("task_id", "position", name="uq_task_items_task_position"),
        sa.UniqueConstraint("id", "task_id", name="uq_task_items_id_task"),
    )
    op.create_index(
        "ix_task_items_task_status_position", "task_items", ["task_id", "status", "position"]
    )

    op.create_table(
        "task_item_attempts",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("task_item_id", uuid, nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", uuid, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("channel_id", sa.String(100)),
        sa.Column("model_id", sa.String(100)),
        sa.Column("model_version", sa.String(100)),
        sa.Column("provider_job_id", sa.String(320)),
        sa.Column("provider_result_ref", sa.String(640)),
        sa.Column("failure_code", sa.String(80)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("next_action_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint("attempt_no > 0", name="attempt_no_check"),
        sa.CheckConstraint(
            "status IN ('prepared', 'submitting', 'submitted', 'polling', 'saving', "
            "'reconciling', 'completed', 'failed')",
            name="status_check",
        ),
        sa.ForeignKeyConstraint(
            ["task_item_id"], ["task_items.id"], ondelete="CASCADE", name="fk_attempts_task_item"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_item_id", "attempt_no", name="uq_attempts_item_number"),
        sa.UniqueConstraint("idempotency_key", name="uq_attempts_idempotency_key"),
    )
    op.create_index(
        "ix_task_item_attempts_status_next_action",
        "task_item_attempts",
        ["status", "next_action_at"],
    )

    op.create_table(
        "task_idempotency_records",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_user_id", uuid, nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("key_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("request_digest", sa.LargeBinary(32), nullable=False),
        sa.Column("digest_version", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("planned_count", sa.SmallInteger(), nullable=False),
        sa.Column("state", sa.String(16), server_default=sa.text("'active'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint("octet_length(key_hash) = 32", name="key_hash_size_check"),
        sa.CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        sa.CheckConstraint("digest_version > 0", name="digest_version_check"),
        sa.CheckConstraint("planned_count BETWEEN 1 AND 8", name="planned_count_check"),
        sa.CheckConstraint("state IN ('active', 'expired')", name="state_check"),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="RESTRICT", name="fk_task_idempotency_owner"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id", "operation", "key_hash", name="uq_task_idempotency_owner_operation_key"
        ),
    )
    op.create_index("ix_task_idempotency_task_id", "task_idempotency_records", ["task_id"])

    op.create_table(
        "task_recommendation_consumptions",
        sa.Column("recommendation_id", uuid, nullable=False),
        sa.Column("owner_user_id", uuid, nullable=False),
        sa.Column("request_digest", sa.LargeBinary(32), nullable=False),
        sa.Column("digest_version", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("planned_count", sa.SmallInteger(), nullable=False),
        sa.Column("state", sa.String(16), server_default=sa.text("'active'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        sa.CheckConstraint("digest_version > 0", name="digest_version_check"),
        sa.CheckConstraint("planned_count BETWEEN 1 AND 8", name="planned_count_check"),
        sa.CheckConstraint("state IN ('active', 'expired')", name="state_check"),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="RESTRICT", name="fk_task_consumption_owner"
        ),
        sa.PrimaryKeyConstraint("recommendation_id"),
    )
    op.create_index("ix_task_consumptions_task_id", "task_recommendation_consumptions", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_consumptions_task_id", table_name="task_recommendation_consumptions")
    op.drop_table("task_recommendation_consumptions")
    op.drop_index("ix_task_idempotency_task_id", table_name="task_idempotency_records")
    op.drop_table("task_idempotency_records")
    op.drop_index("ix_task_item_attempts_status_next_action", table_name="task_item_attempts")
    op.drop_table("task_item_attempts")
    op.drop_index("ix_task_items_task_status_position", table_name="task_items")
    op.drop_table("task_items")
    op.drop_index("ix_tasks_expiry", table_name="tasks")
    op.drop_index("ix_tasks_owner_status_created", table_name="tasks")
    op.drop_table("tasks")
