"""Persist Human Input questions, answers, and their Dify actions.

Revision ID: s014_business_questions
Revises: s013_chatflow_handoff
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s014_business_questions"
down_revision = "s013_chatflow_handoff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())

    op.create_table(
        "business_questions",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("business_session_id", uuid, nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("recommendation_id", uuid, nullable=False),
        sa.Column("owner_user_id", uuid, nullable=False),
        sa.Column("reference_set_id", uuid, nullable=False),
        sa.Column("recommendation_revision", sa.Integer(), nullable=False),
        sa.Column("dify_run_id", sa.String(length=320), nullable=False),
        sa.Column("source_event_id", uuid, nullable=False),
        sa.Column("source_sequence", sa.Integer(), nullable=False),
        sa.Column("digest_version", sa.SmallInteger(), nullable=False),
        sa.Column("event_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("question_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("round_number", sa.SmallInteger(), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("fields_snapshot", jsonb, nullable=False),
        sa.Column("form_token", sa.Text(), nullable=True),
        sa.Column("form_token_digest", sa.LargeBinary(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("round_number BETWEEN 1 AND 2", name="round_number_check"),
        sa.CheckConstraint("recommendation_revision > 0", name="revision_check"),
        sa.CheckConstraint("source_sequence > 0", name="source_sequence_check"),
        sa.CheckConstraint("digest_version > 0", name="digest_version_check"),
        sa.CheckConstraint("octet_length(event_digest) = 32", name="event_digest_size_check"),
        sa.CheckConstraint("octet_length(question_digest) = 32", name="question_digest_size_check"),
        sa.CheckConstraint(
            "(form_token IS NULL AND form_token_digest IS NULL) OR "
            "(form_token IS NOT NULL AND form_token_digest IS NOT NULL "
            "AND octet_length(form_token_digest) = 32)",
            name="form_token_digest_check",
        ),
        sa.CheckConstraint(
            "status IN ('awaiting_form_binding', 'awaiting_answer', 'confirming', 'answered', "
            "'rejected', 'expired', 'reconciling')",
            name="status_check",
        ),
        sa.CheckConstraint(
            "(status = 'awaiting_form_binding' AND form_token IS NULL) OR "
            "(status IN ('awaiting_answer', 'confirming', 'answered', 'rejected', 'expired') "
            "AND form_token IS NOT NULL) OR status = 'reconciling'",
            name="form_binding_status_check",
        ),
        sa.ForeignKeyConstraint(
            ["business_session_id", "owner_user_id", "reference_set_id"],
            [
                "business_sessions.id",
                "business_sessions.owner_user_id",
                "business_sessions.reference_set_id",
            ],
            ondelete="CASCADE",
            name="fk_business_questions_session_owner_ref",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "business_session_id"],
            ["tasks.id", "tasks.business_session_id"],
            ondelete="CASCADE",
            name="fk_business_questions_task_session",
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
            name="fk_business_questions_recommendation_session",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_business_questions"),
        sa.UniqueConstraint(
            "id", "business_session_id", "task_id", name="uq_business_questions_identity"
        ),
        sa.UniqueConstraint(
            "business_session_id", "round_number", name="uq_business_questions_session_round"
        ),
        sa.UniqueConstraint(
            "business_session_id", "source_event_id", name="uq_business_questions_source_event"
        ),
    )
    op.create_index(
        "ix_business_questions_task_status",
        "business_questions",
        ["task_id", "status", "round_number"],
    )
    op.create_index("ix_business_questions_expiry", "business_questions", ["expires_at"])

    op.create_table(
        "business_question_events",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("business_session_id", uuid, nullable=False),
        sa.Column("task_id", uuid, nullable=True),
        sa.Column("question_id", uuid, nullable=True),
        sa.Column("source_event_id", uuid, nullable=False),
        sa.Column("dify_run_id", sa.String(length=320), nullable=False),
        sa.Column("recommendation_revision", sa.Integer(), nullable=False),
        sa.Column("source_sequence", sa.Integer(), nullable=False),
        sa.Column("digest_version", sa.SmallInteger(), nullable=False),
        sa.Column("event_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("disposition", sa.String(length=32), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("source_sequence > 0", name="source_sequence_check"),
        sa.CheckConstraint("recommendation_revision > 0", name="revision_check"),
        sa.CheckConstraint("digest_version > 0", name="digest_version_check"),
        sa.CheckConstraint("octet_length(event_digest) = 32", name="event_digest_size_check"),
        sa.CheckConstraint(
            "disposition IN ('question_created', 'awaiting_form_binding', 'form_bound', "
            "'reconciling', 'stale', 'expired')",
            name="disposition_check",
        ),
        sa.ForeignKeyConstraint(
            ["business_session_id"],
            ["business_sessions.id"],
            ondelete="CASCADE",
            name="fk_business_question_events_session",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "business_session_id"],
            ["tasks.id", "tasks.business_session_id"],
            ondelete="CASCADE",
            name="fk_business_question_events_task_session",
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["business_questions.id"],
            ondelete="SET NULL",
            name="fk_business_question_events_question",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_business_question_events"),
        sa.UniqueConstraint(
            "business_session_id", "source_event_id", name="uq_business_question_event_id"
        ),
        sa.UniqueConstraint(
            "business_session_id",
            "dify_run_id",
            "source_sequence",
            name="uq_business_question_event_sequence",
        ),
    )
    op.create_index(
        "ix_business_question_events_task",
        "business_question_events",
        ["task_id", "source_sequence"],
    )

    op.create_table(
        "business_answer_submissions",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("business_session_id", uuid, nullable=False),
        sa.Column("task_id", uuid, nullable=False),
        sa.Column("business_question_id", uuid, nullable=False),
        sa.Column("owner_user_id", uuid, nullable=False),
        sa.Column("reference_set_id", uuid, nullable=False),
        sa.Column("idempotency_key_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("request_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("digest_version", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("answers", jsonb, nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'confirming'"), nullable=False
        ),
        sa.Column(
            "recovery_attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("recovery_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("external_reason_code", sa.String(length=80), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("digest_version > 0", name="digest_version_check"),
        sa.CheckConstraint("octet_length(idempotency_key_hash) = 32", name="key_hash_size_check"),
        sa.CheckConstraint("octet_length(request_digest) = 32", name="request_digest_size_check"),
        sa.CheckConstraint(
            "status IN ('confirming', 'accepted', 'rejected', 'unknown')", name="status_check"
        ),
        sa.CheckConstraint("recovery_attempts >= 0", name="recovery_attempts_check"),
        sa.ForeignKeyConstraint(
            ["business_session_id", "owner_user_id", "reference_set_id"],
            [
                "business_sessions.id",
                "business_sessions.owner_user_id",
                "business_sessions.reference_set_id",
            ],
            ondelete="CASCADE",
            name="fk_business_answers_session_owner_ref",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "business_session_id"],
            ["tasks.id", "tasks.business_session_id"],
            ondelete="CASCADE",
            name="fk_business_answers_task_session",
        ),
        sa.ForeignKeyConstraint(
            ["business_question_id", "business_session_id", "task_id"],
            [
                "business_questions.id",
                "business_questions.business_session_id",
                "business_questions.task_id",
            ],
            ondelete="CASCADE",
            name="fk_business_answers_question_session_task",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_business_answer_submissions"),
        sa.UniqueConstraint(
            "id",
            "business_question_id",
            "business_session_id",
            "task_id",
            name="uq_business_answers_identity",
        ),
        sa.UniqueConstraint("business_question_id", name="uq_business_answers_question"),
        sa.UniqueConstraint(
            "business_question_id", "idempotency_key_hash", name="uq_business_answers_idempotency"
        ),
    )
    op.create_index(
        "ix_business_answers_task_status", "business_answer_submissions", ["task_id", "status"]
    )

    op.add_column("task_actions", sa.Column("business_question_id", uuid, nullable=True))
    op.add_column("task_actions", sa.Column("business_answer_submission_id", uuid, nullable=True))
    op.create_check_constraint(
        "chatflow_answer_association_check",
        "task_actions",
        "(action_kind IN ('dify.chatflow.answer.v1', 'dify.chatflow.answer.recover.v1') "
        "AND scope_kind = 'business_session' "
        "AND task_id IS NOT NULL AND business_question_id IS NOT NULL "
        "AND business_answer_submission_id IS NOT NULL) OR "
        "(action_kind NOT IN ('dify.chatflow.answer.v1', 'dify.chatflow.answer.recover.v1') "
        "AND business_question_id IS NULL "
        "AND business_answer_submission_id IS NULL)",
    )
    op.create_foreign_key(
        "fk_task_actions_business_question_session_task",
        "task_actions",
        "business_questions",
        ["business_question_id", "business_session_id", "task_id"],
        ["id", "business_session_id", "task_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_task_actions_business_answer_question_session_task",
        "task_actions",
        "business_answer_submissions",
        [
            "business_answer_submission_id",
            "business_question_id",
            "business_session_id",
            "task_id",
        ],
        ["id", "business_question_id", "business_session_id", "task_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_task_actions_business_answer_question_session_task", "task_actions", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_task_actions_business_question_session_task", "task_actions", type_="foreignkey"
    )
    op.drop_constraint("chatflow_answer_association_check", "task_actions", type_="check")
    op.drop_column("task_actions", "business_answer_submission_id")
    op.drop_column("task_actions", "business_question_id")

    op.drop_index("ix_business_answers_task_status", table_name="business_answer_submissions")
    op.drop_table("business_answer_submissions")
    op.drop_index("ix_business_question_events_task", table_name="business_question_events")
    op.drop_table("business_question_events")
    op.drop_index("ix_business_questions_expiry", table_name="business_questions")
    op.drop_index("ix_business_questions_task_status", table_name="business_questions")
    op.drop_table("business_questions")
