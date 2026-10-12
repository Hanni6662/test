"""Persist full Chatflow prompt packages and signed generation permits.

Revision ID: s015_chatflow_prompt_permits
Revises: s014_business_questions
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s015_chatflow_prompt_permits"
down_revision = "s014_business_questions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())

    op.create_unique_constraint(
        "uq_attempts_id_task_item", "task_item_attempts", ["id", "task_item_id"]
    )

    op.create_table(
        "chatflow_prompt_packages",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("submission_id", uuid, nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("business_session_id", uuid, nullable=False),
        sa.Column("recommendation_id", uuid, nullable=False),
        sa.Column("owner_user_id", uuid, nullable=False),
        sa.Column("reference_set_id", uuid, nullable=False),
        sa.Column("recommendation_revision", sa.Integer(), nullable=False),
        sa.Column("dify_run_id", sa.String(length=320), nullable=False),
        sa.Column("contract_version", sa.String(length=32), nullable=False),
        sa.Column("package_version", sa.String(length=80), nullable=False),
        sa.Column("package_digest_version", sa.SmallInteger(), nullable=False),
        sa.Column("package_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("request_digest_version", sa.SmallInteger(), nullable=False),
        sa.Column("request_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("image_types", jsonb, nullable=False),
        sa.Column("package", jsonb, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("recommendation_revision > 0", name="revision_check"),
        sa.CheckConstraint("package_digest_version > 0", name="package_digest_version_check"),
        sa.CheckConstraint("request_digest_version > 0", name="request_digest_version_check"),
        sa.CheckConstraint("octet_length(package_digest) = 32", name="package_digest_size_check"),
        sa.CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        sa.CheckConstraint(
            "contract_version = 'chatflow-handoff-v1'", name="contract_version_check"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chatflow_prompt_packages"),
        sa.UniqueConstraint("submission_id", name="uq_chatflow_prompt_packages_submission"),
        sa.UniqueConstraint("task_id", name="uq_chatflow_prompt_packages_task"),
        sa.UniqueConstraint("id", "task_id", name="uq_chatflow_prompt_packages_id_task"),
        sa.ForeignKeyConstraint(
            ["task_id", "business_session_id"],
            ["tasks.id", "tasks.business_session_id"],
            ondelete="CASCADE",
            name="fk_chatflow_prompt_packages_task_session",
        ),
        sa.ForeignKeyConstraint(
            ["business_session_id", "owner_user_id", "reference_set_id"],
            [
                "business_sessions.id",
                "business_sessions.owner_user_id",
                "business_sessions.reference_set_id",
            ],
            ondelete="CASCADE",
            name="fk_chatflow_prompt_packages_session_owner_ref",
        ),
        sa.ForeignKeyConstraint(
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
    )
    op.create_index(
        "ix_chatflow_prompt_packages_task_created",
        "chatflow_prompt_packages",
        ["task_id", "created_at"],
    )

    op.create_table(
        "chatflow_prompt_images",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("package_id", uuid, nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("task_item_id", uuid, nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("image_type", sa.String(length=80), nullable=False),
        sa.Column("prompt_image", jsonb, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("position BETWEEN 1 AND 8", name="position_check"),
        sa.PrimaryKeyConstraint("id", name="pk_chatflow_prompt_images"),
        sa.UniqueConstraint("task_item_id", name="uq_chatflow_prompt_images_task_item"),
        sa.UniqueConstraint(
            "package_id", "position", name="uq_chatflow_prompt_images_position"
        ),
        sa.ForeignKeyConstraint(
            ["package_id", "task_id"],
            ["chatflow_prompt_packages.id", "chatflow_prompt_packages.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_prompt_images_package_task",
        ),
        sa.ForeignKeyConstraint(
            ["task_item_id", "task_id"],
            ["task_items.id", "task_items.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_prompt_images_task_item",
        ),
    )
    op.create_index("ix_chatflow_prompt_images_package", "chatflow_prompt_images", ["package_id"])

    op.create_table(
        "chatflow_prompt_references",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("package_id", uuid, nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("ref_id", sa.String(length=8), nullable=False),
        sa.Column("asset_id", uuid, nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("position BETWEEN 1 AND 4", name="position_check"),
        sa.CheckConstraint("length(content_sha256) = 64", name="sha256_size_check"),
        sa.PrimaryKeyConstraint("id", name="pk_chatflow_prompt_references"),
        sa.UniqueConstraint(
            "package_id", "ref_id", name="uq_chatflow_prompt_references_alias"
        ),
        sa.UniqueConstraint(
            "package_id", "position", name="uq_chatflow_prompt_references_position"
        ),
        sa.ForeignKeyConstraint(
            ["package_id", "task_id"],
            ["chatflow_prompt_packages.id", "chatflow_prompt_packages.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_prompt_references_package_task",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["image_assets.id"],
            ondelete="RESTRICT",
            name="fk_chatflow_prompt_references_asset",
        ),
    )
    op.create_index(
        "ix_chatflow_prompt_references_package",
        "chatflow_prompt_references",
        ["package_id", "position"],
    )

    op.create_table(
        "chatflow_generation_requests",
        sa.Column("id", uuid, nullable=False),
        sa.Column("package_id", uuid, nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("business_session_id", uuid, nullable=False),
        sa.Column("dify_run_id", sa.String(length=320), nullable=False),
        sa.Column("package_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("request_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("octet_length(package_digest) = 32", name="package_digest_size_check"),
        sa.CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        sa.PrimaryKeyConstraint("id", name="pk_chatflow_generation_requests"),
        sa.UniqueConstraint("package_id", name="uq_chatflow_generation_requests_package"),
        sa.UniqueConstraint("task_id", name="uq_chatflow_generation_requests_task"),
        sa.UniqueConstraint("id", "task_id", name="uq_chatflow_generation_requests_id_task"),
        sa.ForeignKeyConstraint(
            ["package_id", "task_id"],
            ["chatflow_prompt_packages.id", "chatflow_prompt_packages.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_requests_package_task",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "business_session_id"],
            ["tasks.id", "tasks.business_session_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_requests_task_session",
        ),
    )
    op.create_index(
        "ix_chatflow_generation_requests_session",
        "chatflow_generation_requests",
        ["business_session_id", "created_at"],
    )

    op.create_table(
        "chatflow_generation_request_slots",
        sa.Column("generation_request_id", uuid, nullable=False),
        sa.Column("slot_id", sa.String(length=8), nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("task_item_id", uuid, nullable=False),
        sa.Column("attempt_id", uuid, nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("image_type", sa.String(length=80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("position BETWEEN 1 AND 8", name="position_check"),
        sa.CheckConstraint(
            "slot_id = 'slot_0' || position::text", name="slot_position_check"
        ),
        sa.PrimaryKeyConstraint(
            "generation_request_id", "slot_id", name="pk_chatflow_generation_request_slots"
        ),
        sa.UniqueConstraint(
            "task_item_id", "attempt_id", name="uq_chatflow_generation_slot_attempt"
        ),
        sa.UniqueConstraint(
            "generation_request_id", "position", name="uq_chatflow_generation_slot_position"
        ),
        sa.ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_slots_request_task",
        ),
        sa.ForeignKeyConstraint(
            ["task_item_id", "task_id"],
            ["task_items.id", "task_items.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_slots_task_item",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "task_item_id"],
            ["task_item_attempts.id", "task_item_attempts.task_item_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_slots_attempt",
        ),
    )
    op.create_index(
        "ix_chatflow_generation_slots_task",
        "chatflow_generation_request_slots",
        ["task_id", "position"],
    )

    op.create_table(
        "chatflow_generation_permits",
        sa.Column("permit_id", uuid, nullable=False),
        sa.Column("generation_request_id", uuid, nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("package_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("contract_version", sa.String(length=32), nullable=False),
        sa.Column("token_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("octet_length(package_digest) = 32", name="package_digest_size_check"),
        sa.CheckConstraint("octet_length(token_digest) = 32", name="token_digest_size_check"),
        sa.CheckConstraint(
            "contract_version = 'chatflow-handoff-v1'", name="contract_version_check"
        ),
        sa.PrimaryKeyConstraint("permit_id", name="pk_chatflow_generation_permits"),
        sa.UniqueConstraint(
            "generation_request_id", name="uq_chatflow_generation_permit_request"
        ),
        sa.ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_permits_request_task",
        ),
    )
    op.create_index(
        "ix_chatflow_generation_permits_expiry",
        "chatflow_generation_permits",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chatflow_generation_permits_expiry", table_name="chatflow_generation_permits")
    op.drop_table("chatflow_generation_permits")
    op.drop_index(
        "ix_chatflow_generation_slots_task", table_name="chatflow_generation_request_slots"
    )
    op.drop_table("chatflow_generation_request_slots")
    op.drop_index(
        "ix_chatflow_generation_requests_session", table_name="chatflow_generation_requests"
    )
    op.drop_table("chatflow_generation_requests")
    op.drop_index(
        "ix_chatflow_prompt_references_package", table_name="chatflow_prompt_references"
    )
    op.drop_table("chatflow_prompt_references")
    op.drop_index("ix_chatflow_prompt_images_package", table_name="chatflow_prompt_images")
    op.drop_table("chatflow_prompt_images")
    op.drop_index("ix_chatflow_prompt_packages_task_created", table_name="chatflow_prompt_packages")
    op.drop_table("chatflow_prompt_packages")
    op.drop_constraint("uq_attempts_id_task_item", "task_item_attempts", type_="unique")
