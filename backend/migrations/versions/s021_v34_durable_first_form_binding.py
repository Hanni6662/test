"""Allow a persisted run to own a pending form-definition GET.

Revision ID: s021_v34_form_bind
Revises: s020_v34_session_handoff
"""

from __future__ import annotations

from alembic import op

revision = "s021_v34_form_bind"
down_revision = "s020_v34_session_handoff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("form_status_check", "chatflow_v34_session_state", type_="check")
    op.create_check_constraint(
        "form_status_check",
        "chatflow_v34_session_state",
        "form_status IN ('awaiting_event', 'binding', 'ready', 'consumed', 'unknown', "
        "'rejected', 'expired')",
    )
    op.drop_constraint("form_binding_fields_check", "chatflow_v34_session_state", type_="check")
    op.create_check_constraint(
        "form_binding_fields_check",
        "chatflow_v34_session_state",
        "(form_token IS NULL AND form_token_digest IS NULL AND "
        "form_definition_digest IS NULL AND form_expires_at IS NULL AND form_node_id IS NULL) "
        "OR (form_token IS NOT NULL AND form_token_digest IS NOT NULL AND "
        "octet_length(form_token_digest) = 32 AND form_node_id = 'form_0' AND "
        "((form_status IN ('binding', 'unknown') AND form_definition_digest IS NULL AND "
        "form_expires_at IS NULL) OR (form_status NOT IN ('binding', 'unknown') AND "
        "form_definition_digest IS NOT NULL AND octet_length(form_definition_digest) = 32 "
        "AND form_expires_at IS NOT NULL)))",
    )


def downgrade() -> None:
    raise RuntimeError("s021 is forward-only; v3.4 handoff records must not be discarded")
