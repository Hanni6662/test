"""Add the pre-Task Chatflow handoff root and business-session Actions.

Revision ID: s013_chatflow_handoff
Revises: s012_operations_visibility
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s013_chatflow_handoff"
down_revision = "s012_operations_visibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())

    op.create_table(
        "business_sessions",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_user_id", uuid, nullable=False),
        sa.Column("reference_set_id", uuid, nullable=False),
        sa.Column("idempotency_key_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("request_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("digest_version", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("reference_asset_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("reference_assets_snapshot", jsonb, nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'starting'"), nullable=False
        ),
        sa.Column(
            "start_outcome",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("dify_user_id", sa.String(length=160), nullable=False),
        sa.Column("dify_run_id", sa.String(length=320)),
        sa.Column("run_revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "retention_days", sa.SmallInteger(), server_default=sa.text("60"), nullable=False
        ),
        sa.Column(
            "retention_policy_revision", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
            "status IN ('starting', 'awaiting_recommendation', 'recommendation_ready', "
            "'task_created', 'reconciling', 'failed', 'expired')",
            name="status_check",
        ),
        sa.CheckConstraint(
            "start_outcome IN ('pending', 'accepted', 'unknown', 'rejected')",
            name="start_outcome_check",
        ),
        sa.CheckConstraint("octet_length(idempotency_key_hash) = 32", name="key_hash_size_check"),
        sa.CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        sa.CheckConstraint(
            "octet_length(reference_asset_digest) = 32", name="reference_digest_size_check"
        ),
        sa.CheckConstraint("digest_version > 0", name="digest_version_check"),
        sa.CheckConstraint("run_revision > 0", name="run_revision_check"),
        sa.CheckConstraint("retention_days > 0", name="retention_days_check"),
        sa.CheckConstraint("retention_policy_revision > 0", name="retention_policy_revision_check"),
        sa.ForeignKeyConstraint(
            ["reference_set_id", "owner_user_id"],
            ["reference_sets.id", "reference_sets.owner_user_id"],
            ondelete="CASCADE",
            name="fk_business_sessions_reference_owner",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="RESTRICT", name="fk_business_sessions_owner"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_business_sessions"),
        sa.UniqueConstraint(
            "owner_user_id", "idempotency_key_hash", name="uq_business_session_key"
        ),
        sa.UniqueConstraint(
            "id", "owner_user_id", "reference_set_id", name="uq_business_session_owner_ref"
        ),
    )
    op.create_index(
        "ix_business_sessions_owner_status",
        "business_sessions",
        ["owner_user_id", "status", "created_at"],
    )
    op.create_index(
        "ix_business_sessions_reference", "business_sessions", ["reference_set_id", "created_at"]
    )
    op.create_index("ix_business_sessions_expiry", "business_sessions", ["expires_at"])

    op.add_column(
        "recommendations",
        sa.Column("business_session_id", uuid, nullable=True),
    )
    op.add_column(
        "recommendations",
        sa.Column(
            "source_kind",
            sa.String(length=24),
            server_default=sa.text("'synthetic_mock'"),
            nullable=False,
        ),
    )
    op.add_column("recommendations", sa.Column("dify_run_id", sa.String(length=320), nullable=True))
    op.add_column("recommendations", sa.Column("callback_event_id", uuid, nullable=True))
    op.add_column(
        "recommendations", sa.Column("callback_digest_version", sa.SmallInteger(), nullable=True)
    )
    op.add_column(
        "recommendations", sa.Column("callback_digest", sa.LargeBinary(length=32), nullable=True)
    )
    op.create_check_constraint(
        "source_kind_check", "recommendations", "source_kind IN ('synthetic_mock', 'dify_chatflow')"
    )
    op.create_check_constraint(
        "source_association_check",
        "recommendations",
        "(source_kind = 'synthetic_mock' AND business_session_id IS NULL) OR "
        "(source_kind = 'dify_chatflow' AND business_session_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "callback_receipt_check",
        "recommendations",
        "(callback_event_id IS NULL AND callback_digest IS NULL) OR "
        "(callback_event_id IS NOT NULL AND callback_digest IS NOT NULL "
        "AND callback_digest_version > 0 AND octet_length(callback_digest) = 32)",
    )
    op.create_foreign_key(
        "fk_recommendations_business_session_owner_ref",
        "recommendations",
        "business_sessions",
        ["business_session_id", "owner_user_id", "reference_set_id"],
        ["id", "owner_user_id", "reference_set_id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_recommendations_id_session_owner_ref",
        "recommendations",
        ["id", "business_session_id", "owner_user_id", "reference_set_id"],
    )
    op.create_unique_constraint(
        "uq_recommendations_business_session", "recommendations", ["business_session_id"]
    )
    op.create_unique_constraint(
        "uq_recommendations_callback_event", "recommendations", ["callback_event_id"]
    )

    op.add_column("tasks", sa.Column("business_session_id", uuid, nullable=True))
    op.add_column(
        "tasks",
        sa.Column(
            "provenance_kind",
            sa.String(length=24),
            server_default=sa.text("'legacy_mock'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "provenance_association_check",
        "tasks",
        "(provenance_kind = 'legacy_mock' AND business_session_id IS NULL) OR "
        "(provenance_kind = 'dify_chatflow' AND business_session_id IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_tasks_business_session_owner_ref",
        "tasks",
        "business_sessions",
        ["business_session_id", "owner_user_id", "reference_set_id"],
        ["id", "owner_user_id", "reference_set_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_tasks_recommendation_session_owner_ref",
        "tasks",
        "recommendations",
        ["recommendation_id", "business_session_id", "owner_user_id", "reference_set_id"],
        ["id", "business_session_id", "owner_user_id", "reference_set_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint("uq_tasks_business_session", "tasks", ["business_session_id"])
    op.create_unique_constraint(
        "uq_tasks_id_business_session", "tasks", ["id", "business_session_id"]
    )

    op.alter_column("task_actions", "task_id", existing_type=uuid, nullable=True)
    op.add_column("task_actions", sa.Column("business_session_id", uuid, nullable=True))
    op.drop_constraint("scope_kind_check", "task_actions", type_="check")
    op.drop_constraint("scope_association_check", "task_actions", type_="check")
    op.create_check_constraint(
        "scope_kind_check",
        "task_actions",
        "scope_kind IN ('submission', 'attempt', 'business_session')",
    )
    op.create_check_constraint(
        "scope_association_check",
        "task_actions",
        "(scope_kind = 'attempt' AND submission_id IS NULL AND attempt_id IS NOT NULL "
        "AND task_item_id IS NOT NULL AND business_session_id IS NULL AND task_id IS NOT NULL "
        "AND scope_id = attempt_id) OR "
        "(scope_kind = 'submission' AND submission_id IS NOT NULL AND scope_id = submission_id "
        "AND attempt_id IS NULL AND task_item_id IS NULL AND business_session_id IS NULL "
        "AND task_id IS NOT NULL) OR "
        "(scope_kind = 'business_session' AND business_session_id IS NOT NULL "
        "AND scope_id = business_session_id AND submission_id IS NULL "
        "AND attempt_id IS NULL AND task_item_id IS NULL)",
    )
    op.create_foreign_key(
        "fk_task_actions_business_session",
        "task_actions",
        "business_sessions",
        ["business_session_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_task_actions_task_business_session",
        "task_actions",
        "tasks",
        ["task_id", "business_session_id"],
        ["id", "business_session_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_task_actions_task_business_session", "task_actions", type_="foreignkey")
    op.drop_constraint("fk_task_actions_business_session", "task_actions", type_="foreignkey")
    op.drop_constraint("scope_association_check", "task_actions", type_="check")
    op.drop_constraint("scope_kind_check", "task_actions", type_="check")
    op.create_check_constraint(
        "scope_kind_check", "task_actions", "scope_kind IN ('submission', 'attempt')"
    )
    op.create_check_constraint(
        "scope_association_check",
        "task_actions",
        "(scope_kind = 'attempt' AND submission_id IS NULL AND attempt_id IS NOT NULL "
        "AND task_item_id IS NOT NULL AND scope_id = attempt_id) OR "
        "(scope_kind = 'submission' AND submission_id IS NOT NULL "
        "AND scope_id = submission_id AND attempt_id IS NULL AND task_item_id IS NULL)",
    )
    op.drop_column("task_actions", "business_session_id")
    op.alter_column(
        "task_actions", "task_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False
    )

    op.drop_constraint("uq_tasks_id_business_session", "tasks", type_="unique")
    op.drop_constraint("uq_tasks_business_session", "tasks", type_="unique")
    op.drop_constraint("fk_tasks_recommendation_session_owner_ref", "tasks", type_="foreignkey")
    op.drop_constraint("fk_tasks_business_session_owner_ref", "tasks", type_="foreignkey")
    op.drop_constraint("provenance_association_check", "tasks", type_="check")
    op.drop_column("tasks", "provenance_kind")
    op.drop_column("tasks", "business_session_id")

    op.drop_constraint("uq_recommendations_callback_event", "recommendations", type_="unique")
    op.drop_constraint("uq_recommendations_business_session", "recommendations", type_="unique")
    op.drop_constraint("uq_recommendations_id_session_owner_ref", "recommendations", type_="unique")
    op.drop_constraint(
        "fk_recommendations_business_session_owner_ref", "recommendations", type_="foreignkey"
    )
    op.drop_constraint("callback_receipt_check", "recommendations", type_="check")
    op.drop_constraint("source_association_check", "recommendations", type_="check")
    op.drop_constraint("source_kind_check", "recommendations", type_="check")
    for column in (
        "callback_digest",
        "callback_digest_version",
        "callback_event_id",
        "dify_run_id",
        "source_kind",
        "business_session_id",
    ):
        op.drop_column("recommendations", column)

    op.drop_index("ix_business_sessions_expiry", table_name="business_sessions")
    op.drop_index("ix_business_sessions_reference", table_name="business_sessions")
    op.drop_index("ix_business_sessions_owner_status", table_name="business_sessions")
    op.drop_table("business_sessions")
