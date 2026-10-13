from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
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


class BusinessQuestion(Base):
    __tablename__ = "business_questions"
    __table_args__ = (
        CheckConstraint("round_number BETWEEN 1 AND 2", name="round_number_check"),
        CheckConstraint("recommendation_revision > 0", name="revision_check"),
        CheckConstraint("source_sequence > 0", name="source_sequence_check"),
        CheckConstraint("digest_version > 0", name="digest_version_check"),
        CheckConstraint("octet_length(event_digest) = 32", name="event_digest_size_check"),
        CheckConstraint("octet_length(question_digest) = 32", name="question_digest_size_check"),
        CheckConstraint(
            "(form_token IS NULL AND form_token_digest IS NULL) OR "
            "(form_token IS NOT NULL AND form_token_digest IS NOT NULL "
            "AND octet_length(form_token_digest) = 32)",
            name="form_token_digest_check",
        ),
        CheckConstraint(
            "status IN ('awaiting_form_binding', 'awaiting_answer', 'confirming', 'answered', "
            "'rejected', 'expired', 'reconciling')",
            name="status_check",
        ),
        CheckConstraint(
            "(status = 'awaiting_form_binding' AND form_token IS NULL) OR "
            "(status IN ('awaiting_answer', 'confirming', 'answered', 'rejected', 'expired') "
            "AND form_token IS NOT NULL) OR status = 'reconciling'",
            name="form_binding_status_check",
        ),
        ForeignKeyConstraint(
            ["business_session_id", "owner_user_id", "reference_set_id"],
            [
                "business_sessions.id",
                "business_sessions.owner_user_id",
                "business_sessions.reference_set_id",
            ],
            ondelete="CASCADE",
            name="fk_business_questions_session_owner_ref",
        ),
        ForeignKeyConstraint(
            ["task_id", "business_session_id"],
            ["tasks.id", "tasks.business_session_id"],
            ondelete="CASCADE",
            name="fk_business_questions_task_session",
        ),
        ForeignKeyConstraint(
            ["recommendation_id", "business_session_id", "owner_user_id", "reference_set_id"],
            [
                "recommendations.id",
                "recommendations.business_session_id",
                "recommendations.owner_user_id",
                "recommendations.reference_set_id",
            ],
            ondelete="CASCADE",
            name="fk_business_questions_recommendation_session",
        ),
        UniqueConstraint(
            "id", "business_session_id", "task_id", name="uq_business_questions_identity"
        ),
        UniqueConstraint(
            "business_session_id", "round_number", name="uq_business_questions_session_round"
        ),
        UniqueConstraint(
            "business_session_id", "source_event_id", name="uq_business_questions_source_event"
        ),
        Index("ix_business_questions_task_status", "task_id", "status", "round_number"),
        Index("ix_business_questions_expiry", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    recommendation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reference_set_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    recommendation_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    dify_run_id: Mapped[str] = mapped_column(String(320), nullable=False)
    source_event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    digest_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    event_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    question_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    round_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    fields_snapshot: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    form_token: Mapped[str | None] = mapped_column(Text)
    form_token_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class BusinessQuestionEvent(Base):
    """Redacted, durable receipt for a trusted human_input_required event."""

    __tablename__ = "business_question_events"
    __table_args__ = (
        CheckConstraint("source_sequence > 0", name="source_sequence_check"),
        CheckConstraint("recommendation_revision > 0", name="revision_check"),
        CheckConstraint("digest_version > 0", name="digest_version_check"),
        CheckConstraint("octet_length(event_digest) = 32", name="event_digest_size_check"),
        CheckConstraint(
            "disposition IN ('question_created', 'awaiting_form_binding', 'form_bound', "
            "'reconciling', 'stale', 'expired')",
            name="disposition_check",
        ),
        ForeignKeyConstraint(
            ["business_session_id"],
            ["business_sessions.id"],
            ondelete="CASCADE",
            name="fk_business_question_events_session",
        ),
        ForeignKeyConstraint(
            ["task_id", "business_session_id"],
            ["tasks.id", "tasks.business_session_id"],
            ondelete="CASCADE",
            name="fk_business_question_events_task_session",
        ),
        ForeignKeyConstraint(
            ["question_id"],
            ["business_questions.id"],
            ondelete="SET NULL",
            name="fk_business_question_events_question",
        ),
        UniqueConstraint(
            "business_session_id", "source_event_id", name="uq_business_question_event_id"
        ),
        UniqueConstraint(
            "business_session_id",
            "dify_run_id",
            "source_sequence",
            name="uq_business_question_event_sequence",
        ),
        Index("ix_business_question_events_task", "task_id", "source_sequence"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    question_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    source_event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    dify_run_id: Mapped[str] = mapped_column(String(320), nullable=False)
    recommendation_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    digest_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    event_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class BusinessAnswerSubmission(Base):
    __tablename__ = "business_answer_submissions"
    __table_args__ = (
        CheckConstraint("digest_version > 0", name="digest_version_check"),
        CheckConstraint("octet_length(idempotency_key_hash) = 32", name="key_hash_size_check"),
        CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        CheckConstraint(
            "status IN ('confirming', 'accepted', 'rejected', 'unknown')",
            name="status_check",
        ),
        CheckConstraint("recovery_attempts >= 0", name="recovery_attempts_check"),
        ForeignKeyConstraint(
            ["business_session_id", "owner_user_id", "reference_set_id"],
            [
                "business_sessions.id",
                "business_sessions.owner_user_id",
                "business_sessions.reference_set_id",
            ],
            ondelete="CASCADE",
            name="fk_business_answers_session_owner_ref",
        ),
        ForeignKeyConstraint(
            ["task_id", "business_session_id"],
            ["tasks.id", "tasks.business_session_id"],
            ondelete="CASCADE",
            name="fk_business_answers_task_session",
        ),
        ForeignKeyConstraint(
            ["business_question_id", "business_session_id", "task_id"],
            [
                "business_questions.id",
                "business_questions.business_session_id",
                "business_questions.task_id",
            ],
            ondelete="CASCADE",
            name="fk_business_answers_question_session_task",
        ),
        UniqueConstraint(
            "id",
            "business_question_id",
            "business_session_id",
            "task_id",
            name="uq_business_answers_identity",
        ),
        UniqueConstraint("business_question_id", name="uq_business_answers_question"),
        UniqueConstraint(
            "business_question_id", "idempotency_key_hash", name="uq_business_answers_idempotency"
        ),
        Index("ix_business_answers_task_status", "task_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    business_question_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reference_set_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    request_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    digest_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1")
    )
    answers: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'confirming'")
    )
    recovery_attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    recovery_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    external_reason_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
