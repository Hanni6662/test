from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ChatflowV34SessionState(Base):
    """Private first-form and single-selection state for v3.4 sessions only."""

    __tablename__ = "chatflow_v34_session_state"
    __table_args__ = (
        CheckConstraint("flow_version = 'visual-v3.4-single-selection'", name="flow_version_check"),
        CheckConstraint(
            "form_status IN ('awaiting_event', 'binding', 'ready', 'consumed', 'unknown', "
            "'rejected', 'expired', 'cancelling', 'cancelled')",
            name="form_status_check",
        ),
        CheckConstraint(
            "(form_token IS NULL AND form_token_digest IS NULL AND "
            "form_definition_digest IS NULL AND form_expires_at IS NULL "
            "AND form_node_id IS NULL) OR "
            "(form_token IS NOT NULL AND form_token_digest IS NOT NULL AND "
            "octet_length(form_token_digest) = 32 AND form_node_id = 'form_0' AND "
            "((form_status IN ('binding', 'unknown') AND form_definition_digest IS NULL AND "
            "form_expires_at IS NULL) OR (form_status NOT IN ('binding', 'unknown') AND "
            "form_definition_digest IS NOT NULL AND octet_length(form_definition_digest) = 32 "
            "AND form_expires_at IS NOT NULL)))",
            name="form_binding_fields_check",
        ),
        CheckConstraint(
            "(form_status IN ('ready', 'consumed') AND form_token IS NOT NULL) OR "
            "form_status NOT IN ('ready', 'consumed')",
            name="form_ready_binding_check",
        ),
        CheckConstraint(
            "selection_status IN ('not_started', 'pending', 'accepted', 'unknown', 'rejected', "
            "'cancelled', 'expired')",
            name="selection_status_check",
        ),
        CheckConstraint(
            "(selection_status = 'not_started' AND selection_submission_id IS NULL AND "
            "selection_input_digest IS NULL AND selection_inputs IS NULL) OR "
            "(selection_status <> 'not_started' AND selection_submission_id IS NOT NULL AND "
            "selection_input_digest IS NOT NULL AND octet_length(selection_input_digest) = 32 AND "
            "selection_inputs IS NOT NULL)",
            name="selection_input_fields_check",
        ),
        CheckConstraint(
            "(selection_status = 'accepted' AND selection_evidence_kind IN ('http_200', "
            "'selection_applied') AND selection_accepted_at IS NOT NULL) OR "
            "(selection_status <> 'accepted' AND selection_evidence_kind IS NULL "
            "AND selection_accepted_at IS NULL)",
            name="selection_evidence_check",
        ),
        ForeignKeyConstraint(
            ["business_session_id"],
            ["business_sessions.id"],
            ondelete="CASCADE",
            name="fk_chatflow_v34_session_state_session",
        ),
        UniqueConstraint("business_session_id", name="uq_chatflow_v34_session_state_session"),
        Index("ix_chatflow_v34_session_state_form", "form_status", "form_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    flow_version: Mapped[str] = mapped_column(String(64), nullable=False)
    dify_app_id: Mapped[str] = mapped_column(String(160), nullable=False)
    dify_workflow_id: Mapped[str] = mapped_column(String(160), nullable=False)
    form_node_id: Mapped[str | None] = mapped_column(String(80))
    form_token: Mapped[str | None] = mapped_column(Text)
    form_token_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    form_definition_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    form_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    form_status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'awaiting_event'")
    )
    selection_submission_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    selection_input_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    selection_inputs: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    selection_status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'not_started'")
    )
    selection_evidence_kind: Mapped[str | None] = mapped_column(String(24))
    selection_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safe_reason_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class ChatflowV34StageEventReceipt(Base):
    """Bounded durable receipts for strict v3.4 stage callbacks, not raw SSE."""

    __tablename__ = "chatflow_v34_stage_event_receipts"
    __table_args__ = (
        CheckConstraint("flow_version = 'visual-v3.4-single-selection'", name="flow_version_check"),
        CheckConstraint("octet_length(event_digest) = 32", name="event_digest_size_check"),
        CheckConstraint(
            "event_type IN ('recommendation.ready', 'selection.applied', "
            "'pre_generation.incomplete', 'selection.cancelled', 'selection.expired', "
            "'workflow.failed')",
            name="event_type_check",
        ),
        CheckConstraint(
            "disposition IN ('received_pending', 'accepted', 'rejected')",
            name="disposition_check",
        ),
        CheckConstraint("recommendation_revision > 0", name="recommendation_revision_check"),
        ForeignKeyConstraint(
            ["business_session_id"],
            ["business_sessions.id"],
            ondelete="CASCADE",
            name="fk_chatflow_v34_stage_receipts_session",
        ),
        UniqueConstraint(
            "business_session_id", "event_key", name="uq_chatflow_v34_stage_event_key"
        ),
        Index(
            "ix_chatflow_v34_stage_receipts_pending",
            "business_session_id",
            "dify_run_id",
            "disposition",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    flow_version: Mapped[str] = mapped_column(String(64), nullable=False)
    recommendation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    recommendation_revision: Mapped[int] = mapped_column(nullable=False)
    dify_run_id: Mapped[str] = mapped_column(String(320), nullable=False)
    node_id: Mapped[str] = mapped_column(String(80), nullable=False)
    event_key: Mapped[str] = mapped_column(String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    event_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    disposition: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'received_pending'")
    )
    safe_reason_code: Mapped[str | None] = mapped_column(String(80))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatflowV34RunEndEvidence(Base):
    """Backend-owned proof that a bound v3.4 run ended without generation."""

    __tablename__ = "chatflow_v34_run_end_evidence"
    __table_args__ = (
        CheckConstraint("flow_version = 'visual-v3.4-single-selection'", name="flow_version_check"),
        CheckConstraint(
            "terminal_event = 'workflow_finished' AND "
            "terminal_status IN ('partial-succeeded', 'succeeded')",
            name="terminal_event_status_check",
        ),
        CheckConstraint(
            "business_reason_code = 'V34_WORKFLOW_ENDED_WITHOUT_GENERATION'",
            name="business_reason_check",
        ),
        CheckConstraint(
            "closure_outcome = 'failed_without_generation'", name="closure_outcome_check"
        ),
        CheckConstraint("recommendation_revision > 0", name="recommendation_revision_check"),
        CheckConstraint(
            "octet_length(selection_input_digest) = 32", name="selection_digest_size_check"
        ),
        CheckConstraint("octet_length(event_digest) = 32", name="event_digest_size_check"),
        ForeignKeyConstraint(
            ["business_session_id"],
            ["business_sessions.id"],
            ondelete="CASCADE",
            name="fk_chatflow_v34_run_end_session",
        ),
        ForeignKeyConstraint(
            ["task_id", "business_session_id"],
            ["tasks.id", "tasks.business_session_id"],
            ondelete="CASCADE",
            name="fk_chatflow_v34_run_end_task_session",
        ),
        UniqueConstraint(
            "business_session_id", "dify_run_id", name="uq_chatflow_v34_run_end_session_run"
        ),
        UniqueConstraint("task_id", name="uq_chatflow_v34_run_end_task"),
        Index("ix_chatflow_v34_run_end_observed", "observed_at", "task_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    recommendation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    recommendation_revision: Mapped[int] = mapped_column(nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reference_set_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    flow_version: Mapped[str] = mapped_column(String(64), nullable=False)
    dify_app_id: Mapped[str] = mapped_column(String(160), nullable=False)
    dify_published_workflow_id: Mapped[str] = mapped_column(String(160), nullable=False)
    dify_run_id: Mapped[str] = mapped_column(String(320), nullable=False)
    selection_submission_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    selection_input_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    terminal_event: Mapped[str] = mapped_column(String(40), nullable=False)
    terminal_status: Mapped[str] = mapped_column(String(32), nullable=False)
    event_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    business_reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    closure_outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
