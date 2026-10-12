"""Persist pre-selection cancel and terminal v3.4 session states.

Revision ID: s022_v34_terminal_recovery
Revises: s021_v34_form_bind
"""

from __future__ import annotations

from alembic import op

revision = "s022_v34_terminal_recovery"
down_revision = "s021_v34_form_bind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("status_check", "business_sessions", type_="check")
    op.create_check_constraint(
        "status_check",
        "business_sessions",
        "status IN ('starting', 'awaiting_recommendation', 'recommendation_ready', "
        "'task_created', 'reconciling', 'failed', 'expired', 'cancelled')",
    )
    op.drop_constraint("form_status_check", "chatflow_v34_session_state", type_="check")
    op.create_check_constraint(
        "form_status_check",
        "chatflow_v34_session_state",
        "form_status IN ('awaiting_event', 'binding', 'ready', 'consumed', 'unknown', "
        "'rejected', 'expired', 'cancelling', 'cancelled')",
    )


def downgrade() -> None:
    raise RuntimeError("s022 is forward-only; terminal v3.4 session history must be preserved")
