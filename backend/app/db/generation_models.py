"""Unregistered generation mock persistence models for A's later integration."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_JSON = JSON().with_variant(JSONB(), "postgresql")


class MockGenerationJob(Base):
    __tablename__ = "mock_generation_jobs"
    __table_args__ = (
        CheckConstraint(
            "octet_length(request_digest) = 32", name="request_digest_size_check"
        ),
        CheckConstraint("request_digest_version > 0", name="request_digest_version_check"),
        CheckConstraint("octet_length(result_digest) = 32", name="result_digest_size_check"),
        CheckConstraint("query_count >= 0", name="query_count_check"),
        CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed')", name="status_check"
        ),
        Index("ix_mock_generation_jobs_status_created", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    request_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    request_digest_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    request_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    provider_job_id: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    query_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    result_ref: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    result_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.clock_timestamp(),
        onupdate=func.clock_timestamp(),
    )


class MockGenerationInvocation(Base):
    """Durable countable record for every mock submit invocation."""

    __tablename__ = "mock_generation_invocations"
    __table_args__ = (
        CheckConstraint(
            "octet_length(request_digest) = 32", name="request_digest_size_check"
        ),
        CheckConstraint("request_digest_version > 0", name="request_digest_version_check"),
        CheckConstraint(
            "outcome IN ('accepted', 'definitely_rejected')", name="outcome_check"
        ),
        CheckConstraint(
            "(outcome = 'accepted' AND provider_job_id IS NOT NULL) OR "
            "(outcome = 'definitely_rejected' AND provider_job_id IS NULL)",
            name="provider_job_outcome_check",
        ),
        Index("ix_mock_generation_invocations_request_time", "request_key", "invoked_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    request_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    request_digest_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    request_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    provider_job_id: Mapped[str | None] = mapped_column(String(320))
    invoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class GenerationCapacityControl(Base):
    """A seeded singleton row that serializes cross-worker slot reservations."""

    __tablename__ = "generation_capacity_control"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton_check"),
        CheckConstraint("lock_version > 0", name="lock_version_check"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class GenerationCapacityReservation(Base):
    """One persistent global generation slot reservation per attempt."""

    __tablename__ = "generation_capacity_reservations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'released')", name="status_check"
        ),
        CheckConstraint(
            "(status = 'active' AND released_at IS NULL) OR "
            "(status = 'released' AND released_at IS NOT NULL)",
            name="release_timestamp_check",
        ),
        ForeignKeyConstraint(
            ["task_item_id", "task_id"],
            ["task_items.id", "task_items.task_id"],
            ondelete="CASCADE",
            name="fk_generation_capacity_item_task",
        ),
        ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_generation_capacity_owner",
        ),
        UniqueConstraint("attempt_id", name="uq_generation_capacity_attempt"),
        Index("ix_generation_capacity_status_reserved", "status", "reserved_at"),
        Index("ix_generation_capacity_owner_status", "owner_user_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task_item_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    task_item_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'active'")
    )
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GenerationAttemptContext(Base):
    """Private immutable prompt binding and recoverable output state for one attempt."""

    __tablename__ = "generation_attempt_contexts"
    __table_args__ = (
        CheckConstraint("package_digest_version > 0", name="package_digest_version_check"),
        CheckConstraint("octet_length(package_digest) = 32", name="package_digest_size_check"),
        CheckConstraint("request_digest_version > 0", name="request_digest_version_check"),
        CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        CheckConstraint(
            "result_digest IS NULL OR octet_length(result_digest) = 32",
            name="result_digest_size_check",
        ),
        CheckConstraint(
            "save_state IN ('awaiting_result', 'download_pending', 'original_stored', "
            "'attached', 'failed')",
            name="save_state_check",
        ),
        CheckConstraint(
            "(original_size_bytes IS NULL AND original_sha256 IS NULL "
            "AND original_mime_type IS NULL AND original_width IS NULL "
            "AND original_height IS NULL) "
            "OR (original_size_bytes IS NOT NULL AND original_sha256 IS NOT NULL "
            "AND original_mime_type IS NOT NULL AND original_width IS NOT NULL "
            "AND original_height IS NOT NULL AND original_size_bytes > 0 "
            "AND length(original_sha256) = 64 "
            "AND original_mime_type IN ('image/jpeg', 'image/png', 'image/webp') "
            "AND original_width > 0 AND original_height > 0)",
            name="original_metadata_check",
        ),
        CheckConstraint(
            "(preview_size_bytes IS NULL AND preview_sha256 IS NULL "
            "AND preview_width IS NULL AND preview_height IS NULL) OR "
            "(preview_size_bytes IS NOT NULL AND preview_sha256 IS NOT NULL "
            "AND preview_width IS NOT NULL AND preview_height IS NOT NULL "
            "AND preview_size_bytes > 0 AND length(preview_sha256) = 64 "
            "AND preview_width > 0 AND preview_height > 0)",
            name="preview_metadata_check",
        ),
        ForeignKeyConstraint(
            ["task_item_id", "task_id"],
            ["task_items.id", "task_items.task_id"],
            ondelete="CASCADE",
            name="fk_generation_context_item_task",
        ),
        ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_generation_context_owner",
        ),
        ForeignKeyConstraint(
            ["prompt_package_id"],
            ["task_prompt_packages.id"],
            ondelete="RESTRICT",
            name="fk_generation_context_package",
        ),
        ForeignKeyConstraint(
            ["prompt_item_id"],
            ["task_prompt_items.id"],
            ondelete="RESTRICT",
            name="fk_generation_context_prompt_item",
        ),
        Index("ix_generation_context_task_package", "task_id", "prompt_package_id"),
    )

    attempt_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task_item_attempts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_item_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    prompt_package_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    prompt_item_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    package_digest_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    package_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    request_digest_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    request_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    prompt_package_version: Mapped[str] = mapped_column(String(200), nullable=False)
    image_type: Mapped[str] = mapped_column(String(80), nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    reference_asset_ids: Mapped[list[str]] = mapped_column(_JSON, nullable=False)
    excluded_reference_aliases: Mapped[list[str]] = mapped_column(_JSON, nullable=False)
    credential_secret_ref: Mapped[str | None] = mapped_column(String(160))
    result_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    original_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    preview_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    save_state: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'awaiting_result'")
    )
    original_size_bytes: Mapped[int | None] = mapped_column(Integer)
    original_sha256: Mapped[str | None] = mapped_column(String(64))
    original_mime_type: Mapped[str | None] = mapped_column(String(32))
    original_width: Mapped[int | None] = mapped_column(Integer)
    original_height: Mapped[int | None] = mapped_column(Integer)
    preview_size_bytes: Mapped[int | None] = mapped_column(Integer)
    preview_sha256: Mapped[str | None] = mapped_column(String(64))
    preview_width: Mapped[int | None] = mapped_column(Integer)
    preview_height: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class GenerationResult(Base):
    """Private immutable asset pair attached to a completed task item."""

    __tablename__ = "generation_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_item_id", "task_id"],
            ["task_items.id", "task_items.task_id"],
            ondelete="CASCADE",
            name="fk_generation_results_item_task",
        ),
        ForeignKeyConstraint(
            ["original_asset_id", "owner_user_id"],
            ["image_assets.id", "image_assets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_generation_results_original_owner",
        ),
        ForeignKeyConstraint(
            ["preview_asset_id", "owner_user_id"],
            ["image_assets.id", "image_assets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_generation_results_preview_owner",
        ),
        ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_generation_results_owner",
        ),
        UniqueConstraint("task_item_id", name="uq_generation_results_item"),
        Index("ix_generation_results_task_created", "task_id", "created_at"),
    )

    attempt_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task_item_attempts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    task_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    task_item_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    original_asset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    preview_asset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
