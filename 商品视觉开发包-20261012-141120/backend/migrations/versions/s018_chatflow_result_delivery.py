"""Persist Chatflow result manifests and private image delivery intents.

Revision ID: s018_chatflow_result_delivery
Revises: s017_h6_generation_tracking
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s018_chatflow_result_delivery"
down_revision = "s017_h6_generation_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "chatflow_generation_result_manifests",
        sa.Column("generation_request_id", uuid, nullable=False),
        sa.Column("source_sequence", sa.Integer(), nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("business_session_id", uuid, nullable=False),
        sa.Column("batch_id", sa.String(length=128), nullable=False),
        sa.Column("status_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("manifest_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("source_sequence >= 1", name="source_sequence_check"),
        sa.CheckConstraint("octet_length(status_digest) = 32", name="status_digest_size_check"),
        sa.CheckConstraint("octet_length(manifest_digest) = 32", name="manifest_digest_size_check"),
        sa.ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_manifests_request_task",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "business_session_id"],
            ["tasks.id", "tasks.business_session_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_manifests_task_session",
        ),
        sa.PrimaryKeyConstraint(
            "generation_request_id", "source_sequence",
            name="pk_chatflow_generation_result_manifests",
        ),
    )
    op.create_index(
        "ix_chatflow_generation_manifests_received",
        "chatflow_generation_result_manifests",
        ["received_at"],
    )

    op.create_table(
        "chatflow_generation_deliveries",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("generation_request_id", uuid, nullable=False),
        sa.Column("slot_id", sa.String(length=8), nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("task_item_id", uuid, nullable=False),
        sa.Column("attempt_id", uuid, nullable=False),
        sa.Column("owner_user_id", uuid, nullable=False),
        sa.Column("batch_id", sa.String(length=128), nullable=False),
        sa.Column("source_sequence", sa.Integer(), nullable=False),
        sa.Column("source_file_id", sa.String(length=255), nullable=True),
        sa.Column("source_sha256", sa.LargeBinary(length=32), nullable=True),
        sa.Column("source_mime_type", sa.String(length=32), nullable=True),
        sa.Column("source_size_bytes", sa.Integer(), nullable=True),
        sa.Column("source_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("original_object_key", sa.String(length=512), nullable=False),
        sa.Column("preview_object_key", sa.String(length=512), nullable=False),
        sa.Column("original_asset_id", uuid, nullable=True),
        sa.Column("preview_asset_id", uuid, nullable=True),
        sa.Column("original_size_bytes", sa.Integer(), nullable=True),
        sa.Column("original_sha256", sa.String(length=64), nullable=True),
        sa.Column("original_mime_type", sa.String(length=32), nullable=True),
        sa.Column("original_width", sa.Integer(), nullable=True),
        sa.Column("original_height", sa.Integer(), nullable=True),
        sa.Column("preview_size_bytes", sa.Integer(), nullable=True),
        sa.Column("preview_sha256", sa.String(length=64), nullable=True),
        sa.Column("preview_width", sa.Integer(), nullable=True),
        sa.Column("preview_height", sa.Integer(), nullable=True),
        sa.Column(
            "status", sa.String(length=28), server_default="pending_download", nullable=False
        ),
        sa.Column("download_attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("confirmation_attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("download_action_sequence", sa.Integer(), nullable=True),
        sa.Column("confirmation_action_sequence", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending_download', 'saved', 'awaiting_confirmation', 'confirmed', "
            "'source_unavailable', 'protocol_error')",
            name="status_check",
        ),
        sa.CheckConstraint("source_sequence >= 1", name="source_sequence_check"),
        sa.CheckConstraint("download_attempt_count >= 0", name="download_attempt_count_check"),
        sa.CheckConstraint(
            "confirmation_attempt_count >= 0", name="confirmation_attempt_count_check"
        ),
        sa.CheckConstraint(
            "download_action_sequence IS NULL OR download_action_sequence > 0",
            name="download_action_sequence_check",
        ),
        sa.CheckConstraint(
            "confirmation_action_sequence IS NULL OR confirmation_action_sequence > 0",
            name="confirmation_action_sequence_check",
        ),
        sa.CheckConstraint(
            "(source_file_id IS NULL AND source_sha256 IS NULL AND source_mime_type IS NULL "
            "AND source_size_bytes IS NULL) OR "
            "(source_file_id IS NOT NULL AND source_sha256 IS NOT NULL "
            "AND octet_length(source_sha256) = 32 AND source_mime_type IS NOT NULL "
            "AND source_mime_type IN ('image/jpeg', 'image/png', 'image/webp') "
            "AND source_size_bytes IS NOT NULL AND source_size_bytes > 0 "
            "AND source_size_bytes <= 33554432)",
            name="source_metadata_check",
        ),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(
            ["generation_request_id", "slot_id"],
            [
                "chatflow_generation_request_slots.generation_request_id",
                "chatflow_generation_request_slots.slot_id",
            ],
            ondelete="CASCADE",
            name="fk_chatflow_generation_deliveries_slot",
        ),
        sa.ForeignKeyConstraint(
            ["task_item_id", "task_id"],
            ["task_items.id", "task_items.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_deliveries_item_task",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "task_item_id"],
            ["task_item_attempts.id", "task_item_attempts.task_item_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_deliveries_attempt",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="RESTRICT",
            name="fk_chatflow_generation_deliveries_owner",
        ),
        sa.ForeignKeyConstraint(
            ["original_asset_id", "owner_user_id"],
            ["image_assets.id", "image_assets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_chatflow_generation_deliveries_original_asset",
        ),
        sa.ForeignKeyConstraint(
            ["preview_asset_id", "owner_user_id"],
            ["image_assets.id", "image_assets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_chatflow_generation_deliveries_preview_asset",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chatflow_generation_deliveries"),
        sa.UniqueConstraint(
            "generation_request_id", "slot_id", name="uq_chatflow_generation_delivery_slot"
        ),
        sa.UniqueConstraint(
            "id", "generation_request_id", "task_id",
            name="uq_chatflow_generation_delivery_id_request_task",
        ),
        sa.UniqueConstraint(
            "generation_request_id", "task_item_id", "attempt_id",
            name="uq_chatflow_generation_delivery_attempt",
        ),
    )
    op.create_index(
        "ix_chatflow_generation_deliveries_status",
        "chatflow_generation_deliveries",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_chatflow_generation_deliveries_original_key",
        "chatflow_generation_deliveries",
        ["original_object_key"],
    )
    op.create_index(
        "ix_chatflow_generation_deliveries_preview_key",
        "chatflow_generation_deliveries",
        ["preview_object_key"],
    )

    # Delivery Actions use their own scope UUID and sequence, separate from
    # H6 generation_request Actions so result delivery cannot consume an H6
    # query retry sequence.
    op.add_column(
        "task_actions",
        sa.Column("generation_delivery_id", uuid, nullable=True),
    )
    op.drop_constraint("scope_kind_check", "task_actions", type_="check")
    op.drop_constraint("scope_association_check", "task_actions", type_="check")
    op.create_check_constraint(
        "scope_kind_check",
        "task_actions",
        "scope_kind IN ('submission', 'attempt', 'business_session', "
        "'generation_request', 'generation_delivery')",
    )
    op.create_check_constraint(
        "scope_association_check",
        "task_actions",
        "(scope_kind = 'attempt' AND submission_id IS NULL AND attempt_id IS NOT NULL "
        "AND task_item_id IS NOT NULL AND business_session_id IS NULL "
        "AND business_question_id IS NULL AND business_answer_submission_id IS NULL "
        "AND generation_request_id IS NULL AND generation_delivery_id IS NULL "
        "AND task_id IS NOT NULL AND scope_id = attempt_id) OR "
        "(scope_kind = 'submission' AND submission_id IS NOT NULL AND scope_id = submission_id "
        "AND attempt_id IS NULL AND task_item_id IS NULL AND business_session_id IS NULL "
        "AND business_question_id IS NULL AND business_answer_submission_id IS NULL "
        "AND generation_request_id IS NULL AND generation_delivery_id IS NULL "
        "AND task_id IS NOT NULL) OR "
        "(scope_kind = 'business_session' AND business_session_id IS NOT NULL "
        "AND scope_id = business_session_id AND submission_id IS NULL "
        "AND attempt_id IS NULL AND task_item_id IS NULL AND generation_request_id IS NULL "
        "AND generation_delivery_id IS NULL) OR "
        "(scope_kind = 'generation_request' AND generation_request_id IS NOT NULL "
        "AND scope_id = generation_request_id AND task_id IS NOT NULL "
        "AND business_session_id IS NOT NULL AND submission_id IS NULL "
        "AND attempt_id IS NULL AND task_item_id IS NULL "
        "AND business_question_id IS NULL AND business_answer_submission_id IS NULL "
        "AND generation_delivery_id IS NULL) OR "
        "(scope_kind = 'generation_delivery' AND generation_delivery_id IS NOT NULL "
        "AND scope_id = generation_delivery_id AND generation_request_id IS NOT NULL "
        "AND task_id IS NOT NULL AND business_session_id IS NOT NULL "
        "AND task_item_id IS NOT NULL AND attempt_id IS NOT NULL "
        "AND submission_id IS NULL AND business_question_id IS NULL "
        "AND business_answer_submission_id IS NULL)",
    )
    op.create_foreign_key(
        "fk_task_actions_generation_delivery_chain",
        "task_actions",
        "chatflow_generation_deliveries",
        ["generation_delivery_id", "generation_request_id", "task_id"],
        ["id", "generation_request_id", "task_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_task_actions_generation_delivery_chain", "task_actions", type_="foreignkey"
    )
    op.drop_constraint("scope_association_check", "task_actions", type_="check")
    op.drop_constraint("scope_kind_check", "task_actions", type_="check")
    op.create_check_constraint(
        "scope_kind_check",
        "task_actions",
        "scope_kind IN ('submission', 'attempt', 'business_session', 'generation_request')",
    )
    op.create_check_constraint(
        "scope_association_check",
        "task_actions",
        "(scope_kind = 'attempt' AND submission_id IS NULL AND attempt_id IS NOT NULL "
        "AND task_item_id IS NOT NULL AND business_session_id IS NULL "
        "AND business_question_id IS NULL AND business_answer_submission_id IS NULL "
        "AND generation_request_id IS NULL AND generation_delivery_id IS NULL "
        "AND task_id IS NOT NULL AND scope_id = attempt_id) OR "
        "(scope_kind = 'submission' AND submission_id IS NOT NULL AND scope_id = submission_id "
        "AND attempt_id IS NULL AND task_item_id IS NULL AND business_session_id IS NULL "
        "AND business_question_id IS NULL AND business_answer_submission_id IS NULL "
        "AND generation_request_id IS NULL AND generation_delivery_id IS NULL "
        "AND task_id IS NOT NULL) OR "
        "(scope_kind = 'business_session' AND business_session_id IS NOT NULL "
        "AND scope_id = business_session_id AND submission_id IS NULL "
        "AND attempt_id IS NULL AND task_item_id IS NULL AND generation_request_id IS NULL "
        "AND generation_delivery_id IS NULL) OR "
        "(scope_kind = 'generation_request' AND generation_request_id IS NOT NULL "
        "AND scope_id = generation_request_id AND task_id IS NOT NULL "
        "AND business_session_id IS NOT NULL AND submission_id IS NULL "
        "AND attempt_id IS NULL AND task_item_id IS NULL "
        "AND business_question_id IS NULL AND business_answer_submission_id IS NULL "
        "AND generation_delivery_id IS NULL)",
    )
    op.drop_column("task_actions", "generation_delivery_id")
    op.drop_index(
        "ix_chatflow_generation_deliveries_preview_key",
        table_name="chatflow_generation_deliveries",
    )
    op.drop_index(
        "ix_chatflow_generation_deliveries_original_key",
        table_name="chatflow_generation_deliveries",
    )
    op.drop_index(
        "ix_chatflow_generation_deliveries_status",
        table_name="chatflow_generation_deliveries",
    )
    op.drop_table("chatflow_generation_deliveries")
    op.drop_index(
        "ix_chatflow_generation_manifests_received",
        table_name="chatflow_generation_result_manifests",
    )
    op.drop_table("chatflow_generation_result_manifests")
