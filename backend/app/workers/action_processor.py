from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Literal
from uuid import UUID, uuid4

from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.action_models import Action
from app.db.business_question_models import BusinessAnswerSubmission, BusinessQuestion
from app.db.business_session_models import BusinessSession
from app.db.chatflow_generation_models import (
    ChatflowGenerationDelivery,
    ChatflowGenerationRequest,
    ChatflowGenerationRequestSlot,
    ChatflowGenerationSlotState,
    ChatflowGenerationTracking,
)
from app.db.chatflow_reference_files import ChatflowDifyReferenceFile
from app.db.chatflow_v34_models import ChatflowV34SessionState, ChatflowV34StageEventReceipt
from app.db.generation_models import GenerationAttemptContext
from app.db.models import User
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.prompt_models import PromptSubmission, TaskPromptWorkflow
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ImageAsset, ReferenceSet, ReferenceSetImage
from app.db.task_models import Task, TaskItem, TaskItemAttempt
from app.workers.messages import OutboxMessage
from app.workers.options import QueueOptions
from app.workers.processor import EventProcessor, ProcessResult, WorkerStateUnavailable

ACTION_EVENT_TYPE = "task.action.v1"
ACTION_TASK_NAME = "app.workers.process_task_action_event"
ACTION_SCHEMA_VERSION = 1
ACTION_OPERATION_VERSION = 1
_CHATFLOW_ANSWER_ACTION_KINDS = frozenset(
    {"dify.chatflow.answer.v1", "dify.chatflow.answer.recover.v1"}
)
_ACTION_KIND = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
_ACTION_SEQUENCE_NAMESPACE = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_GENERATION_ACTION_KINDS = frozenset(
    {
        "generation.submit.v1",
        "generation.recover.v1",
        "generation.query.v1",
        "generation.download.v1",
        "generation.save.v1",
    }
)


class ActionInputError(ValueError):
    """A persisted action cannot be converted to its immutable I/O input."""


class ActionScheduleConflict(ValueError):
    """A repeated action scope/sequence was scheduled with different data."""


@dataclass(frozen=True, slots=True)
class ActionInput:
    """Immutable transport for one I/O step, built while the claim Session is open."""

    action_id: UUID
    task_id: UUID | None
    scope_kind: str
    scope_id: UUID
    action_kind: str
    sequence: int
    input_digest_version: int
    input_digest: bytes
    payload: object = field(default=None, repr=False)
    sequence_namespace: str = "default"
    task_item_id: UUID | None = None
    attempt_id: UUID | None = None
    business_session_id: UUID | None = None
    business_question_id: UUID | None = None
    business_answer_submission_id: UUID | None = None
    generation_request_id: UUID | None = None
    generation_delivery_id: UUID | None = None

    def __post_init__(self) -> None:
        if len(self.input_digest) != 32:
            raise ValueError("input_digest must contain a SHA-256 digest")
        object.__setattr__(self, "input_digest", bytes(self.input_digest))
        object.__setattr__(self, "payload", _freeze_payload(self.payload))


def _freeze_payload(value: object) -> object:
    """Copy nested handler input into immutable containers before closing the claim session."""
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("action payload mapping keys must be strings")
        return MappingProxyType({key: _freeze_payload(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_payload(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_payload(item) for item in value)
    if value is None or isinstance(
        value, (bool, int, float, str, bytes, UUID, date, datetime, SecretStr)
    ):
        return value
    raise TypeError("action payload must contain immutable scalar values and containers")


@dataclass(frozen=True, slots=True)
class ActionExecutionResult:
    """Adapter result for exactly one call; private provider details stay in value."""

    outcome: Literal["accepted", "definitely_rejected", "unknown", "safe_retry"]
    safe_reason_code: str | None = None
    value: object = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"accepted", "definitely_rejected", "unknown", "safe_retry"}:
            raise ValueError("outcome must be a recognized action result")
        if self.safe_reason_code is not None and not _ERROR_CODE.fullmatch(self.safe_reason_code):
            raise ValueError("safe_reason_code must be a stable uppercase identifier")
        if self.outcome == "safe_retry":
            if self.safe_reason_code is None or self.retry_after_seconds is None:
                raise ValueError("safe_retry requires a reason code and delay")
            if self.retry_after_seconds < 1:
                raise ValueError("safe_retry delay must be positive")
        elif self.retry_after_seconds is not None:
            raise ValueError("retry delay is only valid for safe_retry")
        if self.outcome == "definitely_rejected" and self.safe_reason_code is None:
            raise ValueError("definitely_rejected requires a stable reason code")


PrepareAction = Callable[[Session, Action], ActionInput]
ExecuteAction = Callable[[ActionInput], ActionExecutionResult]
FinalizeAction = Callable[[Session, Action, ActionInput, ActionExecutionResult], None]


@dataclass(frozen=True, slots=True)
class ActionRegistration:
    """Three-phase hooks for one action kind; only execute/recover run without Session."""

    kind: str
    prepare: PrepareAction
    execute: ExecuteAction
    finalize: FinalizeAction
    recover: ExecuteAction | None = None
    scope_kinds: frozenset[str] = frozenset({"attempt"})
    complete_unknown_step: bool = False

    def __post_init__(self) -> None:
        try:
            normalized_scopes = frozenset(self.scope_kinds)
        except TypeError:
            raise ValueError("scope_kinds must be an iterable of supported scope names") from None
        object.__setattr__(self, "scope_kinds", normalized_scopes)
        if not _ACTION_KIND.fullmatch(self.kind):
            raise ValueError("action kind must be a stable lowercase identifier")
        if not callable(self.prepare) or not callable(self.execute) or not callable(self.finalize):
            raise ValueError("action registration requires prepare, execute, and finalize")
        if self.recover is not None and not callable(self.recover):
            raise ValueError("recover must be callable")
        if not isinstance(self.complete_unknown_step, bool):
            raise ValueError("complete_unknown_step must be a boolean")
        if not self.scope_kinds or not self.scope_kinds.issubset(
            {
                "attempt",
                "submission",
                "business_session",
                "generation_request",
                "generation_delivery",
            }
        ):
            raise ValueError("scope_kinds must contain a supported action scope")


@dataclass(frozen=True, slots=True)
class _Claim:
    action_id: UUID
    event_id: UUID
    action_token: UUID
    receipt_token: UUID
    registration: ActionRegistration
    action_input: ActionInput
    recovering: bool


@dataclass(frozen=True, slots=True)
class _LockedActionParents:
    task: Task | None
    task_items: tuple[TaskItem, ...]
    task_item: TaskItem | None
    attempt: TaskItemAttempt | None
    generation_context: GenerationAttemptContext | None
    workflow: TaskPromptWorkflow | None
    submission: PromptSubmission | None
    business_session: BusinessSession | None
    generation_request: ChatflowGenerationRequest | None
    generation_delivery: ChatflowGenerationDelivery | None
    business_question: BusinessQuestion | None
    business_answer_submission: BusinessAnswerSubmission | None
    recommendation: Recommendation | None
    reference_set: ReferenceSet | None
    action: Action


def schedule_action_in_transaction(
    db: Session,
    *,
    action_id: UUID,
    task_id: UUID | None,
    scope_kind: Literal[
        "submission",
        "attempt",
        "business_session",
        "generation_request",
        "generation_delivery",
    ],
    scope_id: UUID,
    action_kind: str,
    sequence: int,
    sequence_namespace: str = "default",
    due_at: datetime,
    input_digest_version: int,
    input_digest: bytes,
    task_item_id: UUID | None = None,
    attempt_id: UUID | None = None,
    submission_id: UUID | None = None,
    business_session_id: UUID | None = None,
    business_question_id: UUID | None = None,
    business_answer_submission_id: UUID | None = None,
    generation_request_id: UUID | None = None,
    generation_delivery_id: UUID | None = None,
) -> Action:
    """Atomically persist one Action, its UUID-addressed event, and its receipt.

    The caller owns the business-parent locks. Submission IDs are copied into a
    dedicated FK column while `(scope_kind, scope_id, sequence_namespace, sequence)`
    remains the idempotency key shared by each Action namespace. A pre-Task Chatflow
    Action is scoped by its persisted BusinessSession and has no fabricated Task.
    """
    if not _ACTION_KIND.fullmatch(action_kind):
        raise ValueError("action kind must be a stable lowercase identifier")
    if not _ACTION_SEQUENCE_NAMESPACE.fullmatch(sequence_namespace):
        raise ValueError("action sequence namespace must be a stable lowercase identifier")
    if scope_kind not in {
        "submission",
        "attempt",
        "business_session",
        "generation_request",
        "generation_delivery",
    }:
        raise ValueError("scope_kind must be a supported action scope")
    if (
        sequence < 1
        or input_digest_version < 1
        or not isinstance(input_digest, (bytes, bytearray, memoryview))
        or len(input_digest) != 32
    ):
        raise ValueError("action sequence and digest metadata are invalid")
    if due_at.tzinfo is None or due_at.utcoffset() != timedelta(0):
        raise ValueError("due_at must be timezone-aware UTC")
    if scope_kind == "attempt":
        if (
            attempt_id is None
            or task_item_id is None
            or scope_id != attempt_id
            or submission_id is not None
            or business_session_id is not None
            or business_question_id is not None
            or business_answer_submission_id is not None
            or generation_request_id is not None
            or generation_delivery_id is not None
            or task_id is None
        ):
            raise ValueError("attempt scope must identify its persisted attempt and item")
        submission_id = None
    elif scope_kind == "submission":
        if (
            task_item_id is not None
            or attempt_id is not None
            or business_question_id is not None
            or business_answer_submission_id is not None
            or generation_request_id is not None
            or generation_delivery_id is not None
        ):
            raise ValueError("submission scope cannot contain an attempt association")
        if task_id is None or business_session_id is not None:
            raise ValueError("submission scope must identify a task and cannot contain a session")
        if submission_id is not None and submission_id != scope_id:
            raise ValueError("submission scope must identify its persisted submission")
        submission_id = scope_id
    elif scope_kind == "business_session":
        if (
            business_session_id is None
            or scope_id != business_session_id
            or task_item_id is not None
            or attempt_id is not None
            or submission_id is not None
            or generation_request_id is not None
            or generation_delivery_id is not None
        ):
            raise ValueError("business_session scope must identify its persisted session")
        if action_kind in _CHATFLOW_ANSWER_ACTION_KINDS:
            if (
                task_id is None
                or business_question_id is None
                or business_answer_submission_id is None
            ):
                raise ValueError(
                    "Chatflow answer Action must identify its Task, question, and submission"
                )
        elif business_question_id is not None or business_answer_submission_id is not None:
            raise ValueError("only a Chatflow answer Action can identify a question or submission")
    elif scope_kind == "generation_request":
        if (
            generation_request_id is None
            or scope_id != generation_request_id
            or task_id is None
            or business_session_id is None
            or task_item_id is not None
            or attempt_id is not None
            or submission_id is not None
            or business_question_id is not None
            or business_answer_submission_id is not None
            or generation_delivery_id is not None
        ):
            raise ValueError(
                "generation request scope must identify its Task, session, and request"
            )
    else:
        if (
            generation_delivery_id is None
            or scope_id != generation_delivery_id
            or generation_request_id is None
            or task_id is None
            or task_item_id is None
            or attempt_id is None
            or business_session_id is None
            or submission_id is not None
            or business_question_id is not None
            or business_answer_submission_id is not None
        ):
            raise ValueError(
                "generation delivery scope must identify its Task, session, request, "
                "item, and attempt"
            )

    existing = db.execute(
        select(Action).where(
            Action.scope_kind == scope_kind,
            Action.scope_id == scope_id,
            Action.sequence_namespace == sequence_namespace,
            Action.sequence == sequence,
        )
    ).scalar_one_or_none()
    if existing is not None:
        same_action = (
            existing.id == action_id
            and existing.task_id == task_id
            and existing.task_item_id == task_item_id
            and existing.attempt_id == attempt_id
            and existing.submission_id == submission_id
            and existing.business_session_id == business_session_id
            and existing.business_question_id == business_question_id
            and existing.business_answer_submission_id == business_answer_submission_id
            and existing.generation_request_id == generation_request_id
            and existing.generation_delivery_id == generation_delivery_id
            and existing.action_kind == action_kind
            and existing.sequence_namespace == sequence_namespace
            and existing.due_at == due_at
            and existing.input_digest_version == input_digest_version
            and existing.input_digest == input_digest
        )
        if not same_action:
            raise ActionScheduleConflict("action scope and sequence already have different data")
        return existing

    event_id = uuid4()
    event = OutboxEvent(
        id=event_id,
        event_type=ACTION_EVENT_TYPE,
        aggregate_id=action_id,
        operation_version=ACTION_OPERATION_VERSION,
        schema_version=ACTION_SCHEMA_VERSION,
        payload={"action_id": str(action_id)},
        status="pending",
        available_at=due_at,
    )
    db.add(event)
    db.flush()
    db.add(
        OutboxReceipt(
            event_id=event_id,
            status="pending",
            attempt_count=0,
            available_at=due_at,
        )
    )
    action = Action(
        id=action_id,
        task_id=task_id,
        task_item_id=task_item_id,
        attempt_id=attempt_id,
        submission_id=submission_id,
        business_session_id=business_session_id,
        business_question_id=business_question_id,
        business_answer_submission_id=business_answer_submission_id,
        generation_request_id=generation_request_id,
        generation_delivery_id=generation_delivery_id,
        scope_kind=scope_kind,
        scope_id=scope_id,
        action_kind=action_kind,
        sequence_namespace=sequence_namespace,
        sequence=sequence,
        due_at=due_at,
        status="scheduled",
        claim_count=0,
        input_digest_version=input_digest_version,
        input_digest=bytes(input_digest),
        outbox_event_id=event_id,
    )
    db.add(action)
    db.flush()
    return action


class ActionProcessor:
    """Execute a durable action with separate claim, I/O, and fenced finalize phases."""

    def __init__(
        self,
        engine: Engine,
        options: QueueOptions,
        registrations: Mapping[str, ActionRegistration],
        *,
        invalid_message_processor: EventProcessor | None = None,
    ) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        self._options = options
        self._registrations = dict(registrations)
        if any(key != registration.kind for key, registration in self._registrations.items()):
            raise ValueError("action registration keys must match their action kinds")
        self._invalid_messages = invalid_message_processor or EventProcessor(engine, options, {})

    def process_payload(self, payload: object) -> ProcessResult:
        try:
            message = OutboxMessage.model_validate(payload)
        except ValidationError:
            return self._invalid_messages.process_payload(payload)

        try:
            claim, result = self._claim(message)
        except SQLAlchemyError:
            raise WorkerStateUnavailable("worker state is temporarily unavailable") from None
        if claim is None:
            return result

        if claim.recovering:
            recovery = claim.registration.recover
            if recovery is None:
                execution = ActionExecutionResult(
                    "unknown", safe_reason_code="ACTION_LEASE_EXPIRED"
                )
            else:
                execution = self._run_io(recovery, claim.action_input)
        else:
            execution = self._run_io(claim.registration.execute, claim.action_input)

        try:
            return self._finalize(claim, execution)
        except SQLAlchemyError:
            raise WorkerStateUnavailable("worker state is temporarily unavailable") from None
        except Exception:
            # Finalization is DB-only. A rolled-back finalize leaves the claim
            # recoverable; a later delivery must use recover(), never execute().
            raise WorkerStateUnavailable("worker state is temporarily unavailable") from None

    @staticmethod
    def _run_io(runner: ExecuteAction, action_input: ActionInput) -> ActionExecutionResult:
        try:
            result = runner(action_input)
        except Exception:
            # A transport/adapter exception cannot prove whether an external side
            # effect happened, so it is never converted into a blind retry.
            return ActionExecutionResult("unknown", safe_reason_code="ACTION_IO_OUTCOME_UNKNOWN")
        if not isinstance(result, ActionExecutionResult):
            return ActionExecutionResult("unknown", safe_reason_code="ACTION_RESULT_INVALID")
        return result

    def _claim(self, message: OutboxMessage) -> tuple[_Claim | None, ProcessResult]:
        with self._sessions() as db, db.begin():
            event = db.execute(
                select(OutboxEvent).where(OutboxEvent.id == message.event_id).with_for_update()
            ).scalar_one_or_none()
            if event is None:
                return None, ProcessResult("missing")

            receipt = _lock_or_create_receipt(db, event.id)
            if receipt.status == "completed":
                return None, ProcessResult("completed")
            if event.status == "dead" or receipt.status == "dead":
                return None, ProcessResult("dead", receipt.last_error_code or event.last_error_code)
            error_code = _message_error(event, receipt, message)
            if error_code is not None:
                _mark_dead(event, receipt, error_code)
                return None, ProcessResult("dead", error_code)
            if event.event_type != ACTION_EVENT_TYPE:
                _mark_dead(event, receipt, "ACTION_EVENT_TYPE_MISMATCH")
                return None, ProcessResult("dead", "ACTION_EVENT_TYPE_MISMATCH")

            action_id = message.payload.get("action_id")
            if action_id is None or action_id != event.aggregate_id:
                _mark_dead(event, receipt, "ACTION_MESSAGE_MISMATCH")
                return None, ProcessResult("dead", "ACTION_MESSAGE_MISMATCH")

            hint = db.execute(select(Action).where(Action.id == action_id)).scalar_one_or_none()
            if hint is None or hint.outbox_event_id != event.id:
                _mark_dead(event, receipt, "ACTION_NOT_FOUND")
                return None, ProcessResult("dead", "ACTION_NOT_FOUND")

            locked = _lock_parent_chain(db, hint)
            if locked is None:
                _mark_dead(event, receipt, "ACTION_PARENT_NOT_FOUND")
                return None, ProcessResult("dead", "ACTION_PARENT_NOT_FOUND")
            parents = locked
            task = parents.task
            task_item = parents.task_item
            attempt = parents.attempt
            workflow = parents.workflow
            submission = parents.submission
            action = parents.action
            if not _same_action_identity(hint, action):
                _mark_dead(event, receipt, "ACTION_IDENTITY_CHANGED")
                return None, ProcessResult("dead", "ACTION_IDENTITY_CHANGED")

            now = db.scalar(select(func.clock_timestamp()))
            if receipt.status == "processing" and receipt.lease_until is not None:
                if receipt.lease_until > now:
                    return None, ProcessResult("already_processing")
            elif receipt.available_at > now or action.due_at > now:
                return None, ProcessResult("not_due")

            if action.status in {"completed", "rejected", "unknown", "cancelled"}:
                receipt.status = "completed"
                receipt.completed_at = func.clock_timestamp()
                receipt.claim_token = None
                receipt.lease_until = None
                return None, ProcessResult("completed")

            recovering = action.status == "claimed"
            if recovering and action.lease_until is not None and action.lease_until > now:
                return None, ProcessResult("already_processing")
            if action.status not in {"scheduled", "claimed"}:
                _mark_dead(event, receipt, "ACTION_STATE_INVALID")
                return None, ProcessResult("dead", "ACTION_STATE_INVALID")

            registration = self._registrations.get(action.action_kind)
            if registration is None:
                if recovering:
                    _mark_domain_reconciling(
                        task,
                        task_item,
                        attempt,
                        business_session=parents.business_session,
                        action_kind=action.action_kind,
                        workflow=workflow,
                        submission=submission,
                        generation_context=parents.generation_context,
                        business_question=parents.business_question,
                        business_answer_submission=parents.business_answer_submission,
                    )
                    action.status = "unknown"
                    action.last_outcome = "unknown"
                    action.last_error_code = "ACTION_LEASE_EXPIRED"
                    action.completed_at = func.clock_timestamp()
                    action.claim_token = None
                    action.lease_until = None
                    receipt.status = "completed"
                    receipt.completed_at = func.clock_timestamp()
                    receipt.claim_token = None
                    receipt.lease_until = None
                    receipt.last_error_code = "ACTION_LEASE_EXPIRED"
                    return None, ProcessResult("unknown", "ACTION_LEASE_EXPIRED")
                action.status = "cancelled"
                action.completed_at = func.clock_timestamp()
                action.last_error_code = "ACTION_KIND_UNSUPPORTED"
                _mark_dead(event, receipt, "ACTION_KIND_UNSUPPORTED")
                return None, ProcessResult("dead", "ACTION_KIND_UNSUPPORTED")
            if action.scope_kind not in registration.scope_kinds:
                action.status = "cancelled"
                action.completed_at = func.clock_timestamp()
                action.last_error_code = "ACTION_SCOPE_UNSUPPORTED"
                _mark_dead(event, receipt, "ACTION_SCOPE_UNSUPPORTED")
                return None, ProcessResult("dead", "ACTION_SCOPE_UNSUPPORTED")

            if not recovering and action.claim_count >= self._options.max_attempts:
                action.status = "cancelled"
                action.completed_at = func.clock_timestamp()
                action.last_error_code = "ACTION_MAX_CLAIMS"
                _mark_dead(event, receipt, "ACTION_MAX_CLAIMS")
                return None, ProcessResult("dead", "ACTION_MAX_CLAIMS")
            if recovering and action.claim_count >= self._options.max_attempts:
                # The previous call may have reached the provider. Exhaustion
                # records uncertainty instead of invoking execute() again.
                _mark_domain_reconciling(
                    task,
                    task_item,
                    attempt,
                    business_session=parents.business_session,
                    action_kind=action.action_kind,
                    workflow=workflow,
                    submission=submission,
                    generation_context=parents.generation_context,
                    business_question=parents.business_question,
                    business_answer_submission=parents.business_answer_submission,
                )
                action.status = "unknown"
                action.last_outcome = "unknown"
                action.last_error_code = "ACTION_RECOVERY_LIMIT"
                action.completed_at = func.clock_timestamp()
                action.claim_token = None
                action.lease_until = None
                receipt.status = "completed"
                receipt.completed_at = func.clock_timestamp()
                receipt.claim_token = None
                receipt.lease_until = None
                receipt.last_error_code = "ACTION_RECOVERY_LIMIT"
                return None, ProcessResult("unknown", "ACTION_RECOVERY_LIMIT")

            try:
                action_input = registration.prepare(db, action)
                _validate_prepared_input(action, action_input)
            except SQLAlchemyError:
                raise
            except Exception:
                action.status = "cancelled"
                action.completed_at = func.clock_timestamp()
                action.last_error_code = "ACTION_PREPARE_FAILED"
                _mark_dead(event, receipt, "ACTION_PREPARE_FAILED")
                return None, ProcessResult("dead", "ACTION_PREPARE_FAILED")

            action_token = uuid4()
            receipt_token = uuid4()
            lease_until = func.clock_timestamp() + timedelta(seconds=self._options.lease_seconds)
            action.status = "claimed"
            action.claim_token = action_token
            action.lease_until = lease_until
            action.claim_count += 1
            action.updated_at = func.clock_timestamp()
            receipt.status = "processing"
            receipt.claim_token = receipt_token
            receipt.lease_until = lease_until
            receipt.attempt_count += 1
            receipt.last_error_code = None
            return (
                _Claim(
                    action_id=action.id,
                    event_id=event.id,
                    action_token=action_token,
                    receipt_token=receipt_token,
                    registration=registration,
                    action_input=action_input,
                    recovering=recovering,
                ),
                ProcessResult("claimed"),
            )

    def _finalize(self, claim: _Claim, result: ActionExecutionResult) -> ProcessResult:
        with self._sessions() as db, db.begin():
            event = db.execute(
                select(OutboxEvent).where(OutboxEvent.id == claim.event_id).with_for_update()
            ).scalar_one_or_none()
            if event is None:
                return ProcessResult("missing")
            receipt = db.execute(
                select(OutboxReceipt).where(OutboxReceipt.event_id == event.id).with_for_update()
            ).scalar_one_or_none()
            if (
                receipt is None
                or receipt.status != "processing"
                or receipt.claim_token != claim.receipt_token
            ):
                return ProcessResult("claim_lost")

            hint = db.execute(
                select(Action).where(Action.id == claim.action_id)
            ).scalar_one_or_none()
            if hint is None:
                return ProcessResult("missing")
            locked = _lock_parent_chain(db, hint)
            if locked is None:
                return ProcessResult("missing")
            parents = locked
            task = parents.task
            task_item = parents.task_item
            attempt = parents.attempt
            workflow = parents.workflow
            submission = parents.submission
            action = parents.action
            if (
                action.status != "claimed"
                or action.claim_token != claim.action_token
                or action.outbox_event_id != event.id
                or not _same_input_identity(action, claim.action_input)
            ):
                return ProcessResult("claim_lost")

            finalize_started_at = db.scalar(select(func.clock_timestamp()))
            claim.registration.finalize(db, action, claim.action_input, result)
            if result.outcome == "unknown":
                selection_evidence_won = False
                if (
                    action.action_kind == "dify.chatflow.continue.v1"
                    and parents.business_session is not None
                    and parents.business_session.flow_version == "visual-v3.4-single-selection"
                ):
                    v34_state = db.execute(
                        select(ChatflowV34SessionState).where(
                            ChatflowV34SessionState.business_session_id
                            == parents.business_session.id
                        )
                    ).scalar_one_or_none()
                    selection_evidence_won = (
                        v34_state is not None
                        and v34_state.selection_status == "accepted"
                        and v34_state.selection_evidence_kind == "selection_applied"
                    )
                if not selection_evidence_won:
                    _mark_domain_reconciling(
                        task,
                        task_item,
                        attempt,
                        business_session=parents.business_session,
                        action_kind=action.action_kind,
                        workflow=workflow,
                        submission=submission,
                        generation_context=parents.generation_context,
                        business_question=parents.business_question,
                        business_answer_submission=parents.business_answer_submission,
                    )
                action.status = (
                    "completed"
                    if claim.registration.complete_unknown_step or selection_evidence_won
                    else "unknown"
                )
            elif result.outcome == "definitely_rejected":
                action.status = "rejected"
            else:
                # safe_retry completes this one I/O step; its finalizer must
                # persist the next due Action with sequence + 1 and new UUID.
                action.status = "completed"

            # A safe-retry finalizer may have inserted and flushed the next
            # Action. Clear the current claim together with its terminal
            # status before the validation query below can autoflush; otherwise
            # PostgreSQL can observe a completed Action with a live lease.
            action.last_outcome = result.outcome
            action.last_error_code = result.safe_reason_code
            action.claim_token = None
            action.lease_until = None
            action.completed_at = func.clock_timestamp()
            action.updated_at = func.clock_timestamp()
            receipt.status = "completed"
            receipt.completed_at = func.clock_timestamp()
            receipt.claim_token = None
            receipt.lease_until = None
            receipt.last_error_code = result.safe_reason_code

            if result.outcome == "safe_retry":
                next_action = db.execute(
                    select(Action).where(
                        Action.scope_kind == action.scope_kind,
                        Action.scope_id == action.scope_id,
                        Action.sequence_namespace == action.sequence_namespace,
                        Action.sequence == action.sequence + 1,
                        Action.status == "scheduled",
                    )
                ).scalar_one_or_none()
                if next_action is None or next_action.due_at < (
                    finalize_started_at + timedelta(seconds=result.retry_after_seconds or 0)
                ):
                    raise ActionInputError("safe retry did not schedule a new due Action")
            return ProcessResult(action.status, result.safe_reason_code)


def mark_expired_action_unknown_in_transaction(
    db: Session,
    event: OutboxEvent,
    receipt: OutboxReceipt,
    *,
    now,
    reason_code: str,
) -> bool:
    """Fence an exhausted expired action lease without assuming it never ran.

    The caller must already hold the OutboxEvent then OutboxReceipt row locks.
    This helper acquires only the remaining business rows in the frozen order.
    """
    if event.event_type != ACTION_EVENT_TYPE or not _ERROR_CODE.fullmatch(reason_code):
        return False
    hint = db.execute(select(Action).where(Action.outbox_event_id == event.id)).scalar_one_or_none()
    if hint is None:
        return False
    locked = _lock_parent_chain(db, hint)
    if locked is None:
        return False
    parents = locked
    task = parents.task
    task_item = parents.task_item
    attempt = parents.attempt
    workflow = parents.workflow
    submission = parents.submission
    action = parents.action
    if (
        action.status != "claimed"
        or action.claim_token is None
        or receipt.status != "processing"
        or receipt.claim_token is None
        or action.lease_until is None
        or receipt.lease_until is None
        or action.lease_until > now
        or receipt.lease_until > now
    ):
        return False

    v34_durable_acceptance = _v34_action_has_durable_acceptance(db, action, parents)
    if not v34_durable_acceptance:
        _mark_domain_reconciling(
            task,
            task_item,
            attempt,
            business_session=parents.business_session,
            action_kind=action.action_kind,
            workflow=workflow,
            submission=submission,
            generation_context=parents.generation_context,
            business_question=parents.business_question,
            business_answer_submission=parents.business_answer_submission,
        )
    action.status = "completed" if v34_durable_acceptance else "unknown"
    if (
        action.action_kind in _CHATFLOW_ANSWER_ACTION_KINDS
        and parents.business_question is not None
        and parents.business_answer_submission is not None
    ):
        try:
            from app.services.chatflow_actions import _schedule_chatflow_answer_recovery

            now_db = db.scalar(select(func.clock_timestamp()))
            scheduled = now_db is not None and _schedule_chatflow_answer_recovery(
                db,
                action,
                parents.business_question,
                parents.business_answer_submission,
                now=now_db,
            )
            if scheduled:
                action.status = "completed"
            else:
                parents.business_question.status = "reconciling"
                parents.business_answer_submission.status = "unknown"
                parents.business_answer_submission.external_reason_code = (
                    "CHATFLOW_ANSWER_RECOVERY_LIMIT"
                )
        except Exception:
            # Keep the Action unknown so retention and operations stay blocked.
            action.status = "unknown"
    action.last_outcome = "unknown"
    action.last_error_code = reason_code
    action.claim_token = None
    action.lease_until = None
    action.completed_at = func.clock_timestamp()
    action.updated_at = func.clock_timestamp()
    receipt.status = "completed"
    receipt.claim_token = None
    receipt.lease_until = None
    receipt.completed_at = func.clock_timestamp()
    receipt.last_error_code = reason_code
    event.status = "published"
    event.claim_token = None
    event.lease_until = None
    event.published_at = event.published_at or func.clock_timestamp()
    event.last_error_code = reason_code
    return True


def _v34_action_has_durable_acceptance(
    db: Session, action: Action, parents: _LockedActionParents
) -> bool:
    """Preserve a V3.4 action when a durable same-run receipt won its lease race."""
    session = parents.business_session
    if (
        session is None
        or session.flow_version != "visual-v3.4-single-selection"
        or session.dify_run_id is None
        or action.business_session_id != session.id
    ):
        return False
    state = db.execute(
        select(ChatflowV34SessionState).where(
            ChatflowV34SessionState.business_session_id == session.id
        )
    ).scalar_one_or_none()
    recommendation = parents.recommendation
    if (
        state is None
        or state.flow_version != session.flow_version
        or recommendation is None
        or recommendation.dify_run_id != session.dify_run_id
    ):
        return False

    accepted_events: tuple[str, ...] = ()
    if action.action_kind in {
        "dify.chatflow.cancel.v1",
        "dify.chatflow.cancel.recover.v1",
    }:
        if state.form_status == "cancelled" and session.status == "cancelled":
            accepted_events = ("selection.cancelled",)
        elif state.form_status == "expired" and session.status == "expired":
            accepted_events = ("selection.expired",)
    elif action.action_kind == "dify.chatflow.continue.v1":
        if state.selection_status == "accepted":
            if state.selection_evidence_kind == "selection_applied":
                accepted_events = ("selection.applied",)
            if session.status == "failed":
                accepted_events += ("pre_generation.incomplete", "workflow.failed")

    if not accepted_events:
        return False
    return (
        db.execute(
            select(ChatflowV34StageEventReceipt.id)
            .where(
                ChatflowV34StageEventReceipt.business_session_id == session.id,
                ChatflowV34StageEventReceipt.flow_version == session.flow_version,
                ChatflowV34StageEventReceipt.recommendation_id == recommendation.id,
                ChatflowV34StageEventReceipt.recommendation_revision == recommendation.revision,
                ChatflowV34StageEventReceipt.dify_run_id == session.dify_run_id,
                ChatflowV34StageEventReceipt.event_type.in_(accepted_events),
                ChatflowV34StageEventReceipt.disposition == "accepted",
                ChatflowV34StageEventReceipt.processed_at.is_not(None),
            )
            .limit(1)
        ).first()
        is not None
    )


def _validate_prepared_input(action: Action, action_input: ActionInput) -> None:
    if not isinstance(action_input, ActionInput):
        raise ActionInputError("prepare must return ActionInput")
    if (
        action_input.action_id != action.id
        or action_input.task_id != action.task_id
        or action_input.scope_kind != action.scope_kind
        or action_input.scope_id != action.scope_id
        or action_input.action_kind != action.action_kind
        or action_input.sequence_namespace != action.sequence_namespace
        or action_input.sequence != action.sequence
        or action_input.input_digest_version != action.input_digest_version
        or action_input.input_digest != action.input_digest
        or action_input.task_item_id != action.task_item_id
        or action_input.attempt_id != action.attempt_id
        or action_input.business_session_id != action.business_session_id
        or action_input.business_question_id != action.business_question_id
        or action_input.business_answer_submission_id != action.business_answer_submission_id
        or action_input.generation_request_id != action.generation_request_id
        or action_input.generation_delivery_id != action.generation_delivery_id
    ):
        raise ActionInputError("prepared input does not match its persisted Action")


def _same_input_identity(action: Action, action_input: ActionInput) -> bool:
    return (
        action.id == action_input.action_id
        and action.task_id == action_input.task_id
        and action.scope_kind == action_input.scope_kind
        and action.scope_id == action_input.scope_id
        and action.action_kind == action_input.action_kind
        and action.sequence_namespace == action_input.sequence_namespace
        and action.sequence == action_input.sequence
        and action.input_digest_version == action_input.input_digest_version
        and action.input_digest == action_input.input_digest
        and action.task_item_id == action_input.task_item_id
        and action.attempt_id == action_input.attempt_id
        and action.business_session_id == action_input.business_session_id
        and action.business_question_id == action_input.business_question_id
        and action.business_answer_submission_id == action_input.business_answer_submission_id
        and action.generation_request_id == action_input.generation_request_id
        and action.generation_delivery_id == action_input.generation_delivery_id
    )


def _same_action_identity(left: Action, right: Action) -> bool:
    return (
        left.id == right.id
        and left.task_id == right.task_id
        and left.task_item_id == right.task_item_id
        and left.attempt_id == right.attempt_id
        and left.submission_id == right.submission_id
        and left.business_session_id == right.business_session_id
        and left.business_question_id == right.business_question_id
        and left.business_answer_submission_id == right.business_answer_submission_id
        and left.generation_request_id == right.generation_request_id
        and left.generation_delivery_id == right.generation_delivery_id
        and left.scope_kind == right.scope_kind
        and left.scope_id == right.scope_id
        and left.action_kind == right.action_kind
        and left.sequence_namespace == right.sequence_namespace
        and left.sequence == right.sequence
        and left.outbox_event_id == right.outbox_event_id
    )


def _lock_parent_chain(db: Session, hint: Action) -> _LockedActionParents | None:
    """Lock each Action's complete parent chain before locking the Action row.

    Submission order remains Task→Workflow→Submission→Action. Attempt order is
    Task→all TaskItems by position→Attempt→optional GenerationAttemptContext→Action.
    Locking every TaskItem before the current Attempt lets generation finalizers
    aggregate sibling states without acquiring a lower-ranked item after Action.
    """
    if hint.scope_kind == "business_session":
        return _lock_business_session_action_parents(db, hint)
    if hint.scope_kind == "generation_request":
        return _lock_chatflow_generation_action_parents(db, hint)
    if hint.scope_kind == "generation_delivery":
        return _lock_chatflow_generation_action_parents(db, hint)

    task_hint = db.execute(
        select(Task.owner_user_id, Task.reference_set_id, Task.recommendation_id).where(
            Task.id == hint.task_id
        )
    ).one_or_none()
    if task_hint is None:
        return None
    owner_user_id, reference_set_id, recommendation_id = task_hint

    locked_user_id = db.execute(
        select(User.id).where(User.id == owner_user_id).with_for_update()
    ).scalar_one_or_none()
    if locked_user_id is None:
        return None
    if (
        db.execute(
            select(ReferenceSet.id).where(ReferenceSet.id == reference_set_id).with_for_update()
        ).scalar_one_or_none()
        is None
    ):
        return None
    if (
        db.execute(
            select(Recommendation.id)
            .where(Recommendation.id == recommendation_id)
            .with_for_update()
        ).scalar_one_or_none()
        is None
    ):
        return None

    task = db.execute(
        select(Task).where(Task.id == hint.task_id).with_for_update()
    ).scalar_one_or_none()
    if task is None or (
        task.owner_user_id != owner_user_id
        or task.reference_set_id != reference_set_id
        or task.recommendation_id != recommendation_id
    ):
        return None

    task_items: tuple[TaskItem, ...] = ()
    task_item = None
    attempt = None
    generation_context = None
    workflow = None
    submission = None
    if hint.scope_kind == "attempt":
        if (
            hint.submission_id is not None
            or hint.attempt_id is None
            or hint.task_item_id is None
            or hint.scope_id != hint.attempt_id
        ):
            return None
        locked_items = (
            db.execute(
                select(TaskItem)
                .where(TaskItem.task_id == task.id)
                .order_by(TaskItem.position, TaskItem.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        task_items = tuple(locked_items)
        task_item = next((item for item in task_items if item.id == hint.task_item_id), None)
        if task_item is None:
            return None
        attempt = db.execute(
            select(TaskItemAttempt)
            .where(
                TaskItemAttempt.id == hint.attempt_id,
                TaskItemAttempt.task_item_id == task_item.id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if attempt is None:
            return None
        generation_context = db.execute(
            select(GenerationAttemptContext)
            .where(GenerationAttemptContext.attempt_id == attempt.id)
            .with_for_update()
        ).scalar_one_or_none()
        if generation_context is not None and (
            generation_context.task_id != task.id
            or generation_context.task_item_id != task_item.id
            or generation_context.owner_user_id != owner_user_id
        ):
            return None
        if hint.action_kind in _GENERATION_ACTION_KINDS and generation_context is None:
            return None
    elif hint.scope_kind == "submission":
        if (
            hint.submission_id is None
            or hint.scope_id != hint.submission_id
            or hint.task_item_id is not None
            or hint.attempt_id is not None
        ):
            return None
        submission_hint = db.execute(
            select(
                PromptSubmission.workflow_id,
                PromptSubmission.workflow_run_id,
                PromptSubmission.workflow_revision,
            ).where(
                PromptSubmission.submission_id == hint.submission_id,
                PromptSubmission.task_id == task.id,
            )
        ).one_or_none()
        if submission_hint is None:
            return None
        workflow_id, workflow_run_id, workflow_revision = submission_hint
        workflow = db.execute(
            select(TaskPromptWorkflow)
            .where(TaskPromptWorkflow.id == workflow_id, TaskPromptWorkflow.task_id == task.id)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            workflow is None
            or workflow.workflow_run_id != workflow_run_id
            or workflow.workflow_revision != workflow_revision
        ):
            return None
        submission = db.execute(
            select(PromptSubmission)
            .where(
                PromptSubmission.submission_id == hint.submission_id,
                PromptSubmission.task_id == task.id,
                PromptSubmission.workflow_id == workflow.id,
                PromptSubmission.workflow_run_id == workflow.workflow_run_id,
                PromptSubmission.workflow_revision == workflow.workflow_revision,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if submission is None:
            return None
    else:
        return None

    action = db.execute(
        select(Action).where(Action.id == hint.id).with_for_update()
    ).scalar_one_or_none()
    if action is None or not _same_action_identity(hint, action):
        return None
    if hint.scope_kind == "submission" and (
        action.submission_id != submission.submission_id
        or action.task_id != submission.task_id
        or action.scope_id != submission.submission_id
    ):
        return None
    return _LockedActionParents(
        task=task,
        task_items=task_items,
        task_item=task_item,
        attempt=attempt,
        generation_context=generation_context,
        workflow=workflow,
        submission=submission,
        business_session=None,
        generation_request=None,
        generation_delivery=None,
        business_question=None,
        business_answer_submission=None,
        recommendation=None,
        reference_set=None,
        action=action,
    )


def _lock_business_session_action_parents(db: Session, hint: Action) -> _LockedActionParents | None:
    """Lock Chatflow parents in the H0 order, with all question rows before answers."""
    session_id = hint.business_session_id
    if (
        session_id is None
        or hint.scope_id != session_id
        or hint.scope_kind != "business_session"
        or hint.task_item_id is not None
        or hint.attempt_id is not None
        or hint.submission_id is not None
    ):
        return None
    session_hint = db.execute(
        select(BusinessSession.owner_user_id, BusinessSession.reference_set_id).where(
            BusinessSession.id == session_id
        )
    ).one_or_none()
    if session_hint is None:
        return None
    owner_user_id, reference_set_id = session_hint
    owner = db.execute(
        select(User).where(User.id == owner_user_id, User.status == "active").with_for_update()
    ).scalar_one_or_none()
    reference_set = db.execute(
        select(ReferenceSet)
        .where(
            ReferenceSet.id == reference_set_id,
            ReferenceSet.owner_user_id == owner_user_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    business_session = db.execute(
        select(BusinessSession)
        .where(
            BusinessSession.id == session_id,
            BusinessSession.owner_user_id == owner_user_id,
            BusinessSession.reference_set_id == reference_set_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    recommendation = db.execute(
        select(Recommendation)
        .where(
            Recommendation.business_session_id == session_id,
            Recommendation.owner_user_id == owner_user_id,
            Recommendation.reference_set_id == reference_set_id,
            Recommendation.source_kind == "dify_chatflow",
        )
        .with_for_update()
    ).scalar_one_or_none()
    if owner is None or reference_set is None or business_session is None or recommendation is None:
        return None
    if business_session.flow_version == "visual-v3.4-single-selection":
        v34_state = db.execute(
            select(ChatflowV34SessionState)
            .where(ChatflowV34SessionState.business_session_id == business_session.id)
            .with_for_update()
        ).scalar_one_or_none()
        if v34_state is None:
            return None

    if hint.action_kind in {
        "dify.chatflow.reference.upload.v1",
        "dify.chatflow.start.v1",
    }:
        # Fixed H8 order: user → reference set → session → recommendation →
        # all reference images → all model assets → session file mappings → Action.
        reference_images = tuple(
            db.execute(
                select(ReferenceSetImage)
                .where(
                    ReferenceSetImage.reference_set_id == reference_set.id,
                    ReferenceSetImage.owner_user_id == owner_user_id,
                )
                .order_by(ReferenceSetImage.position, ReferenceSetImage.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        model_asset_ids = sorted({image.model_asset_id for image in reference_images}, key=str)
        model_assets = tuple(
            db.execute(
                select(ImageAsset)
                .where(
                    ImageAsset.id.in_(model_asset_ids),
                    ImageAsset.owner_user_id == owner_user_id,
                    ImageAsset.asset_kind == "reference_model_input",
                )
                .order_by(ImageAsset.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        reference_files = tuple(
            db.execute(
                select(ChatflowDifyReferenceFile)
                .where(ChatflowDifyReferenceFile.business_session_id == business_session.id)
                .order_by(ChatflowDifyReferenceFile.position, ChatflowDifyReferenceFile.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        if (
            not reference_images
            or len(reference_images) != len(business_session.reference_assets_snapshot)
            or len(model_assets) != len(reference_images)
            or len(reference_files) != len(reference_images)
        ):
            return None
        assets_by_id = {asset.id: asset for asset in model_assets}
        for position, (image, file_row, snapshot) in enumerate(
            zip(
                reference_images,
                reference_files,
                business_session.reference_assets_snapshot,
                strict=True,
            )
        ):
            asset = assets_by_id.get(image.model_asset_id)
            if (
                image.position != position
                or file_row.position != position
                or asset is None
                or file_row.business_session_id != business_session.id
                or file_row.owner_user_id != owner_user_id
                or file_row.reference_set_id != reference_set.id
                or file_row.reference_set_image_id != image.id
                or file_row.model_asset_id != image.model_asset_id
                or file_row.slot != image.slot
                or file_row.content_sha256 != image.content_sha256
                or str(image.id) != snapshot.get("reference_set_image_id")
                or str(image.model_asset_id) != snapshot.get("model_asset_id")
                or image.slot != snapshot.get("slot")
                or image.content_sha256 != snapshot.get("content_sha256")
            ):
                return None

    task: Task | None = None
    task_items: tuple[TaskItem, ...] = ()
    questions: tuple[BusinessQuestion, ...] = ()
    answers: tuple[BusinessAnswerSubmission, ...] = ()
    if hint.task_id is not None:
        task = db.execute(
            select(Task)
            .where(
                Task.id == hint.task_id,
                Task.business_session_id == business_session.id,
                Task.owner_user_id == owner_user_id,
                Task.reference_set_id == reference_set_id,
                Task.recommendation_id == recommendation.id,
                Task.provenance_kind == "dify_chatflow",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if task is None:
            return None
        task_items = tuple(
            db.execute(
                select(TaskItem)
                .where(TaskItem.task_id == task.id)
                .order_by(TaskItem.position, TaskItem.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        questions = tuple(
            db.execute(
                select(BusinessQuestion)
                .where(
                    BusinessQuestion.business_session_id == business_session.id,
                    BusinessQuestion.task_id == task.id,
                )
                .order_by(BusinessQuestion.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        question_ids = [question.id for question in questions]
        if question_ids:
            answers = tuple(
                db.execute(
                    select(BusinessAnswerSubmission)
                    .where(
                        BusinessAnswerSubmission.business_session_id == business_session.id,
                        BusinessAnswerSubmission.task_id == task.id,
                        BusinessAnswerSubmission.business_question_id.in_(question_ids),
                    )
                    .order_by(BusinessAnswerSubmission.id)
                    .with_for_update()
                )
                .scalars()
                .all()
            )
        if (hint.business_question_id is None) != (hint.business_answer_submission_id is None):
            return None
        if hint.business_question_id is not None:
            business_question = next(
                (item for item in questions if item.id == hint.business_question_id), None
            )
            business_answer_submission = next(
                (
                    item
                    for item in answers
                    if item.id == hint.business_answer_submission_id
                    and item.business_question_id == hint.business_question_id
                ),
                None,
            )
            if (
                hint.action_kind not in _CHATFLOW_ANSWER_ACTION_KINDS
                or business_question is None
                or business_answer_submission is None
            ):
                return None
        else:
            if hint.action_kind in _CHATFLOW_ANSWER_ACTION_KINDS:
                return None
            business_question = None
            business_answer_submission = None
    elif (
        db.execute(
            select(Task.id).where(Task.business_session_id == business_session.id).with_for_update()
        ).scalar_one_or_none()
        is not None
    ):
        return None

    action = db.execute(
        select(Action).where(Action.id == hint.id).with_for_update()
    ).scalar_one_or_none()
    if action is None or not _same_action_identity(hint, action):
        return None
    if (
        action.scope_kind != "business_session"
        or action.scope_id != business_session.id
        or action.business_session_id != business_session.id
        or action.task_id != (task.id if task is not None else None)
    ):
        return None
    return _LockedActionParents(
        task=task,
        task_items=task_items,
        task_item=None,
        attempt=None,
        generation_context=None,
        workflow=None,
        submission=None,
        business_session=business_session,
        generation_request=None,
        generation_delivery=None,
        business_question=(business_question if hint.task_id is not None else None),
        business_answer_submission=(
            business_answer_submission if hint.task_id is not None else None
        ),
        recommendation=recommendation,
        reference_set=reference_set,
        action=action,
    )


def _lock_chatflow_generation_action_parents(
    db: Session, hint: Action
) -> _LockedActionParents | None:
    """Lock H0 parents, item/attempts, request, slot state, delivery, then Action."""
    is_delivery_scope = hint.scope_kind == "generation_delivery"
    if (
        hint.generation_request_id is None
        or hint.task_id is None
        or hint.business_session_id is None
        or hint.submission_id is not None
        or hint.business_question_id is not None
        or hint.business_answer_submission_id is not None
        or (
            is_delivery_scope
            and (
                hint.generation_delivery_id is None
                or hint.scope_id != hint.generation_delivery_id
                or hint.task_item_id is None
                or hint.attempt_id is None
            )
        )
        or (
            not is_delivery_scope
            and (
                hint.scope_kind != "generation_request"
                or hint.scope_id != hint.generation_request_id
                or hint.generation_delivery_id is not None
                or hint.task_item_id is not None
                or hint.attempt_id is not None
            )
        )
    ):
        return None
    request_hint = db.execute(
        select(
            ChatflowGenerationRequest.task_id,
            ChatflowGenerationRequest.business_session_id,
        ).where(ChatflowGenerationRequest.id == hint.generation_request_id)
    ).one_or_none()
    if request_hint is None:
        return None
    task_id, session_id = request_hint
    if task_id != hint.task_id or session_id != hint.business_session_id:
        return None
    task_hint = db.execute(
        select(Task.owner_user_id, Task.reference_set_id, Task.recommendation_id).where(
            Task.id == task_id
        )
    ).one_or_none()
    if task_hint is None:
        return None
    owner_id, reference_set_id, recommendation_id = task_hint
    owner = db.execute(
        select(User).where(User.id == owner_id, User.status == "active").with_for_update()
    ).scalar_one_or_none()
    reference_set = db.execute(
        select(ReferenceSet)
        .where(ReferenceSet.id == reference_set_id, ReferenceSet.owner_user_id == owner_id)
        .with_for_update()
    ).scalar_one_or_none()
    business_session = db.execute(
        select(BusinessSession)
        .where(
            BusinessSession.id == session_id,
            BusinessSession.owner_user_id == owner_id,
            BusinessSession.reference_set_id == reference_set_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    recommendation = db.execute(
        select(Recommendation)
        .where(
            Recommendation.id == recommendation_id,
            Recommendation.business_session_id == session_id,
            Recommendation.owner_user_id == owner_id,
            Recommendation.reference_set_id == reference_set_id,
            Recommendation.source_kind == "dify_chatflow",
        )
        .with_for_update()
    ).scalar_one_or_none()
    task = db.execute(
        select(Task)
        .where(
            Task.id == task_id,
            Task.business_session_id == session_id,
            Task.owner_user_id == owner_id,
            Task.reference_set_id == reference_set_id,
            Task.recommendation_id == recommendation_id,
            Task.provenance_kind == "dify_chatflow",
        )
        .with_for_update()
    ).scalar_one_or_none()
    if any(row is None for row in (owner, reference_set, business_session, recommendation, task)):
        return None
    task_items = tuple(
        db.execute(
            select(TaskItem)
            .where(TaskItem.task_id == task.id)
            .order_by(TaskItem.position, TaskItem.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    item_ids = [item.id for item in task_items]
    if not item_ids:
        return None
    attempts = (
        db.execute(
            select(TaskItemAttempt)
            .where(TaskItemAttempt.task_item_id.in_(item_ids))
            .order_by(TaskItemAttempt.task_item_id, TaskItemAttempt.attempt_no, TaskItemAttempt.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    generation_request = db.execute(
        select(ChatflowGenerationRequest)
        .where(
            ChatflowGenerationRequest.id == hint.generation_request_id,
            ChatflowGenerationRequest.task_id == task.id,
            ChatflowGenerationRequest.business_session_id == business_session.id,
            ChatflowGenerationRequest.dify_run_id == business_session.dify_run_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if generation_request is None:
        return None
    request_slots = tuple(
        db.execute(
            select(ChatflowGenerationRequestSlot)
            .where(ChatflowGenerationRequestSlot.generation_request_id == generation_request.id)
            .order_by(ChatflowGenerationRequestSlot.position, ChatflowGenerationRequestSlot.slot_id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    if len(request_slots) != len(task_items) or any(
        slot.task_id != task.id
        or slot.task_item_id not in item_ids
        or not any(
            attempt.id == slot.attempt_id and attempt.task_item_id == slot.task_item_id
            for attempt in attempts
        )
        for slot in request_slots
    ):
        return None
    tracking = db.execute(
        select(ChatflowGenerationTracking)
        .where(ChatflowGenerationTracking.generation_request_id == generation_request.id)
        .with_for_update()
    ).scalar_one_or_none()
    if tracking is None:
        return None
    db.execute(
        select(ChatflowGenerationSlotState)
        .where(ChatflowGenerationSlotState.generation_request_id == generation_request.id)
        .order_by(ChatflowGenerationSlotState.slot_id)
        .with_for_update()
    ).scalars().all()
    if not is_delivery_scope:
        # H6 reconciliation may add or inspect result intents in its finalizer.
        # Lock existing H7 rows before the H6 Action so both scopes share the
        # same generation-parent → slot-state → delivery → Action order.
        db.execute(
            select(ChatflowGenerationDelivery)
            .where(ChatflowGenerationDelivery.generation_request_id == generation_request.id)
            .order_by(ChatflowGenerationDelivery.slot_id)
            .with_for_update()
        ).scalars().all()
    delivery = None
    delivery_item = None
    delivery_attempt = None
    if is_delivery_scope:
        delivery = db.execute(
            select(ChatflowGenerationDelivery)
            .where(
                ChatflowGenerationDelivery.id == hint.generation_delivery_id,
                ChatflowGenerationDelivery.generation_request_id == generation_request.id,
                ChatflowGenerationDelivery.task_id == task.id,
                ChatflowGenerationDelivery.task_item_id == hint.task_item_id,
                ChatflowGenerationDelivery.attempt_id == hint.attempt_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if delivery is None:
            return None
        matching_slot = next(
            (slot for slot in request_slots if slot.slot_id == delivery.slot_id), None
        )
        delivery_item = next(
            (item for item in task_items if item.id == delivery.task_item_id), None
        )
        delivery_attempt = next(
            (
                item_attempt
                for item_attempt in attempts
                if item_attempt.id == delivery.attempt_id
                and item_attempt.task_item_id == delivery.task_item_id
            ),
            None,
        )
        if (
            matching_slot is None
            or matching_slot.task_item_id != delivery.task_item_id
            or matching_slot.attempt_id != delivery.attempt_id
            or delivery.owner_user_id != owner_id
            or delivery_item is None
            or delivery_attempt is None
        ):
            return None
    action = db.execute(
        select(Action).where(Action.id == hint.id).with_for_update()
    ).scalar_one_or_none()
    if action is None or not _same_action_identity(hint, action):
        return None
    if (
        action.scope_kind != hint.scope_kind
        or action.scope_id != (delivery.id if delivery is not None else generation_request.id)
        or action.generation_request_id != generation_request.id
        or action.generation_delivery_id != (delivery.id if delivery is not None else None)
        or action.task_id != task.id
        or action.business_session_id != business_session.id
    ):
        return None
    return _LockedActionParents(
        task=task,
        task_items=task_items,
        task_item=delivery_item,
        attempt=delivery_attempt,
        generation_context=None,
        workflow=None,
        submission=None,
        business_session=business_session,
        generation_request=generation_request,
        generation_delivery=delivery,
        business_question=None,
        business_answer_submission=None,
        recommendation=recommendation,
        reference_set=reference_set,
        action=action,
    )


def _mark_domain_reconciling(
    task: Task | None,
    task_item: TaskItem | None,
    attempt: TaskItemAttempt | None,
    *,
    business_session: BusinessSession | None = None,
    action_kind: str | None = None,
    workflow: TaskPromptWorkflow | None = None,
    submission: PromptSubmission | None = None,
    generation_context: GenerationAttemptContext | None = None,
    business_question: BusinessQuestion | None = None,
    business_answer_submission: BusinessAnswerSubmission | None = None,
) -> None:
    if action_kind == "chatflow.generation.close.observe.v1":
        # A lost read-only observer lease cannot invalidate a generation result.
        return
    # The context is locked before Action with the rest of the attempt chain.
    # Its durable save state is left intact; reconciliation belongs to Task,
    # TaskItem, and Attempt and must not erase stored image recovery metadata.
    del generation_context
    if submission is not None and submission.callback_result_digest is not None:
        # A validated callback is a stronger workflow result than a late or
        # expired submit/recovery response. Preserve its package and task state.
        return
    if attempt is not None and attempt.status not in {"completed", "failed"}:
        attempt.status = "reconciling"
        attempt.next_action_at = None
        attempt.updated_at = func.clock_timestamp()
    if task_item is not None and task_item.status not in {"succeeded", "failed"}:
        task_item.status = "reconciling"
        task_item.updated_at = func.clock_timestamp()
    if task is not None and task.status not in {"succeeded", "partially_succeeded", "failed"}:
        task.status = "reconciling"
        task.updated_at = func.clock_timestamp()
    if business_session is not None and business_session.status not in {"failed", "expired"}:
        business_session.status = "reconciling"
        if business_session.dify_run_id is None:
            business_session.start_outcome = "unknown"
        business_session.updated_at = func.clock_timestamp()
    if submission is not None and submission.status not in {
        "rejected",
        "callback_received",
        "package_ready",
        "failed",
    }:
        submission.status = "unknown"
        submission.updated_at = func.clock_timestamp()
    if workflow is not None and workflow.status not in {"package_ready", "failed"}:
        workflow.status = "reconciling"
        workflow.updated_at = func.clock_timestamp()
    if business_answer_submission is not None and business_answer_submission.status not in {
        "accepted",
        "rejected",
    }:
        business_answer_submission.status = "confirming"
        business_answer_submission.external_reason_code = "CHATFLOW_ANSWER_OUTCOME_UNKNOWN"
        business_answer_submission.updated_at = func.clock_timestamp()
    if business_question is not None and business_question.status not in {
        "answered",
        "rejected",
        "expired",
    }:
        business_question.status = "confirming"
        business_question.updated_at = func.clock_timestamp()


def _lock_or_create_receipt(db: Session, event_id: UUID) -> OutboxReceipt:
    receipt = db.execute(
        select(OutboxReceipt).where(OutboxReceipt.event_id == event_id).with_for_update()
    ).scalar_one_or_none()
    if receipt is None:
        receipt = OutboxReceipt(event_id=event_id, status="pending", attempt_count=0)
        db.add(receipt)
        db.flush()
    return receipt


def _message_error(
    event: OutboxEvent, receipt: OutboxReceipt, message: OutboxMessage
) -> str | None:
    if (
        event.aggregate_id != message.aggregate_id
        or event.operation_version != message.operation_version
        or event.schema_version != message.schema_version
    ):
        return "MESSAGE_EVENT_MISMATCH"
    if message.schema_version != ACTION_SCHEMA_VERSION:
        return "MESSAGE_SCHEMA_UNSUPPORTED"
    if message.operation_version != ACTION_OPERATION_VERSION:
        return "OPERATION_VERSION_UNSUPPORTED"
    try:
        stored_payload = {key: UUID(value) for key, value in event.payload.items()}
    except (TypeError, ValueError):
        return "EVENT_PAYLOAD_INVALID"
    if stored_payload != message.payload:
        return "MESSAGE_EVENT_MISMATCH"
    if receipt.status not in {"pending", "processing", "completed", "dead"}:
        return "RECEIPT_STATE_INVALID"
    return None


def _mark_dead(event: OutboxEvent, receipt: OutboxReceipt, error_code: str) -> None:
    event.status = "dead"
    event.claim_token = None
    event.lease_until = None
    event.last_error_code = error_code
    receipt.status = "dead"
    receipt.claim_token = None
    receipt.lease_until = None
    receipt.last_error_code = error_code
