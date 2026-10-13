"""Persist private reference-to-Dify file mappings for H8.

Revision ID: s019_h8_dify_reference_files
Revises: s018_chatflow_result_delivery
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s019_h8_dify_reference_files"
down_revision = "s018_chatflow_result_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_unique_constraint(
        "uq_reference_set_image_bridge_identity",
        "reference_set_images",
        [
            "id",
            "reference_set_id",
            "owner_user_id",
            "model_asset_id",
            "slot",
            "position",
            "content_sha256",
        ],
    )
    op.create_table(
        "chatflow_dify_reference_files",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("business_session_id", uuid, nullable=False),
        sa.Column("owner_user_id", uuid, nullable=False),
        sa.Column("reference_set_id", uuid, nullable=False),
        sa.Column("reference_set_image_id", uuid, nullable=False),
        sa.Column("model_asset_id", uuid, nullable=False),
        sa.Column("slot", sa.String(length=8), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("dify_app_id", sa.String(length=160), nullable=False),
        sa.Column("dify_user_id", sa.String(length=160), nullable=False),
        sa.Column("upload_action_id", uuid, nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("dify_file_id", sa.String(length=255), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
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
            "(slot = 'PRIMARY' AND position = 0) OR "
            "(slot = 'AUX01' AND position = 1) OR "
            "(slot = 'AUX02' AND position = 2) OR "
            "(slot = 'AUX03' AND position = 3)",
            name="slot_position_check",
        ),
        sa.CheckConstraint("length(content_sha256) = 64", name="sha256_size_check"),
        sa.CheckConstraint(
            "status IN ('pending', 'uploaded', 'unknown', 'rejected')", name="status_check"
        ),
        sa.CheckConstraint(
            "(status = 'uploaded' AND dify_file_id IS NOT NULL AND uploaded_at IS NOT NULL) OR "
            "(status <> 'uploaded' AND dify_file_id IS NULL AND uploaded_at IS NULL)",
            name="uploaded_fields_check",
        ),
        sa.ForeignKeyConstraint(
            ["business_session_id", "owner_user_id", "reference_set_id"],
            [
                "business_sessions.id",
                "business_sessions.owner_user_id",
                "business_sessions.reference_set_id",
            ],
            ondelete="CASCADE",
            name="fk_chatflow_dify_reference_files_session_owner_ref",
        ),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["model_asset_id", "owner_user_id"],
            ["image_assets.id", "image_assets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_chatflow_dify_reference_files_asset_owner",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chatflow_dify_reference_files"),
        sa.UniqueConstraint(
            "business_session_id", "position", name="uq_chatflow_dify_reference_files_position"
        ),
        sa.UniqueConstraint(
            "business_session_id", "model_asset_id", name="uq_chatflow_dify_reference_files_asset"
        ),
        sa.UniqueConstraint("upload_action_id", name="uq_chatflow_dify_reference_files_action"),
        sa.UniqueConstraint(
            "dify_app_id",
            "dify_user_id",
            "dify_file_id",
            name="uq_chatflow_dify_reference_files_remote_file",
        ),
    )
    op.create_index(
        "ix_chatflow_dify_reference_files_session",
        "chatflow_dify_reference_files",
        ["business_session_id", "position"],
    )
    op.create_index(
        "ix_chatflow_dify_reference_files_status",
        "chatflow_dify_reference_files",
        ["status", "created_at"],
    )
    # In-flight S018 starts have no persisted file IDs and cannot be safely
    # replayed. Keep them visibly reconciling and cancel only starts that have
    # not yet been claimed; deployments must quiesce old workers before upgrade.
    op.execute(
        sa.text(
            "UPDATE business_sessions SET status = 'reconciling', start_outcome = 'unknown', "
            "updated_at = clock_timestamp() WHERE status = 'starting' AND start_outcome = 'pending'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE recommendations SET state = jsonb_set(state, '{stage}', "
            "'\"reference_file_bridge_required\"'::jsonb, true), updated_at = clock_timestamp() "
            "WHERE source_kind = 'dify_chatflow' AND status = 'analyzing' AND business_session_id "
            "IN (SELECT id FROM business_sessions WHERE status = 'reconciling' "
            "AND start_outcome = 'unknown')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE task_actions SET status = 'cancelled', last_outcome = 'unknown', "
            "last_error_code = 'H8_REFERENCE_BRIDGE_REQUIRED', "
            "completed_at = clock_timestamp(), updated_at = clock_timestamp() "
            "WHERE action_kind = 'dify.chatflow.start.v1' AND status = 'scheduled' "
            "AND business_session_id IN (SELECT id FROM business_sessions "
            "WHERE status = 'reconciling' AND start_outcome = 'unknown')"
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_chatflow_dify_reference_files_status", table_name="chatflow_dify_reference_files"
    )
    op.drop_index(
        "ix_chatflow_dify_reference_files_session", table_name="chatflow_dify_reference_files"
    )
    op.drop_table("chatflow_dify_reference_files")
    op.drop_constraint(
        "uq_reference_set_image_bridge_identity",
        "reference_set_images",
        type_="unique",
    )
