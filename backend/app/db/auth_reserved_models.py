from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuthIdentity(Base):
    __tablename__ = "auth_identities"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="status_check"),
        UniqueConstraint(
            "provider", "provider_subject", name="uq_auth_identities_provider_subject"
        ),
        Index("ix_auth_identities_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_subject: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[bytes | None] = mapped_column(LargeBinary(512))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("code", name="uq_roles_code"),)

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class Permission(Base):
    __tablename__ = "permissions"
    __table_args__ = (UniqueConstraint("code", name="uq_permissions_code"),)

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(240), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    role_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), primary_key=True
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), primary_key=True
    )
    permission_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("permissions.id", ondelete="RESTRICT"), primary_key=True
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class QuotaAccount(Base):
    __tablename__ = "quota_accounts"
    __table_args__ = (
        CheckConstraint("available_units >= 0", name="available_nonnegative_check"),
        CheckConstraint("reserved_units >= 0", name="reserved_nonnegative_check"),
        CheckConstraint("consumed_units >= 0", name="consumed_nonnegative_check"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    available_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    reserved_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    consumed_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    unit_code: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'image'")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class CreditReservation(Base):
    __tablename__ = "credit_reservations"
    __table_args__ = (
        CheckConstraint("reserved_units > 0", name="reserved_positive_check"),
        CheckConstraint("captured_units >= 0", name="captured_nonnegative_check"),
        CheckConstraint("released_units >= 0", name="released_nonnegative_check"),
        CheckConstraint("status IN ('held', 'captured', 'released')", name="status_check"),
        CheckConstraint(
            "(status = 'held' AND captured_units = 0 AND released_units = 0) OR "
            "(status = 'captured' AND captured_units = reserved_units AND released_units = 0) OR "
            "(status = 'released' AND captured_units = 0 AND released_units = reserved_units)",
            name="terminal_amounts_check",
        ),
        UniqueConstraint("user_id", "idempotency_key", name="uq_credit_reservations_user_key"),
        UniqueConstraint(
            "user_id", "resource_kind", "resource_id", name="uq_credit_reservations_resource"
        ),
        Index("ix_credit_reservations_status_created", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    resource_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    reserved_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    captured_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    released_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    billing_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'held'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CreditLedger(Base):
    __tablename__ = "credit_ledger"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_credit_ledger_user_key"),
        Index("ix_credit_ledger_user_created", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reservation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("credit_reservations.id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    entry_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    available_delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    consumed_delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class BalanceAccount(Base):
    __tablename__ = "balance_accounts"
    __table_args__ = (
        CheckConstraint("available_minor >= 0", name="available_nonnegative_check"),
        CheckConstraint("reserved_minor >= 0", name="reserved_nonnegative_check"),
        CheckConstraint("length(currency) = 3", name="currency_length_check"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    available_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    reserved_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class BalanceLedger(Base):
    __tablename__ = "balance_ledger"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive_check"),
        UniqueConstraint("user_id", "idempotency_key", name="uq_balance_ledger_user_key"),
        Index("ix_balance_ledger_user_created", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    recharge_order_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("recharge_orders.id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    entry_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class RechargeOrder(Base):
    __tablename__ = "recharge_orders"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive_check"),
        CheckConstraint("length(currency) = 3", name="currency_length_check"),
        CheckConstraint("status IN ('pending', 'paid', 'failed', 'expired')", name="status_check"),
        UniqueConstraint("user_id", "idempotency_key", name="uq_recharge_orders_user_key"),
        UniqueConstraint(
            "provider", "provider_transaction_id", name="uq_recharge_orders_provider_tx"
        ),
        Index("ix_recharge_orders_user_created", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    order_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_transaction_id: Mapped[str | None] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
