"""Add mapping-bound Chatflow permit v2 claims.

Revision ID: s016_chatflow_permit_v2
Revises: s015_chatflow_prompt_permits
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "s016_chatflow_permit_v2"
down_revision = "s015_chatflow_prompt_permits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing v1 permits remain intact and explicitly unbound. H4 upgrades an
    # unexpired v1 permit in place when its original submission is replayed.
    op.add_column(
        "chatflow_generation_permits",
        sa.Column("permit_version", sa.SmallInteger(), server_default="1", nullable=False),
    )
    op.add_column(
        "chatflow_generation_permits",
        sa.Column("slot_mapping_digest", sa.LargeBinary(length=32), nullable=True),
    )
    op.add_column(
        "chatflow_generation_permits",
        sa.Column("reference_manifest_digest", sa.LargeBinary(length=32), nullable=True),
    )
    op.create_check_constraint(
        "permit_version_check",
        "chatflow_generation_permits",
        "permit_version IN (1, 2)",
    )
    op.create_check_constraint(
        "permit_v2_digests_check",
        "chatflow_generation_permits",
        "(permit_version = 1 AND slot_mapping_digest IS NULL "
        "AND reference_manifest_digest IS NULL) OR "
        "(permit_version = 2 AND slot_mapping_digest IS NOT NULL "
        "AND reference_manifest_digest IS NOT NULL "
        "AND octet_length(slot_mapping_digest) = 32 "
        "AND octet_length(reference_manifest_digest) = 32)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "permit_v2_digests_check", "chatflow_generation_permits", type_="check"
    )
    op.drop_constraint(
        "permit_version_check", "chatflow_generation_permits", type_="check"
    )
    op.drop_column("chatflow_generation_permits", "reference_manifest_digest")
    op.drop_column("chatflow_generation_permits", "slot_mapping_digest")
    op.drop_column("chatflow_generation_permits", "permit_version")
