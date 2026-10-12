from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from kombu import Connection
from sqlalchemy import Engine, and_, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.generation_models import (
    GenerationAttemptContext,
    GenerationCapacityReservation,
)
from app.db.operations_models import OperationsLoopHeartbeat, OperationsRecoveryAudit
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.prompt_models import PromptSubmission, TaskPromptWorkflow
from app.db.retention_models import CleanupJob, CleanupRun, CleanupTarget
from app.db.task_models import Task, TaskItem, TaskItemAttempt
from app.schemas.operations import (
    ActionBacklog,
    BrokerObservation,
    CapacitySnapshot,
    CleanupFailureDiagnostic,
    CleanupStatusSnapshot,
    DispatcherRegistrations,
    LongTaskDiagnostic,
    LoopHeartbeatResponse,
    OperationsDiagnosticsResponse,
    OutboxBacklog,
    RecoveryOperationResponse,
    SaveFailureDiagnostic,
    WorkerProgressSnapshot,
)
from app.services.generation_runtime import DEFAULT_GLOBAL_GENERATION_LIMIT
from app.services.prompt_workflows import PromptWorkflowUnavailable, _fail_prompt_task_items
from app.workers.action_processor import (
    ACTION_EVENT_TYPE,
    ACTION_OPERATION_VERSION,
    ACTION_SCHEMA_VERSION,
    _lock_parent_chain,
)

LOGGER = logging.getLogger(__name__)
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_SAFE_QUEUE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_BROKER_PROBE_TIMEOUT_SECONDS = 2
_SAVE_ACTION_KIND = "generation.save.v1"
_FOLLOWUP_EXPIRED_CODE = "PROMPT_QUESTION_EXPIRED"
_PROMPT_FAILED_CODE = "PROMPT_WORKFLOW_FAILED"
_AUDIT_RESULT_TO_OUTCOME = {
    "FOLLOWUP_TERMINATED": "expired",
    "SAVE_ACTION_EXPEDITED": "expedited",
    "SAVE_ACTION_ALREADY_DUE": "already_due",
}


class OperationsDiagnosticsService:
    """Redacted operations view and narrow, idempotent maintenance recovery."""

    def __init__(
        self,
        engine: Engine,
        *,
        broker_url: str | None,
        queue_name: str,
        heartbeat_stale_seconds: int,
        long_state_seconds: int,
        detail_limit: int,
    ) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        self._broker_url = broker_url
        self._queue_name = queue_name
        self._heartbeat_stale_seconds = heartbeat_stale_seconds
        self._long_state_seconds = long_state_seconds
        self._detail_limit = detail_limit

    def snapshot(self) -> OperationsDiagnosticsResponse:
        try:
            with self._sessions() as db:
                now = db.scalar(select(func.clock_timestamp()))
                if now is None or now.tzinfo is None:
                    raise SQLAlchemyError("database clock was unavailable")
                dispatcher = self._loop_status(db, "dispatcher", now, self._heartbeat_stale_seconds)
                scheduler = self._loop_status(db, "scheduler", now, self._heartbeat_stale_seconds)
                outbox = self._outbox_backlog(db, now)
                actions = self._action_backlog(db, now)
                long_tasks = self._long_tasks(db, now)
                capacity = self._capacity(db)
                save_failures = self._save_failures(db, now)
                cleanup = self._cleanup_status(db)
                worker_progress = self._worker_progress(db, now)
            broker = self._probe_broker()
        except DomainError:
            raise
        except SQLAlchemyError:
            raise DomainError(
                503, "OPERATIONS_DATABASE_UNAVAILABLE", "运维诊断暂时无法读取数据库"
            ) from None

        return OperationsDiagnosticsResponse(
            as_of=now,
            heartbeat_stale_seconds=self._heartbeat_stale_seconds,
            long_state_seconds=self._long_state_seconds,
            broker=broker,
            dispatcher=dispatcher,
            scheduler=scheduler,
            outbox=outbox,
            actions=actions,
            long_tasks=long_tasks,
            capacity=capacity,
            save_failures=save_failures,
            cleanup=cleanup,
            worker_progress=worker_progress,
        )

    def expire_followup(self, task_id: UUID, *, idempotency_key: str) -> RecoveryOperationResponse:
        operation = "expire_followup"
        key_hash = hashlib.sha256(idempotency_key.encode("ascii")).digest()
        request_digest = _request_digest(operation, task_id=task_id)
        try:
            with self._sessions() as db, db.begin():
                replay = _find_replay(db, key_hash, request_digest, operation)
                if replay is not None:
                    return _recovery_response(replay, replayed=True)

                workflow_hint = db.execute(
                    select(TaskPromptWorkflow)
                    .where(TaskPromptWorkflow.task_id == task_id)
                    .order_by(TaskPromptWorkflow.workflow_revision.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if workflow_hint is None:
                    raise _recovery_not_available()
                submission_hint = db.execute(
                    select(PromptSubmission)
                    .where(PromptSubmission.workflow_id == workflow_hint.id)
                    .order_by(
                        PromptSubmission.followup_round.desc(),
                        PromptSubmission.created_at.desc(),
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if submission_hint is None:
                    raise _recovery_not_available()
                action_hints = (
                    db.execute(
                        select(Action)
                        .where(
                            Action.scope_kind == "submission",
                            Action.scope_id == submission_hint.submission_id,
                        )
                        .order_by(Action.sequence, Action.id)
                    )
                    .scalars()
                    .all()
                )
                if not action_hints:
                    raise _recovery_not_available()
                action_id = action_hints[0].id
                event_ids = sorted({action.outbox_event_id for action in action_hints}, key=str)
                locked_events = self._lock_events_and_receipts(db, event_ids)
                if locked_events is None:
                    raise _recovery_not_available()

                locked = _lock_parent_chain(db, action_hints[0])
                if locked is None:
                    raise _recovery_not_available()
                task = locked.task
                workflow = locked.workflow
                submission = locked.submission
                if workflow is None or submission is None:
                    raise _recovery_not_available()

                current_actions = (
                    db.execute(
                        select(Action)
                        .where(
                            Action.scope_kind == "submission",
                            Action.scope_id == submission.submission_id,
                        )
                        .order_by(Action.sequence, Action.id)
                        .with_for_update()
                    )
                    .scalars()
                    .all()
                )
                if {action.outbox_event_id for action in current_actions} != set(event_ids):
                    raise _recovery_not_available()
                if any(action.status in {"scheduled", "claimed"} for action in current_actions):
                    raise _recovery_not_available()
                if any(receipt.status == "processing" for _, receipt in locked_events):
                    raise _recovery_not_available()

                latest_revision = db.scalar(
                    select(func.max(TaskPromptWorkflow.workflow_revision)).where(
                        TaskPromptWorkflow.task_id == task.id
                    )
                )
                latest_submission_id = db.scalar(
                    select(PromptSubmission.submission_id)
                    .where(PromptSubmission.workflow_id == workflow.id)
                    .order_by(
                        PromptSubmission.followup_round.desc(),
                        PromptSubmission.created_at.desc(),
                    )
                    .limit(1)
                )
                now = db.scalar(select(func.clock_timestamp()))
                if (
                    workflow.workflow_revision != latest_revision
                    or submission.submission_id != latest_submission_id
                    or task.status != "needs_input"
                    or task.terminal_at is not None
                    or task.expires_at is not None
                    or workflow.status != "needs_input"
                    or submission.status != "callback_received"
                    or submission.callback_outcome != "needs_input"
                    or submission.callback_result_digest is None
                    or submission.callback_valid_until is None
                    or submission.callback_valid_until > now
                ):
                    raise _recovery_not_available()

                submission.status = "failed"
                submission.callback_reason_code = _FOLLOWUP_EXPIRED_CODE
                submission.callback_round = None
                submission.callback_allowed_fields = None
                submission.callback_locked_fields = None
                submission.callback_valid_until = None
                submission.updated_at = now
                workflow.status = "failed"
                workflow.updated_at = now
                try:
                    _fail_prompt_task_items(db, task, error_code=_PROMPT_FAILED_CODE)
                except PromptWorkflowUnavailable:
                    raise _recovery_not_available() from None

                audit = _new_audit(
                    task_id=task.id,
                    action_id=action_id,
                    operation_kind=operation,
                    idempotency_key_hash=key_hash,
                    request_digest=request_digest,
                    result_code="FOLLOWUP_TERMINATED",
                )
                db.add(audit)
                db.flush()
                return _recovery_response(audit, replayed=False)
        except IntegrityError:
            replay = self._read_replay(key_hash, request_digest, operation)
            if replay is not None:
                return _recovery_response(replay, replayed=True)
            raise DomainError(409, "OPERATIONS_RECOVERY_CONFLICT", "恢复操作状态已变化") from None
        except DomainError:
            raise
        except SQLAlchemyError:
            raise DomainError(
                503, "OPERATIONS_DATABASE_UNAVAILABLE", "运维恢复暂时无法访问数据库"
            ) from None

    def expedite_same_attempt_save(
        self, action_id: UUID, *, idempotency_key: str
    ) -> RecoveryOperationResponse:
        operation = "expedite_generation_save"
        key_hash = hashlib.sha256(idempotency_key.encode("ascii")).digest()
        request_digest = _request_digest(operation, action_id=action_id)
        try:
            with self._sessions() as db, db.begin():
                replay = _find_replay(db, key_hash, request_digest, operation)
                if replay is not None:
                    return _recovery_response(replay, replayed=True)

                hint = db.execute(select(Action).where(Action.id == action_id)).scalar_one_or_none()
                if hint is None:
                    raise _recovery_not_available()
                locked_events = self._lock_events_and_receipts(db, [hint.outbox_event_id])
                if locked_events is None:
                    raise _recovery_not_available()
                event, receipt = locked_events[0]
                locked = _lock_parent_chain(db, hint)
                if locked is None:
                    raise _recovery_not_available()
                action = locked.action
                task = locked.task
                item = locked.task_item
                attempt = locked.attempt
                context = locked.generation_context
                now = db.scalar(select(func.clock_timestamp()))
                if (
                    action.id != action_id
                    or action.scope_kind != "attempt"
                    or action.action_kind != _SAVE_ACTION_KIND
                    or action.input_digest_version != 1
                    or action.status != "scheduled"
                    or event.event_type != ACTION_EVENT_TYPE
                    or event.operation_version != ACTION_OPERATION_VERSION
                    or event.schema_version != ACTION_SCHEMA_VERSION
                    or event.status not in {"pending", "published"}
                    or receipt.status != "pending"
                    or context is None
                    or context.save_state != "original_stored"
                    or context.original_sha256 is None
                    or attempt is None
                    or attempt.status not in {"saving", "reconciling"}
                    or item is None
                    or item.status not in {"saving", "reconciling"}
                    or task.status not in {"generating", "reconciling"}
                ):
                    raise _recovery_not_available()

                latest_sequence = db.scalar(
                    select(func.max(Action.sequence)).where(
                        Action.scope_kind == "attempt", Action.scope_id == action.scope_id
                    )
                )
                if latest_sequence != action.sequence:
                    raise _recovery_not_available()
                if event.status == "claimed" or receipt.status == "processing":
                    raise _recovery_not_available()

                already_due = (
                    action.due_at <= now
                    and event.status == "pending"
                    and event.available_at <= now
                    and receipt.available_at <= now
                )
                if not already_due:
                    action.due_at = now
                    action.updated_at = now
                    attempt.next_action_at = now
                    attempt.updated_at = now
                    event.status = "pending"
                    event.available_at = now
                    event.claim_token = None
                    event.lease_until = None
                    receipt.available_at = now

                result_code = "SAVE_ACTION_ALREADY_DUE" if already_due else "SAVE_ACTION_EXPEDITED"
                audit = _new_audit(
                    task_id=task.id,
                    action_id=action.id,
                    operation_kind=operation,
                    idempotency_key_hash=key_hash,
                    request_digest=request_digest,
                    result_code=result_code,
                )
                db.add(audit)
                db.flush()
                return _recovery_response(audit, replayed=False)
        except IntegrityError:
            replay = self._read_replay(key_hash, request_digest, operation)
            if replay is not None:
                return _recovery_response(replay, replayed=True)
            raise DomainError(409, "OPERATIONS_RECOVERY_CONFLICT", "恢复操作状态已变化") from None
        except DomainError:
            raise
        except SQLAlchemyError:
            raise DomainError(
                503, "OPERATIONS_DATABASE_UNAVAILABLE", "运维恢复暂时无法访问数据库"
            ) from None

    def _lock_events_and_receipts(
        self, db: Session, event_ids: list[UUID]
    ) -> list[tuple[OutboxEvent, OutboxReceipt]] | None:
        locked: list[tuple[OutboxEvent, OutboxReceipt]] = []
        for event_id in sorted(set(event_ids), key=str):
            event = db.execute(
                select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
            ).scalar_one_or_none()
            if event is None:
                return None
            receipt = db.execute(
                select(OutboxReceipt).where(OutboxReceipt.event_id == event_id).with_for_update()
            ).scalar_one_or_none()
            if receipt is None:
                return None
            locked.append((event, receipt))
        return locked

    def _read_replay(
        self, key_hash: bytes, request_digest: bytes, operation: str
    ) -> OperationsRecoveryAudit | None:
        try:
            with self._sessions() as db:
                replay = db.execute(
                    select(OperationsRecoveryAudit).where(
                        OperationsRecoveryAudit.idempotency_key_hash == key_hash
                    )
                ).scalar_one_or_none()
        except SQLAlchemyError:
            raise DomainError(
                503, "OPERATIONS_DATABASE_UNAVAILABLE", "运维恢复暂时无法访问数据库"
            ) from None
        if replay is None:
            return None
        if replay.operation_kind != operation or replay.request_digest != request_digest:
            raise DomainError(409, "IDEMPOTENCY_CONFLICT", "幂等键已用于其他运维操作")
        return replay

    def _probe_broker(self) -> BrokerObservation:
        checked_at = datetime.now(UTC)
        if self._broker_url is None:
            return BrokerObservation(state="not_configured", checked_at=checked_at)
        if not _SAFE_QUEUE.fullmatch(self._queue_name):
            return BrokerObservation(
                state="unavailable",
                checked_at=checked_at,
                safe_error_code="BROKER_PROBE_CONFIG_INVALID",
            )
        try:
            connection = Connection(
                self._broker_url,
                connect_timeout=_BROKER_PROBE_TIMEOUT_SECONDS,
                heartbeat=0,
            )
            with connection:
                channel = connection.channel()
                try:
                    _name, ready_messages, consumers = channel.queue_declare(
                        queue=self._queue_name, passive=True
                    )
                finally:
                    channel.close()
            return BrokerObservation(
                state="connected",
                checked_at=checked_at,
                ready_messages=max(0, int(ready_messages)),
                consumers=max(0, int(consumers)),
            )
        except Exception as error:
            code = getattr(error, "code", None) or getattr(error, "reply_code", None)
            if code == 404:
                return BrokerObservation(state="queue_missing", checked_at=checked_at)
            return BrokerObservation(
                state="unavailable",
                checked_at=checked_at,
                safe_error_code="BROKER_PROBE_FAILED",
            )

    def _loop_status(
        self,
        db: Session,
        component: str,
        now: datetime,
        stale_seconds: int,
    ) -> LoopHeartbeatResponse:
        row = db.get(OperationsLoopHeartbeat, component)
        if row is None:
            return LoopHeartbeatResponse(component=component, state="not_observed")
        age = max(0.0, (now - row.last_heartbeat_at).total_seconds())
        state = "fresh" if age <= stale_seconds else "stale"
        registrations = _dispatcher_registrations(row.registration_state)
        return LoopHeartbeatResponse(
            component=component,
            state=state,
            instance_id=row.instance_id,
            started_at=row.started_at,
            last_heartbeat_at=row.last_heartbeat_at,
            last_iteration_started_at=row.last_iteration_started_at,
            last_iteration_completed_at=row.last_iteration_completed_at,
            last_successful_scan_at=row.last_successful_scan_at,
            last_outbox_scan_at=row.last_outbox_scan_at,
            last_cleanup_scan_at=row.last_cleanup_scan_at,
            last_broker_confirmed_at=row.last_broker_confirmed_at,
            last_broker_error_at=row.last_broker_error_at,
            iteration_count=row.iteration_count,
            successful_iteration_count=row.successful_iteration_count,
            failed_iteration_count=row.failed_iteration_count,
            last_safe_error_code=_safe_code_or_none(row.last_safe_error_code),
            registrations=registrations if component == "dispatcher" else None,
            last_progress=_safe_progress(row.last_progress),
        )

    def _outbox_backlog(self, db: Session, now: datetime) -> OutboxBacklog:
        pending_due = _count(
            db,
            OutboxEvent,
            OutboxEvent.status == "pending",
            OutboxEvent.available_at <= now,
        )
        claimed = _count(db, OutboxEvent, OutboxEvent.status == "claimed")
        expired_claims = _count(
            db,
            OutboxEvent,
            OutboxEvent.status == "claimed",
            OutboxEvent.lease_until <= now,
        )
        published_waiting = int(
            db.scalar(
                select(func.count(OutboxEvent.id))
                .select_from(OutboxEvent)
                .join(OutboxReceipt, OutboxReceipt.event_id == OutboxEvent.id)
                .where(OutboxEvent.status == "published", OutboxReceipt.status == "pending")
            )
            or 0
        )
        processing = _count(db, OutboxReceipt, OutboxReceipt.status == "processing")
        dead = int(
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    or_(
                        OutboxEvent.status == "dead",
                        select(OutboxReceipt.event_id)
                        .where(
                            OutboxReceipt.event_id == OutboxEvent.id,
                            OutboxReceipt.status == "dead",
                        )
                        .exists(),
                    )
                )
            )
            or 0
        )
        oldest_pending = db.scalar(
            select(func.min(OutboxEvent.available_at)).where(
                OutboxEvent.status == "pending", OutboxEvent.available_at <= now
            )
        )
        oldest_published = db.scalar(
            select(func.min(OutboxReceipt.available_at))
            .select_from(OutboxReceipt)
            .join(OutboxEvent, OutboxEvent.id == OutboxReceipt.event_id)
            .where(
                OutboxEvent.status == "published",
                OutboxReceipt.status == "pending",
                OutboxReceipt.available_at <= now,
            )
        )
        due_times = [value for value in (oldest_pending, oldest_published) if value is not None]
        oldest_due_at = min(due_times) if due_times else None
        return OutboxBacklog(
            pending_due=pending_due,
            claimed=claimed,
            expired_claims=expired_claims,
            published_waiting=published_waiting,
            processing_receipts=processing,
            dead=dead,
            oldest_due_at=oldest_due_at,
            oldest_due_age_seconds=(
                _age_seconds(now, oldest_due_at) if oldest_due_at is not None else None
            ),
        )

    def _action_backlog(self, db: Session, now: datetime) -> ActionBacklog:
        oldest_due_at = db.scalar(
            select(func.min(Action.due_at)).where(
                Action.status == "scheduled", Action.due_at <= now
            )
        )
        return ActionBacklog(
            scheduled_due=_count(db, Action, Action.status == "scheduled", Action.due_at <= now),
            claimed=_count(db, Action, Action.status == "claimed"),
            expired_claims=_count(
                db,
                Action,
                Action.status == "claimed",
                Action.lease_until <= now,
            ),
            unknown=_count(db, Action, Action.status == "unknown"),
            rejected=_count(db, Action, Action.status == "rejected"),
            cancelled=_count(db, Action, Action.status == "cancelled"),
            oldest_due_at=oldest_due_at,
            oldest_due_age_seconds=(
                _age_seconds(now, oldest_due_at) if oldest_due_at is not None else None
            ),
        )

    def _long_tasks(self, db: Session, now: datetime) -> list[LongTaskDiagnostic]:
        cutoff = now - timedelta(seconds=self._long_state_seconds)
        rows = db.execute(
            select(Task.id, Task.status, Task.updated_at)
            .where(
                Task.status.in_(("prompting", "needs_input", "reconciling")),
                Task.updated_at <= cutoff,
            )
            .order_by(Task.updated_at, Task.id)
            .limit(self._detail_limit)
        ).all()
        if not rows:
            return []
        task_ids = [row.id for row in rows]
        active_actions = (
            db.execute(
                select(Action.task_id)
                .where(Action.task_id.in_(task_ids), Action.status.in_(("scheduled", "claimed")))
                .distinct()
            )
            .scalars()
            .all()
        )
        active_task_ids = set(active_actions)
        safe_errors: dict[UUID, str | None] = {}
        actions = db.execute(
            select(Action.task_id, Action.last_error_code)
            .where(Action.task_id.in_(task_ids), Action.last_error_code.is_not(None))
            .order_by(Action.updated_at.desc(), Action.id)
        ).all()
        for action_task_id, error_code in actions:
            safe_errors.setdefault(action_task_id, _safe_code_or_none(error_code))
        latest_submission = db.execute(
            select(PromptSubmission.task_id, PromptSubmission.callback_valid_until)
            .where(PromptSubmission.task_id.in_(task_ids))
            .order_by(PromptSubmission.task_id, PromptSubmission.created_at.desc())
        ).all()
        valid_until_by_task: dict[UUID, datetime | None] = {}
        for task_id, valid_until in latest_submission:
            valid_until_by_task.setdefault(task_id, valid_until)
        result: list[LongTaskDiagnostic] = []
        for row in rows:
            if row.status == "needs_input":
                valid_until = valid_until_by_task.get(row.id)
                step = (
                    "expire_followup_if_due"
                    if valid_until is not None
                    and valid_until <= now
                    and row.id not in active_task_ids
                    else "await_worker_or_review"
                )
            elif row.status == "reconciling":
                step = (
                    "await_existing_action"
                    if row.id in active_task_ids
                    else "manual_review_no_blind_retry"
                )
            else:
                step = (
                    "await_existing_action"
                    if row.id in active_task_ids
                    else "await_worker_or_review"
                )
            result.append(
                LongTaskDiagnostic(
                    task_id=row.id,
                    stage=row.status,
                    updated_at=row.updated_at,
                    age_seconds=_age_seconds(now, row.updated_at),
                    last_safe_error_code=safe_errors.get(row.id),
                    recommended_step=step,
                )
            )
        return result

    def _capacity(self, db: Session) -> CapacitySnapshot:
        return CapacitySnapshot(
            active_reservations=_count(
                db, GenerationCapacityReservation, GenerationCapacityReservation.status == "active"
            ),
            configured_limit=DEFAULT_GLOBAL_GENERATION_LIMIT,
        )

    def _save_failures(self, db: Session, now: datetime) -> list[SaveFailureDiagnostic]:
        latest_sequence = (
            select(func.max(Action.sequence))
            .where(Action.scope_kind == "attempt", Action.scope_id == TaskItemAttempt.id)
            .correlate(TaskItemAttempt)
            .scalar_subquery()
        )
        stmt = (
            select(
                Task.id.label("task_id"),
                TaskItem.id.label("task_item_id"),
                TaskItemAttempt.id.label("attempt_id"),
                Action.id.label("action_id"),
                Action.status.label("action_status"),
                Action.updated_at.label("action_updated_at"),
                Action.last_error_code.label("action_error_code"),
                OutboxEvent.status.label("event_status"),
                OutboxEvent.last_error_code.label("event_error_code"),
                OutboxReceipt.status.label("receipt_status"),
                OutboxReceipt.last_error_code.label("receipt_error_code"),
            )
            .select_from(TaskItemAttempt)
            .join(TaskItem, TaskItem.id == TaskItemAttempt.task_item_id)
            .join(Task, Task.id == TaskItem.task_id)
            .join(
                GenerationAttemptContext,
                GenerationAttemptContext.attempt_id == TaskItemAttempt.id,
            )
            .join(
                Action,
                and_(
                    Action.scope_kind == "attempt",
                    Action.scope_id == TaskItemAttempt.id,
                    Action.sequence == latest_sequence,
                    Action.action_kind == _SAVE_ACTION_KIND,
                ),
            )
            .join(OutboxEvent, OutboxEvent.id == Action.outbox_event_id)
            .outerjoin(OutboxReceipt, OutboxReceipt.event_id == OutboxEvent.id)
            .where(
                TaskItemAttempt.status.in_(("saving", "reconciling")),
                TaskItem.status.in_(("saving", "reconciling")),
                GenerationAttemptContext.save_state == "original_stored",
                or_(
                    Action.status.in_(("claimed", "unknown", "rejected", "cancelled")),
                    and_(Action.status == "scheduled", Action.due_at <= now),
                    OutboxEvent.status == "dead",
                    OutboxReceipt.status == "dead",
                ),
            )
            .order_by(Action.updated_at.desc(), Action.id)
            .limit(self._detail_limit)
        )
        rows = db.execute(stmt).all()
        result: list[SaveFailureDiagnostic] = []
        for row in rows:
            safe_error = _safe_code_or_none(
                row.action_error_code
                or row.event_error_code
                or row.receipt_error_code
                or row.event_status
            )
            retryable = (
                row.action_status == "scheduled"
                and row.event_status in {"pending", "published"}
                and row.receipt_status == "pending"
            )
            result.append(
                SaveFailureDiagnostic(
                    task_id=row.task_id,
                    task_item_id=row.task_item_id,
                    attempt_id=row.attempt_id,
                    action_id=row.action_id,
                    action_status=row.action_status,
                    updated_at=row.action_updated_at,
                    safe_error_code=safe_error,
                    retryable_same_attempt_save=retryable,
                )
            )
        return result

    def _cleanup_status(self, db: Session) -> CleanupStatusSnapshot:
        failed_runs = _count(db, CleanupRun, CleanupRun.status == "failed")
        blocked_jobs = _count(db, CleanupJob, CleanupJob.status == "blocked")
        failed_targets = _count(db, CleanupTarget, CleanupTarget.status == "failed")
        failures: dict[UUID, CleanupFailureDiagnostic] = {}
        job_rows = db.execute(
            select(
                CleanupJob.run_id,
                CleanupJob.id,
                CleanupJob.aggregate_id,
                CleanupJob.status,
                CleanupJob.updated_at,
                CleanupJob.last_error_code,
            )
            .where(or_(CleanupJob.status == "blocked", CleanupJob.last_error_code.is_not(None)))
            .order_by(CleanupJob.updated_at.desc(), CleanupJob.id)
            .limit(self._detail_limit)
        ).all()
        for row in job_rows:
            failures[row.id] = CleanupFailureDiagnostic(
                run_id=row.run_id,
                job_id=row.id,
                aggregate_id=row.aggregate_id,
                status=row.status,
                updated_at=row.updated_at,
                safe_error_code=_safe_code_or_none(row.last_error_code),
            )
        target_rows = db.execute(
            select(
                CleanupJob.run_id,
                CleanupJob.id,
                CleanupJob.aggregate_id,
                CleanupJob.status,
                CleanupTarget.updated_at,
                CleanupTarget.last_error_code,
            )
            .select_from(CleanupTarget)
            .join(CleanupJob, CleanupJob.id == CleanupTarget.job_id)
            .where(CleanupTarget.status == "failed")
            .order_by(CleanupTarget.updated_at.desc(), CleanupTarget.id)
            .limit(self._detail_limit)
        ).all()
        for row in target_rows:
            failures.setdefault(
                row.id,
                CleanupFailureDiagnostic(
                    run_id=row.run_id,
                    job_id=row.id,
                    aggregate_id=row.aggregate_id,
                    status=row.status,
                    updated_at=row.updated_at,
                    safe_error_code=_safe_code_or_none(row.last_error_code),
                ),
            )
        recent = sorted(failures.values(), key=lambda item: item.updated_at, reverse=True)[
            : self._detail_limit
        ]
        return CleanupStatusSnapshot(
            failed_runs=failed_runs,
            blocked_jobs=blocked_jobs,
            failed_targets=failed_targets,
            recent_failures=recent,
        )

    def _worker_progress(self, db: Session, now: datetime) -> WorkerProgressSnapshot:
        return WorkerProgressSnapshot(
            processing_receipts=_count(db, OutboxReceipt, OutboxReceipt.status == "processing"),
            completed_actions_last_hour=_count(
                db,
                Action,
                Action.status == "completed",
                Action.completed_at >= now - timedelta(hours=1),
            ),
            unknown_actions=_count(db, Action, Action.status == "unknown"),
        )


def _count(db: Session, model: type, *criteria: object) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(*criteria)) or 0)


def _age_seconds(now: datetime, earlier: datetime) -> int:
    return max(0, int((now - earlier).total_seconds()))


def _safe_code_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else None


def _safe_progress(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key, count in value.items():
        if (
            isinstance(key, str)
            and re.fullmatch(r"[a-z][a-z0-9_]{0,60}", key)
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 0
        ):
            result[key] = count
    return result


def _dispatcher_registrations(value: object) -> DispatcherRegistrations | None:
    if not isinstance(value, dict):
        return None
    event_count = value.get("event_handler_count")
    action_count = value.get("action_registration_count")
    prompt_ready = value.get("prompt_runtime_registered")
    generation_ready = value.get("generation_runtime_registered")
    if (
        not isinstance(event_count, int)
        or isinstance(event_count, bool)
        or event_count < 0
        or not isinstance(action_count, int)
        or isinstance(action_count, bool)
        or action_count < 0
        or not isinstance(prompt_ready, bool)
        or not isinstance(generation_ready, bool)
    ):
        return None
    return DispatcherRegistrations(
        event_handler_count=event_count,
        action_registration_count=action_count,
        prompt_runtime_registered=prompt_ready,
        generation_runtime_registered=generation_ready,
    )


def _request_digest(
    operation: str, *, task_id: UUID | None = None, action_id: UUID | None = None
) -> bytes:
    document = {
        "operation": operation,
        "task_id": str(task_id) if task_id is not None else None,
        "action_id": str(action_id) if action_id is not None else None,
        "version": 1,
    }
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).digest()


def _new_audit(
    *,
    task_id: UUID,
    action_id: UUID,
    operation_kind: str,
    idempotency_key_hash: bytes,
    request_digest: bytes,
    result_code: str,
) -> OperationsRecoveryAudit:
    return OperationsRecoveryAudit(
        task_id=task_id,
        action_id=action_id,
        operation_kind=operation_kind,
        idempotency_key_hash=idempotency_key_hash,
        request_digest=request_digest,
        result_code=result_code,
    )


def _find_replay(
    db: Session, key_hash: bytes, request_digest: bytes, operation: str
) -> OperationsRecoveryAudit | None:
    replay = db.execute(
        select(OperationsRecoveryAudit)
        .where(OperationsRecoveryAudit.idempotency_key_hash == key_hash)
        .with_for_update()
    ).scalar_one_or_none()
    if replay is None:
        return None
    if replay.operation_kind != operation or replay.request_digest != request_digest:
        raise DomainError(409, "IDEMPOTENCY_CONFLICT", "幂等键已用于其他运维操作")
    return replay


def _recovery_response(
    audit: OperationsRecoveryAudit, *, replayed: bool
) -> RecoveryOperationResponse:
    outcome = _AUDIT_RESULT_TO_OUTCOME.get(audit.result_code)
    if outcome is None:
        raise DomainError(503, "OPERATIONS_AUDIT_UNAVAILABLE", "恢复操作记录不可用")
    return RecoveryOperationResponse(
        operation=audit.operation_kind,
        outcome=outcome,
        task_id=audit.task_id,
        action_id=audit.action_id,
        replayed=replayed,
    )


def _recovery_not_available() -> DomainError:
    return DomainError(409, "SAFE_RECOVERY_NOT_AVAILABLE", "当前状态不支持该安全恢复操作")
