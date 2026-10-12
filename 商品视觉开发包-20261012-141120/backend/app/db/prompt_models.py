"""Unregistered R2 prompt workflow models for A's later migration integration."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_JSON = JSON().with_variant(JSONB(), "postgresql")


class TaskPromptWorkflow(Base):
    __tablename__ = "task_prompt_workflows"
    __table_args__ = (
        CheckConstraint("workflow_revision > 0", name="workflow_revision_positive_check"),
        CheckConstraint(
            "octet_length(trusted_analysis_snapshot_digest) = 32",
            name="trusted_snapshot_digest_size_check",
        ),
        CheckConstraint(
            "status IN ('pending_submission', 'submitted', 'reconciling', 'needs_input', "
            "'package_ready', 'failed')",
            name="status_check",
        ),
        UniqueConstraint("task_id", "workflow_revision", name="uq_task_prompt_workflow_revision"),
        UniqueConstraint(
            "workflow_run_id", "workflow_revision", name="uq_task_prompt_workflow_run_revision"
        ),
        UniqueConstraint(
            "workflow_run_id",
            "workflow_revision",
            "task_id",
            name="uq_task_prompt_workflow_run_revision_task",
        ),
        UniqueConstraint("id", "task_id", name="uq_task_prompt_workflow_id_task"),
        Index("ix_task_prompt_workflows_task_status", "task_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    task_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tasks.id", ondelete="RESTRICT", name="fk_task_prompt_workflows_task"),
        nullable=False,
    )
    workflow_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    workflow_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    trusted_analysis_snapshot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trusted_analysis_snapshot_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'pending_submission'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class PromptSubmission(Base):
    __tablename__ = "task_prompt_submissions"
    __table_args__ = (
        CheckConstraint("workflow_revision > 0", name="workflow_revision_positive_check"),
        CheckConstraint("followup_round BETWEEN 0 AND 2", name="followup_round_check"),
        CheckConstraint("content_digest_version > 0", name="content_digest_version_positive_check"),
        CheckConstraint("octet_length(content_digest) = 32", name="content_digest_size_check"),
        CheckConstraint(
            "submit_result_digest IS NULL OR octet_length(submit_result_digest) = 32",
            name="submit_result_digest_size_check",
        ),
        CheckConstraint(
            "callback_result_digest IS NULL OR octet_length(callback_result_digest) = 32",
            name="callback_result_digest_size_check",
        ),
        CheckConstraint(
            "phase IN ('initial_preferences', 'followup_answer')",
            name="phase_check",
        ),
        CheckConstraint(
            "(phase = 'initial_preferences' AND followup_round = 0) OR "
            "(phase = 'followup_answer' AND followup_round BETWEEN 1 AND 2)",
            name="phase_round_check",
        ),
        CheckConstraint(
            "status IN ('prepared', 'accepted', 'rejected', 'unknown', 'callback_received', "
            "'package_ready', 'failed')",
            name="status_check",
        ),
        CheckConstraint(
            "submit_outcome IS NULL OR submit_outcome IN "
            "('accepted', 'definitely_rejected', 'unknown')",
            name="submit_outcome_check",
        ),
        CheckConstraint(
            "callback_outcome IS NULL OR callback_outcome IN "
            "('completed', 'needs_input', 'definitely_rejected')",
            name="callback_outcome_check",
        ),
        UniqueConstraint(
            "workflow_id", "phase", "followup_round", name="uq_prompt_submission_step"
        ),
        UniqueConstraint("submission_id", "task_id", name="uq_prompt_submission_id_task"),
        ForeignKeyConstraint(
            ["workflow_id", "task_id"],
            ["task_prompt_workflows.id", "task_prompt_workflows.task_id"],
            ondelete="RESTRICT",
            name="fk_task_prompt_submissions_workflow_task",
        ),
        ForeignKeyConstraint(
            ["workflow_run_id", "workflow_revision", "task_id"],
            [
                "task_prompt_workflows.workflow_run_id",
                "task_prompt_workflows.workflow_revision",
                "task_prompt_workflows.task_id",
            ],
            ondelete="RESTRICT",
            name="fk_task_prompt_submissions_run_revision_task",
        ),
        Index("ix_task_prompt_submissions_task_created", "task_id", "created_at"),
    )

    submission_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workflow_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tasks.id", ondelete="RESTRICT", name="fk_task_prompt_submissions_task"),
        nullable=False,
    )
    workflow_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    workflow_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    followup_round: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    content_digest_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    content_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    private_payload: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'prepared'")
    )
    submit_outcome: Mapped[str | None] = mapped_column(String(24))
    submit_result_digest_version: Mapped[int | None] = mapped_column(SmallInteger)
    submit_result_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    provider_submission_ref: Mapped[str | None] = mapped_column(String(512))
    provider_run_ref: Mapped[str | None] = mapped_column(String(512))
    submit_reason_code: Mapped[str | None] = mapped_column(String(80))
    callback_outcome: Mapped[str | None] = mapped_column(String(24))
    callback_result_digest_version: Mapped[int | None] = mapped_column(SmallInteger)
    callback_result_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    callback_reason_code: Mapped[str | None] = mapped_column(String(80))
    callback_round: Mapped[int | None] = mapped_column(SmallInteger)
    callback_allowed_fields: Mapped[list[str] | None] = mapped_column(_JSON)
    callback_locked_fields: Mapped[list[str] | None] = mapped_column(_JSON)
    callback_valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    callback_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class MockPromptSubmission(Base):
    __tablename__ = "mock_prompt_submissions"
    __table_args__ = (
        CheckConstraint("workflow_revision > 0", name="workflow_revision_positive_check"),
        CheckConstraint("content_digest_version > 0", name="content_digest_version_positive_check"),
        CheckConstraint("octet_length(content_digest) = 32", name="content_digest_size_check"),
        CheckConstraint(
            "result_digest IS NULL OR octet_length(result_digest) = 32",
            name="result_digest_size_check",
        ),
        CheckConstraint("outcome IN ('accepted', 'definitely_rejected')", name="outcome_check"),
        CheckConstraint(
            "(outcome = 'accepted' AND provider_submission_ref IS NOT NULL "
            "AND result_package IS NOT NULL AND result_digest_version > 0 "
            "AND result_digest IS NOT NULL) OR "
            "(outcome = 'definitely_rejected' AND provider_submission_ref IS NULL "
            "AND result_package IS NULL AND result_digest_version IS NULL "
            "AND result_digest IS NULL)",
            name="outcome_fields_check",
        ),
        UniqueConstraint("provider_submission_ref", name="uq_mock_prompt_provider_submission_ref"),
        Index("ix_mock_prompt_submissions_created", "created_at"),
    )

    submission_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    workflow_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    workflow_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content_digest_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    content_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    provider_submission_ref: Mapped[str | None] = mapped_column(String(512))
    provider_run_ref: Mapped[str | None] = mapped_column(String(512))
    result_package: Mapped[dict[str, object] | None] = mapped_column(_JSON)
    result_digest_version: Mapped[int | None] = mapped_column(SmallInteger)
    result_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    safe_reason_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class TaskPromptPackage(Base):
    __tablename__ = "task_prompt_packages"
    __table_args__ = (
        CheckConstraint("workflow_revision > 0", name="workflow_revision_positive_check"),
        CheckConstraint("package_digest_version > 0", name="package_digest_version_positive_check"),
        CheckConstraint("octet_length(package_digest) = 32", name="package_digest_size_check"),
        CheckConstraint(
            "octet_length(trusted_analysis_snapshot_digest) = 32",
            name="snapshot_digest_size_check",
        ),
        UniqueConstraint("workflow_id", name="uq_task_prompt_packages_workflow"),
        UniqueConstraint("task_id", "workflow_revision", name="uq_task_prompt_packages_revision"),
        UniqueConstraint("submission_id", name="uq_task_prompt_packages_submission"),
        UniqueConstraint("id", "task_id", name="uq_task_prompt_package_id_task"),
        ForeignKeyConstraint(
            ["workflow_id", "task_id"],
            ["task_prompt_workflows.id", "task_prompt_workflows.task_id"],
            ondelete="RESTRICT",
            name="fk_task_prompt_packages_workflow_task",
        ),
        ForeignKeyConstraint(
            ["submission_id", "task_id"],
            ["task_prompt_submissions.submission_id", "task_prompt_submissions.task_id"],
            ondelete="RESTRICT",
            name="fk_task_prompt_packages_submission_task",
        ),
        ForeignKeyConstraint(
            ["workflow_run_id", "workflow_revision", "task_id"],
            [
                "task_prompt_workflows.workflow_run_id",
                "task_prompt_workflows.workflow_revision",
                "task_prompt_workflows.task_id",
            ],
            ondelete="RESTRICT",
            name="fk_task_prompt_packages_run_revision_task",
        ),
        Index("ix_task_prompt_packages_task_created", "task_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workflow_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    submission_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tasks.id", ondelete="RESTRICT", name="fk_task_prompt_packages_task"),
        nullable=False,
    )
    workflow_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    workflow_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    trusted_analysis_snapshot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trusted_analysis_snapshot_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    chatflow_contract_version: Mapped[str] = mapped_column(String(32), nullable=False)
    validator_version: Mapped[str] = mapped_column(String(80), nullable=False)
    package_digest_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    package_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    package: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class TaskPromptItem(Base):
    __tablename__ = "task_prompt_items"
    __table_args__ = (
        CheckConstraint("position >= 1", name="position_positive_check"),
        UniqueConstraint("task_item_id", name="uq_task_prompt_items_task_item"),
        UniqueConstraint("package_id", "position", name="uq_task_prompt_items_package_position"),
        ForeignKeyConstraint(
            ["package_id", "task_id"],
            ["task_prompt_packages.id", "task_prompt_packages.task_id"],
            ondelete="RESTRICT",
            name="fk_task_prompt_items_package_task",
        ),
        ForeignKeyConstraint(
            ["task_item_id", "task_id"],
            ["task_items.id", "task_items.task_id"],
            ondelete="RESTRICT",
            name="fk_task_prompt_items_item_task",
        ),
        Index("ix_task_prompt_items_package", "package_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    package_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tasks.id", ondelete="RESTRICT", name="fk_task_prompt_items_task"),
        nullable=False,
    )
    task_item_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    image_type: Mapped[str] = mapped_column(String(80), nullable=False)
    design_prompt: Mapped[str] = mapped_column(String(16000), nullable=False)
    identity_prompt: Mapped[str] = mapped_column(String(3500), nullable=False)
    identity_anchor_asset_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "image_assets.id",
            ondelete="RESTRICT",
            name="fk_task_prompt_items_identity_asset",
        ),
        nullable=False,
    )
    allowed_reference_aliases: Mapped[list[str]] = mapped_column(_JSON, nullable=False)
    excluded_reference_aliases: Mapped[list[str]] = mapped_column(_JSON, nullable=False)
    allowed_reference_asset_ids: Mapped[list[str]] = mapped_column(_JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
