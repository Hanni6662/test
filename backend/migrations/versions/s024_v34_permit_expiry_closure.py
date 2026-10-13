"""Persist v3.4 permit expiry evidence and isolated Action sequence spaces.

Revision ID: s024_v34_permit_expiry_closure
Revises: s023_flow_version_lock
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s024_v34_permit_expiry_closure"
down_revision = "s023_flow_version_lock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("status_check", "chatflow_generation_tracking", type_="check")
    op.create_check_constraint(
        "status_check",
        "chatflow_generation_tracking",
        "status IN ('awaiting_batch', 'queued', 'running', 'submission_unknown', "
        "'completed', 'partial_failed', 'failed', 'rejected_before_acceptance')",
    )
    op.add_column(
        "task_actions",
        sa.Column(
            "sequence_namespace",
            sa.String(length=80),
            server_default=sa.text("'default'"),
            nullable=False,
        ),
    )
    op.drop_constraint("uq_task_actions_scope_sequence", "task_actions", type_="unique")
    op.create_check_constraint(
        "sequence_namespace_check",
        "task_actions",
        "length(sequence_namespace) BETWEEN 1 AND 80",
    )
    op.create_unique_constraint(
        "uq_task_actions_scope_namespace_sequence",
        "task_actions",
        ["scope_kind", "scope_id", "sequence_namespace", "sequence"],
    )

    op.add_column(
        "chatflow_generation_requests",
        sa.Column("service_authority_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "chatflow_generation_permits",
        sa.Column("service_authority_id", sa.Uuid(), nullable=True),
    )

    op.create_table(
        "chatflow_generation_acceptance_evidence",
        sa.Column("generation_request_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("service_authority_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("batch_id", sa.String(length=128), nullable=True),
        sa.Column("permit_id", sa.Uuid(), nullable=False),
        sa.Column("permit_version", sa.SmallInteger(), nullable=False),
        sa.Column("permit_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("service_request_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("evidence_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("receipt_digest", sa.LargeBinary(length=32), nullable=True),
        sa.Column("receipt_id", sa.String(length=36), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("outcome IN ('accepted', 'rejected_expired')", name="outcome_check"),
        sa.CheckConstraint("octet_length(evidence_digest) = 32", name="evidence_digest_size_check"),
        sa.CheckConstraint(
            "octet_length(service_request_digest) = 32",
            name="service_request_digest_size_check",
        ),
        sa.CheckConstraint("permit_version = 2", name="permit_version_check"),
        sa.CheckConstraint(
            "(outcome = 'accepted' AND batch_id IS NOT NULL AND receipt_digest IS NULL "
            "AND receipt_id IS NULL AND closed_at IS NULL) OR "
            "(outcome = 'rejected_expired' AND batch_id IS NULL "
            "AND receipt_digest IS NOT NULL AND receipt_id IS NOT NULL)",
            name="evidence_shape_check",
        ),
        sa.ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_acceptance_request_task",
        ),
        sa.PrimaryKeyConstraint("generation_request_id"),
        sa.UniqueConstraint("batch_id", name="uq_chatflow_generation_acceptance_batch"),
    )
    op.create_index(
        "ix_chatflow_generation_acceptance_task",
        "chatflow_generation_acceptance_evidence",
        ["task_id", "created_at"],
    )

    op.create_table(
        "chatflow_generation_expiry_assessments",
        sa.Column("generation_request_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=24),
            server_default=sa.text("'scheduled'"),
            nullable=False,
        ),
        sa.Column("query_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('scheduled', 'active', 'accepted', 'rejected_expired', 'unknown')",
            name="state_check",
        ),
        sa.CheckConstraint("query_count BETWEEN 0 AND 3", name="query_count_check"),
        sa.ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_expiry_request_task",
        ),
        sa.PrimaryKeyConstraint("generation_request_id"),
    )
    op.create_index(
        "ix_chatflow_generation_expiry_due",
        "chatflow_generation_expiry_assessments",
        ["state", "next_action_at"],
    )

    op.create_table(
        "chatflow_generation_accepted_batch_followups",
        sa.Column("generation_request_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.String(length=128), nullable=False),
        sa.Column("service_authority_id", sa.Uuid(), nullable=False),
        sa.Column("acceptance_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "state",
            sa.String(length=24),
            server_default=sa.text("'scheduled'"),
            nullable=False,
        ),
        sa.Column("query_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "state IN ('scheduled', 'active', 'awaiting_delivery', 'complete', 'unknown')",
            name="state_check",
        ),
        sa.CheckConstraint("query_count BETWEEN 0 AND 5", name="query_count_check"),
        sa.CheckConstraint(
            "octet_length(acceptance_digest) = 32", name="acceptance_digest_size_check"
        ),
        sa.CheckConstraint(
            "(state = 'complete' AND resolved_at IS NOT NULL AND next_action_at IS NULL) OR "
            "(state <> 'complete' AND resolved_at IS NULL)",
            name="resolution_timestamp_check",
        ),
        sa.ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_followup_request_task",
        ),
        sa.PrimaryKeyConstraint("generation_request_id"),
        sa.UniqueConstraint("batch_id", name="uq_chatflow_generation_followup_batch"),
    )
    op.create_index(
        "ix_chatflow_generation_followup_due",
        "chatflow_generation_accepted_batch_followups",
        ["state", "next_action_at"],
    )


def downgrade() -> None:
    raise RuntimeError("s024 is forward-only; permit expiry evidence must be preserved")
