from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

SafeCode = Annotated[str | None, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]


class DispatcherRegistrations(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_handler_count: Annotated[int, Field(ge=0)]
    action_registration_count: Annotated[int, Field(ge=0)]
    prompt_runtime_registered: bool
    generation_runtime_registered: bool


class LoopHeartbeatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["dispatcher", "scheduler"]
    state: Literal["fresh", "stale", "not_observed"]
    instance_id: UUID | None = None
    started_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_iteration_started_at: datetime | None = None
    last_iteration_completed_at: datetime | None = None
    last_successful_scan_at: datetime | None = None
    last_outbox_scan_at: datetime | None = None
    last_cleanup_scan_at: datetime | None = None
    last_broker_confirmed_at: datetime | None = None
    last_broker_error_at: datetime | None = None
    iteration_count: Annotated[int, Field(ge=0)] = 0
    successful_iteration_count: Annotated[int, Field(ge=0)] = 0
    failed_iteration_count: Annotated[int, Field(ge=0)] = 0
    last_safe_error_code: SafeCode = None
    registrations: DispatcherRegistrations | None = None
    last_progress: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)


class BrokerObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["connected", "queue_missing", "unavailable", "not_configured"]
    checked_at: datetime | None = None
    ready_messages: Annotated[int, Field(ge=0)] | None = None
    consumers: Annotated[int, Field(ge=0)] | None = None
    safe_error_code: SafeCode = None


class OutboxBacklog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pending_due: Annotated[int, Field(ge=0)]
    claimed: Annotated[int, Field(ge=0)]
    expired_claims: Annotated[int, Field(ge=0)]
    published_waiting: Annotated[int, Field(ge=0)]
    processing_receipts: Annotated[int, Field(ge=0)]
    dead: Annotated[int, Field(ge=0)]
    oldest_due_at: datetime | None = None
    oldest_due_age_seconds: Annotated[int, Field(ge=0)] | None = None


class ActionBacklog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scheduled_due: Annotated[int, Field(ge=0)]
    claimed: Annotated[int, Field(ge=0)]
    expired_claims: Annotated[int, Field(ge=0)]
    unknown: Annotated[int, Field(ge=0)]
    rejected: Annotated[int, Field(ge=0)]
    cancelled: Annotated[int, Field(ge=0)]
    oldest_due_at: datetime | None = None
    oldest_due_age_seconds: Annotated[int, Field(ge=0)] | None = None


class LongTaskDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    stage: Literal["prompting", "needs_input", "reconciling"]
    updated_at: datetime
    age_seconds: Annotated[int, Field(ge=0)]
    last_safe_error_code: SafeCode = None
    recommended_step: Literal[
        "await_existing_action",
        "expire_followup_if_due",
        "await_worker_or_review",
        "manual_review_no_blind_retry",
    ]


class CapacitySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    active_reservations: Annotated[int, Field(ge=0)]
    configured_limit: Annotated[int, Field(ge=1)]


class SaveFailureDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    task_item_id: UUID
    attempt_id: UUID
    action_id: UUID | None = None
    action_status: Literal["scheduled", "claimed", "unknown", "rejected", "cancelled"] | None = None
    updated_at: datetime
    safe_error_code: SafeCode = None
    retryable_same_attempt_save: bool = False


class CleanupFailureDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    job_id: UUID
    aggregate_id: UUID
    status: Literal["pending", "claimed", "retry", "completed", "blocked"]
    updated_at: datetime
    safe_error_code: SafeCode = None


class CleanupStatusSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    failed_runs: Annotated[int, Field(ge=0)]
    blocked_jobs: Annotated[int, Field(ge=0)]
    failed_targets: Annotated[int, Field(ge=0)]
    recent_failures: list[CleanupFailureDiagnostic] = Field(default_factory=list)


class WorkerProgressSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    processing_receipts: Annotated[int, Field(ge=0)]
    completed_actions_last_hour: Annotated[int, Field(ge=0)]
    unknown_actions: Annotated[int, Field(ge=0)]
    evidence_scope: Literal["aggregate_database_evidence"] = "aggregate_database_evidence"


class OperationsDiagnosticsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    as_of: datetime
    api_process: Literal["responding"] = "responding"
    database: Literal["connected"] = "connected"
    heartbeat_stale_seconds: Annotated[int, Field(ge=5)]
    long_state_seconds: Annotated[int, Field(ge=60)]
    broker: BrokerObservation
    dispatcher: LoopHeartbeatResponse
    scheduler: LoopHeartbeatResponse
    outbox: OutboxBacklog
    actions: ActionBacklog
    long_tasks: list[LongTaskDiagnostic] = Field(default_factory=list)
    capacity: CapacitySnapshot
    save_failures: list[SaveFailureDiagnostic] = Field(default_factory=list)
    cleanup: CleanupStatusSnapshot
    worker_progress: WorkerProgressSnapshot


class RecoveryOperationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    operation: Literal["expire_followup", "expedite_generation_save"]
    outcome: Literal["expired", "expedited", "already_due"]
    task_id: UUID
    action_id: UUID
    replayed: bool = False


class RecoveryOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
