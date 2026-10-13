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
from app.db.business_session_models import BusinessSession as _BusinessSession  # noqa: F401


class Recommendation(Base):
    __tablename__ = "recommendations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('analyzing', 'ready', 'failed', 'expired')", name="status_check"
        ),
        CheckConstraint("revision > 0", name="revision_positive_check"),
        CheckConstraint(
            "source_kind IN ('synthetic_mock', 'dify_chatflow')", name="source_kind_check"
        ),
        CheckConstraint(
            "(source_kind = 'synthetic_mock' AND business_session_id IS NULL) OR "
            "(source_kind = 'dify_chatflow' AND business_session_id IS NOT NULL)",
            name="source_association_check",
        ),
        CheckConstraint(
            "(callback_event_id IS NULL AND callback_digest IS NULL) OR "
            "(callback_event_id IS NOT NULL AND callback_digest IS NOT NULL "
            "AND callback_digest_version > 0 AND octet_length(callback_digest) = 32)",
            name="callback_receipt_check",
        ),
        CheckConstraint("retention_days BETWEEN 1 AND 3650", name="retention_days_check"),
        CheckConstraint("retention_policy_revision > 0", name="retention_policy_revision_check"),
        CheckConstraint("data_expires_at > created_at", name="data_expiration_check"),
        CheckConstraint(
            "status <> 'ready' OR (ready_at IS NOT NULL AND valid_until IS NOT NULL "
            "AND public_options IS NOT NULL AND original_recommendation IS NOT NULL)",
            name="ready_fields_check",
        ),
        CheckConstraint("status <> 'failed' OR failure_code IS NOT NULL", name="failed_code_check"),
        ForeignKeyConstraint(
            ["reference_set_id", "owner_user_id"],
            ["reference_sets.id", "reference_sets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_recommendations_reference_owner",
        ),
        ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="RESTRICT", name="fk_recommendations_owner"
        ),
        ForeignKeyConstraint(
            ["business_session_id", "owner_user_id", "reference_set_id"],
            [
                "business_sessions.id",
                "business_sessions.owner_user_id",
                "business_sessions.reference_set_id",
            ],
            ondelete="CASCADE",
            name="fk_recommendations_business_session_owner_ref",
        ),
        UniqueConstraint("id", "owner_user_id", name="uq_recommendations_id_owner"),
        UniqueConstraint(
            "id",
            "business_session_id",
            "owner_user_id",
            "reference_set_id",
            name="uq_recommendations_id_session_owner_ref",
        ),
        UniqueConstraint("business_session_id", name="uq_recommendations_business_session"),
        UniqueConstraint("run_ref", name="uq_recommendations_run_ref"),
        UniqueConstraint("callback_event_id", name="uq_recommendations_callback_event"),
        Index("ix_recommendations_owner_status", "owner_user_id", "status", "created_at"),
        Index("ix_recommendations_reference", "reference_set_id", "created_at"),
        Index("ix_recommendations_expiry", "data_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reference_set_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    business_session_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    source_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'synthetic_mock'")
    )
    dify_run_id: Mapped[str | None] = mapped_column(String(320))
    callback_event_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    callback_digest_version: Mapped[int | None] = mapped_column(SmallInteger)
    callback_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'analyzing'")
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    original_recommendation: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    public_options: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    image_types: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    audit: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    state: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    run_ref: Mapped[str | None] = mapped_column(String(320))
    form_ref: Mapped[str | None] = mapped_column(String(320))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retention_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("60")
    )
    retention_policy_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class MockRecommendationRun(Base):
    """Persistent synthetic Dify run; never stores an image or contacts a model."""

    __tablename__ = "mock_recommendation_runs"
    __table_args__ = (
        CheckConstraint("status IN ('analyzing', 'ready', 'failed')", name="status_check"),
        CheckConstraint(
            "status <> 'ready' OR (public_options IS NOT NULL "
            "AND original_recommendation IS NOT NULL AND valid_until IS NOT NULL)",
            name="ready_fields_check",
        ),
        CheckConstraint("status <> 'failed' OR failure_code IS NOT NULL", name="failed_code_check"),
        ForeignKeyConstraint(
            ["business_id"],
            ["recommendations.id"],
            ondelete="RESTRICT",
            name="fk_mock_recommendation_runs_business",
        ),
        ForeignKeyConstraint(
            ["reference_set_id"],
            ["reference_sets.id"],
            ondelete="RESTRICT",
            name="fk_mock_recommendation_runs_reference_set",
        ),
        UniqueConstraint("business_id", name="uq_mock_recommendation_runs_business"),
        UniqueConstraint("run_ref", name="uq_mock_recommendation_runs_run_ref"),
        Index("ix_mock_recommendation_runs_status", "status", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reference_set_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_ref: Mapped[str] = mapped_column(String(320), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    reference_assets: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'ready'"))
    original_recommendation: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    public_options: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    image_types: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    audit: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    state: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    form_ref: Mapped[str | None] = mapped_column(String(320))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
