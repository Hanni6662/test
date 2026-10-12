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


class OperationsLoopHeartbeat(Base):
    """Latest durable work-loop evidence for each singleton control process."""

    __tablename__ = "operations_loop_heartbeats"
    __table_args__ = (
        CheckConstraint("component IN ('dispatcher', 'scheduler')", name="component_check"),
        CheckConstraint("iteration_count >= 0", name="iteration_count_check"),
        CheckConstraint("successful_iteration_count >= 0", name="success_count_check"),
        CheckConstraint("failed_iteration_count >= 0", name="failure_count_check"),
        CheckConstraint(
            "last_safe_error_code IS NULL OR last_safe_error_code ~ '^[A-Z][A-Z0-9_]{0,79}$'",
            name="safe_error_code_check",
        ),
    )

    component: Mapped[str] = mapped_column(String(24), primary_key=True)
    instance_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    last_iteration_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_iteration_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbox_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_cleanup_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_broker_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_broker_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    iteration_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    successful_iteration_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    failed_iteration_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_safe_error_code: Mapped[str | None] = mapped_column(String(80))
    registration_state: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    last_progress: Mapped[dict[str, int]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class OperationsRecoveryAudit(Base):
    """Idempotency/audit evidence linked to the task action it modifies."""

    __tablename__ = "operations_recovery_audits"
    __table_args__ = (
        CheckConstraint(
            "operation_kind IN ('expire_followup', 'expedite_generation_save')",
            name="operation_kind_check",
        ),
        CheckConstraint(
            "octet_length(idempotency_key_hash) = 32", name="idempotency_hash_size_check"
        ),
        CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        CheckConstraint("result_code ~ '^[A-Z][A-Z0-9_]{0,79}$'", name="result_code_check"),
        Index("ix_operations_recovery_audits_task_created", "task_id", "created_at"),
        UniqueConstraint("idempotency_key_hash", name="uq_operations_recovery_idempotency"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    task_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE", name="fk_operations_audit_task"),
        nullable=False,
    )
    action_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task_actions.id", ondelete="CASCADE", name="fk_operations_audit_action"),
        nullable=False,
    )
    operation_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    request_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    result_code: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
