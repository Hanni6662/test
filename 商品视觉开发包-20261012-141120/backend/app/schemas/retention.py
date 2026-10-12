from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

CleanupKind = Literal["task", "recommendation", "reference_set", "upload_group", "orphan"]
CleanupStatus = Literal["queued", "running", "completed", "failed"]
CleanupTrigger = Literal["manual", "scheduled", "catchup"]
CleanupReasonCode = Literal[
    "REFERENCE_STILL_REFERENCED",
    "RECOMMENDATION_STILL_REFERENCED",
    "ACTIVE_UPLOAD_LEASE",
    "TASK_NOT_SAFE_TO_DELETE",
    "ACTIVE_OR_UNRESOLVED_TASK",
    "RECOMMENDATION_ANALYZING",
    "GENERATION_SLOT_ACTIVE",
    "INVENTORY_OBJECT_REFERENCED",
    "INVENTORY_UNAVAILABLE",
    "INVENTORY_SCAN_FAILED",
    "AGGREGATE_NOT_ELIGIBLE",
    "CANDIDATE_NO_LONGER_ELIGIBLE",
    "CLEANUP_TARGETS_INCOMPLETE",
    "AGGREGATE_DELETE_BLOCKED",
    "INVENTORY_DELETE_FAILED",
    "STORAGE_DELETE_FAILED",
]


class CleanupReasonCount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: CleanupReasonCode
    count: Annotated[int, Field(ge=0)]


class RetentionPolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    revision: Annotated[int, Field(ge=1)]
    task_retention_days: Annotated[int, Field(ge=1, le=3650)]
    reference_retention_days: Annotated[int, Field(ge=1, le=3650)]
    recommendation_retention_days: Annotated[int, Field(ge=1, le=3650)]
    schedule_timezone: Annotated[str, Field(min_length=1, max_length=80)] = "Asia/Shanghai"
    schedule_daily_time: Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")] = (
        "03:00"
    )


class RetentionPolicyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_revision: Annotated[int, Field(ge=1)]
    task_retention_days: Annotated[int, Field(ge=1, le=3650)]
    reference_retention_days: Annotated[int, Field(ge=1, le=3650)]
    recommendation_retention_days: Annotated[int, Field(ge=1, le=3650)]
    apply_to_existing: bool = False


class CleanupPreviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    as_of: datetime
    eligible_tasks: Annotated[int, Field(ge=0)] = 0
    eligible_recommendations: Annotated[int, Field(ge=0)] = 0
    eligible_reference_sets: Annotated[int, Field(ge=0)] = 0
    eligible_upload_groups: Annotated[int, Field(ge=0)] = 0
    eligible_inventory_entries: Annotated[int, Field(ge=0)] = 0
    estimated_files: Annotated[int, Field(ge=0)] = 0
    estimated_bytes: Annotated[int, Field(ge=0)] = 0
    skip_reasons: list[CleanupReasonCount] = Field(default_factory=list)
    pending_jobs: Annotated[int, Field(ge=0)] = 0
    inventory_available: bool = False
    safe_error_code: Annotated[str | None, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")] = None


class CleanupRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CleanupCounters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tasks_deleted: Annotated[int, Field(ge=0)] = 0
    recommendations_deleted: Annotated[int, Field(ge=0)] = 0
    reference_sets_deleted: Annotated[int, Field(ge=0)] = 0
    upload_groups_deleted: Annotated[int, Field(ge=0)] = 0
    inventory_entries_deleted: Annotated[int, Field(ge=0)] = 0
    inventory_entries_failed: Annotated[int, Field(ge=0)] = 0
    jobs_pending: Annotated[int, Field(ge=0)] = 0
    jobs_completed: Annotated[int, Field(ge=0)] = 0
    jobs_succeeded: Annotated[int, Field(ge=0)] = 0
    jobs_blocked: Annotated[int, Field(ge=0)] = 0
    jobs_skipped: Annotated[int, Field(ge=0)] = 0
    jobs_failed: Annotated[int, Field(ge=0)] = 0
    reason_counts: list[CleanupReasonCount] = Field(default_factory=list)


class CleanupRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    run_id: UUID
    trigger_kind: CleanupTrigger
    status: CleanupStatus
    policy_revision: Annotated[int, Field(ge=1)]
    counters: CleanupCounters
    scheduled_for: datetime | None = None
    catchup_from: datetime | None = None
    catchup_to: datetime | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    safe_error_code: Annotated[str | None, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")] = None


class CleanupRunAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    run_id: UUID
    status: CleanupStatus
    replayed: bool = False
