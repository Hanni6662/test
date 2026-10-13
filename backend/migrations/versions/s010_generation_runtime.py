"""Persist generation capacity, attempt context, and private image results.

Revision ID: s010_generation_runtime
Revises: s009_prompt_workflows
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s010_generation_runtime"
down_revision = "s009_prompt_workflows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())

    op.drop_constraint("asset_kind_check", "image_assets", type_="check")
    op.create_check_constraint(
        "asset_kind_check",
        "image_assets",
        "asset_kind IN ('reference_source', 'reference_model_input', "
        "'generation_original', 'generation_preview')",
    )

    op.create_table(
        "generation_capacity_control",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("lock_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="singleton_check"),
        sa.CheckConstraint("lock_version > 0", name="lock_version_check"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "generation_capacity_reservations",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("attempt_id", uuid, nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("task_item_id", uuid, nullable=False),
        sa.Column("owner_user_id", uuid, nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'active'"), nullable=False
        ),
        sa.Column(
            "reserved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active', 'released')", name="status_check"),
        sa.CheckConstraint(
            "(status = 'active' AND released_at IS NULL) OR "
            "(status = 'released' AND released_at IS NOT NULL)",
            name="release_timestamp_check",
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["task_item_attempts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["task_item_id", "task_id"],
            ["task_items.id", "task_items.task_id"],
            ondelete="CASCADE",
            name="fk_generation_capacity_item_task",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_generation_capacity_owner",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", name="uq_generation_capacity_attempt"),
    )
    op.create_index(
        "ix_generation_capacity_status_reserved",
        "generation_capacity_reservations",
        ["status", "reserved_at"],
    )
    op.create_index(
        "ix_generation_capacity_owner_status",
        "generation_capacity_reservations",
        ["owner_user_id", "status"],
    )

    op.create_table(
        "generation_attempt_contexts",
        sa.Column("attempt_id", uuid, nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("task_item_id", uuid, nullable=False),
        sa.Column("owner_user_id", uuid, nullable=False),
        sa.Column("prompt_package_id", uuid, nullable=False),
        sa.Column("prompt_item_id", uuid, nullable=False),
        sa.Column("package_digest_version", sa.SmallInteger(), nullable=False),
        sa.Column("package_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("request_digest_version", sa.SmallInteger(), nullable=False),
        sa.Column("request_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("prompt_package_version", sa.String(length=200), nullable=False),
        sa.Column("image_type", sa.String(length=80), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("reference_asset_ids", jsonb, nullable=False),
        sa.Column("excluded_reference_aliases", jsonb, nullable=False),
        sa.Column("credential_secret_ref", sa.String(length=160), nullable=True),
        sa.Column("result_digest", sa.LargeBinary(length=32), nullable=True),
        sa.Column("original_object_key", sa.String(length=512), nullable=False),
        sa.Column("preview_object_key", sa.String(length=512), nullable=False),
        sa.Column(
            "save_state",
            sa.String(length=24),
            server_default=sa.text("'awaiting_result'"),
            nullable=False,
        ),
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
        sa.CheckConstraint("package_digest_version > 0", name="package_digest_version_check"),
        sa.CheckConstraint("octet_length(package_digest) = 32", name="package_digest_size_check"),
        sa.CheckConstraint("request_digest_version > 0", name="request_digest_version_check"),
        sa.CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        sa.CheckConstraint(
            "result_digest IS NULL OR octet_length(result_digest) = 32",
            name="result_digest_size_check",
        ),
        sa.CheckConstraint(
            "save_state IN ('awaiting_result', 'download_pending', 'original_stored', "
            "'attached', 'failed')",
            name="save_state_check",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "(preview_size_bytes IS NULL AND preview_sha256 IS NULL "
            "AND preview_width IS NULL AND preview_height IS NULL) OR "
            "(preview_size_bytes IS NOT NULL AND preview_sha256 IS NOT NULL "
            "AND preview_width IS NOT NULL AND preview_height IS NOT NULL "
            "AND preview_size_bytes > 0 AND length(preview_sha256) = 64 "
            "AND preview_width > 0 AND preview_height > 0)",
            name="preview_metadata_check",
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["task_item_attempts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["task_item_id", "task_id"],
            ["task_items.id", "task_items.task_id"],
            ondelete="CASCADE",
            name="fk_generation_context_item_task",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="RESTRICT", name="fk_generation_context_owner"
        ),
        sa.ForeignKeyConstraint(
            ["prompt_package_id"],
            ["task_prompt_packages.id"],
            ondelete="RESTRICT",
            name="fk_generation_context_package",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_item_id"],
            ["task_prompt_items.id"],
            ondelete="RESTRICT",
            name="fk_generation_context_prompt_item",
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
    )
    op.create_index(
        "ix_generation_context_task_package",
        "generation_attempt_contexts",
        ["task_id", "prompt_package_id"],
    )

    op.create_table(
        "generation_results",
        sa.Column("attempt_id", uuid, nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("task_item_id", uuid, nullable=False),
        sa.Column("owner_user_id", uuid, nullable=False),
        sa.Column("original_asset_id", uuid, nullable=False),
        sa.Column("preview_asset_id", uuid, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["task_item_attempts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["task_item_id", "task_id"],
            ["task_items.id", "task_items.task_id"],
            ondelete="CASCADE",
            name="fk_generation_results_item_task",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="RESTRICT", name="fk_generation_results_owner"
        ),
        sa.ForeignKeyConstraint(
            ["original_asset_id", "owner_user_id"],
            ["image_assets.id", "image_assets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_generation_results_original_owner",
        ),
        sa.ForeignKeyConstraint(
            ["preview_asset_id", "owner_user_id"],
            ["image_assets.id", "image_assets.owner_user_id"],
            ondelete="RESTRICT",
            name="fk_generation_results_preview_owner",
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint("task_item_id", name="uq_generation_results_item"),
    )
    op.create_index(
        "ix_generation_results_task_created", "generation_results", ["task_id", "created_at"]
    )

    op.execute(sa.text("INSERT INTO generation_capacity_control (id, lock_version) VALUES (1, 1)"))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM generation_capacity_control WHERE id = 1"))
    op.drop_index("ix_generation_results_task_created", table_name="generation_results")
    op.drop_table("generation_results")
    op.drop_index("ix_generation_context_task_package", table_name="generation_attempt_contexts")
    op.drop_table("generation_attempt_contexts")
    op.drop_index(
        "ix_generation_capacity_owner_status", table_name="generation_capacity_reservations"
    )
    op.drop_index(
        "ix_generation_capacity_status_reserved", table_name="generation_capacity_reservations"
    )
    op.drop_table("generation_capacity_reservations")
    op.drop_table("generation_capacity_control")

    op.drop_constraint("asset_kind_check", "image_assets", type_="check")
    op.create_check_constraint(
        "asset_kind_check",
        "image_assets",
        "asset_kind IN ('reference_source', 'reference_model_input')",
    )
