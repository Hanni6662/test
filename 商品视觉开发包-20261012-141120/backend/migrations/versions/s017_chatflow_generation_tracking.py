"""Persist Chatflow batch status and read-only reconciliation actions.

Revision ID: s017_h6_generation_tracking
Revises: s016_chatflow_permit_v2
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s017_h6_generation_tracking"
down_revision = "s016_chatflow_permit_v2"
branch_labels = None
depends_on = None

_GENERATION_RECOVERY_ACTION = "chatflow.generation.recover.v1"
_GENERATION_RECOVERY_NAMESPACE = UUID("f068dad7-64ca-43db-a8a6-ea76a667c073")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "chatflow_generation_tracking",
        sa.Column("generation_request_id", uuid, nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("batch_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(24), server_default="awaiting_batch", nullable=False),
        sa.Column("source_sequence", sa.Integer(), nullable=True),
        sa.Column("status_digest", sa.LargeBinary(length=32), nullable=True),
        sa.Column("reconcile_state", sa.String(16), server_default="scheduled", nullable=False),
        sa.Column("reconcile_query_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_reconcile_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reconcile_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('awaiting_batch', 'queued', 'running', 'submission_unknown', "
            "'completed', 'partial_failed', 'failed')",
            name="status_check",
        ),
        sa.CheckConstraint(
            "reconcile_state IN ('scheduled', 'active', 'complete', 'unknown')",
            name="reconcile_state_check",
        ),
        sa.CheckConstraint("reconcile_query_count >= 0", name="reconcile_count_check"),
        sa.CheckConstraint(
            "source_sequence IS NULL OR source_sequence >= 1", name="source_sequence_check"
        ),
        sa.ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_tracking_request_task",
        ),
        sa.PrimaryKeyConstraint("generation_request_id", name="pk_chatflow_generation_tracking"),
        sa.UniqueConstraint("batch_id", name="uq_chatflow_generation_tracking_batch"),
    )
    op.create_index(
        "ix_chatflow_generation_tracking_due",
        "chatflow_generation_tracking",
        ["reconcile_state", "next_reconcile_at"],
    )
    op.execute(
        "INSERT INTO chatflow_generation_tracking "
        "(generation_request_id, task_id, status, reconcile_state, reconcile_query_count) "
        "SELECT id, task_id, 'awaiting_batch', 'scheduled', 0 "
        "FROM chatflow_generation_requests ON CONFLICT (generation_request_id) DO NOTHING"
    )
    op.create_table(
        "chatflow_generation_slot_states",
        sa.Column("generation_request_id", uuid, nullable=False),
        sa.Column("slot_id", sa.String(8), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("status_version", sa.Integer(), nullable=False),
        sa.Column("status_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("known_retry_count", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'submitting', 'submission_unknown', 'pending', 'processing', "
            "'persisting', 'completed', 'failed', 'cancelled', 'protocol_error')",
            name="status_check",
        ),
        sa.CheckConstraint("status_version >= 1", name="status_version_check"),
        sa.CheckConstraint(
            "known_retry_count IS NULL OR known_retry_count >= 0", name="retry_count_check"
        ),
        sa.CheckConstraint("octet_length(status_digest) = 32", name="status_digest_size_check"),
        sa.ForeignKeyConstraint(
            ["generation_request_id", "slot_id"],
            [
                "chatflow_generation_request_slots.generation_request_id",
                "chatflow_generation_request_slots.slot_id",
            ],
            ondelete="CASCADE",
            name="fk_chatflow_generation_slot_states_slot",
        ),
        sa.PrimaryKeyConstraint(
            "generation_request_id", "slot_id", name="pk_chatflow_generation_slot_states"
        ),
    )

    op.create_table(
        "chatflow_generation_event_receipts",
        sa.Column("event_id", uuid, nullable=False),
        sa.Column("generation_request_id", uuid, nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("source_sequence", sa.Integer(), nullable=False),
        sa.Column("event_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("snapshot_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("source_sequence >= 1", name="source_sequence_check"),
        sa.CheckConstraint("octet_length(event_digest) = 32", name="event_digest_size_check"),
        sa.CheckConstraint("octet_length(snapshot_digest) = 32", name="snapshot_digest_size_check"),
        sa.ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="CASCADE",
            name="fk_chatflow_generation_event_request_task",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_chatflow_generation_event_receipts"),
    )
    op.create_index(
        "ix_chatflow_generation_event_request_seq",
        "chatflow_generation_event_receipts",
        ["generation_request_id", "source_sequence"],
    )

    op.add_column(
        "task_actions",
        sa.Column("generation_request_id", uuid, nullable=True),
    )
    op.alter_column(
        "task_actions",
        "scope_kind",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
    op.drop_constraint("scope_kind_check", "task_actions", type_="check")
    op.drop_constraint("scope_association_check", "task_actions", type_="check")
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
        "AND generation_request_id IS NULL AND task_id IS NOT NULL AND scope_id = attempt_id) OR "
        "(scope_kind = 'submission' AND submission_id IS NOT NULL AND scope_id = submission_id "
        "AND attempt_id IS NULL AND task_item_id IS NULL AND business_session_id IS NULL "
        "AND business_question_id IS NULL AND business_answer_submission_id IS NULL "
        "AND generation_request_id IS NULL AND task_id IS NOT NULL) OR "
        "(scope_kind = 'business_session' AND business_session_id IS NOT NULL "
        "AND scope_id = business_session_id AND submission_id IS NULL "
        "AND attempt_id IS NULL AND task_item_id IS NULL AND generation_request_id IS NULL) OR "
        "(scope_kind = 'generation_request' AND generation_request_id IS NOT NULL "
        "AND scope_id = generation_request_id AND task_id IS NOT NULL "
        "AND business_session_id IS NOT NULL AND submission_id IS NULL "
        "AND attempt_id IS NULL AND task_item_id IS NULL "
        "AND business_question_id IS NULL AND business_answer_submission_id IS NULL)",
    )
    op.create_foreign_key(
        "fk_task_actions_generation_request_task",
        "task_actions",
        "chatflow_generation_requests",
        ["generation_request_id", "task_id"],
        ["id", "task_id"],
        ondelete="RESTRICT",
    )
    _backfill_generation_recovery_actions()


def downgrade() -> None:
    bind = op.get_bind()
    action_event_ids = bind.execute(
        sa.text(
            "DELETE FROM task_actions WHERE scope_kind = 'generation_request' "
            "RETURNING outbox_event_id"
        )
    ).scalars().all()
    for event_id in action_event_ids:
        bind.execute(
            sa.text("DELETE FROM outbox_receipts WHERE event_id = :event_id"),
            {"event_id": event_id},
        )
        bind.execute(
            sa.text("DELETE FROM outbox_events WHERE id = :event_id"),
            {"event_id": event_id},
        )
    op.drop_constraint(
        "fk_task_actions_generation_request_task", "task_actions", type_="foreignkey"
    )
    op.drop_constraint("scope_association_check", "task_actions", type_="check")
    op.drop_constraint("scope_kind_check", "task_actions", type_="check")
    op.create_check_constraint(
        "scope_kind_check",
        "task_actions",
        "scope_kind IN ('submission', 'attempt', 'business_session')",
    )
    op.create_check_constraint(
        "scope_association_check",
        "task_actions",
        "(scope_kind = 'attempt' AND submission_id IS NULL AND attempt_id IS NOT NULL "
        "AND task_item_id IS NOT NULL AND business_session_id IS NULL "
        "AND business_question_id IS NULL AND business_answer_submission_id IS NULL "
        "AND task_id IS NOT NULL AND scope_id = attempt_id) OR "
        "(scope_kind = 'submission' AND submission_id IS NOT NULL AND scope_id = submission_id "
        "AND attempt_id IS NULL AND task_item_id IS NULL AND business_session_id IS NULL "
        "AND business_question_id IS NULL AND business_answer_submission_id IS NULL "
        "AND task_id IS NOT NULL) OR "
        "(scope_kind = 'business_session' AND business_session_id IS NOT NULL "
        "AND scope_id = business_session_id AND submission_id IS NULL "
        "AND attempt_id IS NULL AND task_item_id IS NULL)",
    )
    op.alter_column(
        "task_actions",
        "scope_kind",
        existing_type=sa.String(length=32),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
    op.drop_column("task_actions", "generation_request_id")
    op.drop_index(
        "ix_chatflow_generation_event_request_seq",
        table_name="chatflow_generation_event_receipts",
    )
    op.drop_table("chatflow_generation_event_receipts")
    op.drop_table("chatflow_generation_slot_states")
    op.drop_index(
        "ix_chatflow_generation_tracking_due", table_name="chatflow_generation_tracking"
    )
    op.drop_table("chatflow_generation_tracking")


def _backfill_generation_recovery_actions(bind=None) -> None:
    """Enqueue GET-only recovery for pre-S017 H4 requests with a v2 permit."""
    if bind is None:
        bind = op.get_bind()
    requests = bind.execute(
        sa.text(
            "SELECT request.id, request.task_id, request.business_session_id "
            "FROM chatflow_generation_requests AS request "
            "JOIN chatflow_generation_permits AS permit "
            "ON permit.generation_request_id = request.id "
            "WHERE permit.permit_version = 2 "
            "AND permit.slot_mapping_digest IS NOT NULL "
            "AND permit.reference_manifest_digest IS NOT NULL "
            "ORDER BY request.created_at, request.id"
        )
    ).mappings()
    for row in requests:
        request_id = UUID(str(row["id"]))
        task_id = UUID(str(row["task_id"]))
        session_id = UUID(str(row["business_session_id"]))
        sequence = 1
        action_id = uuid5(
            _GENERATION_RECOVERY_NAMESPACE, f"{request_id}:{sequence}"
        )
        event_id = uuid4()
        due_at = datetime.now(UTC) + timedelta(seconds=10)
        input_value = {
            "action_kind": _GENERATION_RECOVERY_ACTION,
            "generation_request_id": str(request_id),
            "task_id": str(task_id),
            "business_session_id": str(session_id),
            "sequence": sequence,
        }
        input_digest = hashlib.sha256(
            json.dumps(input_value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).digest()
        bind.execute(
            sa.text(
                "UPDATE chatflow_generation_tracking "
                "SET next_reconcile_at = :due_at "
                "WHERE generation_request_id = :request_id"
            ),
            {"due_at": due_at, "request_id": request_id},
        )
        bind.execute(
            sa.text(
                "INSERT INTO outbox_events "
                "(id, event_type, aggregate_id, operation_version, schema_version, payload, "
                "status, available_at, attempt_count) "
                "VALUES (:id, 'task.action.v1', :aggregate_id, 1, 1, "
                "CAST(:payload AS jsonb), 'pending', :available_at, 0)"
            ),
            {
                "id": event_id,
                "aggregate_id": action_id,
                "payload": json.dumps({"action_id": str(action_id)}, separators=(",", ":")),
                "available_at": due_at,
            },
        )
        bind.execute(
            sa.text(
                "INSERT INTO outbox_receipts "
                "(event_id, status, available_at, attempt_count) "
                "VALUES (:event_id, 'pending', :available_at, 0)"
            ),
            {"event_id": event_id, "available_at": due_at},
        )
        bind.execute(
            sa.text(
                "INSERT INTO task_actions "
                "(id, task_id, business_session_id, generation_request_id, scope_kind, scope_id, "
                "action_kind, sequence, due_at, status, claim_count, input_digest_version, "
                "input_digest, outbox_event_id) "
                "VALUES (:id, :task_id, :session_id, :request_id, 'generation_request', "
                ":request_id, :action_kind, :sequence, :due_at, 'scheduled', 0, 1, "
                ":input_digest, :event_id)"
            ),
            {
                "id": action_id,
                "task_id": task_id,
                "session_id": session_id,
                "request_id": request_id,
                "action_kind": _GENERATION_RECOVERY_ACTION,
                "sequence": sequence,
                "due_at": due_at,
                "input_digest": input_digest,
                "event_id": event_id,
            },
        )
