from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
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

_JSON = JSON().with_variant(JSONB(), "postgresql")


class ChatflowPromptPackage(Base):
    """Immutable H4 prompt package tied to the H2 Chatflow Task."""

    __tablename__ = "chatflow_prompt_packages"
    __table_args__ = (
        CheckConstraint("recommendation_revision > 0", name="revision_check"),
        CheckConstraint("package_digest_version > 0", name="package_digest_version_check"),
        CheckConstraint("request_digest_version > 0", name="request_digest_version_check"),
        CheckConstraint("octet_length(package_digest) = 32", name="package_digest_size_check"),
        CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        CheckConstraint("contract_version = 'chatflow-handoff-v1'", name="contract_version_check"),
        UniqueConstraint("submission_id", name="uq_chatflow_prompt_packages_submission"),
        UniqueConstraint("task_id", name="uq_chatflow_prompt_packages_task"),
        UniqueConstraint("id", "task_id", name="uq_chatflow_prompt_packages_id_task"),
        ForeignKeyConstraint(
            ["task_id", "business_session_id"],
            ["tasks.id", "tasks.business_session_id"],
            ondelete="CASCADE",
            name="fk_chatflow_prompt_packages_task_session",
        ),
        ForeignKeyConstraint(
            ["business_session_id", "owner_user_id", "reference_set_id"],
            [
                "business_sessions.id",
                "business_sessions.owner_user_id",
                "business_sessions.reference_set_id",
            ],
            ondelete="CASCADE",
            name="fk_chatflow_prompt_packages_session_owner_ref",
        ),
        ForeignKeyConstraint(
            ["recommendation_id", "business_session_id", "owner_user_id", "reference_set_id"],
            [
                "recommendations.id",
                "recommendations.business_session_id",
                "recommendations.owner_user_id",
                "recommendations.reference_set_id",
            ],
            ondelete="CASCADE",
            name="fk_chatflow_prompt_packages_recommendation_session",
        ),
        Index("ix_chatflow_prompt_packages_task_created", "task_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    submission_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    business_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    recommendation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reference_set_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    recommendation_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    dify_run_id: Mapped[str] = mapped_column(String(320), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(32), nullable=False)
    package_version: Mapped[str] = mapped_column(String(80), nullable=False)
    package_digest_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    package_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    request_digest_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    request_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    image_types: Mapped[list[str]] = mapped_column(_JSON, nullable=False)
    package: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class ChatflowPromptImage(Base):
    """One immutable prompt image and its stable existing TaskItem mapping."""

    __tablename__ = "chatflow_prompt_images"
    __table_args__ = (
        CheckConstraint("position BETWEEN 1 AND 8", name="position_check"),
        UniqueConstraint("task_item_id", name="uq_chatflow_prompt_images_task_item"),
        UniqueConstraint("package_id", "position", name="uq_chatflow_prompt_images_position"),
        ForeignKeyConstraint(
            ["package_id", "task_id"],
            ["chatflow_prompt_packages.id", "chatflow_prompt_packages.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_prompt_images_package_task",
        ),
        ForeignKeyConstraint(
            ["task_item_id", "task_id"],
            ["task_items.id", "task_items.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_prompt_images_task_item",
        ),
        Index("ix_chatflow_prompt_images_package", "package_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    package_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_item_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    image_type: Mapped[str] = mapped_column(String(80), nullable=False)
    prompt_image: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class ChatflowPromptReference(Base):
    """Ordered reference manifest pinned to the exact task package."""

    __tablename__ = "chatflow_prompt_references"
    __table_args__ = (
        CheckConstraint("position BETWEEN 1 AND 4", name="position_check"),
        CheckConstraint("length(content_sha256) = 64", name="sha256_size_check"),
        UniqueConstraint("package_id", "ref_id", name="uq_chatflow_prompt_references_alias"),
        UniqueConstraint("package_id", "position", name="uq_chatflow_prompt_references_position"),
        ForeignKeyConstraint(
            ["package_id", "task_id"],
            ["chatflow_prompt_packages.id", "chatflow_prompt_packages.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_prompt_references_package_task",
        ),
        ForeignKeyConstraint(
            ["asset_id"],
            ["image_assets.id"],
            ondelete="RESTRICT",
            name="fk_chatflow_prompt_references_asset",
        ),
        Index("ix_chatflow_prompt_references_package", "package_id", "position"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    package_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    ref_id: Mapped[str] = mapped_column(String(8), nullable=False)
    asset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class ChatflowGenerationRequest(Base):
    """One stable initial generation request produced by an H4 submission."""

    __tablename__ = "chatflow_generation_requests"
    __table_args__ = (
        CheckConstraint("octet_length(package_digest) = 32", name="package_digest_size_check"),
        CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        UniqueConstraint("package_id", name="uq_chatflow_generation_requests_package"),
        UniqueConstraint("task_id", name="uq_chatflow_generation_requests_task"),
        UniqueConstraint("id", "task_id", name="uq_chatflow_generation_requests_id_task"),
        ForeignKeyConstraint(
            ["package_id", "task_id"],
            ["chatflow_prompt_packages.id", "chatflow_prompt_packages.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_requests_package_task",
        ),
        ForeignKeyConstraint(
            ["task_id", "business_session_id"],
            ["tasks.id", "tasks.business_session_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_requests_task_session",
        ),
        Index("ix_chatflow_generation_requests_session", "business_session_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    package_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    business_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    service_authority_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    dify_run_id: Mapped[str] = mapped_column(String(320), nullable=False)
    package_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    request_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class ChatflowGenerationRequestSlot(Base):
    """Stable slot ↔ existing TaskItem/TaskItemAttempt association."""

    __tablename__ = "chatflow_generation_request_slots"
    __table_args__ = (
        CheckConstraint("position BETWEEN 1 AND 8", name="position_check"),
        CheckConstraint("slot_id = 'slot_0' || position::text", name="slot_position_check"),
        UniqueConstraint("task_item_id", "attempt_id", name="uq_chatflow_generation_slot_attempt"),
        UniqueConstraint(
            "generation_request_id", "position", name="uq_chatflow_generation_slot_position"
        ),
        ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_slots_request_task",
        ),
        ForeignKeyConstraint(
            ["task_item_id", "task_id"],
            ["task_items.id", "task_items.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_slots_task_item",
        ),
        ForeignKeyConstraint(
            ["attempt_id", "task_item_id"],
            ["task_item_attempts.id", "task_item_attempts.task_item_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_slots_attempt",
        ),
        Index("ix_chatflow_generation_slots_task", "task_id", "position"),
    )

    generation_request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    slot_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_item_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    image_type: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class ChatflowGenerationPermit(Base):
    """Persisted permit claim; only a digest of the signed bearer is stored."""

    __tablename__ = "chatflow_generation_permits"
    __table_args__ = (
        CheckConstraint("octet_length(package_digest) = 32", name="package_digest_size_check"),
        CheckConstraint("octet_length(token_digest) = 32", name="token_digest_size_check"),
        CheckConstraint("contract_version = 'chatflow-handoff-v1'", name="contract_version_check"),
        CheckConstraint("permit_version IN (1, 2)", name="permit_version_check"),
        CheckConstraint(
            "(permit_version = 1 AND slot_mapping_digest IS NULL "
            "AND reference_manifest_digest IS NULL) OR "
            "(permit_version = 2 AND slot_mapping_digest IS NOT NULL "
            "AND reference_manifest_digest IS NOT NULL "
            "AND octet_length(slot_mapping_digest) = 32 "
            "AND octet_length(reference_manifest_digest) = 32)",
            name="permit_v2_digests_check",
        ),
        UniqueConstraint("generation_request_id", name="uq_chatflow_generation_permit_request"),
        ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_permits_request_task",
        ),
        Index("ix_chatflow_generation_permits_expiry", "expires_at"),
    )

    permit_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    generation_request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    service_authority_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    package_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(32), nullable=False)
    permit_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    slot_mapping_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32), nullable=True)
    reference_manifest_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32), nullable=True)
    token_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class ChatflowGenerationTracking(Base):
    """Backend-owned status and bounded read-only reconciliation state per request."""

    __tablename__ = "chatflow_generation_tracking"
    __table_args__ = (
        CheckConstraint(
            "status IN ('awaiting_batch', 'queued', 'running', 'submission_unknown', "
            "'completed', 'partial_failed', 'failed', 'rejected_before_acceptance')",
            name="status_check",
        ),
        CheckConstraint(
            "reconcile_state IN ('scheduled', 'active', 'complete', 'unknown')",
            name="reconcile_state_check",
        ),
        CheckConstraint("reconcile_query_count >= 0", name="reconcile_count_check"),
        CheckConstraint(
            "source_sequence IS NULL OR source_sequence >= 1", name="source_sequence_check"
        ),
        UniqueConstraint("batch_id", name="uq_chatflow_generation_tracking_batch"),
        ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_tracking_request_task",
        ),
        Index("ix_chatflow_generation_tracking_due", "reconcile_state", "next_reconcile_at"),
    )

    generation_request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    batch_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'awaiting_batch'")
    )
    source_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32), nullable=True)
    reconcile_state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'scheduled'")
    )
    reconcile_query_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    next_reconcile_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconcile_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class ChatflowGenerationSlotState(Base):
    """Latest accepted status for one immutable H4 request slot."""

    __tablename__ = "chatflow_generation_slot_states"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'submitting', 'submission_unknown', 'pending', 'processing', "
            "'persisting', 'completed', 'failed', 'cancelled', 'protocol_error')",
            name="status_check",
        ),
        CheckConstraint("status_version >= 1", name="status_version_check"),
        CheckConstraint(
            "known_retry_count IS NULL OR known_retry_count >= 0", name="retry_count_check"
        ),
        CheckConstraint("octet_length(status_digest) = 32", name="status_digest_size_check"),
        ForeignKeyConstraint(
            ["generation_request_id", "slot_id"],
            [
                "chatflow_generation_request_slots.generation_request_id",
                "chatflow_generation_request_slots.slot_id",
            ],
            ondelete="CASCADE",
            name="fk_chatflow_generation_slot_states_slot",
        ),
    )

    generation_request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    slot_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    status_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    known_retry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class ChatflowGenerationEventReceipt(Base):
    """Idempotency and source-sequence fence for Dify-forwarded service snapshots."""

    __tablename__ = "chatflow_generation_event_receipts"
    __table_args__ = (
        CheckConstraint("source_sequence >= 1", name="source_sequence_check"),
        CheckConstraint("octet_length(event_digest) = 32", name="event_digest_size_check"),
        CheckConstraint("octet_length(snapshot_digest) = 32", name="snapshot_digest_size_check"),
        ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_event_request_task",
        ),
        Index(
            "ix_chatflow_generation_event_request_seq",
            "generation_request_id",
            "source_sequence",
        ),
    )

    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    generation_request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    snapshot_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class ChatflowGenerationResultManifest(Base):
    """Immutable receipt for one H6 service snapshot plus its source artifacts."""

    __tablename__ = "chatflow_generation_result_manifests"
    __table_args__ = (
        CheckConstraint("source_sequence >= 1", name="source_sequence_check"),
        CheckConstraint("octet_length(status_digest) = 32", name="status_digest_size_check"),
        CheckConstraint("octet_length(manifest_digest) = 32", name="manifest_digest_size_check"),
        ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_manifests_request_task",
        ),
        ForeignKeyConstraint(
            ["task_id", "business_session_id"],
            ["tasks.id", "tasks.business_session_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_manifests_task_session",
        ),
        Index("ix_chatflow_generation_manifests_received", "received_at"),
    )

    generation_request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    source_sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    business_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    manifest_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class ChatflowGenerationDelivery(Base):
    """One immutable H6 slot result and its private-save/confirmation lifecycle."""

    __tablename__ = "chatflow_generation_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_download', 'saved', 'awaiting_confirmation', 'confirmed', "
            "'source_unavailable', 'protocol_error')",
            name="status_check",
        ),
        CheckConstraint("source_sequence >= 1", name="source_sequence_check"),
        CheckConstraint("download_attempt_count >= 0", name="download_attempt_count_check"),
        CheckConstraint("confirmation_attempt_count >= 0", name="confirmation_attempt_count_check"),
        CheckConstraint(
            "download_action_sequence IS NULL OR download_action_sequence > 0",
            name="download_action_sequence_check",
        ),
        CheckConstraint(
            "confirmation_action_sequence IS NULL OR confirmation_action_sequence > 0",
            name="confirmation_action_sequence_check",
        ),
        CheckConstraint(
            "(source_file_id IS NULL AND source_sha256 IS NULL AND source_mime_type IS NULL "
            "AND source_size_bytes IS NULL) OR "
            "(source_file_id IS NOT NULL AND source_sha256 IS NOT NULL "
            "AND octet_length(source_sha256) = 32 AND source_mime_type IS NOT NULL "
            "AND source_mime_type IN ('image/jpeg', 'image/png', 'image/webp') "
            "AND source_size_bytes IS NOT NULL AND source_size_bytes > 0 "
            "AND source_size_bytes <= 33554432)",
            name="source_metadata_check",
        ),
        CheckConstraint(
            "(status IN ('saved', 'awaiting_confirmation', 'confirmed') "
            "AND original_asset_id IS NOT NULL AND preview_asset_id IS NOT NULL "
            "AND original_size_bytes IS NOT NULL AND original_sha256 IS NOT NULL "
            "AND original_mime_type IS NOT NULL AND original_width IS NOT NULL "
            "AND original_height IS NOT NULL AND preview_size_bytes IS NOT NULL "
            "AND preview_sha256 IS NOT NULL AND preview_width IS NOT NULL "
            "AND preview_height IS NOT NULL) OR "
            "(status NOT IN ('saved', 'awaiting_confirmation', 'confirmed'))",
            name="saved_asset_metadata_check",
        ),
        ForeignKeyConstraint(
            ["generation_request_id", "slot_id"],
            [
                "chatflow_generation_request_slots.generation_request_id",
                "chatflow_generation_request_slots.slot_id",
            ],
            ondelete="CASCADE",
            name="fk_chatflow_generation_deliveries_slot",
        ),
        ForeignKeyConstraint(
            ["task_item_id", "task_id"],
            ["task_items.id", "task_items.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_deliveries_item_task",
        ),
        ForeignKeyConstraint(
            ["attempt_id", "task_item_id"],
            ["task_item_attempts.id", "task_item_attempts.task_item_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_deliveries_attempt",
        ),
        ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_chatflow_generation_deliveries_owner",
        ),
        ForeignKeyConstraint(
            ["original_asset_id", "owner_user_id"],
            ["image_assets.id", "image_assets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_chatflow_generation_deliveries_original_asset",
        ),
        ForeignKeyConstraint(
            ["preview_asset_id", "owner_user_id"],
            ["image_assets.id", "image_assets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_chatflow_generation_deliveries_preview_asset",
        ),
        UniqueConstraint(
            "generation_request_id", "slot_id", name="uq_chatflow_generation_delivery_slot"
        ),
        UniqueConstraint(
            "id",
            "generation_request_id",
            "task_id",
            name="uq_chatflow_generation_delivery_id_request_task",
        ),
        UniqueConstraint(
            "generation_request_id",
            "task_item_id",
            "attempt_id",
            name="uq_chatflow_generation_delivery_attempt",
        ),
        Index("ix_chatflow_generation_deliveries_status", "status", "updated_at"),
        Index("ix_chatflow_generation_deliveries_original_key", "original_object_key"),
        Index("ix_chatflow_generation_deliveries_preview_key", "preview_object_key"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    generation_request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    slot_id: Mapped[str] = mapped_column(String(8), nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_item_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_sha256: Mapped[bytes | None] = mapped_column(LargeBinary(32), nullable=True)
    source_mime_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    original_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    preview_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    original_asset_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    preview_asset_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    original_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    original_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_mime_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    original_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    original_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preview_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preview_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preview_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preview_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(28), nullable=False, server_default=text("'pending_download'")
    )
    download_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    confirmation_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    download_action_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmation_action_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class ChatflowGenerationAcceptanceEvidence(Base):
    """One immutable authoritative service observation for the original request."""

    __tablename__ = "chatflow_generation_acceptance_evidence"
    __table_args__ = (
        CheckConstraint("outcome IN ('accepted', 'rejected_expired')", name="outcome_check"),
        CheckConstraint("octet_length(evidence_digest) = 32", name="evidence_digest_size_check"),
        CheckConstraint(
            "octet_length(service_request_digest) = 32", name="service_request_digest_size_check"
        ),
        CheckConstraint("permit_version = 2", name="permit_version_check"),
        CheckConstraint(
            "(outcome = 'accepted' AND batch_id IS NOT NULL AND receipt_digest IS NULL "
            "AND receipt_id IS NULL AND closed_at IS NULL) OR "
            "(outcome = 'rejected_expired' AND batch_id IS NULL "
            "AND receipt_digest IS NOT NULL AND receipt_id IS NOT NULL)",
            name="evidence_shape_check",
        ),
        ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_acceptance_request_task",
        ),
        UniqueConstraint("batch_id", name="uq_chatflow_generation_acceptance_batch"),
        Index("ix_chatflow_generation_acceptance_task", "task_id", "created_at"),
    )

    generation_request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    service_authority_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    batch_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    permit_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    permit_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    permit_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    service_request_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    evidence_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    receipt_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32), nullable=True)
    receipt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    evidence: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatflowGenerationExpiryAssessment(Base):
    """Persistent bounded GET-only acceptance assessment budget per request."""

    __tablename__ = "chatflow_generation_expiry_assessments"
    __table_args__ = (
        CheckConstraint(
            "state IN ('scheduled', 'active', 'accepted', 'rejected_expired', 'unknown')",
            name="state_check",
        ),
        CheckConstraint("query_count BETWEEN 0 AND 3", name="query_count_check"),
        ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_expiry_request_task",
        ),
        Index("ix_chatflow_generation_expiry_due", "state", "next_action_at"),
    )

    generation_request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'scheduled'")
    )
    query_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class ChatflowGenerationAcceptedBatchFollowup(Base):
    """Independent, bounded status continuation after accepted evidence and H6 exhaustion."""

    __tablename__ = "chatflow_generation_accepted_batch_followups"
    __table_args__ = (
        CheckConstraint(
            "state IN ('scheduled', 'active', 'awaiting_delivery', 'complete', 'unknown')",
            name="state_check",
        ),
        CheckConstraint("query_count BETWEEN 0 AND 5", name="query_count_check"),
        CheckConstraint(
            "octet_length(acceptance_digest) = 32", name="acceptance_digest_size_check"
        ),
        CheckConstraint(
            "(state = 'complete' AND resolved_at IS NOT NULL AND next_action_at IS NULL) OR "
            "(state <> 'complete' AND resolved_at IS NULL)",
            name="resolution_timestamp_check",
        ),
        ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_followup_request_task",
        ),
        UniqueConstraint("batch_id", name="uq_chatflow_generation_followup_batch"),
        Index("ix_chatflow_generation_followup_due", "state", "next_action_at"),
    )

    generation_request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(128), nullable=False)
    service_authority_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    acceptance_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'scheduled'")
    )
    query_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
