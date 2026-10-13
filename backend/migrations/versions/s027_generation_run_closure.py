"""Observe generated runs without redefining transport or image success.

Revision ID: s027_generation_run_closure
Revises: s026_v34_ready0_run_end_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "s027_generation_run_closure"
down_revision = "s026_v34_ready0_run_end_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chatflow_generation_run_closures",
        sa.Column("generation_request_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("business_session_id", sa.Uuid(), nullable=False),
        sa.Column("dify_run_id", sa.String(320), nullable=False),
        sa.Column("dify_app_id", sa.String(160), nullable=False),
        sa.Column("dify_workflow_id", sa.String(160), nullable=False),
        sa.Column("state", sa.String(48), nullable=False),
        sa.Column("workflow_status", sa.String(32)),
        sa.Column("workflow_error_code", sa.String(80)),
        sa.Column("observation_digest", sa.LargeBinary(32)),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("task_status", sa.String(24), nullable=False),
        sa.Column("delivery_status", sa.String(24), nullable=False),
        sa.Column("confirmed_count", sa.Integer(), nullable=False),
        sa.Column("query_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(80)),
        sa.Column("next_observation_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.PrimaryKeyConstraint("generation_request_id", name="pk_generation_run_closure"),
        sa.ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="CASCADE",
            name="fk_generation_closure_request_task",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "business_session_id"],
            ["tasks.id", "tasks.business_session_id"],
            ondelete="CASCADE",
            name="fk_generation_closure_task_session",
        ),
        sa.UniqueConstraint("business_session_id", "dify_run_id", name="uq_generation_closure_run"),
        sa.CheckConstraint("query_count BETWEEN 0 AND 32", name="query_count_check"),
        sa.CheckConstraint(
            "state IN ('pending', 'unknown', 'completed', 'completed_with_execution_warning')",
            name="state_check",
        ),
        sa.CheckConstraint(
            "workflow_status IS NULL OR workflow_status IN "
            "('running', 'paused', 'succeeded', 'partial-succeeded', 'failed', 'stopped')",
            name="workflow_status_check",
        ),
        sa.CheckConstraint(
            "delivery_status IN ('pending', 'confirmed', 'unresolved', 'not_applicable')",
            name="delivery_status_check",
        ),
        sa.CheckConstraint(
            "observation_digest IS NULL OR octet_length(observation_digest) = 32",
            name="digest_size_check",
        ),
    )
    op.create_index(
        "ix_generation_closure_due",
        "chatflow_generation_run_closures",
        ["state", "next_observation_at"],
    )


def downgrade() -> None:
    op.drop_table("chatflow_generation_run_closures")
