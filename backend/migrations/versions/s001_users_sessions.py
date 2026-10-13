"""Create users, sessions, and the initial session policy.

Revision ID: s001_users_sessions
Revises:
Create Date: 2026-10-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from app.core.config import get_settings
from sqlalchemy.dialects import postgresql

revision = "s001_users_sessions"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("identity_kind", sa.String(length=16), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'active'"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("identity_kind IN ('guest', 'member')", name="identity_kind_check"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="status_check"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )

    op.create_table(
        "session_policies",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("ttl_days", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="singleton_check"),
        sa.CheckConstraint("ttl_days > 0", name="ttl_days_check"),
        sa.CheckConstraint("revision > 0", name="revision_check"),
        sa.PrimaryKeyConstraint("id", name="pk_session_policies"),
    )

    initial_ttl_days = get_settings().guest_session_ttl_days
    op.execute(
        sa.text(
            "INSERT INTO session_policies (id, ttl_days, revision) "
            "VALUES (1, :ttl_days, 1)"
        ).bindparams(ttl_days=initial_ttl_days)
    )

    op.create_table(
        "sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "last_renewed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ttl_days", sa.Integer(), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("octet_length(token_hash) = 32", name="token_hash_size_check"),
        sa.CheckConstraint("ttl_days > 0", name="ttl_days_check"),
        sa.CheckConstraint("policy_revision > 0", name="policy_revision_check"),
        sa.CheckConstraint("expires_at > created_at", name="expiration_order_check"),
        sa.CheckConstraint("last_renewed_at >= created_at", name="renewal_order_check"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("session_policies")
    op.drop_table("users")
