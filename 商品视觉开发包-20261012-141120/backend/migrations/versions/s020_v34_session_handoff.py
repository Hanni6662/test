"""Add versioned v3.4 first-form state and bounded stage-event receipts.

Revision ID: s020_v34_session_handoff
Revises: s019_h8_dify_reference_files
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s020_v34_session_handoff"
down_revision = "s019_h8_dify_reference_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.add_column(
        "business_sessions",
        sa.Column(
            "flow_version",
            sa.String(length=64),
            server_default="legacy",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "flow_version_check",
        "business_sessions",
        "flow_version IN ('legacy', 'visual-v3.4-single-selection')",
    )

    op.create_table(
        "chatflow_v34_session_state",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("business_session_id", uuid, nullable=False),
        sa.Column("flow_version", sa.String(length=64), nullable=False),
        sa.Column("dify_app_id", sa.String(length=160), nullable=False),
        sa.Column("dify_workflow_id", sa.String(length=160), nullable=False),
        sa.Column("form_node_id", sa.String(length=80), nullable=True),
        sa.Column("form_token", sa.Text(), nullable=True),
        sa.Column("form_token_digest", sa.LargeBinary(length=32), nullable=True),
        sa.Column("form_definition_digest", sa.LargeBinary(length=32), nullable=True),
        sa.Column("form_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "form_status",
            sa.String(length=24),
            server_default="awaiting_event",
            nullable=False,
        ),
        sa.Column("selection_submission_id", uuid, nullable=True),
        sa.Column("selection_input_digest", sa.LargeBinary(length=32), nullable=True),
        sa.Column("selection_inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "selection_status",
            sa.String(length=24),
            server_default="not_started",
            nullable=False,
        ),
        sa.Column("selection_evidence_kind", sa.String(length=24), nullable=True),
        sa.Column("selection_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safe_reason_code", sa.String(length=80), nullable=True),
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
            "flow_version = 'visual-v3.4-single-selection'", name="flow_version_check"
        ),
        sa.CheckConstraint(
            "form_status IN ('awaiting_event', 'ready', 'consumed', 'unknown', "
            "'rejected', 'expired')",
            name="form_status_check",
        ),
        sa.CheckConstraint(
            "(form_token IS NULL AND form_token_digest IS NULL AND "
            "form_definition_digest IS NULL AND form_expires_at IS NULL "
            "AND form_node_id IS NULL) OR "
            "(form_token IS NOT NULL AND form_token_digest IS NOT NULL AND "
            "octet_length(form_token_digest) = 32 AND form_definition_digest IS NOT NULL AND "
            "octet_length(form_definition_digest) = 32 AND form_expires_at IS NOT NULL AND "
            "form_node_id = 'form_0')",
            name="form_binding_fields_check",
        ),
        sa.CheckConstraint(
            "(form_status IN ('ready', 'consumed') AND form_token IS NOT NULL) OR "
            "form_status NOT IN ('ready', 'consumed')",
            name="form_ready_binding_check",
        ),
        sa.CheckConstraint(
            "selection_status IN ('not_started', 'pending', 'accepted', 'unknown', 'rejected', "
            "'cancelled', 'expired')",
            name="selection_status_check",
        ),
        sa.CheckConstraint(
            "(selection_status = 'not_started' AND selection_submission_id IS NULL AND "
            "selection_input_digest IS NULL AND selection_inputs IS NULL) OR "
            "(selection_status <> 'not_started' AND selection_submission_id IS NOT NULL AND "
            "selection_input_digest IS NOT NULL AND octet_length(selection_input_digest) = 32 AND "
            "selection_inputs IS NOT NULL)",
            name="selection_input_fields_check",
        ),
        sa.CheckConstraint(
            "(selection_status = 'accepted' AND selection_evidence_kind IN ('http_200', "
            "'selection_applied') AND selection_accepted_at IS NOT NULL) OR "
            "(selection_status <> 'accepted' AND selection_evidence_kind IS NULL "
            "AND selection_accepted_at IS NULL)",
            name="selection_evidence_check",
        ),
        sa.ForeignKeyConstraint(
            ["business_session_id"],
            ["business_sessions.id"],
            ondelete="CASCADE",
            name="fk_chatflow_v34_session_state_session",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chatflow_v34_session_state"),
        sa.UniqueConstraint("business_session_id", name="uq_chatflow_v34_session_state_session"),
    )
    op.create_index(
        "ix_chatflow_v34_session_state_form",
        "chatflow_v34_session_state",
        ["form_status", "form_expires_at"],
    )

    op.create_table(
        "chatflow_v34_stage_event_receipts",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("business_session_id", uuid, nullable=False),
        sa.Column("flow_version", sa.String(length=64), nullable=False),
        sa.Column("recommendation_id", uuid, nullable=False),
        sa.Column("recommendation_revision", sa.Integer(), nullable=False),
        sa.Column("dify_run_id", sa.String(length=320), nullable=False),
        sa.Column("node_id", sa.String(length=80), nullable=False),
        sa.Column("event_key", sa.String(length=160), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("event_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "disposition",
            sa.String(length=24),
            server_default="received_pending",
            nullable=False,
        ),
        sa.Column("safe_reason_code", sa.String(length=80), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "flow_version = 'visual-v3.4-single-selection'", name="flow_version_check"
        ),
        sa.CheckConstraint("octet_length(event_digest) = 32", name="event_digest_size_check"),
        sa.CheckConstraint(
            "event_type IN ('recommendation.ready', 'selection.applied', "
            "'pre_generation.incomplete', 'selection.cancelled', 'selection.expired', "
            "'workflow.failed')",
            name="event_type_check",
        ),
        sa.CheckConstraint(
            "disposition IN ('received_pending', 'accepted', 'rejected')",
            name="disposition_check",
        ),
        sa.CheckConstraint("recommendation_revision > 0", name="recommendation_revision_check"),
        sa.ForeignKeyConstraint(
            ["business_session_id"],
            ["business_sessions.id"],
            ondelete="CASCADE",
            name="fk_chatflow_v34_stage_receipts_session",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chatflow_v34_stage_event_receipts"),
        sa.UniqueConstraint(
            "business_session_id", "event_key", name="uq_chatflow_v34_stage_event_key"
        ),
    )
    op.create_index(
        "ix_chatflow_v34_stage_receipts_pending",
        "chatflow_v34_stage_event_receipts",
        ["business_session_id", "dify_run_id", "disposition"],
    )


def downgrade() -> None:
    raise RuntimeError("s020 is forward-only; v3.4 handoff records must not be discarded")
