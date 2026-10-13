from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import timedelta
from uuid import UUID, uuid5

from sqlalchemy import Engine, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.action_models import Action
from app.db.business_session_models import BusinessSession
from app.db.chatflow_generation_closure_models import ChatflowGenerationRunClosure
from app.db.chatflow_generation_models import (
    ChatflowGenerationDelivery,
    ChatflowGenerationRequest,
    ChatflowGenerationRequestSlot,
    ChatflowGenerationTracking,
)
from app.db.chatflow_v34_models import ChatflowV34SessionState
from app.db.generation_models import GenerationResult
from app.db.reference_models import ImageAsset
from app.db.task_models import Task, TaskItem
from app.integrations.dify.run_details import (
    RUN_STATUSES,
    TERMINAL_STATUSES,
    DifyRunDetailsPort,
    HttpDifyRunDetailsPort,
)
from app.workers.action_processor import (
    ActionExecutionResult,
    ActionInput,
    ActionRegistration,
    schedule_action_in_transaction,
)

CLOSE_ACTION = "chatflow.generation.close.observe.v1"
CLOSE_NAMESPACE = "generation_close.v1"
_NAMESPACE = UUID("43b619b5-88d4-421a-b755-b02c9199f452")
MAX_OBSERVATIONS = 32
_CLOSED = {"completed", "completed_with_execution_warning"}
_TASK_TERMINAL = {"succeeded", "partially_succeeded", "failed"}


def _digest(row: ChatflowGenerationRunClosure, sequence: int) -> bytes:
    return hashlib.sha256(
        f"{row.generation_request_id}:{row.task_id}:{row.business_session_id}:"
        f"{row.dify_app_id}:{row.dify_workflow_id}:{row.dify_run_id}:{sequence}".encode()
    ).digest()


def _delivery_evidence(
    db: Session, request: ChatflowGenerationRequest, task: Task
) -> tuple[str, int]:
    items = db.scalars(select(TaskItem).where(TaskItem.task_id == task.id)).all()
    slots = db.scalars(
        select(ChatflowGenerationRequestSlot).where(
            ChatflowGenerationRequestSlot.generation_request_id == request.id
        )
    ).all()
    deliveries = db.scalars(
        select(ChatflowGenerationDelivery).where(
            ChatflowGenerationDelivery.generation_request_id == request.id
        )
    ).all()
    by_item = {row.task_item_id: row for row in deliveries}
    slot_by_item = {row.task_item_id: row for row in slots}
    confirmed = sum(row.status == "confirmed" for row in deliveries)
    if len(items) != task.planned_count or len(slots) != len(items) or not items:
        return "unresolved", confirmed
    if {row.id for row in items} != set(slot_by_item):
        return "unresolved", confirmed
    if any(row.status not in {"succeeded", "failed"} for row in items):
        return "pending", confirmed
    succeeded = sum(row.status == "succeeded" for row in items)
    expected_task_status = (
        "succeeded"
        if succeeded == len(items)
        else "failed"
        if not succeeded
        else "partially_succeeded"
    )
    if task.status != expected_task_status or task.terminal_at is None:
        return "unresolved", confirmed
    for item in items:
        delivery = by_item.get(item.id)
        slot = slot_by_item[item.id]
        if slot.task_id != task.id:
            return "unresolved", confirmed
        if item.status == "failed":
            if delivery is not None and delivery.status not in {"confirmed", "protocol_error"}:
                return "unresolved", confirmed
            continue
        if delivery is None or delivery.status != "confirmed":
            return "pending", confirmed
        if (
            delivery.task_id != task.id
            or delivery.owner_user_id != task.owner_user_id
            or delivery.slot_id != slot.slot_id
            or delivery.attempt_id != slot.attempt_id
            or delivery.original_asset_id != item.image_id
            or not item.preview_ready
        ):
            return "unresolved", confirmed
        result = db.get(GenerationResult, slot.attempt_id)
        original = db.get(ImageAsset, delivery.original_asset_id)
        preview = db.get(ImageAsset, delivery.preview_asset_id)
        if (
            result is None
            or result.task_id != task.id
            or result.task_item_id != item.id
            or result.owner_user_id != task.owner_user_id
            or result.original_asset_id != delivery.original_asset_id
            or result.preview_asset_id != delivery.preview_asset_id
        ):
            return "unresolved", confirmed
        if (
            original is None
            or preview is None
            or original.owner_user_id != task.owner_user_id
            or preview.owner_user_id != task.owner_user_id
            or original.asset_kind != "generation_original"
            or preview.asset_kind != "generation_preview"
            or original.object_key != delivery.original_object_key
            or preview.object_key != delivery.preview_object_key
            or original.content_sha256 != delivery.original_sha256
            or preview.content_sha256 != delivery.preview_sha256
            or original.size_bytes != delivery.original_size_bytes
            or preview.size_bytes != delivery.preview_size_bytes
            or original.width != item.width
            or original.height != item.height
        ):
            return "unresolved", confirmed
    return ("confirmed" if succeeded else "not_applicable"), confirmed


def refresh_closure(db: Session, row: ChatflowGenerationRunClosure, now) -> None:
    request = db.get(ChatflowGenerationRequest, row.generation_request_id)
    task = db.get(Task, row.task_id)
    tracking = db.get(ChatflowGenerationTracking, row.generation_request_id)
    if request is None or task is None:
        raise ValueError("closure parents missing")
    row.task_status = task.status
    row.delivery_status, row.confirmed_count = _delivery_evidence(db, request, task)
    business_done = (
        task.status in _TASK_TERMINAL
        and task.terminal_at is not None
        and row.delivery_status in {"confirmed", "not_applicable"}
        and tracking is not None
        and tracking.reconcile_state == "complete"
    )
    if business_done and row.workflow_status in TERMINAL_STATUSES:
        row.state = (
            "completed_with_execution_warning"
            if row.workflow_status in {"failed", "stopped", "partial-succeeded"}
            else "completed"
        )
        row.closed_at = row.closed_at or now
        row.next_observation_at = None
        row.last_error_code = None
    row.updated_at = now


def ensure_generation_close_action(db: Session, request_id: UUID, now) -> Action | None:
    """Use the worker parent lock order suffix: session, task, request, closure/action."""
    hint = db.get(ChatflowGenerationRequest, request_id)
    if hint is None:
        return None
    session = db.scalar(
        select(BusinessSession)
        .where(BusinessSession.id == hint.business_session_id)
        .with_for_update()
    )
    task = db.scalar(select(Task).where(Task.id == hint.task_id).with_for_update())
    request = db.scalar(
        select(ChatflowGenerationRequest)
        .where(ChatflowGenerationRequest.id == request_id)
        .with_for_update()
    )
    state = db.scalar(
        select(ChatflowV34SessionState).where(
            ChatflowV34SessionState.business_session_id == hint.business_session_id
        )
    )
    if (
        session is None
        or task is None
        or state is None
        or request is None
        or session.flow_version != "visual-v3.4-single-selection"
        or session.expires_at <= now
        or task.business_session_id != session.id
        or task.provenance_kind != "dify_chatflow"
        or session.dify_run_id != request.dify_run_id
        or state.flow_version != session.flow_version
        or not state.dify_app_id
        or not state.dify_workflow_id
        or state.selection_status != "accepted"
    ):
        return None
    row = db.get(ChatflowGenerationRunClosure, request_id)
    if row is None:
        row = ChatflowGenerationRunClosure(
            generation_request_id=request_id,
            task_id=task.id,
            business_session_id=session.id,
            dify_run_id=request.dify_run_id,
            dify_app_id=state.dify_app_id,
            dify_workflow_id=state.dify_workflow_id,
            task_status=task.status,
            delivery_status="pending",
            confirmed_count=0,
            query_count=0,
            state="pending",
            next_observation_at=now,
        )
        db.add(row)
        db.flush()
    if (
        row.task_id != task.id
        or row.business_session_id != session.id
        or row.dify_run_id != request.dify_run_id
        or row.dify_app_id != state.dify_app_id
        or row.dify_workflow_id != state.dify_workflow_id
    ):
        raise ValueError("closure immutable identity mismatch")
    refresh_closure(db, row, now)
    if row.state in _CLOSED:
        return None
    if row.query_count >= MAX_OBSERVATIONS:
        row.state = "unknown"
        row.next_observation_at = None
        return None
    live = db.scalar(
        select(Action).where(
            Action.generation_request_id == request_id,
            Action.sequence_namespace == CLOSE_NAMESPACE,
            Action.status.in_({"scheduled", "claimed"}),
        )
    )
    if live is not None:
        return live
    if row.next_observation_at is None or row.next_observation_at > now:
        return None
    latest = (
        db.scalar(
            select(func.max(Action.sequence)).where(
                Action.generation_request_id == request_id,
                Action.sequence_namespace == CLOSE_NAMESPACE,
            )
        )
        or 0
    )
    sequence = latest + 1
    return schedule_action_in_transaction(
        db,
        action_id=uuid5(_NAMESPACE, f"{request_id}:{sequence}"),
        task_id=task.id,
        scope_kind="generation_request",
        scope_id=request_id,
        action_kind=CLOSE_ACTION,
        sequence=sequence,
        sequence_namespace=CLOSE_NAMESPACE,
        due_at=now,
        input_digest_version=1,
        input_digest=_digest(row, sequence),
        business_session_id=session.id,
        generation_request_id=request_id,
    )


def prepare_close_observation(db: Session, action: Action) -> ActionInput:
    row = db.get(ChatflowGenerationRunClosure, action.generation_request_id)
    if (
        row is None
        or action.action_kind != CLOSE_ACTION
        or action.scope_kind != "generation_request"
        or action.scope_id != row.generation_request_id
        or action.sequence_namespace != CLOSE_NAMESPACE
        or action.input_digest_version != 1
        or action.task_id != row.task_id
        or action.business_session_id != row.business_session_id
        or action.input_digest != _digest(row, action.sequence)
    ):
        raise ValueError("invalid closure observation binding")
    state = db.scalar(
        select(ChatflowV34SessionState).where(
            ChatflowV34SessionState.business_session_id == row.business_session_id
        )
    )
    session = db.get(BusinessSession, row.business_session_id)
    request = db.get(ChatflowGenerationRequest, row.generation_request_id)
    if (
        state is None
        or session is None
        or request is None
        or state.dify_app_id != row.dify_app_id
        or state.dify_workflow_id != row.dify_workflow_id
        or session.dify_run_id != row.dify_run_id
        or request.dify_run_id != row.dify_run_id
    ):
        raise ValueError("closure run identity changed")
    skip = (
        row.state in _CLOSED
        or row.workflow_status in TERMINAL_STATUSES
        or row.query_count >= MAX_OBSERVATIONS
        or session.expires_at <= db.scalar(select(func.clock_timestamp()))
    )
    if not skip:
        # Count the committed I/O intent before GET, including a lost worker lease.
        row.query_count += 1
    return ActionInput(
        action_id=action.id,
        task_id=action.task_id,
        scope_kind=action.scope_kind,
        scope_id=action.scope_id,
        action_kind=action.action_kind,
        sequence=action.sequence,
        sequence_namespace=action.sequence_namespace,
        input_digest_version=1,
        input_digest=action.input_digest,
        business_session_id=row.business_session_id,
        generation_request_id=row.generation_request_id,
        payload={
            "run_id": row.dify_run_id,
            "app_id": row.dify_app_id,
            "workflow_id": row.dify_workflow_id,
            "skip": skip,
        },
    )


def execute_close_observation(
    port: DifyRunDetailsPort, value: ActionInput
) -> ActionExecutionResult:
    payload = value.payload
    if not isinstance(payload, Mapping):
        raise ValueError("invalid closure observation input")
    if payload["skip"]:
        return ActionExecutionResult("accepted", value={"queried": False})
    try:
        detail = port.get_run(payload["run_id"], payload["app_id"], payload["workflow_id"])
        if (
            not isinstance(detail, dict)
            or detail.get("status") not in RUN_STATUSES
            or not isinstance(detail.get("digest"), bytes)
            or len(detail["digest"]) != 32
        ):
            raise ValueError("invalid observation result")
        return ActionExecutionResult("accepted", value={"queried": True, "detail": detail})
    except Exception:
        # Observation failure is not a business I/O failure: never downgrade stored images.
        return ActionExecutionResult(
            "accepted",
            safe_reason_code="DIFY_RUN_DETAIL_UNAVAILABLE",
            value={"queried": True},
        )


def finalize_close_observation(
    db: Session, action: Action, value: ActionInput, result: ActionExecutionResult
) -> None:
    row = db.get(ChatflowGenerationRunClosure, action.generation_request_id)
    if row is None or action.input_digest != _digest(row, action.sequence):
        raise ValueError("closure finalization binding changed")
    now = db.scalar(select(func.clock_timestamp()))
    payload = result.value if isinstance(result.value, Mapping) else {}
    detail = payload.get("detail")
    if isinstance(detail, Mapping):
        # A delayed running snapshot must not overwrite previously observed termination.
        if row.workflow_status not in TERMINAL_STATUSES:
            row.workflow_status = detail["status"]
            row.workflow_error_code = detail["error_code"]
            row.observation_digest = detail["digest"]
            row.observed_at = now
    row.last_error_code = result.safe_reason_code
    refresh_closure(db, row, now)
    if row.state not in _CLOSED:
        aged = now >= row.created_at + timedelta(minutes=30)
        row.state = (
            "unknown"
            if aged or result.safe_reason_code or row.query_count >= MAX_OBSERVATIONS
            else "pending"
        )
        row.next_observation_at = (
            None
            if row.query_count >= MAX_OBSERVATIONS
            else now
            + timedelta(
                seconds=900 if aged else min(300, 30 * 2 ** min(max(row.query_count - 1, 0), 4))
            )
        )


def build_close_observation_registration(port: DifyRunDetailsPort) -> ActionRegistration:
    def execute(value: ActionInput) -> ActionExecutionResult:
        return execute_close_observation(port, value)

    return ActionRegistration(
        kind=CLOSE_ACTION,
        prepare=prepare_close_observation,
        execute=execute,
        recover=execute,
        finalize=finalize_close_observation,
        scope_kinds=frozenset({"generation_request"}),
        complete_unknown_step=True,
    )


def configured_close_registrations(settings) -> dict[str, ActionRegistration]:
    """One registration gate shared by publishers and consumers."""
    if (
        getattr(settings, "chatflow_handoff_enabled", False)
        and settings.chatflow_dify_mode == "http"
        and settings.chatflow_flow_version == "visual-v3.4-single-selection"
        and settings.dify_reference_upload_api_base_url is not None
        and settings.dify_reference_upload_api_token is not None
        and settings.dify_app_id is not None
    ):
        return {
            CLOSE_ACTION: build_close_observation_registration(
                HttpDifyRunDetailsPort(
                    str(settings.dify_reference_upload_api_base_url),
                    settings.dify_app_id,
                    settings.dify_reference_upload_api_token.get_secret_value(),
                )
            )
        }
    return {}


def project_generation_completion(db: Session, task_id: UUID | None) -> dict | None:
    if task_id is None:
        return None
    row = db.scalar(
        select(ChatflowGenerationRunClosure).where(ChatflowGenerationRunClosure.task_id == task_id)
    )
    if row is None:
        return None
    task = db.get(Task, task_id)
    if task is None:
        return None
    # A later retry belongs to the live Task, not to the initial run's closed snapshot.
    current_state = row.state
    delivery_status = row.delivery_status
    closed_at = row.closed_at
    if row.state in _CLOSED and (task.status != row.task_status or task.terminal_at is None):
        current_state, delivery_status, closed_at = "pending", "pending", None
    return {
        "task_status": task.status,
        "delivery_status": delivery_status,
        "dify_run_status": row.workflow_status,
        "reconciliation_status": current_state,
        "reason_code": row.workflow_error_code or row.last_error_code,
        "confirmed_count": row.confirmed_count,
        "closed_at": closed_at,
    }


class GenerationClosureScheduler:
    def __init__(self, engine: Engine, *, app_id: str | None = None):
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self._app_id = app_id

    def scan_once(self, *, limit: int = 25) -> int:
        scheduled = 0
        with self._sessions() as db, db.begin():
            now = db.scalar(select(func.clock_timestamp()))
            query = (
                select(ChatflowGenerationRequest.id)
                .join(
                    BusinessSession,
                    BusinessSession.id == ChatflowGenerationRequest.business_session_id,
                )
                .outerjoin(
                    ChatflowGenerationRunClosure,
                    ChatflowGenerationRunClosure.generation_request_id
                    == ChatflowGenerationRequest.id,
                )
                .where(
                    BusinessSession.flow_version == "visual-v3.4-single-selection",
                    BusinessSession.expires_at > now,
                    or_(
                        ChatflowGenerationRunClosure.generation_request_id.is_(None),
                        ChatflowGenerationRunClosure.next_observation_at <= now,
                    ),
                )
                .order_by(
                    func.coalesce(
                        ChatflowGenerationRunClosure.next_observation_at,
                        ChatflowGenerationRequest.created_at,
                    )
                )
                .limit(limit)
            )
            if self._app_id is not None:
                query = query.where(
                    select(ChatflowV34SessionState.id)
                    .where(
                        ChatflowV34SessionState.business_session_id
                        == ChatflowGenerationRequest.business_session_id,
                        ChatflowV34SessionState.dify_app_id == self._app_id,
                    )
                    .exists()
                )
            ids = db.scalars(query).all()
            for request_id in ids:
                action = ensure_generation_close_action(db, request_id, now)
                scheduled += action is not None
        return scheduled
