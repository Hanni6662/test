"""Draft R4 mock generation schema; A must integrate a new formal revision."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s007_generation_draft"
down_revision = None
branch_labels = None
depends_on = None


def _clock_timestamp() -> sa.TextClause:
    return sa.text("clock_timestamp()")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "mock_generation_jobs",
        sa.Column(
            "id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("request_key", uuid, nullable=False),
        sa.Column("request_digest_version", sa.SmallInteger(), nullable=False),
        sa.Column("request_digest", sa.LargeBinary(32), nullable=False),
        sa.Column("provider_job_id", sa.String(320), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("query_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("result_ref", sa.String(100), nullable=False),
        sa.Column("result_digest", sa.LargeBinary(32), nullable=False),
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
        sa.CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        sa.CheckConstraint("request_digest_version > 0", name="request_digest_version_check"),
        sa.CheckConstraint("octet_length(result_digest) = 32", name="result_digest_size_check"),
        sa.CheckConstraint("query_count >= 0", name="query_count_check"),
        sa.CheckConstraint("status IN ('pending', 'succeeded', 'failed')", name="status_check"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_key", name="uq_mock_generation_jobs_request_key"),
        sa.UniqueConstraint("provider_job_id", name="uq_mock_generation_jobs_provider_job_id"),
        sa.UniqueConstraint("result_ref", name="uq_mock_generation_jobs_result_ref"),
    )
    op.create_index(
        "ix_mock_generation_jobs_status_created",
        "mock_generation_jobs",
        ["status", "created_at"],
    )

    op.create_table(
        "mock_generation_invocations",
        sa.Column(
            "id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("request_key", uuid, nullable=False),
        sa.Column("request_digest_version", sa.SmallInteger(), nullable=False),
        sa.Column("request_digest", sa.LargeBinary(32), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("provider_job_id", sa.String(320)),
        sa.Column(
            "invoked_at",
            sa.DateTime(timezone=True),
            server_default=_clock_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(request_digest) = 32", name="request_digest_size_check"
        ),
        sa.CheckConstraint("request_digest_version > 0", name="request_digest_version_check"),
        sa.CheckConstraint(
            "outcome IN ('accepted', 'definitely_rejected')", name="outcome_check"
        ),
        sa.CheckConstraint(
            "(outcome = 'accepted' AND provider_job_id IS NOT NULL) OR "
            "(outcome = 'definitely_rejected' AND provider_job_id IS NULL)",
            name="provider_job_outcome_check",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mock_generation_invocations_request_time",
        "mock_generation_invocations",
        ["request_key", "invoked_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mock_generation_invocations_request_time", table_name="mock_generation_invocations"
    )
    op.drop_table("mock_generation_invocations")
    op.drop_index("ix_mock_generation_jobs_status_created", table_name="mock_generation_jobs")
    op.drop_table("mock_generation_jobs")
