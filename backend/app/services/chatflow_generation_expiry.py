"""Bounded acceptance proof and accepted-batch continuation for v3.4 H4 permits."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.action_models import Action
from app.db.business_session_models import BusinessSession
from app.db.chatflow_generation_models import (
    ChatflowGenerationAcceptanceEvidence,
    ChatflowGenerationAcceptedBatchFollowup,
    ChatflowGenerationDelivery,
    ChatflowGenerationEventReceipt,
    ChatflowGenerationExpiryAssessment,
    ChatflowGenerationPermit,
    ChatflowGenerationRequest,
    ChatflowGenerationRequestSlot,
    ChatflowGenerationResultManifest,
    ChatflowGenerationSlotState,
    ChatflowGenerationTracking,
    ChatflowPromptPackage,
    ChatflowPromptReference,
)
from app.db.chatflow_v34_models import ChatflowV34SessionState
from app.db.generation_models import GenerationAttemptContext, GenerationResult
from app.db.models import User
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ReferenceSet
from app.db.task_models import Task, TaskItem, TaskItemAttempt
from app.integrations.generation_acceptance import (
    GenerationAcceptanceExpected,
    GenerationAcceptanceObservation,
    GenerationAcceptancePort,
)
from app.schemas.chatflow_v34 import V34_FLOW_VERSION
from app.schemas.generation_acceptance import (
    GENERATION_IMAGE_CONTRACT_V3,
    GenerationAcceptanceAccepted,
    GenerationAcceptanceRejectedExpired,
)
from app.schemas.prompt_workflow import canonical_sha256
from app.services.chatflow_generation_actions import (
    MAX_RECONCILE_QUERIES,
    GenerationStatusPort,
    _apply_service_snapshot,
)
from app.services.chatflow_generation_contracts import image_generation_contract_for_flow_version
from app.services.chatflow_v34_prompt_contract import selection_evidence_matches_action
from app.workers.action_processor import (
    ActionExecutionResult,
    ActionInput,
    ActionRegistration,
    schedule_action_in_transaction,
)

EXPIRY_ASSESS_ACTION = "chatflow.generation.expiry_assess.v1"
EXPIRY_ASSESS_SEQUENCE_NAMESPACE = "generation_expiry_assess.v1"
ACCEPTED_BATCH_RECOVERY_ACTION = "chatflow.generation.accepted_batch_recover.v1"
ACCEPTED_BATCH_SEQUENCE_NAMESPACE = "generation_accepted_batch.v1"
MAX_EXPIRY_QUERIES = 3
MAX_ACCEPTED_BATCH_QUERIES = 5
EXPIRY_BACKOFF_SECONDS = (30, 120)
_EXPIRY_ID_NAMESPACE = UUID("7b08d4f2-760f-4b61-b9a2-680882ef7ba6")
_ACCEPTED_BATCH_ID_NAMESPACE = UUID("e6a29c94-19b7-4b04-ae7a-c29bdb1715c1")
_GENERATION_PERMIT_EXPIRED = "GENERATION_PERMIT_EXPIRED_BEFORE_ACCEPTANCE"


class UnavailableGenerationAcceptancePort:
    def get_acceptance(
        self, _expected: GenerationAcceptanceExpected
    ) -> GenerationAcceptanceObservation:
        return GenerationAcceptanceObservation(
            "unknown", unknown_reason="GENERATION_ACCEPTANCE_SERVICE_NOT_CONFIGURED"
        )


def ensure_generation_expiry_assessment_action(
    db: Session,
    *,
    request: ChatflowGenerationRequest,
    permit: ChatflowGenerationPermit,
    task_id: UUID,
    session_id: UUID,
    tracking: ChatflowGenerationTracking,
) -> Action | None:
    """Create one expiry-due GET-only assessment alongside the first v3.4 H4 handoff."""
    if (
        request.task_id != task_id
        or request.business_session_id != session_id
        or permit.generation_request_id != request.id
        or permit.task_id != task_id
        or request.service_authority_id is None
        or request.service_authority_id != permit.service_authority_id
        or permit.permit_version != 2
        or permit.expires_at.tzinfo is None
        or tracking.generation_request_id != request.id
    ):
        return None
    assessment = db.execute(
        select(ChatflowGenerationExpiryAssessment)
        .where(ChatflowGenerationExpiryAssessment.generation_request_id == request.id)
        .with_for_update()
    ).scalar_one_or_none()
    if assessment is None:
        assessment = ChatflowGenerationExpiryAssessment(
            generation_request_id=request.id,
            task_id=task_id,
            state="scheduled",
            query_count=0,
            next_action_at=permit.expires_at,
        )
        db.add(assessment)
        db.flush()
    if assessment.task_id != task_id or assessment.state != "scheduled" or assessment.query_count:
        return None
    existing = db.execute(
        select(Action).where(
            Action.scope_kind == "generation_request",
            Action.scope_id == request.id,
            Action.sequence_namespace == EXPIRY_ASSESS_SEQUENCE_NAMESPACE,
            Action.sequence == 1,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    return _schedule_expiry_action(
        db,
        request=request,
        task_id=task_id,
        session_id=session_id,
        sequence=1,
        due_at=permit.expires_at,
    )


def _expiry_action_digest(
    *, request_id: UUID, task_id: UUID, session_id: UUID, sequence: int
) -> bytes:
    return canonical_sha256(
        {
            "action_kind": EXPIRY_ASSESS_ACTION,
            "generation_request_id": str(request_id),
            "task_id": str(task_id),
            "business_session_id": str(session_id),
            "sequence_namespace": EXPIRY_ASSESS_SEQUENCE_NAMESPACE,
            "sequence": sequence,
        }
    )


def _expiry_action_id(request_id: UUID, sequence: int) -> UUID:
    return uuid5(_EXPIRY_ID_NAMESPACE, f"{request_id}:{sequence}")


def _schedule_expiry_action(
    db: Session,
    *,
    request: ChatflowGenerationRequest,
    task_id: UUID,
    session_id: UUID,
    sequence: int,
    due_at: datetime,
) -> Action:
    return schedule_action_in_transaction(
        db,
        action_id=_expiry_action_id(request.id, sequence),
        task_id=task_id,
        scope_kind="generation_request",
        scope_id=request.id,
        action_kind=EXPIRY_ASSESS_ACTION,
        sequence=sequence,
        sequence_namespace=EXPIRY_ASSESS_SEQUENCE_NAMESPACE,
        due_at=due_at,
        input_digest_version=1,
        input_digest=_expiry_action_digest(
            request_id=request.id,
            task_id=task_id,
            session_id=session_id,
            sequence=sequence,
        ),
        business_session_id=session_id,
        generation_request_id=request.id,
    )


def build_generation_expiry_registrations(
    acceptance_port: GenerationAcceptancePort,
    status_port: GenerationStatusPort,
) -> dict[str, ActionRegistration]:
    return {
        EXPIRY_ASSESS_ACTION: ActionRegistration(
            kind=EXPIRY_ASSESS_ACTION,
            prepare=prepare_generation_expiry_assessment,
            execute=lambda action_input: execute_generation_expiry_assessment(
                acceptance_port, action_input
            ),
            recover=recover_interrupted_expiry_assessment,
            finalize=finalize_generation_expiry_assessment,
            scope_kinds=frozenset({"generation_request"}),
        ),
        ACCEPTED_BATCH_RECOVERY_ACTION: ActionRegistration(
            kind=ACCEPTED_BATCH_RECOVERY_ACTION,
            prepare=prepare_accepted_batch_recovery,
            execute=lambda action_input: execute_accepted_batch_recovery(status_port, action_input),
            recover=recover_interrupted_accepted_batch_recovery,
            finalize=finalize_accepted_batch_recovery,
            scope_kinds=frozenset({"generation_request"}),
        ),
    }


def _load_h4_rows(db: Session, action: Action):
    request_id = action.generation_request_id
    if (
        request_id is None
        or action.scope_kind != "generation_request"
        or action.scope_id != request_id
        or action.task_id is None
        or action.business_session_id is None
    ):
        raise ValueError("generation expiry Action identity is invalid")
    request = db.execute(
        select(ChatflowGenerationRequest).where(
            ChatflowGenerationRequest.id == request_id,
            ChatflowGenerationRequest.task_id == action.task_id,
            ChatflowGenerationRequest.business_session_id == action.business_session_id,
        )
    ).scalar_one_or_none()
    permit = db.execute(
        select(ChatflowGenerationPermit).where(
            ChatflowGenerationPermit.generation_request_id == request_id,
            ChatflowGenerationPermit.task_id == action.task_id,
        )
    ).scalar_one_or_none()
    package = db.get(ChatflowPromptPackage, request.package_id) if request is not None else None
    session = db.get(BusinessSession, action.business_session_id)
    tracking = db.execute(
        select(ChatflowGenerationTracking).where(
            ChatflowGenerationTracking.generation_request_id == request_id
        )
    ).scalar_one_or_none()
    slots = (
        db.execute(
            select(ChatflowGenerationRequestSlot)
            .where(ChatflowGenerationRequestSlot.generation_request_id == request_id)
            .order_by(ChatflowGenerationRequestSlot.position, ChatflowGenerationRequestSlot.slot_id)
        )
        .scalars()
        .all()
    )
    if (
        request is None
        or permit is None
        or package is None
        or session is None
        or tracking is None
        or not slots
    ):
        raise ValueError("generation expiry H4 identity is incomplete")
    if (
        session.flow_version != V34_FLOW_VERSION
        or request.dify_run_id != session.dify_run_id
        or request.service_authority_id is None
        or request.service_authority_id != permit.service_authority_id
        or permit.permit_version != 2
        or permit.contract_version != "chatflow-handoff-v1"
        or package.task_id != request.task_id
        or package.business_session_id != session.id
        or package.dify_run_id != session.dify_run_id
        or package.package_digest != request.package_digest
        or permit.package_digest != request.package_digest
        or permit.slot_mapping_digest is None
        or permit.reference_manifest_digest is None
    ):
        raise ValueError("generation expiry H4 binding is inconsistent")
    return request, permit, package, session, tracking, slots


def _expected_identity(request, permit, session) -> dict[str, object]:
    if (
        permit.slot_mapping_digest is None
        or permit.reference_manifest_digest is None
        or request.service_authority_id is None
    ):
        raise ValueError("generation acceptance identity is not authority-bound")
    return {
        "generation_request_id": request.id,
        "task_id": request.task_id,
        "dify_run_id": request.dify_run_id,
        "permit_id": permit.permit_id,
        "permit_version": 2,
        "permit_expires_at": permit.expires_at,
        "generation_contract_version": GENERATION_IMAGE_CONTRACT_V3,
        "package_digest": request.package_digest.hex(),
        "slot_mapping_digest": permit.slot_mapping_digest.hex(),
        "reference_manifest_digest": permit.reference_manifest_digest.hex(),
        "service_authority_id": request.service_authority_id,
    }


def _action_input(action: Action, payload: dict[str, object]) -> ActionInput:
    return ActionInput(
        action_id=action.id,
        task_id=action.task_id,
        scope_kind=action.scope_kind,
        scope_id=action.scope_id,
        action_kind=action.action_kind,
        sequence=action.sequence,
        input_digest_version=action.input_digest_version,
        input_digest=action.input_digest,
        payload=payload,
        sequence_namespace=action.sequence_namespace,
        business_session_id=action.business_session_id,
        generation_request_id=action.generation_request_id,
    )


def prepare_generation_expiry_assessment(db: Session, action: Action) -> ActionInput:
    if (
        action.action_kind != EXPIRY_ASSESS_ACTION
        or action.sequence_namespace != EXPIRY_ASSESS_SEQUENCE_NAMESPACE
        or action.sequence < 1
        or action.input_digest
        != _expiry_action_digest(
            request_id=action.generation_request_id,
            task_id=action.task_id,
            session_id=action.business_session_id,
            sequence=action.sequence,
        )
    ):
        raise ValueError("generation expiry assessment Action identity is invalid")
    request, permit, _package, session, _tracking, _slots = _load_h4_rows(db, action)
    assessment = db.execute(
        select(ChatflowGenerationExpiryAssessment)
        .where(ChatflowGenerationExpiryAssessment.generation_request_id == request.id)
        .with_for_update()
    ).scalar_one_or_none()
    if assessment is None or assessment.task_id != request.task_id:
        raise ValueError("generation expiry assessment row is missing")
    should_query = False
    if action.status == "scheduled":
        if (
            assessment.state != "scheduled"
            or action.sequence != assessment.query_count + 1
            or assessment.query_count >= MAX_EXPIRY_QUERIES
        ):
            raise ValueError("generation expiry assessment budget is invalid")
        assessment.query_count += 1
        assessment.state = "active"
        assessment.next_action_at = None
        assessment.updated_at = func.clock_timestamp()
        should_query = True
    elif action.status == "claimed":
        # A GET might already have reached image-generation. Its reservation was
        # committed before I/O, so recovery settles unknown instead of repeating.
        if assessment.query_count < action.sequence:
            raise ValueError("generation expiry query reservation is missing")
    else:
        raise ValueError("generation expiry Action is not executable")
    return _action_input(
        action,
        {
            **_expected_identity(request, permit, session),
            "query_number": action.sequence,
            "should_query": should_query,
        },
    )


def _expected_from_payload(payload: Mapping[str, object]) -> GenerationAcceptanceExpected:
    return GenerationAcceptanceExpected(
        generation_request_id=UUID(str(payload["generation_request_id"])),
        task_id=UUID(str(payload["task_id"])),
        dify_run_id=str(payload["dify_run_id"]),
        permit_id=UUID(str(payload["permit_id"])),
        permit_version=2,
        permit_expires_at=payload["permit_expires_at"],  # type: ignore[arg-type]
        generation_contract_version=GENERATION_IMAGE_CONTRACT_V3,
        package_digest=str(payload["package_digest"]),
        slot_mapping_digest=str(payload["slot_mapping_digest"]),
        reference_manifest_digest=str(payload["reference_manifest_digest"]),
        service_authority_id=UUID(str(payload["service_authority_id"])),
    )


def execute_generation_expiry_assessment(
    port: GenerationAcceptancePort, action_input: ActionInput
) -> ActionExecutionResult:
    payload = action_input.payload
    if not isinstance(payload, Mapping) or payload.get("should_query") is not True:
        return ActionExecutionResult(
            "unknown", safe_reason_code="GENERATION_ACCEPTANCE_READ_INTERRUPTED"
        )
    try:
        observation = port.get_acceptance(_expected_from_payload(payload))
    except Exception:
        return ActionExecutionResult(
            "unknown",
            safe_reason_code="GENERATION_ACCEPTANCE_READ_UNAVAILABLE",
            value={"queried": True},
        )
    if not isinstance(observation, GenerationAcceptanceObservation):
        observation = GenerationAcceptanceObservation(
            "unknown", unknown_reason="GENERATION_ACCEPTANCE_RESPONSE_INVALID"
        )
    if observation.outcome == "accepted" or observation.outcome == "rejected_expired":
        return ActionExecutionResult(
            "accepted", value={"queried": True, "observation": observation}
        )
    query_number = int(payload.get("query_number", 1))
    if query_number < MAX_EXPIRY_QUERIES:
        return ActionExecutionResult(
            "safe_retry",
            safe_reason_code=observation.unknown_reason,
            value={"queried": True},
            retry_after_seconds=EXPIRY_BACKOFF_SECONDS[query_number - 1],
        )
    return ActionExecutionResult(
        "unknown",
        safe_reason_code=observation.unknown_reason or "GENERATION_ACCEPTANCE_NOT_OBSERVED",
        value={"queried": True},
    )


def recover_interrupted_expiry_assessment(_action_input: ActionInput) -> ActionExecutionResult:
    return ActionExecutionResult(
        "unknown",
        safe_reason_code="GENERATION_ACCEPTANCE_READ_INTERRUPTED",
        value={"queried": False},
    )


def finalize_generation_expiry_assessment(
    db: Session, action: Action, action_input: ActionInput, result: ActionExecutionResult
) -> None:
    request_id = action.generation_request_id
    if request_id is None:
        raise ValueError("generation expiry request identity is missing")
    assessment = db.execute(
        select(ChatflowGenerationExpiryAssessment)
        .where(ChatflowGenerationExpiryAssessment.generation_request_id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    tracking = db.execute(
        select(ChatflowGenerationTracking)
        .where(ChatflowGenerationTracking.generation_request_id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if assessment is None or tracking is None:
        raise ValueError("generation expiry finalizer parents are missing")
    now = db.scalar(select(func.clock_timestamp()))
    assessment.last_checked_at = now
    assessment.last_error_code = result.safe_reason_code
    observation = None
    value = result.value if isinstance(result.value, Mapping) else {}
    candidate = value.get("observation")
    if isinstance(candidate, GenerationAcceptanceObservation):
        observation = candidate
    if result.outcome == "safe_retry" and assessment.query_count < MAX_EXPIRY_QUERIES:
        due_at = now + timedelta(seconds=result.retry_after_seconds or 1)
        assessment.state = "scheduled"
        assessment.next_action_at = due_at
        request, _permit, _package, session, _tracking, _slots = _load_h4_rows(db, action)
        _schedule_expiry_action(
            db,
            request=request,
            task_id=action.task_id,
            session_id=session.id,
            sequence=action.sequence + 1,
            due_at=due_at,
        )
    elif observation is None:
        assessment.state = "unknown"
        assessment.next_action_at = None
        assessment.updated_at = now
        return
    elif isinstance(observation.acceptance, GenerationAcceptanceAccepted):
        accepted = observation.acceptance
        evidence = (
            None
            if _accepted_observation_conflicts(db, action, accepted)
            else _evidence_row(
                db,
                action=action,
                acceptance=accepted,
                outcome="accepted",
                closed_at=None,
            )
        )
        if evidence is None:
            assessment.state = "unknown"
            assessment.last_error_code = "GENERATION_ACCEPTANCE_CONFLICT"
        else:
            assessment.state = "accepted"
            assessment.last_error_code = None
            _ensure_accepted_batch_followup(db, action.generation_request_id)
        assessment.next_action_at = None
    elif isinstance(observation.acceptance, GenerationAcceptanceRejectedExpired):
        rejected = observation.acceptance
        closed = _close_task_from_rejection(
            db,
            action=action,
            acceptance=rejected,
            now=now,
        )
        if closed:
            assessment.state = "rejected_expired"
            assessment.last_error_code = None
        else:
            assessment.state = "unknown"
            assessment.last_error_code = "GENERATION_ACCEPTANCE_CONTRADICTS_BACKEND_STATE"
        assessment.next_action_at = None
    else:
        assessment.state = "unknown"
        assessment.next_action_at = None
        assessment.last_error_code = result.safe_reason_code
    assessment.updated_at = now
    tracking.updated_at = now


def _evidence_row(
    db: Session,
    *,
    action: Action,
    acceptance: GenerationAcceptanceAccepted | GenerationAcceptanceRejectedExpired,
    outcome: str,
    closed_at: datetime | None,
) -> ChatflowGenerationAcceptanceEvidence | None:
    request_id = action.generation_request_id
    if request_id is None:
        return None
    saved = acceptance.model_dump(mode="json")
    digest = canonical_sha256(saved)
    existing = db.execute(
        select(ChatflowGenerationAcceptanceEvidence)
        .where(ChatflowGenerationAcceptanceEvidence.generation_request_id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.task_id != action.task_id
            or existing.outcome != outcome
            or existing.batch_id != getattr(acceptance, "batch_id", None)
            or existing.service_authority_id != acceptance.service_authority_id
            or existing.permit_id != acceptance.permit_id
            or existing.evidence_digest != digest
            or existing.receipt_digest
            != (
                bytes.fromhex(acceptance.receipt_digest)
                if isinstance(acceptance, GenerationAcceptanceRejectedExpired)
                else None
            )
        ):
            return None
        if closed_at is not None and existing.closed_at is None:
            existing.closed_at = closed_at
        return existing
    row = ChatflowGenerationAcceptanceEvidence(
        generation_request_id=request_id,
        task_id=action.task_id,
        service_authority_id=acceptance.service_authority_id,
        outcome=outcome,
        batch_id=getattr(acceptance, "batch_id", None),
        permit_id=acceptance.permit_id,
        permit_version=acceptance.permit_version,
        permit_expires_at=acceptance.permit_expires_at,
        service_request_digest=bytes.fromhex(acceptance.service_request_digest),
        evidence_digest=digest,
        receipt_digest=(
            bytes.fromhex(acceptance.receipt_digest)
            if isinstance(acceptance, GenerationAcceptanceRejectedExpired)
            else None
        ),
        receipt_id=(
            str(acceptance.receipt_id)
            if isinstance(acceptance, GenerationAcceptanceRejectedExpired)
            else None
        ),
        evidence=saved,
        closed_at=closed_at,
    )
    db.add(row)
    db.flush()
    return row


def _accepted_observation_conflicts(
    db: Session,
    action: Action,
    acceptance: GenerationAcceptanceAccepted,
) -> bool:
    request_id = action.generation_request_id
    if request_id is None:
        return True
    try:
        request, permit, _package, session, _tracking, _slots = _load_h4_rows(db, action)
    except (TypeError, ValueError):
        return True
    tracking = db.execute(
        select(ChatflowGenerationTracking)
        .where(ChatflowGenerationTracking.generation_request_id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    existing = db.execute(
        select(ChatflowGenerationAcceptanceEvidence)
        .where(ChatflowGenerationAcceptanceEvidence.generation_request_id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    return (
        request.id != acceptance.generation_request_id
        or request.task_id != acceptance.task_id
        or request.dify_run_id != acceptance.dify_run_id
        or session.dify_run_id != acceptance.dify_run_id
        or request.service_authority_id != acceptance.service_authority_id
        or permit.service_authority_id != acceptance.service_authority_id
        or permit.permit_id != acceptance.permit_id
        or permit.permit_version != acceptance.permit_version
        or permit.expires_at.astimezone(UTC) != acceptance.permit_expires_at
        or request.package_digest.hex() != acceptance.package_digest
        or permit.slot_mapping_digest is None
        or permit.slot_mapping_digest.hex() != acceptance.slot_mapping_digest
        or permit.reference_manifest_digest is None
        or permit.reference_manifest_digest.hex() != acceptance.reference_manifest_digest
        or tracking is None
        or (tracking.batch_id is not None and tracking.batch_id != acceptance.batch_id)
        or (
            existing is not None
            and (
                existing.outcome != "accepted"
                or existing.batch_id != acceptance.batch_id
                or existing.service_authority_id != acceptance.service_authority_id
            )
        )
    )


def _close_task_from_rejection(
    db: Session,
    *,
    action: Action,
    acceptance: GenerationAcceptanceRejectedExpired,
    now: datetime,
) -> bool:
    request_id = action.generation_request_id
    if request_id is None:
        return False
    request, permit, package, session, tracking, slots = _load_h4_rows(db, action)
    hint = db.execute(
        select(Task.owner_user_id, Task.reference_set_id, Task.recommendation_id).where(
            Task.id == request.task_id
        )
    ).one_or_none()
    if hint is None:
        return False
    owner_id, reference_set_id, recommendation_id = hint
    owner = db.execute(
        select(User).where(User.id == owner_id, User.status == "active").with_for_update()
    ).scalar_one_or_none()
    reference_set = db.execute(
        select(ReferenceSet)
        .where(
            ReferenceSet.id == reference_set_id,
            ReferenceSet.owner_user_id == owner_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    recommendation = db.execute(
        select(Recommendation)
        .where(
            Recommendation.id == recommendation_id,
            Recommendation.business_session_id == session.id,
            Recommendation.owner_user_id == owner_id,
            Recommendation.reference_set_id == reference_set_id,
            Recommendation.source_kind == "dify_chatflow",
        )
        .with_for_update()
    ).scalar_one_or_none()
    task = db.execute(
        select(Task)
        .where(
            Task.id == request.task_id,
            Task.business_session_id == session.id,
            Task.owner_user_id == owner_id,
            Task.reference_set_id == reference_set_id,
            Task.recommendation_id == recommendation_id,
            Task.provenance_kind == "dify_chatflow",
        )
        .with_for_update()
    ).scalar_one_or_none()
    items = (
        db.execute(
            select(TaskItem)
            .where(TaskItem.task_id == request.task_id)
            .order_by(TaskItem.position, TaskItem.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    attempts = (
        db.execute(
            select(TaskItemAttempt)
            .where(TaskItemAttempt.task_item_id.in_([item.id for item in items]))
            .order_by(TaskItemAttempt.task_item_id, TaskItemAttempt.attempt_no, TaskItemAttempt.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    state = db.execute(
        select(ChatflowV34SessionState)
        .where(ChatflowV34SessionState.business_session_id == session.id)
        .with_for_update()
    ).scalar_one_or_none()
    if any(value is None for value in (owner, reference_set, recommendation, task, state)):
        return False
    selection_action = (
        db.execute(
            select(Action).where(Action.id == state.selection_submission_id)
        ).scalar_one_or_none()
        if state.selection_submission_id is not None
        else None
    )
    try:
        from app.services.chatflow_actions import chatflow_v34_selection_action_digest
        from app.services.tasks import validate_dify_chatflow_task_snapshot

        selection_digest = chatflow_v34_selection_action_digest(
            session=session,
            recommendation=recommendation,
            state=state,
            task=task,
        )
        validate_dify_chatflow_task_snapshot(task, session)
    except (TypeError, ValueError, AttributeError):
        selection_digest = None
    if (
        session.flow_version != V34_FLOW_VERSION
        or session.start_outcome != "accepted"
        or session.status not in {"task_created", "reconciling"}
        or session.dify_run_id is None
        or recommendation.status != "ready"
        or recommendation.dify_run_id != session.dify_run_id
        or recommendation.revision != task.recommendation_revision
        or task.terminal_at is not None
        or task.status not in {"queued", "prompting", "reconciling"}
        or task.provenance_kind != "dify_chatflow"
        or state.flow_version != V34_FLOW_VERSION
        or state.form_status != "consumed"
        or state.selection_status != "accepted"
        or state.selection_submission_id is None
        or state.selection_input_digest is None
        or state.selection_evidence_kind not in {"http_200", "selection_applied"}
        or state.form_node_id != "form_0"
        or state.selection_inputs is None
        or selection_action is None
        or selection_action.task_id != task.id
        or selection_action.business_session_id != session.id
        or selection_action.scope_kind != "business_session"
        or selection_action.scope_id != session.id
        or selection_action.action_kind != "dify.chatflow.continue.v1"
        or selection_action.sequence != 2
        or selection_action.input_digest != selection_digest
        or not selection_evidence_matches_action(
            evidence_kind=state.selection_evidence_kind,
            action_status=selection_action.status,
            last_outcome=selection_action.last_outcome,
        )
        or len(items) != task.planned_count
        or len(slots) != len(items)
        or len(attempts) != len(items)
        or any(
            item.status != "queued"
            or item.image_id is not None
            or item.preview_ready
            or item.width is not None
            or item.height is not None
            for item in items
        )
        or any(
            attempt.status != "prepared"
            or attempt.channel_id is not None
            or attempt.model_id is not None
            or attempt.model_version is not None
            or attempt.provider_job_id is not None
            or attempt.provider_result_ref is not None
            or attempt.started_at is not None
            for attempt in attempts
        )
        or tracking.batch_id is not None
        or tracking.source_sequence is not None
        or tracking.status not in {"awaiting_batch", "submission_unknown"}
        or bool(_slot_states(db, request_id))
        or db.execute(
            select(ChatflowGenerationDelivery.id)
            .where(ChatflowGenerationDelivery.generation_request_id == request_id)
            .limit(1)
        ).first()
        is not None
        or db.execute(
            select(ChatflowGenerationEventReceipt.event_id)
            .where(ChatflowGenerationEventReceipt.generation_request_id == request_id)
            .limit(1)
        ).first()
        is not None
        or db.execute(
            select(ChatflowGenerationResultManifest.generation_request_id)
            .where(ChatflowGenerationResultManifest.generation_request_id == request_id)
            .limit(1)
        ).first()
        is not None
        or db.execute(
            select(GenerationAttemptContext.attempt_id)
            .where(GenerationAttemptContext.attempt_id.in_([attempt.id for attempt in attempts]))
            .limit(1)
        ).first()
        is not None
        or db.execute(
            select(GenerationResult.attempt_id)
            .where(GenerationResult.attempt_id.in_([attempt.id for attempt in attempts]))
            .limit(1)
        ).first()
        is not None
        or request.service_authority_id != acceptance.service_authority_id
        or permit.service_authority_id != acceptance.service_authority_id
        or acceptance.generation_request_id != request.id
        or acceptance.task_id != task.id
        or acceptance.dify_run_id != session.dify_run_id
        or acceptance.permit_id != permit.permit_id
        or acceptance.permit_version != permit.permit_version
        or acceptance.permit_expires_at != permit.expires_at.astimezone(UTC)
        or acceptance.package_digest != request.package_digest.hex()
        or acceptance.slot_mapping_digest != permit.slot_mapping_digest.hex()
        or acceptance.reference_manifest_digest != permit.reference_manifest_digest.hex()
        or package.package_digest != request.package_digest
        or package.owner_user_id != owner.id
        or package.reference_set_id != reference_set.id
        or package.recommendation_id != recommendation.id
        or package.recommendation_revision != recommendation.revision
        or package.request_digest != request.request_digest
        or db.execute(
            select(ChatflowGenerationAcceptanceEvidence.generation_request_id)
            .where(ChatflowGenerationAcceptanceEvidence.generation_request_id == request_id)
            .limit(1)
        ).first()
        is not None
    ):
        return False
    in_flight_action = db.execute(
        select(Action.id)
        .where(
            Action.scope_kind == "generation_request",
            Action.scope_id == request_id,
            Action.status == "claimed",
            Action.id != action.id,
        )
        .limit(1)
        .with_for_update()
    ).first()
    if in_flight_action is not None:
        return False
    existing_attempts = {attempt.id for attempt in attempts}
    if any(slot.attempt_id not in existing_attempts for slot in slots):
        return False
    reference_rows = (
        db.execute(
            select(ChatflowPromptReference)
            .where(
                ChatflowPromptReference.package_id == package.id,
                ChatflowPromptReference.task_id == task.id,
            )
            .order_by(ChatflowPromptReference.position, ChatflowPromptReference.ref_id)
        )
        .scalars()
        .all()
    )
    reference_manifest = [
        {
            "asset_id": str(row.asset_id),
            "position": row.position,
            "content_sha256": row.content_sha256,
            "ref_id": row.ref_id,
        }
        for row in reference_rows
    ]
    slot_mapping = [
        {
            "slot_id": row.slot_id,
            "position": row.position,
            "image_type": row.image_type,
            "task_item_id": str(row.task_item_id),
            "attempt_id": str(row.attempt_id),
        }
        for row in slots
    ]
    if (
        canonical_sha256(reference_manifest) != permit.reference_manifest_digest
        or canonical_sha256(slot_mapping) != permit.slot_mapping_digest
    ):
        return False
    db.add(
        ChatflowGenerationAcceptanceEvidence(
            generation_request_id=request.id,
            task_id=task.id,
            service_authority_id=acceptance.service_authority_id,
            outcome="rejected_expired",
            batch_id=None,
            permit_id=permit.permit_id,
            permit_version=permit.permit_version,
            permit_expires_at=permit.expires_at,
            service_request_digest=bytes.fromhex(acceptance.service_request_digest),
            evidence_digest=canonical_sha256(acceptance.model_dump(mode="json")),
            receipt_digest=bytes.fromhex(acceptance.receipt_digest),
            receipt_id=str(acceptance.receipt_id),
            evidence=acceptance.model_dump(mode="json"),
            closed_at=now,
        )
    )
    db.flush()
    tracking.status = "rejected_before_acceptance"
    tracking.reconcile_state = "complete"
    tracking.next_reconcile_at = None
    task.status = "failed"
    task.terminal_at = now
    task.expires_at = now + timedelta(days=task.retention_days)
    task.updated_at = now
    session.status = "failed"
    session.updated_at = now
    for item in items:
        item.status = "failed"
        item.image_id = None
        item.preview_ready = False
        item.width = None
        item.height = None
        item.error_code = _GENERATION_PERMIT_EXPIRED
        item.updated_at = now
    for attempt in attempts:
        attempt.status = "failed"
        attempt.failure_code = _GENERATION_PERMIT_EXPIRED
        attempt.completed_at = now
        attempt.next_action_at = None
        attempt.updated_at = now
    _cancel_request_actions(db, request_id, current_action_id=action.id, now=now)
    _cancel_closed_task_run_observations(
        db, business_session_id=session.id, task_id=task.id, now=now
    )
    return True


def _slot_states(db: Session, request_id: UUID) -> list[ChatflowGenerationSlotState]:
    return (
        db.execute(
            select(ChatflowGenerationSlotState)
            .where(ChatflowGenerationSlotState.generation_request_id == request_id)
            .order_by(ChatflowGenerationSlotState.slot_id)
            .with_for_update()
        )
        .scalars()
        .all()
    )


def _cancel_request_actions(
    db: Session, request_id: UUID, *, current_action_id: UUID, now: datetime
) -> None:
    actions = (
        db.execute(
            select(Action)
            .where(
                Action.scope_kind == "generation_request",
                Action.scope_id == request_id,
                Action.status == "scheduled",
                Action.id != current_action_id,
            )
            .order_by(Action.sequence_namespace, Action.sequence, Action.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    for action in actions:
        action.status = "cancelled"
        action.last_outcome = None
        action.last_error_code = _GENERATION_PERMIT_EXPIRED
        action.claim_token = None
        action.lease_until = None
        action.completed_at = now
        action.updated_at = now


def _cancel_closed_task_run_observations(
    db: Session, *, business_session_id: UUID, task_id: UUID, now: datetime
) -> None:
    """Retire only scheduled observers made obsolete by this exact Task closure."""
    from app.services.chatflow_actions import CHATFLOW_RUN_OBSERVATION_ACTION

    actions = (
        db.execute(
            select(Action)
            .where(
                Action.business_session_id == business_session_id,
                Action.task_id == task_id,
                Action.action_kind == CHATFLOW_RUN_OBSERVATION_ACTION,
                Action.scope_kind == "business_session",
                Action.scope_id == business_session_id,
                Action.status == "scheduled",
            )
            .order_by(Action.sequence_namespace, Action.sequence, Action.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    for action in actions:
        action.status = "cancelled"
        action.last_outcome = None
        action.last_error_code = _GENERATION_PERMIT_EXPIRED
        action.claim_token = None
        action.lease_until = None
        action.completed_at = now
        action.updated_at = now


def ensure_accepted_batch_followup_from_tracking(db: Session, request_id: UUID) -> None:
    _ensure_accepted_batch_followup(db, request_id)


def _ensure_accepted_batch_followup(db: Session, request_id: UUID) -> None:
    request = db.execute(
        select(ChatflowGenerationRequest).where(ChatflowGenerationRequest.id == request_id)
    ).scalar_one_or_none()
    if request is None:
        return
    tracking = db.execute(
        select(ChatflowGenerationTracking)
        .where(ChatflowGenerationTracking.generation_request_id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    evidence = db.execute(
        select(ChatflowGenerationAcceptanceEvidence)
        .where(ChatflowGenerationAcceptanceEvidence.generation_request_id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if (
        tracking is None
        or evidence is None
        or evidence.outcome != "accepted"
        or evidence.batch_id is None
        or evidence.service_authority_id != request.service_authority_id
        or tracking.reconcile_query_count != MAX_RECONCILE_QUERIES
        or tracking.reconcile_state != "unknown"
        or tracking.status not in {"awaiting_batch", "submission_unknown", "queued", "running"}
        or (tracking.batch_id is not None and tracking.batch_id != evidence.batch_id)
    ):
        return
    followup = db.execute(
        select(ChatflowGenerationAcceptedBatchFollowup)
        .where(ChatflowGenerationAcceptedBatchFollowup.generation_request_id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if followup is not None:
        if (
            followup.batch_id != evidence.batch_id
            or followup.acceptance_digest != evidence.evidence_digest
            or followup.service_authority_id != evidence.service_authority_id
        ):
            return
        return
    permit = db.execute(
        select(ChatflowGenerationPermit).where(
            ChatflowGenerationPermit.generation_request_id == request_id
        )
    ).scalar_one_or_none()
    if permit is None:
        return
    now = db.scalar(select(func.clock_timestamp()))
    followup = ChatflowGenerationAcceptedBatchFollowup(
        generation_request_id=request_id,
        task_id=request.task_id,
        batch_id=evidence.batch_id,
        service_authority_id=evidence.service_authority_id,
        acceptance_digest=evidence.evidence_digest,
        state="scheduled",
        query_count=0,
        next_action_at=now,
    )
    db.add(followup)
    db.flush()
    _schedule_accepted_batch_action(
        db,
        request=request,
        batch_id=evidence.batch_id,
        task_id=request.task_id,
        session_id=request.business_session_id,
        sequence=1,
        due_at=now,
    )


def _accepted_batch_action_id(request_id: UUID, batch_id: str, sequence: int) -> UUID:
    return uuid5(_ACCEPTED_BATCH_ID_NAMESPACE, f"{request_id}:{batch_id}:{sequence}")


def _accepted_batch_action_digest(
    *, request_id: UUID, task_id: UUID, session_id: UUID, batch_id: str, sequence: int
) -> bytes:
    return canonical_sha256(
        {
            "action_kind": ACCEPTED_BATCH_RECOVERY_ACTION,
            "generation_request_id": str(request_id),
            "task_id": str(task_id),
            "business_session_id": str(session_id),
            "batch_id": batch_id,
            "sequence_namespace": ACCEPTED_BATCH_SEQUENCE_NAMESPACE,
            "sequence": sequence,
        }
    )


def _schedule_accepted_batch_action(
    db: Session,
    *,
    request: ChatflowGenerationRequest,
    batch_id: str,
    task_id: UUID,
    session_id: UUID,
    sequence: int,
    due_at: datetime,
) -> Action:
    return schedule_action_in_transaction(
        db,
        action_id=_accepted_batch_action_id(request.id, batch_id, sequence),
        task_id=task_id,
        scope_kind="generation_request",
        scope_id=request.id,
        action_kind=ACCEPTED_BATCH_RECOVERY_ACTION,
        sequence=sequence,
        sequence_namespace=ACCEPTED_BATCH_SEQUENCE_NAMESPACE,
        due_at=due_at,
        input_digest_version=1,
        input_digest=_accepted_batch_action_digest(
            request_id=request.id,
            task_id=task_id,
            session_id=session_id,
            batch_id=batch_id,
            sequence=sequence,
        ),
        business_session_id=session_id,
        generation_request_id=request.id,
    )


def prepare_accepted_batch_recovery(db: Session, action: Action) -> ActionInput:
    if (
        action.action_kind != ACCEPTED_BATCH_RECOVERY_ACTION
        or action.sequence_namespace != ACCEPTED_BATCH_SEQUENCE_NAMESPACE
        or action.generation_request_id is None
        or action.task_id is None
        or action.business_session_id is None
    ):
        raise ValueError("accepted batch Action identity is invalid")
    request, permit, _package, session, tracking, slots = _load_h4_rows(db, action)
    followup = db.execute(
        select(ChatflowGenerationAcceptedBatchFollowup)
        .where(ChatflowGenerationAcceptedBatchFollowup.generation_request_id == request.id)
        .with_for_update()
    ).scalar_one_or_none()
    evidence = db.execute(
        select(ChatflowGenerationAcceptanceEvidence).where(
            ChatflowGenerationAcceptanceEvidence.generation_request_id == request.id
        )
    ).scalar_one_or_none()
    if (
        followup is None
        or evidence is None
        or followup.task_id != request.task_id
        or evidence.outcome != "accepted"
        or followup.batch_id != evidence.batch_id
        or followup.service_authority_id != evidence.service_authority_id
        or followup.acceptance_digest != evidence.evidence_digest
        or request.service_authority_id != evidence.service_authority_id
        or permit.service_authority_id != evidence.service_authority_id
        or action.input_digest
        != _accepted_batch_action_digest(
            request_id=request.id,
            task_id=request.task_id,
            session_id=session.id,
            batch_id=followup.batch_id,
            sequence=action.sequence,
        )
    ):
        raise ValueError("accepted batch evidence linkage is invalid")
    should_query = False
    if action.status == "scheduled":
        if (
            followup.state != "scheduled"
            or action.sequence != followup.query_count + 1
            or followup.query_count >= MAX_ACCEPTED_BATCH_QUERIES
            or tracking.reconcile_query_count != MAX_RECONCILE_QUERIES
            or tracking.reconcile_state not in {"unknown", "scheduled"}
            or (tracking.batch_id is not None and tracking.batch_id != followup.batch_id)
        ):
            raise ValueError("accepted batch continuation is not eligible")
        followup.query_count += 1
        followup.state = "active"
        followup.next_action_at = None
        followup.last_error_code = None
        followup.updated_at = func.clock_timestamp()
        should_query = True
    elif action.status == "claimed":
        if followup.query_count < action.sequence:
            raise ValueError("accepted batch GET reservation is missing")
    else:
        raise ValueError("accepted batch Action is not executable")
    return _action_input(
        action,
        {
            "generation_request_id": str(request.id),
            "task_id": str(request.task_id),
            "business_session_id": str(session.id),
            "dify_run_id": request.dify_run_id,
            "package_digest": request.package_digest.hex(),
            "slot_mapping_digest": permit.slot_mapping_digest.hex(),
            "reference_manifest_digest": permit.reference_manifest_digest.hex(),
            "image_generation_contract_version": image_generation_contract_for_flow_version(
                session.flow_version
            ),
            "batch_id": followup.batch_id,
            "accepted_batch_id": followup.batch_id,
            "slots": [
                {
                    "slot_id": row.slot_id,
                    "task_item_id": str(row.task_item_id),
                    "attempt_id": str(row.attempt_id),
                    "position": row.position,
                    "image_type": row.image_type,
                }
                for row in slots
            ],
            "query_number": action.sequence,
            "should_query": should_query,
        },
    )


def execute_accepted_batch_recovery(
    port: GenerationStatusPort, action_input: ActionInput
) -> ActionExecutionResult:
    payload = action_input.payload
    if not isinstance(payload, Mapping) or payload.get("should_query") is not True:
        return ActionExecutionResult("unknown", safe_reason_code="ACCEPTED_BATCH_READ_INTERRUPTED")
    from app.services.chatflow_generation_actions import execute_generation_recovery

    result = execute_generation_recovery(port, action_input)
    value = result.value if isinstance(result.value, Mapping) else {}
    snapshot = value.get("snapshot")
    if isinstance(snapshot, Mapping) and snapshot.get("batch_id") != payload.get(
        "accepted_batch_id"
    ):
        return ActionExecutionResult(
            "unknown",
            safe_reason_code="ACCEPTED_BATCH_IDENTITY_MISMATCH",
            value={"queried": True, "snapshot": None},
        )
    return result


def recover_interrupted_accepted_batch_recovery(
    _action_input: ActionInput,
) -> ActionExecutionResult:
    return ActionExecutionResult(
        "unknown", safe_reason_code="ACCEPTED_BATCH_READ_INTERRUPTED", value={"queried": False}
    )


def finalize_accepted_batch_recovery(
    db: Session, action: Action, action_input: ActionInput, result: ActionExecutionResult
) -> None:
    request_id = action.generation_request_id
    if request_id is None:
        raise ValueError("accepted batch request identity is missing")
    tracking = db.execute(
        select(ChatflowGenerationTracking)
        .where(ChatflowGenerationTracking.generation_request_id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    followup = db.execute(
        select(ChatflowGenerationAcceptedBatchFollowup)
        .where(ChatflowGenerationAcceptedBatchFollowup.generation_request_id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if tracking is None or followup is None:
        raise ValueError("accepted batch continuation state is missing")
    now = db.scalar(select(func.clock_timestamp()))
    followup.last_checked_at = now
    followup.last_error_code = result.safe_reason_code
    value = result.value if isinstance(result.value, Mapping) else {}
    snapshot = value.get("snapshot")
    if isinstance(snapshot, Mapping):
        if snapshot.get("batch_id") != followup.batch_id:
            followup.state = "unknown"
            followup.next_action_at = None
            followup.last_error_code = "ACCEPTED_BATCH_IDENTITY_MISMATCH"
            tracking.reconcile_state = "unknown"
            tracking.next_reconcile_at = None
            _maybe_resolve_accepted_batch_followup(db, request_id, now=now)
            return
        _apply_service_snapshot(db, tracking, action_input, snapshot, now=now)
        from app.services.chatflow_generation_results import ingest_reconciled_snapshot

        ingest_reconciled_snapshot(
            db,
            generation_request_id=request_id,
            task_id=action.task_id,
            session_id=action.business_session_id,
            snapshot=snapshot,
            now=now,
        )
    if result.outcome == "safe_retry" and followup.query_count < MAX_ACCEPTED_BATCH_QUERIES:
        due_at = now + timedelta(seconds=result.retry_after_seconds or 1)
        followup.state = "scheduled"
        followup.next_action_at = due_at
        tracking.reconcile_state = "scheduled"
        tracking.next_reconcile_at = due_at
        request, *_rest = _load_h4_rows(db, action)
        _schedule_accepted_batch_action(
            db,
            request=request,
            batch_id=followup.batch_id,
            task_id=action.task_id,
            session_id=action.business_session_id,
            sequence=action.sequence + 1,
            due_at=due_at,
        )
    elif (
        result.outcome == "accepted" and isinstance(snapshot, Mapping) and snapshot.get("terminal")
    ):
        tracking.reconcile_state = "complete"
        tracking.next_reconcile_at = None
        followup.state = "awaiting_delivery"
        followup.next_action_at = None
        _maybe_resolve_accepted_batch_followup(db, request_id, now=now)
    else:
        followup.state = "unknown"
        followup.next_action_at = None
        tracking.reconcile_state = "unknown"
        tracking.next_reconcile_at = None
        if tracking.batch_id is None:
            tracking.status = "submission_unknown"
    followup.updated_at = now
    tracking.updated_at = now


def _maybe_resolve_accepted_batch_followup(db: Session, request_id: UUID, *, now: datetime) -> None:
    followup = db.execute(
        select(ChatflowGenerationAcceptedBatchFollowup)
        .where(ChatflowGenerationAcceptedBatchFollowup.generation_request_id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    request = db.get(ChatflowGenerationRequest, request_id)
    task = db.get(Task, followup.task_id) if followup is not None else None
    if followup is None or request is None or task is None:
        return
    evidence = db.get(ChatflowGenerationAcceptanceEvidence, request_id)
    tracking = db.get(ChatflowGenerationTracking, request_id)
    items = (
        db.execute(
            select(TaskItem)
            .where(TaskItem.task_id == task.id)
            .order_by(TaskItem.position, TaskItem.id)
        )
        .scalars()
        .all()
    )
    deliveries = (
        db.execute(
            select(ChatflowGenerationDelivery)
            .where(ChatflowGenerationDelivery.generation_request_id == request_id)
            .order_by(ChatflowGenerationDelivery.slot_id)
        )
        .scalars()
        .all()
    )
    deliveries_by_item = {row.task_item_id: row for row in deliveries}
    if (
        evidence is None
        or evidence.outcome != "accepted"
        or evidence.batch_id != followup.batch_id
        or evidence.evidence_digest != followup.acceptance_digest
        or tracking is None
        or tracking.batch_id != followup.batch_id
        or tracking.reconcile_state != "complete"
    ):
        return
    if (
        not task.terminal_at
        or task.status not in {"succeeded", "partially_succeeded", "failed"}
        or not items
        or any(item.status not in {"succeeded", "failed"} for item in items)
        or any(
            (
                item.status == "succeeded"
                and (
                    deliveries_by_item.get(item.id) is None
                    or deliveries_by_item[item.id].status != "confirmed"
                )
            )
            or (
                item.status == "failed"
                and deliveries_by_item.get(item.id) is not None
                and deliveries_by_item[item.id].status not in {"confirmed", "protocol_error"}
            )
            for item in items
        )
    ):
        return
    followup.state = "complete"
    followup.resolved_at = followup.resolved_at or now
    followup.next_action_at = None
    followup.updated_at = now


def mark_accepted_batch_delivery_progress(db: Session, request_id: UUID, *, now: datetime) -> None:
    _maybe_resolve_accepted_batch_followup(db, request_id, now=now)
