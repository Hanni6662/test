"""Allow the durable pre-acceptance rejection state in generation tracking.

Revision ID: s025_tracking_state_size
Revises: s024_v34_permit_expiry_closure
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "s025_tracking_state_size"
down_revision = "s024_v34_permit_expiry_closure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "chatflow_generation_tracking",
        "status",
        existing_type=sa.String(length=24),
        type_=sa.String(length=32),
        existing_nullable=False,
        existing_server_default=sa.text("'awaiting_batch'"),
    )


def downgrade() -> None:
    op.alter_column(
        "chatflow_generation_tracking",
        "status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=24),
        existing_nullable=False,
        existing_server_default=sa.text("'awaiting_batch'"),
    )
