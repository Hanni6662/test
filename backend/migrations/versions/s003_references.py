"""Add private reference sets, assets, and durable object reservations."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s003_references"
down_revision = "s002_identity_reserved"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reference_sets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_category", sa.String(length=100), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'uploading'"), nullable=False
        ),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("upload_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint("status IN ('uploading', 'ready', 'failed')", name="status_check"),
        sa.CheckConstraint(
            "length(product_category) BETWEEN 1 AND 100", name="category_size_check"
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR (ready_at IS NOT NULL AND expires_at > ready_at)",
            name="expiration_order_check",
        ),
        sa.CheckConstraint(
            "(status = 'ready' AND ready_at IS NOT NULL AND expires_at IS NOT NULL "
            "AND upload_lease_until IS NULL) OR status <> 'ready'",
            name="ready_fields_check",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_reference_sets"),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_reference_sets_id_owner"),
    )
    op.create_index(
        "ix_reference_sets_owner_created", "reference_sets", ["owner_user_id", "created_at"]
    )
    op.create_index(
        "ix_reference_sets_status_lease", "reference_sets", ["status", "upload_lease_until"]
    )
    op.create_index("ix_reference_sets_expires_at", "reference_sets", ["expires_at"])

    op.create_table(
        "image_assets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_kind", sa.String(length=32), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=32), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "asset_kind IN ('reference_source', 'reference_model_input')", name="asset_kind_check"
        ),
        sa.CheckConstraint("length(content_sha256) = 64", name="sha256_size_check"),
        sa.CheckConstraint("size_bytes > 0", name="size_positive_check"),
        sa.CheckConstraint("width > 0 AND height > 0", name="dimensions_positive_check"),
        sa.CheckConstraint(
            "mime_type IN ('image/jpeg', 'image/png', 'image/webp')", name="mime_type_check"
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_image_assets"),
        sa.UniqueConstraint("object_key", name="uq_image_assets_object_key"),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_image_assets_id_owner"),
    )
    op.create_index(
        "ix_image_assets_owner_created", "image_assets", ["owner_user_id", "created_at"]
    )

    op.create_table(
        "reference_set_images",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("reference_set_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slot", sa.String(length=8), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("source_asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
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
        sa.ForeignKeyConstraint(
            ["reference_set_id", "owner_user_id"],
            ["reference_sets.id", "reference_sets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_reference_set_images_set_owner",
        ),
        sa.ForeignKeyConstraint(
            ["source_asset_id", "owner_user_id"],
            ["image_assets.id", "image_assets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_reference_set_images_source_owner",
        ),
        sa.ForeignKeyConstraint(
            ["model_asset_id", "owner_user_id"],
            ["image_assets.id", "image_assets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_reference_set_images_model_owner",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reference_set_images"),
        sa.UniqueConstraint("reference_set_id", "slot", name="uq_reference_set_images_slot"),
        sa.UniqueConstraint(
            "reference_set_id", "position", name="uq_reference_set_images_position"
        ),
        sa.UniqueConstraint(
            "reference_set_id", "content_sha256", name="uq_reference_set_images_content"
        ),
    )
    op.create_index(
        "ix_reference_set_images_owner",
        "reference_set_images",
        ["owner_user_id", "reference_set_id"],
    )

    op.create_table(
        "reference_upload_objects",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("reference_set_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slot", sa.String(length=8), nullable=False),
        sa.Column("asset_kind", sa.String(length=16), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("expected_sha256", sa.String(length=64), nullable=False),
        sa.Column("expected_size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default=sa.text("'planned'"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("asset_kind IN ('source', 'model_input')", name="asset_kind_check"),
        sa.CheckConstraint(
            "status IN ('planned', 'stored', 'attached', 'cleanup_pending', 'deleted')",
            name="status_check",
        ),
        sa.CheckConstraint("length(expected_sha256) = 64", name="sha256_size_check"),
        sa.CheckConstraint("expected_size_bytes > 0", name="size_positive_check"),
        sa.ForeignKeyConstraint(
            ["reference_set_id", "owner_user_id"],
            ["reference_sets.id", "reference_sets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_reference_upload_objects_set_owner",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reference_upload_objects"),
        sa.UniqueConstraint("object_key", name="uq_reference_upload_objects_key"),
    )
    op.create_index(
        "ix_reference_upload_objects_recovery",
        "reference_upload_objects",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_reference_upload_objects_set",
        "reference_upload_objects",
        ["reference_set_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_reference_upload_objects_set", table_name="reference_upload_objects")
    op.drop_index("ix_reference_upload_objects_recovery", table_name="reference_upload_objects")
    op.drop_table("reference_upload_objects")
    op.drop_index("ix_reference_set_images_owner", table_name="reference_set_images")
    op.drop_table("reference_set_images")
    op.drop_index("ix_image_assets_owner_created", table_name="image_assets")
    op.drop_table("image_assets")
    op.drop_index("ix_reference_sets_expires_at", table_name="reference_sets")
    op.drop_index("ix_reference_sets_status_lease", table_name="reference_sets")
    op.drop_index("ix_reference_sets_owner_created", table_name="reference_sets")
    op.drop_table("reference_sets")
