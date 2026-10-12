"""Draft outbox schema for integration onto the then-current Alembic head.

This file is deliberately outside ``migrations/versions``. The integration owner
must choose ``down_revision`` from the final shared migration chain before use.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s004_outbox_draft"
down_revision = None
branch_labels = None
depends_on = None


def _clock_timestamp() -> sa.TextClause:
    return sa.text("clock_timestamp()")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "outbox_events",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("aggregate_id", uuid, nullable=False),
        sa.Column("operation_version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.Column("claim_token", uuid),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error_code", sa.String(80)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint("operation_version > 0", name="operation_version_positive_check"),
        sa.CheckConstraint("schema_version > 0", name="schema_version_positive_check"),
        sa.CheckConstraint("attempt_count >= 0", name="attempt_nonnegative_check"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object_check"),
        sa.CheckConstraint(
            "status <> 'claimed' OR attempt_count > 0", name="claim_attempt_positive_check"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'published', 'dead')", name="status_check"
        ),
        sa.CheckConstraint(
            "(status = 'claimed' AND claim_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status <> 'claimed' AND claim_token IS NULL AND lease_until IS NULL)",
            name="claim_lease_consistency_check",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_type", "aggregate_id", "operation_version", name="uq_outbox_business_action"
        ),
    )
    op.create_index(
        "ix_outbox_events_status_available",
        "outbox_events",
        ["status", "available_at", "created_at"],
    )
    op.create_index("ix_outbox_events_lease_until", "outbox_events", ["lease_until"])

    op.create_table(
        "outbox_receipts",
        sa.Column("event_id", uuid, nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.Column("claim_token", uuid),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(80)),
        sa.CheckConstraint("attempt_count >= 0", name="attempt_nonnegative_check"),
        sa.CheckConstraint(
            "status <> 'processing' OR attempt_count > 0", name="processing_attempt_positive_check"
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL) OR "
            "(status <> 'completed' AND completed_at IS NULL)",
            name="completed_timestamp_consistency_check",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'dead')", name="status_check"
        ),
        sa.CheckConstraint(
            "(status = 'processing' AND claim_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status <> 'processing' AND claim_token IS NULL AND lease_until IS NULL)",
            name="claim_lease_consistency_check",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["outbox_events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_outbox_receipts_status_available", "outbox_receipts", ["status", "available_at"]
    )
    op.create_index("ix_outbox_receipts_lease_until", "outbox_receipts", ["lease_until"])

    op.create_table(
        "outbox_demo_effects",
        sa.Column("event_id", uuid, nullable=False),
        sa.Column("aggregate_id", uuid, nullable=False),
        sa.Column("operation_version", sa.Integer(), nullable=False),
        sa.Column("observed_schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["event_id"], ["outbox_events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_outbox_demo_effects_aggregate", "outbox_demo_effects", ["aggregate_id"]
    )

    op.create_table(
        "outbox_rejected_messages",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("event_id", uuid),
        sa.Column("message_fingerprint", sa.LargeBinary(length=32), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(message_fingerprint) = 32", name="fingerprint_size_check"
        ),
        sa.CheckConstraint("occurrence_count > 0", name="occurrence_positive_check"),
        sa.ForeignKeyConstraint(["event_id"], ["outbox_events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_fingerprint", name="uq_outbox_rejected_fingerprint"),
    )
    op.create_index(
        "ix_outbox_rejected_event_received",
        "outbox_rejected_messages",
        ["event_id", "received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_rejected_event_received", table_name="outbox_rejected_messages")
    op.drop_table("outbox_rejected_messages")
    op.drop_index("ix_outbox_demo_effects_aggregate", table_name="outbox_demo_effects")
    op.drop_table("outbox_demo_effects")
    op.drop_index("ix_outbox_receipts_lease_until", table_name="outbox_receipts")
    op.drop_index("ix_outbox_receipts_status_available", table_name="outbox_receipts")
    op.drop_table("outbox_receipts")
    op.drop_index("ix_outbox_events_lease_until", table_name="outbox_events")
    op.drop_index("ix_outbox_events_status_available", table_name="outbox_events")
    op.drop_table("outbox_events")
