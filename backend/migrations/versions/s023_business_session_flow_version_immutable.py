"""Keep a business session's flow contract fixed after creation."""

from __future__ import annotations

from alembic import op

revision = "s023_flow_version_lock"
down_revision = "s022_v34_terminal_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION reject_business_session_flow_version_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.flow_version IS DISTINCT FROM OLD.flow_version THEN
                RAISE EXCEPTION 'business_sessions.flow_version is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER business_sessions_flow_version_immutable
        BEFORE UPDATE OF flow_version ON business_sessions
        FOR EACH ROW
        EXECUTE FUNCTION reject_business_session_flow_version_change()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER business_sessions_flow_version_immutable ON business_sessions")
    op.execute("DROP FUNCTION reject_business_session_flow_version_change()")
