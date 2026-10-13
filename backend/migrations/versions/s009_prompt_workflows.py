"""Persist mock prompt workflows and bind submission Actions.

Revision ID: s009_prompt_workflows
Revises: s008_mock_generation
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s009_prompt_workflows"
down_revision = "s008_mock_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())

    op.create_table(
        "task_prompt_workflows",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("workflow_run_id", uuid, nullable=False),
        sa.Column("workflow_revision", sa.Integer(), nullable=False),
        sa.Column("trusted_analysis_snapshot_id", uuid, nullable=False),
        sa.Column("trusted_analysis_snapshot_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default=sa.text("'pending_submission'"),
            nullable=False,
        ),
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
        sa.CheckConstraint("workflow_revision > 0", name="workflow_revision_positive_check"),
        sa.CheckConstraint(
            "octet_length(trusted_analysis_snapshot_digest) = 32",
            name="trusted_snapshot_digest_size_check",
        ),
        sa.CheckConstraint(
            "status IN ('pending_submission', 'submitted', 'reconciling', 'needs_input', "
            "'package_ready', 'failed')",
            name="status_check",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="RESTRICT", name="fk_task_prompt_workflows_task"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_prompt_workflows"),
        sa.UniqueConstraint(
            "task_id", "workflow_revision", name="uq_task_prompt_workflow_revision"
        ),
        sa.UniqueConstraint(
            "workflow_run_id", "workflow_revision", name="uq_task_prompt_workflow_run_revision"
        ),
        sa.UniqueConstraint(
            "workflow_run_id",
            "workflow_revision",
            "task_id",
            name="uq_task_prompt_workflow_run_revision_task",
        ),
        sa.UniqueConstraint("id", "task_id", name="uq_task_prompt_workflow_id_task"),
    )
    op.create_index(
        "ix_task_prompt_workflows_task_status",
        "task_prompt_workflows",
        ["task_id", "status"],
    )

    op.create_table(
        "task_prompt_submissions",
        sa.Column(
            "submission_id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("workflow_id", uuid, nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("workflow_run_id", uuid, nullable=False),
        sa.Column("workflow_revision", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("followup_round", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("content_digest_version", sa.SmallInteger(), nullable=False),
        sa.Column("content_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("private_payload", jsonb, nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default=sa.text("'prepared'"), nullable=False
        ),
        sa.Column("submit_outcome", sa.String(length=24), nullable=True),
        sa.Column("submit_result_digest_version", sa.SmallInteger(), nullable=True),
        sa.Column("submit_result_digest", sa.LargeBinary(length=32), nullable=True),
        sa.Column("provider_submission_ref", sa.String(length=512), nullable=True),
        sa.Column("provider_run_ref", sa.String(length=512), nullable=True),
        sa.Column("submit_reason_code", sa.String(length=80), nullable=True),
        sa.Column("callback_outcome", sa.String(length=24), nullable=True),
        sa.Column("callback_result_digest_version", sa.SmallInteger(), nullable=True),
        sa.Column("callback_result_digest", sa.LargeBinary(length=32), nullable=True),
        sa.Column("callback_reason_code", sa.String(length=80), nullable=True),
        sa.Column("callback_round", sa.SmallInteger(), nullable=True),
        sa.Column("callback_allowed_fields", jsonb, nullable=True),
        sa.Column("callback_locked_fields", jsonb, nullable=True),
        sa.Column("callback_valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("callback_received_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("workflow_revision > 0", name="workflow_revision_positive_check"),
        sa.CheckConstraint("followup_round BETWEEN 0 AND 2", name="followup_round_check"),
        sa.CheckConstraint(
            "content_digest_version > 0", name="content_digest_version_positive_check"
        ),
        sa.CheckConstraint("octet_length(content_digest) = 32", name="content_digest_size_check"),
        sa.CheckConstraint(
            "submit_result_digest IS NULL OR octet_length(submit_result_digest) = 32",
            name="submit_result_digest_size_check",
        ),
        sa.CheckConstraint(
            "callback_result_digest IS NULL OR octet_length(callback_result_digest) = 32",
            name="callback_result_digest_size_check",
        ),
        sa.CheckConstraint(
            "phase IN ('initial_preferences', 'followup_answer')", name="phase_check"
        ),
        sa.CheckConstraint(
            "(phase = 'initial_preferences' AND followup_round = 0) OR "
            "(phase = 'followup_answer' AND followup_round BETWEEN 1 AND 2)",
            name="phase_round_check",
        ),
        sa.CheckConstraint(
            "status IN ('prepared', 'accepted', 'rejected', 'unknown', 'callback_received', "
            "'package_ready', 'failed')",
            name="status_check",
        ),
        sa.CheckConstraint(
            "submit_outcome IS NULL OR submit_outcome IN "
            "('accepted', 'definitely_rejected', 'unknown')",
            name="submit_outcome_check",
        ),
        sa.CheckConstraint(
            "callback_outcome IS NULL OR callback_outcome IN "
            "('completed', 'needs_input', 'definitely_rejected')",
            name="callback_outcome_check",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="RESTRICT", name="fk_task_prompt_submissions_task"
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id", "task_id"],
            ["task_prompt_workflows.id", "task_prompt_workflows.task_id"],
            ondelete="RESTRICT",
            name="fk_task_prompt_submissions_workflow_task",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id", "workflow_revision", "task_id"],
            [
                "task_prompt_workflows.workflow_run_id",
                "task_prompt_workflows.workflow_revision",
                "task_prompt_workflows.task_id",
            ],
            ondelete="RESTRICT",
            name="fk_task_prompt_submissions_run_revision_task",
        ),
        sa.PrimaryKeyConstraint("submission_id", name="pk_task_prompt_submissions"),
        sa.UniqueConstraint(
            "workflow_id", "phase", "followup_round", name="uq_prompt_submission_step"
        ),
        sa.UniqueConstraint("submission_id", "task_id", name="uq_prompt_submission_id_task"),
    )
    op.create_index(
        "ix_task_prompt_submissions_task_created",
        "task_prompt_submissions",
        ["task_id", "created_at"],
    )

    op.create_table(
        "mock_prompt_submissions",
        sa.Column("submission_id", uuid, nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("workflow_run_id", uuid, nullable=False),
        sa.Column("workflow_revision", sa.Integer(), nullable=False),
        sa.Column("content_digest_version", sa.SmallInteger(), nullable=False),
        sa.Column("content_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("provider_submission_ref", sa.String(length=512), nullable=True),
        sa.Column("provider_run_ref", sa.String(length=512), nullable=True),
        sa.Column("result_package", jsonb, nullable=True),
        sa.Column("result_digest_version", sa.SmallInteger(), nullable=True),
        sa.Column("result_digest", sa.LargeBinary(length=32), nullable=True),
        sa.Column("safe_reason_code", sa.String(length=80), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("workflow_revision > 0", name="workflow_revision_positive_check"),
        sa.CheckConstraint(
            "content_digest_version > 0", name="content_digest_version_positive_check"
        ),
        sa.CheckConstraint("octet_length(content_digest) = 32", name="content_digest_size_check"),
        sa.CheckConstraint(
            "result_digest IS NULL OR octet_length(result_digest) = 32",
            name="result_digest_size_check",
        ),
        sa.CheckConstraint("outcome IN ('accepted', 'definitely_rejected')", name="outcome_check"),
        sa.CheckConstraint(
            "(outcome = 'accepted' AND provider_submission_ref IS NOT NULL "
            "AND result_package IS NOT NULL AND result_digest_version > 0 "
            "AND result_digest IS NOT NULL) OR "
            "(outcome = 'definitely_rejected' AND provider_submission_ref IS NULL "
            "AND result_package IS NULL AND result_digest_version IS NULL "
            "AND result_digest IS NULL)",
            name="outcome_fields_check",
        ),
        sa.PrimaryKeyConstraint("submission_id", name="pk_mock_prompt_submissions"),
        sa.UniqueConstraint(
            "provider_submission_ref", name="uq_mock_prompt_provider_submission_ref"
        ),
    )
    op.create_index("ix_mock_prompt_submissions_created", "mock_prompt_submissions", ["created_at"])

    op.create_table(
        "task_prompt_packages",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workflow_id", uuid, nullable=False),
        sa.Column("submission_id", uuid, nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("workflow_run_id", uuid, nullable=False),
        sa.Column("workflow_revision", sa.Integer(), nullable=False),
        sa.Column("trusted_analysis_snapshot_id", uuid, nullable=False),
        sa.Column("trusted_analysis_snapshot_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("chatflow_contract_version", sa.String(length=32), nullable=False),
        sa.Column("validator_version", sa.String(length=80), nullable=False),
        sa.Column("package_digest_version", sa.SmallInteger(), nullable=False),
        sa.Column("package_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("package", jsonb, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("workflow_revision > 0", name="workflow_revision_positive_check"),
        sa.CheckConstraint(
            "package_digest_version > 0", name="package_digest_version_positive_check"
        ),
        sa.CheckConstraint("octet_length(package_digest) = 32", name="package_digest_size_check"),
        sa.CheckConstraint(
            "octet_length(trusted_analysis_snapshot_digest) = 32",
            name="snapshot_digest_size_check",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="RESTRICT", name="fk_task_prompt_packages_task"
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id", "task_id"],
            ["task_prompt_workflows.id", "task_prompt_workflows.task_id"],
            ondelete="RESTRICT",
            name="fk_task_prompt_packages_workflow_task",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id", "task_id"],
            ["task_prompt_submissions.submission_id", "task_prompt_submissions.task_id"],
            ondelete="RESTRICT",
            name="fk_task_prompt_packages_submission_task",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id", "workflow_revision", "task_id"],
            [
                "task_prompt_workflows.workflow_run_id",
                "task_prompt_workflows.workflow_revision",
                "task_prompt_workflows.task_id",
            ],
            ondelete="RESTRICT",
            name="fk_task_prompt_packages_run_revision_task",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_prompt_packages"),
        sa.UniqueConstraint("workflow_id", name="uq_task_prompt_packages_workflow"),
        sa.UniqueConstraint(
            "task_id", "workflow_revision", name="uq_task_prompt_packages_revision"
        ),
        sa.UniqueConstraint("submission_id", name="uq_task_prompt_packages_submission"),
        sa.UniqueConstraint("id", "task_id", name="uq_task_prompt_package_id_task"),
    )
    op.create_index(
        "ix_task_prompt_packages_task_created",
        "task_prompt_packages",
        ["task_id", "created_at"],
    )

    op.create_table(
        "task_prompt_items",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("package_id", uuid, nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("task_item_id", uuid, nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("image_type", sa.String(length=80), nullable=False),
        sa.Column("design_prompt", sa.String(length=16000), nullable=False),
        sa.Column("identity_prompt", sa.String(length=3500), nullable=False),
        sa.Column("identity_anchor_asset_id", uuid, nullable=False),
        sa.Column("allowed_reference_aliases", jsonb, nullable=False),
        sa.Column("excluded_reference_aliases", jsonb, nullable=False),
        sa.Column("allowed_reference_asset_ids", jsonb, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("position >= 1", name="position_positive_check"),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="RESTRICT", name="fk_task_prompt_items_task"
        ),
        sa.ForeignKeyConstraint(
            ["package_id", "task_id"],
            ["task_prompt_packages.id", "task_prompt_packages.task_id"],
            ondelete="RESTRICT",
            name="fk_task_prompt_items_package_task",
        ),
        sa.ForeignKeyConstraint(
            ["task_item_id", "task_id"],
            ["task_items.id", "task_items.task_id"],
            ondelete="RESTRICT",
            name="fk_task_prompt_items_item_task",
        ),
        sa.ForeignKeyConstraint(
            ["identity_anchor_asset_id"],
            ["image_assets.id"],
            ondelete="RESTRICT",
            name="fk_task_prompt_items_identity_asset",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_prompt_items"),
        sa.UniqueConstraint("task_item_id", name="uq_task_prompt_items_task_item"),
        sa.UniqueConstraint("package_id", "position", name="uq_task_prompt_items_package_position"),
    )
    op.create_index("ix_task_prompt_items_package", "task_prompt_items", ["package_id"])

    op.add_column("task_actions", sa.Column("submission_id", uuid, nullable=True))
    op.drop_constraint("scope_association_check", "task_actions", type_="check")
    op.create_check_constraint(
        "scope_association_check",
        "task_actions",
        "(scope_kind = 'attempt' AND submission_id IS NULL AND attempt_id IS NOT NULL "
        "AND task_item_id IS NOT NULL AND scope_id = attempt_id) OR "
        "(scope_kind = 'submission' AND submission_id IS NOT NULL "
        "AND scope_id = submission_id AND attempt_id IS NULL AND task_item_id IS NULL)",
    )
    op.create_foreign_key(
        "fk_task_actions_submission_task",
        "task_actions",
        "task_prompt_submissions",
        ["submission_id", "task_id"],
        ["submission_id", "task_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_task_actions_submission_task", "task_actions", type_="foreignkey")
    op.drop_constraint("scope_association_check", "task_actions", type_="check")
    op.create_check_constraint(
        "scope_association_check",
        "task_actions",
        "(scope_kind = 'attempt' AND attempt_id IS NOT NULL AND task_item_id IS NOT NULL "
        "AND scope_id = attempt_id) OR "
        "(scope_kind = 'submission' AND attempt_id IS NULL AND task_item_id IS NULL)",
    )
    op.drop_column("task_actions", "submission_id")

    op.drop_index("ix_task_prompt_items_package", table_name="task_prompt_items")
    op.drop_table("task_prompt_items")
    op.drop_index("ix_task_prompt_packages_task_created", table_name="task_prompt_packages")
    op.drop_table("task_prompt_packages")
    op.drop_index("ix_mock_prompt_submissions_created", table_name="mock_prompt_submissions")
    op.drop_table("mock_prompt_submissions")
    op.drop_index("ix_task_prompt_submissions_task_created", table_name="task_prompt_submissions")
    op.drop_table("task_prompt_submissions")
    op.drop_index("ix_task_prompt_workflows_task_status", table_name="task_prompt_workflows")
    op.drop_table("task_prompt_workflows")
