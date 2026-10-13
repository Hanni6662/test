from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint("operation_version > 0", name="operation_version_positive_check"),
        CheckConstraint("schema_version > 0", name="schema_version_positive_check"),
        CheckConstraint("attempt_count >= 0", name="attempt_nonnegative_check"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object_check"),
        CheckConstraint(
            "status <> 'claimed' OR attempt_count > 0", name="claim_attempt_positive_check"
        ),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'published', 'dead')", name="status_check"
        ),
        CheckConstraint(
            "(status = 'claimed' AND claim_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status <> 'claimed' AND claim_token IS NULL AND lease_until IS NULL)",
            name="claim_lease_consistency_check",
        ),
        UniqueConstraint(
            "event_type", "aggregate_id", "operation_version", name="uq_outbox_business_action"
        ),
        Index("ix_outbox_events_status_available", "status", "available_at", "created_at"),
        Index("ix_outbox_events_lease_until", "lease_until"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    operation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    claim_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class OutboxReceipt(Base):
    __tablename__ = "outbox_receipts"
    __table_args__ = (
        CheckConstraint("attempt_count >= 0", name="attempt_nonnegative_check"),
        CheckConstraint(
            "status <> 'processing' OR attempt_count > 0", name="processing_attempt_positive_check"
        ),
        CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL) OR "
            "(status <> 'completed' AND completed_at IS NULL)",
            name="completed_timestamp_consistency_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'dead')", name="status_check"
        ),
        CheckConstraint(
            "(status = 'processing' AND claim_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status <> 'processing' AND claim_token IS NULL AND lease_until IS NULL)",
            name="claim_lease_consistency_check",
        ),
        Index("ix_outbox_receipts_status_available", "status", "available_at"),
        Index("ix_outbox_receipts_lease_until", "lease_until"),
    )

    event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("outbox_events.id", ondelete="RESTRICT"), primary_key=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    claim_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))


class OutboxDemoEffect(Base):
    """Durable, non-paid effect used to exercise queue recovery and deduplication."""

    __tablename__ = "outbox_demo_effects"
    __table_args__ = (Index("ix_outbox_demo_effects_aggregate", "aggregate_id"),)

    event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("outbox_events.id", ondelete="RESTRICT"), primary_key=True
    )
    aggregate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    operation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class OutboxRejectedMessage(Base):
    """Safe durable evidence of malformed envelopes without retaining their body."""

    __tablename__ = "outbox_rejected_messages"
    __table_args__ = (
        CheckConstraint("octet_length(message_fingerprint) = 32", name="fingerprint_size_check"),
        CheckConstraint("occurrence_count > 0", name="occurrence_positive_check"),
        UniqueConstraint("message_fingerprint", name="uq_outbox_rejected_fingerprint"),
        Index("ix_outbox_rejected_event_received", "event_id", "received_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    event_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("outbox_events.id", ondelete="RESTRICT")
    )
    message_fingerprint: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    error_code: Mapped[str] = mapped_column(String(80), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
