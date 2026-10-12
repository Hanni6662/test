from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.business_session_models import BusinessSession as _BusinessSession  # noqa: F401
from app.db.chatflow_generation_models import (
    ChatflowGenerationDelivery as _ChatflowGenerationDelivery,  # noqa: F401
)


class Action(Base):
    """Durable single-step external action; the outbox event only wakes its executor."""

    __tablename__ = "task_actions"
    __table_args__ = (
        CheckConstraint("sequence > 0", name="sequence_positive_check"),
        CheckConstraint(
            "length(sequence_namespace) BETWEEN 1 AND 80",
            name="sequence_namespace_check",
        ),
        CheckConstraint("input_digest_version > 0", name="input_digest_version_check"),
        CheckConstraint("octet_length(input_digest) = 32", name="input_digest_size_check"),
        CheckConstraint("claim_count >= 0", name="claim_count_nonnegative_check"),
        CheckConstraint("length(action_kind) BETWEEN 1 AND 80", name="action_kind_size_check"),
        CheckConstraint(
            "scope_kind IN ('submission', 'attempt', 'business_session', "
            "'generation_request', 'generation_delivery')",
            name="scope_kind_check",
        ),
        CheckConstraint(
            "(scope_kind = 'attempt' AND submission_id IS NULL AND attempt_id IS NOT NULL "
            "AND task_item_id IS NOT NULL "
            "AND business_session_id IS NULL AND business_question_id IS NULL "
            "AND business_answer_submission_id IS NULL AND task_id IS NOT NULL "
            "AND generation_request_id IS NULL AND generation_delivery_id IS NULL "
            "AND scope_id = attempt_id) OR "
            "(scope_kind = 'submission' AND submission_id IS NOT NULL "
            "AND scope_id = submission_id AND attempt_id IS NULL AND task_item_id IS NULL "
            "AND business_session_id IS NULL AND business_question_id IS NULL "
            "AND generation_request_id IS NULL AND generation_delivery_id IS NULL "
            "AND business_answer_submission_id IS NULL AND task_id IS NOT NULL) OR "
            "(scope_kind = 'business_session' AND business_session_id IS NOT NULL "
            "AND scope_id = business_session_id AND submission_id IS NULL "
            "AND attempt_id IS NULL AND task_item_id IS NULL "
            "AND generation_request_id IS NULL AND generation_delivery_id IS NULL) OR "
            "(scope_kind = 'generation_request' AND generation_request_id IS NOT NULL "
            "AND scope_id = generation_request_id AND task_id IS NOT NULL "
            "AND business_session_id IS NOT NULL AND submission_id IS NULL "
            "AND attempt_id IS NULL AND task_item_id IS NULL "
            "AND business_question_id IS NULL AND business_answer_submission_id IS NULL "
            "AND generation_delivery_id IS NULL) OR "
            "(scope_kind = 'generation_delivery' AND generation_delivery_id IS NOT NULL "
            "AND scope_id = generation_delivery_id AND generation_request_id IS NOT NULL "
            "AND task_id IS NOT NULL AND business_session_id IS NOT NULL "
            "AND task_item_id IS NOT NULL AND attempt_id IS NOT NULL "
            "AND submission_id IS NULL AND business_question_id IS NULL "
            "AND business_answer_submission_id IS NULL)",
            name="scope_association_check",
        ),
        CheckConstraint(
            "(action_kind IN ('dify.chatflow.answer.v1', 'dify.chatflow.answer.recover.v1') "
            "AND scope_kind = 'business_session' "
            "AND task_id IS NOT NULL AND business_question_id IS NOT NULL "
            "AND business_answer_submission_id IS NOT NULL) OR "
            "(action_kind NOT IN ('dify.chatflow.answer.v1', 'dify.chatflow.answer.recover.v1') "
            "AND business_question_id IS NULL "
            "AND business_answer_submission_id IS NULL)",
            name="chatflow_answer_association_check",
        ),
        CheckConstraint(
            "status IN ('scheduled', 'claimed', 'completed', 'rejected', 'unknown', 'cancelled')",
            name="status_check",
        ),
        CheckConstraint(
            "(status = 'claimed' AND claim_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status <> 'claimed' AND claim_token IS NULL AND lease_until IS NULL)",
            name="claim_lease_consistency_check",
        ),
        CheckConstraint(
            "(status IN ('scheduled', 'claimed') AND completed_at IS NULL) OR "
            "(status IN ('completed', 'rejected', 'unknown', 'cancelled') "
            "AND completed_at IS NOT NULL)",
            name="completion_timestamp_consistency_check",
        ),
        CheckConstraint(
            "last_outcome IS NULL OR last_outcome IN "
            "('accepted', 'definitely_rejected', 'unknown', 'safe_retry')",
            name="last_outcome_check",
        ),
        ForeignKeyConstraint(
            ["task_item_id", "task_id"],
            ["task_items.id", "task_items.task_id"],
            ondelete="RESTRICT",
            name="fk_task_actions_item_task",
        ),
        ForeignKeyConstraint(
            ["submission_id", "task_id"],
            ["task_prompt_submissions.submission_id", "task_prompt_submissions.task_id"],
            ondelete="RESTRICT",
            name="fk_task_actions_submission_task",
        ),
        ForeignKeyConstraint(
            ["business_session_id"],
            ["business_sessions.id"],
            ondelete="CASCADE",
            name="fk_task_actions_business_session",
        ),
        ForeignKeyConstraint(
            ["task_id", "business_session_id"],
            ["tasks.id", "tasks.business_session_id"],
            ondelete="RESTRICT",
            name="fk_task_actions_task_business_session",
        ),
        ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="RESTRICT",
            name="fk_task_actions_generation_request_task",
        ),
        ForeignKeyConstraint(
            ["generation_delivery_id", "generation_request_id", "task_id"],
            [
                "chatflow_generation_deliveries.id",
                "chatflow_generation_deliveries.generation_request_id",
                "chatflow_generation_deliveries.task_id",
            ],
            ondelete="RESTRICT",
            name="fk_task_actions_generation_delivery_chain",
        ),
        ForeignKeyConstraint(
            ["business_question_id", "business_session_id", "task_id"],
            [
                "business_questions.id",
                "business_questions.business_session_id",
                "business_questions.task_id",
            ],
            ondelete="RESTRICT",
            name="fk_task_actions_business_question_session_task",
        ),
        ForeignKeyConstraint(
            [
                "business_answer_submission_id",
                "business_question_id",
                "business_session_id",
                "task_id",
            ],
            [
                "business_answer_submissions.id",
                "business_answer_submissions.business_question_id",
                "business_answer_submissions.business_session_id",
                "business_answer_submissions.task_id",
            ],
            ondelete="RESTRICT",
            name="fk_task_actions_business_answer_question_session_task",
        ),
        UniqueConstraint(
            "scope_kind",
            "scope_id",
            "sequence_namespace",
            "sequence",
            name="uq_task_actions_scope_namespace_sequence",
        ),
        UniqueConstraint("outbox_event_id", name="uq_task_actions_outbox_event"),
        Index("ix_task_actions_status_due", "status", "due_at", "created_at"),
        Index("ix_task_actions_status_lease", "status", "lease_until"),
        Index("ix_task_actions_task_created", "task_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    task_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tasks.id", ondelete="RESTRICT", name="fk_task_actions_task"),
        nullable=True,
    )
    business_session_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    business_question_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    business_answer_submission_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    generation_request_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    generation_delivery_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    task_item_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task_item_attempts.id", ondelete="RESTRICT", name="fk_task_actions_attempt"),
    )
    submission_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    scope_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    action_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    sequence_namespace: Mapped[str] = mapped_column(
        String(80), nullable=False, server_default=text("'default'")
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'scheduled'")
    )
    claim_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    input_digest_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1")
    )
    input_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    outbox_event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("outbox_events.id", ondelete="RESTRICT", name="fk_task_actions_outbox_event"),
        nullable=False,
    )
    last_outcome: Mapped[str | None] = mapped_column(String(24))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
