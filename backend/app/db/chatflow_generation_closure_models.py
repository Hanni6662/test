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
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ChatflowGenerationRunClosure(Base):
    """Observed engine outcome and business delivery outcome, kept separately."""

    __tablename__ = "chatflow_generation_run_closures"
    __table_args__ = (
        ForeignKeyConstraint(
            ["generation_request_id", "task_id"],
            ["chatflow_generation_requests.id", "chatflow_generation_requests.task_id"],
            ondelete="CASCADE",
            name="fk_generation_closure_request_task",
        ),
        ForeignKeyConstraint(
            ["task_id", "business_session_id"],
            ["tasks.id", "tasks.business_session_id"],
            ondelete="CASCADE",
            name="fk_generation_closure_task_session",
        ),
        UniqueConstraint("business_session_id", "dify_run_id", name="uq_generation_closure_run"),
        CheckConstraint("query_count BETWEEN 0 AND 32", name="query_count_check"),
        CheckConstraint(
            "state IN ('pending', 'unknown', 'completed', 'completed_with_execution_warning')",
            name="state_check",
        ),
        CheckConstraint(
            "workflow_status IS NULL OR workflow_status IN "
            "('running', 'paused', 'succeeded', 'partial-succeeded', 'failed', 'stopped')",
            name="workflow_status_check",
        ),
        CheckConstraint(
            "delivery_status IN ('pending', 'confirmed', 'unresolved', 'not_applicable')",
            name="delivery_status_check",
        ),
        CheckConstraint(
            "observation_digest IS NULL OR octet_length(observation_digest) = 32",
            name="digest_size_check",
        ),
        Index("ix_generation_closure_due", "state", "next_observation_at"),
    )

    generation_request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    business_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    dify_run_id: Mapped[str] = mapped_column(String(320), nullable=False)
    dify_app_id: Mapped[str] = mapped_column(String(160), nullable=False)
    dify_workflow_id: Mapped[str] = mapped_column(String(160), nullable=False)
    state: Mapped[str] = mapped_column(String(48), nullable=False, default="pending")
    workflow_status: Mapped[str | None] = mapped_column(String(32))
    workflow_error_code: Mapped[str | None] = mapped_column(String(80))
    observation_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    task_status: Mapped[str] = mapped_column(String(24), nullable=False)
    delivery_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    confirmed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    query_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    next_observation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
