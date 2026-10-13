from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RetentionPolicy(Base):
    __tablename__ = "retention_policies"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton_check"),
        CheckConstraint("task_retention_days BETWEEN 1 AND 3650", name="task_days_check"),
        CheckConstraint("reference_retention_days BETWEEN 1 AND 3650", name="reference_days_check"),
        CheckConstraint(
            "recommendation_retention_days BETWEEN 1 AND 3650",
            name="recommendation_days_check",
        ),
        CheckConstraint("revision > 0", name="revision_check"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default=text("1"))
    task_retention_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("60")
    )
    reference_retention_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("60")
    )
    recommendation_retention_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("60")
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class CleanupRun(Base):
    __tablename__ = "cleanup_runs"
    __table_args__ = (
        CheckConstraint(
            "trigger_kind IN ('manual', 'scheduled', 'catchup')", name="trigger_kind_check"
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')", name="status_check"
        ),
        CheckConstraint("policy_revision > 0", name="policy_revision_check"),
        CheckConstraint(
            "idempotency_key_hash IS NULL OR octet_length(idempotency_key_hash) = 32",
            name="idempotency_key_hash_size_check",
        ),
        CheckConstraint(
            "request_digest IS NULL OR octet_length(request_digest) = 32",
            name="request_digest_size_check",
        ),
        CheckConstraint(
            "(trigger_kind = 'manual' AND idempotency_key_hash IS NOT NULL "
            "AND request_digest IS NOT NULL AND scheduled_for IS NULL) OR "
            "(trigger_kind = 'scheduled' AND idempotency_key_hash IS NULL "
            "AND request_digest IS NULL AND scheduled_for IS NOT NULL) OR "
            "(trigger_kind = 'catchup' AND idempotency_key_hash IS NULL "
            "AND request_digest IS NULL AND scheduled_for IS NULL)",
            name="trigger_fields_check",
        ),
        CheckConstraint(
            "(catchup_from IS NULL AND catchup_to IS NULL) OR "
            "(catchup_from IS NOT NULL AND catchup_to IS NOT NULL "
            "AND catchup_to > catchup_from)",
            name="catchup_range_check",
        ),
        CheckConstraint("jsonb_typeof(counts) = 'object'", name="counts_object_check"),
        CheckConstraint(
            "orphan_scan_cursor IS NULL OR length(orphan_scan_cursor) > 0",
            name="orphan_scan_cursor_check",
        ),
        CheckConstraint(
            "(orphan_scan_lease_token IS NULL AND orphan_scan_lease_until IS NULL) OR "
            "(orphan_scan_lease_token IS NOT NULL AND orphan_scan_lease_until IS NOT NULL)",
            name="orphan_scan_lease_consistency_check",
        ),
        CheckConstraint(
            "orphan_scan_completed_at IS NULL OR "
            "(orphan_scan_lease_token IS NULL AND orphan_scan_lease_until IS NULL)",
            name="orphan_scan_completion_lease_check",
        ),
        UniqueConstraint("trigger_key", name="uq_cleanup_runs_trigger_key"),
        UniqueConstraint("idempotency_key_hash", name="uq_cleanup_runs_idempotency_hash"),
        Index("ix_cleanup_runs_status_created", "status", "created_at"),
        Index(
            "ix_cleanup_runs_orphan_scan_claim",
            "status",
            "orphan_scan_completed_at",
            "orphan_scan_lease_until",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    trigger_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger_key: Mapped[str] = mapped_column(String(160), nullable=False)
    idempotency_key_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    request_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'queued'"))
    counts: Mapped[dict[str, int]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    catchup_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    catchup_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    orphan_scan_cursor: Mapped[str | None] = mapped_column(String(1024))
    orphan_scan_lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    orphan_scan_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    orphan_scan_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CleanupJob(Base):
    __tablename__ = "cleanup_jobs"
    __table_args__ = (
        CheckConstraint(
            "aggregate_kind IN ('task', 'recommendation', 'reference_set', "
            "'upload_group', 'orphan')",
            name="aggregate_kind_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'retry', 'completed', 'blocked')",
            name="status_check",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_check"),
        CheckConstraint(
            "(status = 'claimed' AND lease_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status <> 'claimed' AND lease_token IS NULL AND lease_until IS NULL)",
            name="claim_lease_consistency_check",
        ),
        ForeignKeyConstraint(
            ["run_id"],
            ["cleanup_runs.id"],
            ondelete="CASCADE",
            name="fk_cleanup_jobs_run",
        ),
        UniqueConstraint(
            "run_id", "aggregate_kind", "aggregate_id", name="uq_cleanup_jobs_run_aggregate"
        ),
        Index("ix_cleanup_jobs_claim", "status", "next_retry_at", "created_at"),
        Index("ix_cleanup_jobs_run_status", "run_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    aggregate_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CleanupTarget(Base):
    __tablename__ = "cleanup_targets"
    __table_args__ = (
        CheckConstraint("sequence > 0", name="sequence_check"),
        CheckConstraint("entry_kind IN ('object', 'staging', 'sidecar')", name="entry_kind_check"),
        CheckConstraint("status IN ('pending', 'deleted', 'failed')", name="status_check"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_check"),
        CheckConstraint(
            "(source_kind IS NULL AND source_id IS NULL) OR "
            "(source_kind IS NOT NULL AND source_id IS NOT NULL)",
            name="source_pair_check",
        ),
        CheckConstraint(
            "(status IN ('pending', 'failed') AND relative_locator IS NOT NULL) OR "
            "(status = 'deleted' AND relative_locator IS NULL AND object_key IS NULL)",
            name="locator_state_check",
        ),
        CheckConstraint(
            "entry_kind <> 'staging' OR object_key IS NULL", name="staging_object_key_check"
        ),
        ForeignKeyConstraint(
            ["job_id"],
            ["cleanup_jobs.id"],
            ondelete="CASCADE",
            name="fk_cleanup_targets_job",
        ),
        UniqueConstraint(
            "job_id", "entry_kind", "relative_locator", name="uq_cleanup_targets_locator"
        ),
        Index("ix_cleanup_targets_job_status", "job_id", "status", "sequence"),
        Index("ix_cleanup_targets_source", "source_kind", "source_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    relative_locator: Mapped[str | None] = mapped_column(String(1024))
    object_key: Mapped[str | None] = mapped_column(String(512))
    source_kind: Mapped[str | None] = mapped_column(String(32))
    source_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
