"""Add durable single-step task actions.

Revision ID: s007_task_actions
Revises: s006_tasks
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s007_task_actions"
down_revision = "s006_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "task_actions",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("task_item_id", uuid, nullable=True),
        sa.Column("attempt_id", uuid, nullable=True),
        sa.Column("scope_kind", sa.String(16), nullable=False),
        sa.Column("scope_id", uuid, nullable=False),
        sa.Column("action_kind", sa.String(80), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status", sa.String(16), server_default=sa.text("'scheduled'"), nullable=False
        ),
        sa.Column("claim_token", uuid, nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "input_digest_version", sa.SmallInteger(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("input_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("outbox_event_id", uuid, nullable=False),
        sa.Column("last_outcome", sa.String(24), nullable=True),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("sequence > 0", name="sequence_positive_check"),
        sa.CheckConstraint("input_digest_version > 0", name="input_digest_version_check"),
        sa.CheckConstraint("octet_length(input_digest) = 32", name="input_digest_size_check"),
        sa.CheckConstraint("claim_count >= 0", name="claim_count_nonnegative_check"),
        sa.CheckConstraint("length(action_kind) BETWEEN 1 AND 80", name="action_kind_size_check"),
        sa.CheckConstraint("scope_kind IN ('submission', 'attempt')", name="scope_kind_check"),
        sa.CheckConstraint(
            "(scope_kind = 'attempt' AND attempt_id IS NOT NULL AND task_item_id IS NOT NULL "
            "AND scope_id = attempt_id) OR "
            "(scope_kind = 'submission' AND attempt_id IS NULL AND task_item_id IS NULL)",
            name="scope_association_check",
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'claimed', 'completed', 'rejected', 'unknown', 'cancelled')",
            name="status_check",
        ),
        sa.CheckConstraint(
            "(status = 'claimed' AND claim_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status <> 'claimed' AND claim_token IS NULL AND lease_until IS NULL)",
            name="claim_lease_consistency_check",
        ),
        sa.CheckConstraint(
            "(status IN ('scheduled', 'claimed') AND completed_at IS NULL) OR "
            "(status IN ('completed', 'rejected', 'unknown', 'cancelled') "
            "AND completed_at IS NOT NULL)",
            name="completion_timestamp_consistency_check",
        ),
        sa.CheckConstraint(
            "last_outcome IS NULL OR last_outcome IN "
            "('accepted', 'definitely_rejected', 'unknown', 'safe_retry')",
            name="last_outcome_check",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="RESTRICT", name="fk_task_actions_task"
        ),
        sa.ForeignKeyConstraint(
            ["task_item_id", "task_id"],
            ["task_items.id", "task_items.task_id"],
            ondelete="RESTRICT",
            name="fk_task_actions_item_task",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["task_item_attempts.id"],
            ondelete="RESTRICT",
            name="fk_task_actions_attempt",
        ),
        sa.ForeignKeyConstraint(
            ["outbox_event_id"],
            ["outbox_events.id"],
            ondelete="RESTRICT",
            name="fk_task_actions_outbox_event",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_actions"),
        sa.UniqueConstraint(
            "scope_kind", "scope_id", "sequence", name="uq_task_actions_scope_sequence"
        ),
        sa.UniqueConstraint("outbox_event_id", name="uq_task_actions_outbox_event"),
    )
    op.create_index(
        "ix_task_actions_status_due", "task_actions", ["status", "due_at", "created_at"]
    )
    op.create_index(
        "ix_task_actions_status_lease", "task_actions", ["status", "lease_until"]
    )
    op.create_index(
        "ix_task_actions_task_created", "task_actions", ["task_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_task_actions_task_created", table_name="task_actions")
    op.drop_index("ix_task_actions_status_lease", table_name="task_actions")
    op.drop_index("ix_task_actions_status_due", table_name="task_actions")
    op.drop_table("task_actions")
