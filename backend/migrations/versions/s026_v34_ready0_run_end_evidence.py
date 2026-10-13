"""Persist trusted V3.4 workflow end evidence without mislabeling it as a callback.

Revision ID: s026_v34_ready0_run_end_evidence
Revises: s025_tracking_state_size
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "s026_v34_ready0_run_end_evidence"
down_revision = "s025_tracking_state_size"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chatflow_v34_run_end_evidence",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("business_session_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("task_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("recommendation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("recommendation_revision", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("reference_set_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("flow_version", sa.String(length=64), nullable=False),
        sa.Column("dify_app_id", sa.String(length=160), nullable=False),
        sa.Column("dify_published_workflow_id", sa.String(length=160), nullable=False),
        sa.Column("dify_run_id", sa.String(length=320), nullable=False),
        sa.Column("selection_submission_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("selection_input_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("terminal_event", sa.String(length=40), nullable=False),
        sa.Column("terminal_status", sa.String(length=32), nullable=False),
        sa.Column("event_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("business_reason_code", sa.String(length=80), nullable=False),
        sa.Column("closure_outcome", sa.String(length=40), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "flow_version = 'visual-v3.4-single-selection'", name="flow_version_check"
        ),
        sa.CheckConstraint(
            "terminal_event = 'workflow_finished' AND "
            "terminal_status IN ('partial-succeeded', 'succeeded')",
            name="terminal_event_status_check",
        ),
        sa.CheckConstraint(
            "business_reason_code = 'V34_WORKFLOW_ENDED_WITHOUT_GENERATION'",
            name="business_reason_check",
        ),
        sa.CheckConstraint(
            "closure_outcome = 'failed_without_generation'", name="closure_outcome_check"
        ),
        sa.CheckConstraint("recommendation_revision > 0", name="recommendation_revision_check"),
        sa.CheckConstraint(
            "octet_length(selection_input_digest) = 32", name="selection_digest_size_check"
        ),
        sa.CheckConstraint("octet_length(event_digest) = 32", name="event_digest_size_check"),
        sa.ForeignKeyConstraint(
            ["business_session_id"],
            ["business_sessions.id"],
            ondelete="CASCADE",
            name="fk_chatflow_v34_run_end_session",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "business_session_id"],
            ["tasks.id", "tasks.business_session_id"],
            ondelete="CASCADE",
            name="fk_chatflow_v34_run_end_task_session",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chatflow_v34_run_end_evidence"),
        sa.UniqueConstraint(
            "business_session_id", "dify_run_id", name="uq_chatflow_v34_run_end_session_run"
        ),
        sa.UniqueConstraint("task_id", name="uq_chatflow_v34_run_end_task"),
    )
    op.create_index(
        "ix_chatflow_v34_run_end_observed",
        "chatflow_v34_run_end_evidence",
        ["observed_at", "task_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_chatflow_v34_run_end_observed", table_name="chatflow_v34_run_end_evidence")
    op.drop_table("chatflow_v34_run_end_evidence")
