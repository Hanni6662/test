"""Add retention policy snapshots and durable cleanup coordination.

Revision ID: s011_retention_cleanup
Revises: s010_generation_runtime
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s011_retention_cleanup"
down_revision = "s010_generation_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())

    op.create_table(
        "retention_policies",
        sa.Column("id", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "task_retention_days", sa.SmallInteger(), server_default=sa.text("60"), nullable=False
        ),
        sa.Column(
            "reference_retention_days",
            sa.SmallInteger(),
            server_default=sa.text("60"),
            nullable=False,
        ),
        sa.Column(
            "recommendation_retention_days",
            sa.SmallInteger(),
            server_default=sa.text("60"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="singleton_check"),
        sa.CheckConstraint("task_retention_days BETWEEN 1 AND 3650", name="task_days_check"),
        sa.CheckConstraint(
            "reference_retention_days BETWEEN 1 AND 3650", name="reference_days_check"
        ),
        sa.CheckConstraint(
            "recommendation_retention_days BETWEEN 1 AND 3650",
            name="recommendation_days_check",
        ),
        sa.CheckConstraint("revision > 0", name="revision_check"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO retention_policies "
            "(id, task_retention_days, reference_retention_days, "
            "recommendation_retention_days, revision) VALUES (1, 60, 60, 60, 1)"
        )
    )

    op.add_column(
        "reference_sets",
        sa.Column(
            "retention_days", sa.SmallInteger(), server_default=sa.text("60"), nullable=False
        ),
    )
    op.add_column(
        "reference_sets",
        sa.Column(
            "retention_policy_revision", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
    )
    op.execute(
        sa.text(
            "UPDATE reference_sets SET retention_days = GREATEST(1, "
            "ROUND(EXTRACT(EPOCH FROM (expires_at - ready_at)) / 86400.0)::integer)::smallint "
            "WHERE status = 'ready' AND expires_at IS NOT NULL AND ready_at IS NOT NULL"
        )
    )
    op.create_check_constraint(
        "retention_days_check", "reference_sets", "retention_days BETWEEN 1 AND 3650"
    )
    op.create_check_constraint(
        "retention_policy_revision_check", "reference_sets", "retention_policy_revision > 0"
    )

    op.add_column(
        "recommendations",
        sa.Column(
            "retention_days", sa.SmallInteger(), server_default=sa.text("60"), nullable=False
        ),
    )
    op.add_column(
        "recommendations",
        sa.Column(
            "retention_policy_revision", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
    )
    op.execute(
        sa.text(
            "UPDATE recommendations SET retention_days = GREATEST(1, "
            "ROUND(EXTRACT(EPOCH FROM (data_expires_at - created_at)) / 86400.0)::integer)"
            "::smallint"
        )
    )
    op.create_check_constraint(
        "retention_days_check", "recommendations", "retention_days BETWEEN 1 AND 3650"
    )
    op.create_check_constraint(
        "retention_policy_revision_check", "recommendations", "retention_policy_revision > 0"
    )

    op.drop_constraint("status_check", "tasks", type_="check")
    op.create_check_constraint(
        "status_check",
        "tasks",
        "status IN ('queued', 'prompting', 'needs_input', 'generating', 'reconciling', "
        "'succeeded', 'partially_succeeded', 'failed', 'deleting')",
    )

    op.create_table(
        "cleanup_runs",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("trigger_kind", sa.String(length=16), nullable=False),
        sa.Column("trigger_key", sa.String(length=160), nullable=False),
        sa.Column("idempotency_key_hash", sa.LargeBinary(length=32), nullable=True),
        sa.Column("request_digest", sa.LargeBinary(length=32), nullable=True),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'queued'"), nullable=False
        ),
        sa.Column("counts", jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("catchup_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("catchup_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("orphan_scan_cursor", sa.String(length=1024), nullable=True),
        sa.Column("orphan_scan_lease_token", uuid, nullable=True),
        sa.Column("orphan_scan_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("orphan_scan_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "trigger_kind IN ('manual', 'scheduled', 'catchup')", name="trigger_kind_check"
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')", name="status_check"
        ),
        sa.CheckConstraint("policy_revision > 0", name="policy_revision_check"),
        sa.CheckConstraint(
            "idempotency_key_hash IS NULL OR octet_length(idempotency_key_hash) = 32",
            name="idempotency_key_hash_size_check",
        ),
        sa.CheckConstraint(
            "request_digest IS NULL OR octet_length(request_digest) = 32",
            name="request_digest_size_check",
        ),
        sa.CheckConstraint(
            "(trigger_kind = 'manual' AND idempotency_key_hash IS NOT NULL "
            "AND request_digest IS NOT NULL AND scheduled_for IS NULL) OR "
            "(trigger_kind = 'scheduled' AND idempotency_key_hash IS NULL "
            "AND request_digest IS NULL AND scheduled_for IS NOT NULL) OR "
            "(trigger_kind = 'catchup' AND idempotency_key_hash IS NULL "
            "AND request_digest IS NULL AND scheduled_for IS NULL)",
            name="trigger_fields_check",
        ),
        sa.CheckConstraint(
            "(catchup_from IS NULL AND catchup_to IS NULL) OR "
            "(catchup_from IS NOT NULL AND catchup_to IS NOT NULL AND catchup_to > catchup_from)",
            name="catchup_range_check",
        ),
        sa.CheckConstraint("jsonb_typeof(counts) = 'object'", name="counts_object_check"),
        sa.CheckConstraint(
            "orphan_scan_cursor IS NULL OR length(orphan_scan_cursor) > 0",
            name="orphan_scan_cursor_check",
        ),
        sa.CheckConstraint(
            "(orphan_scan_lease_token IS NULL AND orphan_scan_lease_until IS NULL) OR "
            "(orphan_scan_lease_token IS NOT NULL AND orphan_scan_lease_until IS NOT NULL)",
            name="orphan_scan_lease_consistency_check",
        ),
        sa.CheckConstraint(
            "orphan_scan_completed_at IS NULL OR "
            "(orphan_scan_lease_token IS NULL AND orphan_scan_lease_until IS NULL)",
            name="orphan_scan_completion_lease_check",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trigger_key", name="uq_cleanup_runs_trigger_key"),
        sa.UniqueConstraint("idempotency_key_hash", name="uq_cleanup_runs_idempotency_hash"),
    )
    op.create_index("ix_cleanup_runs_status_created", "cleanup_runs", ["status", "created_at"])
    op.create_index(
        "ix_cleanup_runs_orphan_scan_claim",
        "cleanup_runs",
        ["status", "orphan_scan_completed_at", "orphan_scan_lease_until"],
    )

    op.create_table(
        "cleanup_jobs",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("aggregate_kind", sa.String(length=24), nullable=False),
        sa.Column("aggregate_id", uuid, nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("lease_token", uuid, nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
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
        sa.CheckConstraint(
            "aggregate_kind IN ('task', 'recommendation', 'reference_set', "
            "'upload_group', 'orphan')",
            name="aggregate_kind_check",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'retry', 'completed', 'blocked')",
            name="status_check",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="attempt_count_check"),
        sa.CheckConstraint(
            "(status = 'claimed' AND lease_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status <> 'claimed' AND lease_token IS NULL AND lease_until IS NULL)",
            name="claim_lease_consistency_check",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["cleanup_runs.id"], ondelete="CASCADE", name="fk_cleanup_jobs_run"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "aggregate_kind", "aggregate_id", name="uq_cleanup_jobs_run_aggregate"
        ),
    )
    op.create_index(
        "ix_cleanup_jobs_claim", "cleanup_jobs", ["status", "next_retry_at", "created_at"]
    )
    op.create_index("ix_cleanup_jobs_run_status", "cleanup_jobs", ["run_id", "status"])

    op.create_table(
        "cleanup_targets",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("job_id", uuid, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("entry_kind", sa.String(length=16), nullable=False),
        sa.Column("relative_locator", sa.String(length=1024), nullable=True),
        sa.Column("object_key", sa.String(length=512), nullable=True),
        sa.Column("source_kind", sa.String(length=32), nullable=True),
        sa.Column("source_id", uuid, nullable=True),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
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
        sa.CheckConstraint("sequence > 0", name="sequence_check"),
        sa.CheckConstraint(
            "entry_kind IN ('object', 'staging', 'sidecar')", name="entry_kind_check"
        ),
        sa.CheckConstraint("status IN ('pending', 'deleted', 'failed')", name="status_check"),
        sa.CheckConstraint("attempt_count >= 0", name="attempt_count_check"),
        sa.CheckConstraint(
            "(source_kind IS NULL AND source_id IS NULL) OR "
            "(source_kind IS NOT NULL AND source_id IS NOT NULL)",
            name="source_pair_check",
        ),
        sa.CheckConstraint(
            "(status IN ('pending', 'failed') AND relative_locator IS NOT NULL) OR "
            "(status = 'deleted' AND relative_locator IS NULL AND object_key IS NULL)",
            name="locator_state_check",
        ),
        sa.CheckConstraint(
            "entry_kind <> 'staging' OR object_key IS NULL", name="staging_object_key_check"
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["cleanup_jobs.id"], ondelete="CASCADE", name="fk_cleanup_targets_job"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id", "entry_kind", "relative_locator", name="uq_cleanup_targets_locator"
        ),
    )
    op.create_index(
        "ix_cleanup_targets_job_status", "cleanup_targets", ["job_id", "status", "sequence"]
    )
    op.create_index("ix_cleanup_targets_source", "cleanup_targets", ["source_kind", "source_id"])


def downgrade() -> None:
    op.drop_index("ix_cleanup_targets_source", table_name="cleanup_targets")
    op.drop_index("ix_cleanup_targets_job_status", table_name="cleanup_targets")
    op.drop_table("cleanup_targets")
    op.drop_index("ix_cleanup_jobs_run_status", table_name="cleanup_jobs")
    op.drop_index("ix_cleanup_jobs_claim", table_name="cleanup_jobs")
    op.drop_table("cleanup_jobs")
    op.drop_index("ix_cleanup_runs_orphan_scan_claim", table_name="cleanup_runs")
    op.drop_index("ix_cleanup_runs_status_created", table_name="cleanup_runs")
    op.drop_table("cleanup_runs")

    op.drop_constraint("status_check", "tasks", type_="check")
    op.create_check_constraint(
        "status_check",
        "tasks",
        "status IN ('queued', 'prompting', 'needs_input', 'generating', 'reconciling', "
        "'succeeded', 'partially_succeeded', 'failed')",
    )
    op.drop_constraint("retention_policy_revision_check", "recommendations", type_="check")
    op.drop_constraint("retention_days_check", "recommendations", type_="check")
    op.drop_column("recommendations", "retention_policy_revision")
    op.drop_column("recommendations", "retention_days")
    op.drop_constraint("retention_policy_revision_check", "reference_sets", type_="check")
    op.drop_constraint("retention_days_check", "reference_sets", type_="check")
    op.drop_column("reference_sets", "retention_policy_revision")
    op.drop_column("reference_sets", "retention_days")
    op.drop_table("retention_policies")
