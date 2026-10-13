"""Draft identity, permission, quota, balance, and recharge reservation schema.

This is intentionally outside ``migrations/versions``. The integration owner must
review and merge it into the shared Alembic chain before any database migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s002_identity_reserved_draft"
down_revision = "s001_users_sessions"
branch_labels = None
depends_on = None


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "auth_identities",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", uuid, nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_subject", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.LargeBinary(512)),
        sa.Column("status", sa.String(16), server_default=sa.text("'active'"), nullable=False),
        _created_at(),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="status_check"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "provider_subject", name="uq_auth_identities_provider_subject"
        ),
    )
    op.create_index("ix_auth_identities_user_id", "auth_identities", ["user_id"])

    op.create_table(
        "roles",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("label", sa.String(160), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_roles_code"),
    )
    op.create_table(
        "permissions",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("description", sa.String(240), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_permissions_code"),
    )
    op.create_table(
        "user_roles",
        sa.Column("user_id", uuid, nullable=False),
        sa.Column("role_id", uuid, nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("user_id", "role_id"),
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_id", uuid, nullable=False),
        sa.Column("permission_id", uuid, nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["permission_id"], ["permissions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("role_id", "permission_id"),
    )

    op.create_table(
        "quota_accounts",
        sa.Column("user_id", uuid, nullable=False),
        sa.Column("available_units", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("reserved_units", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("consumed_units", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("unit_code", sa.String(32), server_default=sa.text("'image'"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("available_units >= 0", name="available_nonnegative_check"),
        sa.CheckConstraint("reserved_units >= 0", name="reserved_nonnegative_check"),
        sa.CheckConstraint("consumed_units >= 0", name="consumed_nonnegative_check"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "credit_reservations",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", uuid, nullable=False),
        sa.Column("resource_kind", sa.String(40), nullable=False),
        sa.Column("resource_id", uuid, nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("reserved_units", sa.BigInteger(), nullable=False),
        sa.Column("captured_units", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("released_units", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("billing_revision", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'held'"), nullable=False),
        _created_at(),
        sa.Column("terminal_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("reserved_units > 0", name="reserved_positive_check"),
        sa.CheckConstraint("captured_units >= 0", name="captured_nonnegative_check"),
        sa.CheckConstraint("released_units >= 0", name="released_nonnegative_check"),
        sa.CheckConstraint("status IN ('held', 'captured', 'released')", name="status_check"),
        sa.CheckConstraint(
            "(status = 'held' AND captured_units = 0 AND released_units = 0) OR "
            "(status = 'captured' AND captured_units = reserved_units AND released_units = 0) OR "
            "(status = 'released' AND captured_units = 0 AND released_units = reserved_units)",
            name="terminal_amounts_check",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_credit_reservations_user_key"),
        sa.UniqueConstraint(
            "user_id", "resource_kind", "resource_id", name="uq_credit_reservations_resource"
        ),
    )
    op.create_index(
        "ix_credit_reservations_status_created", "credit_reservations", ["status", "created_at"]
    )
    op.create_table(
        "credit_ledger",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", uuid, nullable=False),
        sa.Column("reservation_id", uuid),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("entry_kind", sa.String(32), nullable=False),
        sa.Column("available_delta", sa.BigInteger(), nullable=False),
        sa.Column("reserved_delta", sa.BigInteger(), nullable=False),
        sa.Column("consumed_delta", sa.BigInteger(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["reservation_id"], ["credit_reservations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_credit_ledger_user_key"),
    )
    op.create_index("ix_credit_ledger_user_created", "credit_ledger", ["user_id", "created_at"])

    op.create_table(
        "balance_accounts",
        sa.Column("user_id", uuid, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("available_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("reserved_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("available_minor >= 0", name="available_nonnegative_check"),
        sa.CheckConstraint("reserved_minor >= 0", name="reserved_nonnegative_check"),
        sa.CheckConstraint("length(currency) = 3", name="currency_length_check"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "recharge_orders",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", uuid, nullable=False),
        sa.Column("order_no", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("provider_transaction_id", sa.String(200)),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'pending'"), nullable=False),
        _created_at(),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("amount_minor > 0", name="amount_positive_check"),
        sa.CheckConstraint("length(currency) = 3", name="currency_length_check"),
        sa.CheckConstraint(
            "status IN ('pending', 'paid', 'failed', 'expired')", name="status_check"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_no", name="uq_recharge_orders_order_no"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_recharge_orders_user_key"),
        sa.UniqueConstraint(
            "provider", "provider_transaction_id", name="uq_recharge_orders_provider_tx"
        ),
    )
    op.create_index("ix_recharge_orders_user_created", "recharge_orders", ["user_id", "created_at"])
    op.create_table(
        "balance_ledger",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", uuid, nullable=False),
        sa.Column("recharge_order_id", uuid),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("entry_kind", sa.String(32), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        _created_at(),
        sa.CheckConstraint("amount_minor > 0", name="amount_positive_check"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recharge_order_id"], ["recharge_orders.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_balance_ledger_user_key"),
    )
    op.create_index("ix_balance_ledger_user_created", "balance_ledger", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_balance_ledger_user_created", table_name="balance_ledger")
    op.drop_table("balance_ledger")
    op.drop_index("ix_recharge_orders_user_created", table_name="recharge_orders")
    op.drop_table("recharge_orders")
    op.drop_table("balance_accounts")
    op.drop_index("ix_credit_ledger_user_created", table_name="credit_ledger")
    op.drop_table("credit_ledger")
    op.drop_index("ix_credit_reservations_status_created", table_name="credit_reservations")
    op.drop_table("credit_reservations")
    op.drop_table("quota_accounts")
    op.drop_table("role_permissions")
    op.drop_table("user_roles")
    op.drop_table("permissions")
    op.drop_table("roles")
    op.drop_index("ix_auth_identities_user_id", table_name="auth_identities")
    op.drop_table("auth_identities")
