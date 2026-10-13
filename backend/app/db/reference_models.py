from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ReferenceSet(Base):
    __tablename__ = "reference_sets"
    __table_args__ = (
        CheckConstraint("status IN ('uploading', 'ready', 'failed')", name="status_check"),
        CheckConstraint("length(product_category) BETWEEN 1 AND 100", name="category_size_check"),
        CheckConstraint("retention_days BETWEEN 1 AND 3650", name="retention_days_check"),
        CheckConstraint("retention_policy_revision > 0", name="retention_policy_revision_check"),
        CheckConstraint(
            "expires_at IS NULL OR (ready_at IS NOT NULL AND expires_at > ready_at)",
            name="expiration_order_check",
        ),
        CheckConstraint(
            "(status = 'ready' AND ready_at IS NOT NULL AND expires_at IS NOT NULL "
            "AND upload_lease_until IS NULL) OR status <> 'ready'",
            name="ready_fields_check",
        ),
        UniqueConstraint("id", "owner_user_id", name="uq_reference_sets_id_owner"),
        Index("ix_reference_sets_owner_created", "owner_user_id", "created_at"),
        Index("ix_reference_sets_status_lease", "status", "upload_lease_until"),
        Index("ix_reference_sets_expires_at", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    product_category: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'uploading'")
    )
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    upload_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    retention_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("60")
    )
    retention_policy_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )


class ImageAsset(Base):
    __tablename__ = "image_assets"
    __table_args__ = (
        CheckConstraint(
            "asset_kind IN ('reference_source', 'reference_model_input', "
            "'generation_original', 'generation_preview')",
            name="asset_kind_check",
        ),
        CheckConstraint("length(content_sha256) = 64", name="sha256_size_check"),
        CheckConstraint("size_bytes > 0", name="size_positive_check"),
        CheckConstraint("width > 0 AND height > 0", name="dimensions_positive_check"),
        CheckConstraint(
            "mime_type IN ('image/jpeg', 'image/png', 'image/webp')", name="mime_type_check"
        ),
        UniqueConstraint("object_key", name="uq_image_assets_object_key"),
        UniqueConstraint("id", "owner_user_id", name="uq_image_assets_id_owner"),
        Index("ix_image_assets_owner_created", "owner_user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    asset_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(32), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class ReferenceSetImage(Base):
    __tablename__ = "reference_set_images"
    __table_args__ = (
        CheckConstraint(
            "(slot = 'PRIMARY' AND position = 0) OR "
            "(slot = 'AUX01' AND position = 1) OR "
            "(slot = 'AUX02' AND position = 2) OR "
            "(slot = 'AUX03' AND position = 3)",
            name="slot_position_check",
        ),
        CheckConstraint("length(content_sha256) = 64", name="sha256_size_check"),
        ForeignKeyConstraint(
            ["reference_set_id", "owner_user_id"],
            ["reference_sets.id", "reference_sets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_reference_set_images_set_owner",
        ),
        ForeignKeyConstraint(
            ["source_asset_id", "owner_user_id"],
            ["image_assets.id", "image_assets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_reference_set_images_source_owner",
        ),
        ForeignKeyConstraint(
            ["model_asset_id", "owner_user_id"],
            ["image_assets.id", "image_assets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_reference_set_images_model_owner",
        ),
        UniqueConstraint("reference_set_id", "slot", name="uq_reference_set_images_slot"),
        UniqueConstraint("reference_set_id", "position", name="uq_reference_set_images_position"),
        UniqueConstraint(
            "id",
            "reference_set_id",
            "owner_user_id",
            "model_asset_id",
            "slot",
            "position",
            "content_sha256",
            name="uq_reference_set_image_bridge_identity",
        ),
        UniqueConstraint(
            "reference_set_id", "content_sha256", name="uq_reference_set_images_content"
        ),
        Index("ix_reference_set_images_owner", "owner_user_id", "reference_set_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    reference_set_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    slot: Mapped[str] = mapped_column(String(8), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    source_asset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    model_asset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class ReferenceUploadObject(Base):
    """Durable object locator reserved before storage I/O for orphan recovery."""

    __tablename__ = "reference_upload_objects"
    __table_args__ = (
        CheckConstraint("asset_kind IN ('source', 'model_input')", name="asset_kind_check"),
        CheckConstraint(
            "status IN ('planned', 'stored', 'attached', 'cleanup_pending', 'deleted')",
            name="status_check",
        ),
        CheckConstraint("length(expected_sha256) = 64", name="sha256_size_check"),
        CheckConstraint("expected_size_bytes > 0", name="size_positive_check"),
        ForeignKeyConstraint(
            ["reference_set_id", "owner_user_id"],
            ["reference_sets.id", "reference_sets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_reference_upload_objects_set_owner",
        ),
        UniqueConstraint("object_key", name="uq_reference_upload_objects_key"),
        Index("ix_reference_upload_objects_recovery", "status", "created_at"),
        Index("ix_reference_upload_objects_set", "reference_set_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    reference_set_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    slot: Mapped[str] = mapped_column(String(8), nullable=False)
    asset_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'planned'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
