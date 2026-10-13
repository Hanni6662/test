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


class BusinessSession(Base):
    """Durable Chatflow handoff root, created before any Dify action runs."""

    __tablename__ = "business_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('starting', 'awaiting_recommendation', 'recommendation_ready', "
            "'task_created', 'reconciling', 'failed', 'expired', 'cancelled')",
            name="status_check",
        ),
        CheckConstraint(
            "start_outcome IN ('pending', 'accepted', 'unknown', 'rejected')",
            name="start_outcome_check",
        ),
        CheckConstraint(
            "flow_version IN ('legacy', 'visual-v3.4-single-selection')",
            name="flow_version_check",
        ),
        CheckConstraint("octet_length(idempotency_key_hash) = 32", name="key_hash_size_check"),
        CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        CheckConstraint(
            "octet_length(reference_asset_digest) = 32", name="reference_digest_size_check"
        ),
        CheckConstraint("digest_version > 0", name="digest_version_check"),
        CheckConstraint("run_revision > 0", name="run_revision_check"),
        CheckConstraint("retention_days > 0", name="retention_days_check"),
        CheckConstraint("retention_policy_revision > 0", name="retention_policy_revision_check"),
        ForeignKeyConstraint(
            ["reference_set_id", "owner_user_id"],
            ["reference_sets.id", "reference_sets.owner_user_id"],
            ondelete="CASCADE",
            name="fk_business_sessions_reference_owner",
        ),
        ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="RESTRICT", name="fk_business_sessions_owner"
        ),
        UniqueConstraint("owner_user_id", "idempotency_key_hash", name="uq_business_session_key"),
        UniqueConstraint(
            "id", "owner_user_id", "reference_set_id", name="uq_business_session_owner_ref"
        ),
        Index("ix_business_sessions_owner_status", "owner_user_id", "status", "created_at"),
        Index("ix_business_sessions_reference", "reference_set_id", "created_at"),
        Index("ix_business_sessions_expiry", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reference_set_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    request_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    digest_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1")
    )
    reference_asset_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    reference_assets_snapshot: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'starting'")
    )
    flow_version: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'legacy'")
    )
    start_outcome: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    dify_user_id: Mapped[str] = mapped_column(String(160), nullable=False)
    dify_run_id: Mapped[str | None] = mapped_column(String(320))
    run_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    retention_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("60")
    )
    retention_policy_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
