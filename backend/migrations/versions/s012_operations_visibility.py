"""Add operations loop evidence and idempotent recovery audit records.

Revision ID: s012_operations_visibility
Revises: s011_retention_cleanup
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s012_operations_visibility"
down_revision = "s011_retention_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())

    op.create_table(
        "operations_loop_heartbeats",
        sa.Column("component", sa.String(length=24), nullable=False),
        sa.Column("instance_id", uuid, nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "last_heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("last_iteration_started_at", sa.DateTime(timezone=True)),
        sa.Column("last_iteration_completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_successful_scan_at", sa.DateTime(timezone=True)),
        sa.Column("last_outbox_scan_at", sa.DateTime(timezone=True)),
        sa.Column("last_cleanup_scan_at", sa.DateTime(timezone=True)),
        sa.Column("last_broker_confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("last_broker_error_at", sa.DateTime(timezone=True)),
        sa.Column("iteration_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "successful_iteration_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "failed_iteration_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_safe_error_code", sa.String(length=80)),
        sa.Column(
            "registration_state", jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("last_progress", jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.CheckConstraint("component IN ('dispatcher', 'scheduler')", name="component_check"),
        sa.CheckConstraint("iteration_count >= 0", name="iteration_count_check"),
        sa.CheckConstraint("successful_iteration_count >= 0", name="success_count_check"),
        sa.CheckConstraint("failed_iteration_count >= 0", name="failure_count_check"),
        sa.CheckConstraint(
            "last_safe_error_code IS NULL OR last_safe_error_code ~ '^[A-Z][A-Z0-9_]{0,79}$'",
            name="safe_error_code_check",
        ),
        sa.PrimaryKeyConstraint("component"),
    )

    op.create_table(
        "operations_recovery_audits",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("action_id", uuid, nullable=False),
        sa.Column("operation_kind", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("request_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("result_code", sa.String(length=80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "operation_kind IN ('expire_followup', 'expedite_generation_save')",
            name="operation_kind_check",
        ),
        sa.CheckConstraint(
            "octet_length(idempotency_key_hash) = 32", name="idempotency_hash_size_check"
        ),
        sa.CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        sa.CheckConstraint("result_code ~ '^[A-Z][A-Z0-9_]{0,79}$'", name="result_code_check"),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="CASCADE", name="fk_operations_audit_task"
        ),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["task_actions.id"],
            ondelete="CASCADE",
            name="fk_operations_audit_action",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key_hash", name="uq_operations_recovery_idempotency"),
    )
    op.create_index(
        "ix_operations_recovery_audits_task_created",
        "operations_recovery_audits",
        ["task_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_operations_recovery_audits_task_created", table_name="operations_recovery_audits"
    )
    op.drop_table("operations_recovery_audits")
    op.drop_table("operations_loop_heartbeats")
