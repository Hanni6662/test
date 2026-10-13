"""Draft recommendation and persistent mock-Dify schema for later integration."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s005_recommendations_draft"
down_revision = None
branch_labels = None
depends_on = None


def _clock_timestamp() -> sa.TextClause:
    return sa.text("clock_timestamp()")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "recommendations",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_user_id", uuid, nullable=False),
        sa.Column("reference_set_id", uuid, nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'analyzing'"), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("original_recommendation", jsonb),
        sa.Column("public_options", jsonb),
        sa.Column("image_types", jsonb, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("audit", jsonb),
        sa.Column("state", jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("run_ref", sa.String(320)),
        sa.Column("form_ref", sa.String(320)),
        sa.Column("valid_until", sa.DateTime(timezone=True)),
        sa.Column("data_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True)),
        sa.Column("failure_code", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('analyzing', 'ready', 'failed', 'expired')", name="status_check"
        ),
        sa.CheckConstraint("revision > 0", name="revision_positive_check"),
        sa.CheckConstraint("data_expires_at > created_at", name="data_expiration_check"),
        sa.CheckConstraint(
            "status <> 'ready' OR (ready_at IS NOT NULL AND valid_until IS NOT NULL "
            "AND public_options IS NOT NULL AND original_recommendation IS NOT NULL)",
            name="ready_fields_check",
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR failure_code IS NOT NULL", name="failed_code_check"
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_recommendations_owner",
        ),
        sa.ForeignKeyConstraint(
            ["reference_set_id", "owner_user_id"],
            ["reference_sets.id", "reference_sets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_recommendations_reference_owner",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_recommendations_id_owner"),
        sa.UniqueConstraint("run_ref", name="uq_recommendations_run_ref"),
    )
    op.create_index(
        "ix_recommendations_owner_status",
        "recommendations",
        ["owner_user_id", "status", "created_at"],
    )
    op.create_index(
        "ix_recommendations_reference", "recommendations", ["reference_set_id", "created_at"]
    )
    op.create_index("ix_recommendations_expiry", "recommendations", ["data_expires_at"])

    op.create_table(
        "mock_recommendation_runs",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("business_id", uuid, nullable=False),
        sa.Column("reference_set_id", uuid, nullable=False),
        sa.Column("run_ref", sa.String(320), nullable=False),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("reference_assets", jsonb, nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'ready'"), nullable=False),
        sa.Column("original_recommendation", jsonb),
        sa.Column("public_options", jsonb),
        sa.Column("image_types", jsonb, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("audit", jsonb),
        sa.Column("state", jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("form_ref", sa.String(320)),
        sa.Column("valid_until", sa.DateTime(timezone=True)),
        sa.Column("failure_code", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('analyzing', 'ready', 'failed')", name="status_check"),
        sa.CheckConstraint(
            "status <> 'ready' OR (public_options IS NOT NULL "
            "AND original_recommendation IS NOT NULL AND valid_until IS NOT NULL)",
            name="ready_fields_check",
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR failure_code IS NOT NULL", name="failed_code_check"
        ),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["recommendations.id"],
            ondelete="RESTRICT",
            name="fk_mock_recommendation_runs_business",
        ),
        sa.ForeignKeyConstraint(
            ["reference_set_id"],
            ["reference_sets.id"],
            ondelete="RESTRICT",
            name="fk_mock_recommendation_runs_reference_set",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", name="uq_mock_recommendation_runs_business"),
        sa.UniqueConstraint("run_ref", name="uq_mock_recommendation_runs_run_ref"),
    )
    op.create_index(
        "ix_mock_recommendation_runs_status",
        "mock_recommendation_runs",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mock_recommendation_runs_status", table_name="mock_recommendation_runs")
    op.drop_table("mock_recommendation_runs")
    op.drop_index("ix_recommendations_expiry", table_name="recommendations")
    op.drop_index("ix_recommendations_reference", table_name="recommendations")
    op.drop_index("ix_recommendations_owner_status", table_name="recommendations")
    op.drop_table("recommendations")
