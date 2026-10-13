from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ChatflowDifyReferenceFile(Base):
    """Private, owner-scoped mapping from a normalized reference to one Dify file ID."""

    __tablename__ = "chatflow_dify_reference_files"
    __table_args__ = (
        CheckConstraint(
            "(slot = 'PRIMARY' AND position = 0) OR "
            "(slot = 'AUX01' AND position = 1) OR "
            "(slot = 'AUX02' AND position = 2) OR "
            "(slot = 'AUX03' AND position = 3)",
            name="slot_position_check",
        ),
        CheckConstraint("length(content_sha256) = 64", name="sha256_size_check"),
        CheckConstraint(
            "status IN ('pending', 'uploaded', 'unknown', 'rejected')", name="status_check"
        ),
        CheckConstraint(
            "(status = 'uploaded' AND dify_file_id IS NOT NULL AND uploaded_at IS NOT NULL) OR "
            "(status <> 'uploaded' AND dify_file_id IS NULL AND uploaded_at IS NULL)",
            name="uploaded_fields_check",
        ),
        ForeignKeyConstraint(
            ["business_session_id", "owner_user_id", "reference_set_id"],
            [
                "business_sessions.id",
                "business_sessions.owner_user_id",
                "business_sessions.reference_set_id",
            ],
            ondelete="CASCADE",
            name="fk_chatflow_dify_reference_files_session_owner_ref",
        ),
        ForeignKeyConstraint(
            [
                "reference_set_image_id",
                "reference_set_id",
                "owner_user_id",
                "model_asset_id",
                "slot",
                "position",
                "content_sha256",
            ],
            [
                "reference_set_images.id",
                "reference_set_images.reference_set_id",
                "reference_set_images.owner_user_id",
                "reference_set_images.model_asset_id",
                "reference_set_images.slot",
                "reference_set_images.position",
                "reference_set_images.content_sha256",
            ],
            ondelete="RESTRICT",
            name="fk_chatflow_dify_reference_files_image_identity",
        ),
        ForeignKeyConstraint(
            ["model_asset_id", "owner_user_id"],
            ["image_assets.id", "image_assets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_chatflow_dify_reference_files_asset_owner",
        ),
        UniqueConstraint(
            "business_session_id", "position", name="uq_chatflow_dify_reference_files_position"
        ),
        UniqueConstraint(
            "business_session_id", "model_asset_id", name="uq_chatflow_dify_reference_files_asset"
        ),
        UniqueConstraint(
            "upload_action_id", name="uq_chatflow_dify_reference_files_action"
        ),
        UniqueConstraint(
            "dify_app_id", "dify_user_id", "dify_file_id",
            name="uq_chatflow_dify_reference_files_remote_file",
        ),
        Index("ix_chatflow_dify_reference_files_session", "business_session_id", "position"),
        Index("ix_chatflow_dify_reference_files_status", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reference_set_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reference_set_image_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    model_asset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    slot: Mapped[str] = mapped_column(String(8), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dify_app_id: Mapped[str] = mapped_column(String(160), nullable=False)
    dify_user_id: Mapped[str] = mapped_column(String(160), nullable=False)
    upload_action_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    dify_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
