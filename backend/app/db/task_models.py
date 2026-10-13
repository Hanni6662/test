from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
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
from app.db.business_session_models import BusinessSession as _BusinessSession  # noqa: F401


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'prompting', 'needs_input', 'generating', 'reconciling', "
            "'succeeded', 'partially_succeeded', 'failed', 'deleting')",
            name="status_check",
        ),
        CheckConstraint("schema_version = 2", name="schema_version_check"),
        CheckConstraint("planned_count BETWEEN 1 AND 8", name="planned_count_check"),
        CheckConstraint("billing_mode = 'disabled'", name="billing_mode_check"),
        CheckConstraint(
            "(provenance_kind = 'legacy_mock' AND business_session_id IS NULL) OR "
            "(provenance_kind = 'dify_chatflow' AND business_session_id IS NOT NULL)",
            name="provenance_association_check",
        ),
        CheckConstraint("request_digest_version > 0", name="request_digest_version_check"),
        CheckConstraint("retention_days > 0", name="retention_days_check"),
        CheckConstraint("retention_policy_revision > 0", name="retention_policy_revision_check"),
        CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        CheckConstraint(
            "expires_at IS NULL OR terminal_at IS NOT NULL", name="expiration_terminal_check"
        ),
        ForeignKeyConstraint(
            ["reference_set_id", "owner_user_id"],
            ["reference_sets.id", "reference_sets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_tasks_reference_owner",
        ),
        ForeignKeyConstraint(
            ["recommendation_id", "owner_user_id"],
            ["recommendations.id", "recommendations.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_tasks_recommendation_owner",
        ),
        ForeignKeyConstraint(
            ["business_session_id", "owner_user_id", "reference_set_id"],
            [
                "business_sessions.id",
                "business_sessions.owner_user_id",
                "business_sessions.reference_set_id",
            ],
            ondelete="RESTRICT",
            name="fk_tasks_business_session_owner_ref",
        ),
        ForeignKeyConstraint(
            ["recommendation_id", "business_session_id", "owner_user_id", "reference_set_id"],
            [
                "recommendations.id",
                "recommendations.business_session_id",
                "recommendations.owner_user_id",
                "recommendations.reference_set_id",
            ],
            ondelete="RESTRICT",
            name="fk_tasks_recommendation_session_owner_ref",
        ),
        ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="RESTRICT", name="fk_tasks_owner"
        ),
        UniqueConstraint("recommendation_id", name="uq_tasks_initial_recommendation"),
        UniqueConstraint("business_session_id", name="uq_tasks_business_session"),
        UniqueConstraint("id", "business_session_id", name="uq_tasks_id_business_session"),
        UniqueConstraint("id", "owner_user_id", name="uq_tasks_id_owner"),
        Index("ix_tasks_owner_status_created", "owner_user_id", "status", "created_at"),
        Index("ix_tasks_expiry", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reference_set_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    recommendation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    business_session_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    provenance_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'legacy_mock'")
    )
    recommendation_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    recommendation_snapshot_version: Mapped[str] = mapped_column(String(120), nullable=False)
    schema_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("2")
    )
    workflow_version: Mapped[str] = mapped_column(String(80), nullable=False)
    request_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    request_digest_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1")
    )
    request_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    resolved_preferences: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    recommendation_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    reference_assets_snapshot: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False
    )
    category_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    image_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    planned_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    billing_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'disabled'")
    )
    billing_policy_version: Mapped[str] = mapped_column(
        String(80), nullable=False, server_default=text("'disabled-v1'")
    )
    billing_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'queued'"))
    retention_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("60")
    )
    retention_policy_revision: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1")
    )
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class TaskItem(Base):
    __tablename__ = "task_items"
    __table_args__ = (
        CheckConstraint("position BETWEEN 1 AND 8", name="position_check"),
        CheckConstraint(
            "status IN ('queued', 'running', 'saving', 'reconciling', 'succeeded', 'failed')",
            name="status_check",
        ),
        CheckConstraint(
            "(width IS NULL AND height IS NULL) OR (width > 0 AND height > 0)",
            name="dimensions_check",
        ),
        ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="CASCADE", name="fk_task_items_task"
        ),
        UniqueConstraint("task_id", "image_type", name="uq_task_items_task_type"),
        UniqueConstraint("task_id", "position", name="uq_task_items_task_position"),
        UniqueConstraint("id", "task_id", name="uq_task_items_id_task"),
        Index("ix_task_items_task_status_position", "task_id", "status", "position"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    image_type: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'queued'"))
    image_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    preview_ready: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class TaskItemAttempt(Base):
    """Extensible attempt record; S6 deliberately creates no external attempts."""

    __tablename__ = "task_item_attempts"
    __table_args__ = (
        CheckConstraint("attempt_no > 0", name="attempt_no_check"),
        CheckConstraint(
            "status IN ('prepared', 'submitting', 'submitted', 'polling', 'saving', "
            "'reconciling', 'completed', 'failed')",
            name="status_check",
        ),
        ForeignKeyConstraint(
            ["task_item_id"], ["task_items.id"], ondelete="CASCADE", name="fk_attempts_task_item"
        ),
        UniqueConstraint("id", "task_item_id", name="uq_attempts_id_task_item"),
        UniqueConstraint("task_item_id", "attempt_no", name="uq_attempts_item_number"),
        UniqueConstraint("idempotency_key", name="uq_attempts_idempotency_key"),
        Index("ix_task_item_attempts_status_next_action", "status", "next_action_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    task_item_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_id: Mapped[str | None] = mapped_column(String(100))
    model_id: Mapped[str | None] = mapped_column(String(100))
    model_version: Mapped[str | None] = mapped_column(String(100))
    provider_job_id: Mapped[str | None] = mapped_column(String(320))
    provider_result_ref: Mapped[str | None] = mapped_column(String(640))
    failure_code: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class TaskIdempotencyRecord(Base):
    """Minimal key mapping retained independently of task deletion."""

    __tablename__ = "task_idempotency_records"
    __table_args__ = (
        CheckConstraint("octet_length(key_hash) = 32", name="key_hash_size_check"),
        CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        CheckConstraint("digest_version > 0", name="digest_version_check"),
        CheckConstraint("planned_count BETWEEN 1 AND 8", name="planned_count_check"),
        CheckConstraint("state IN ('active', 'expired')", name="state_check"),
        ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="RESTRICT", name="fk_task_idempotency_owner"
        ),
        UniqueConstraint(
            "owner_user_id", "operation", "key_hash", name="uq_task_idempotency_owner_operation_key"
        ),
        Index("ix_task_idempotency_task_id", "task_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    key_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    request_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    digest_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1")
    )
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    planned_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class TaskRecommendationConsumption(Base):
    """Minimal per-recommendation one-shot marker; intentionally has no task FK."""

    __tablename__ = "task_recommendation_consumptions"
    __table_args__ = (
        CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        CheckConstraint("digest_version > 0", name="digest_version_check"),
        CheckConstraint("planned_count BETWEEN 1 AND 8", name="planned_count_check"),
        CheckConstraint("state IN ('active', 'expired')", name="state_check"),
        ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="RESTRICT", name="fk_task_consumption_owner"
        ),
        Index("ix_task_consumptions_task_id", "task_id"),
    )

    recommendation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    request_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    digest_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1")
    )
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    planned_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
