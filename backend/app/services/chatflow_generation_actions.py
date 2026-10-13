from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Protocol
from uuid import UUID, uuid5

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.action_models import Action
from app.db.business_session_models import BusinessSession
from app.db.chatflow_generation_models import (
    ChatflowGenerationPermit,
    ChatflowGenerationRequest,
    ChatflowGenerationRequestSlot,
    ChatflowGenerationSlotState,
    ChatflowGenerationTracking,
)
from app.schemas.chatflow_generation import (
    ChatflowGenerationSlotEvent,
    aggregate_generation_status,
    generation_snapshot_digest,
    service_generation_status,
)
from app.schemas.prompt_workflow import canonical_sha256
from app.services.chatflow_generation_contracts import (
    IMAGE_GENERATION_CONTRACT_V2,
    IMAGE_GENERATION_CONTRACT_V3,
    image_generation_contract_for_flow_version,
)
from app.workers.action_processor import (
    ActionExecutionResult,
    ActionInput,
    ActionRegistration,
    schedule_action_in_transaction,
)

GENERATION_RECOVERY_ACTION = "chatflow.generation.recover.v1"
GENERATION_RECOVERY_SEQUENCE_NAMESPACE = "default"
GENERATION_RECOVERY_NAMESPACE = UUID("f068dad7-64ca-43db-a8a6-ea76a667c073")
MAX_RECONCILE_QUERIES = 5
RECONCILE_BACKOFF_SECONDS = (10, 20, 40, 80, 120)


class GenerationStatusPort(Protocol):
    def get_by_request(
        self, generation_request_id: UUID, *, contract_version: str
    ) -> Mapping[str, object]: ...

    def get_by_batch(self, batch_id: str, *, contract_version: str) -> Mapping[str, object]: ...


class ImageGenerationReadError(RuntimeError):
    def __init__(self, code: str, *, not_found: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.not_found = not_found


class HttpGenerationStatusPort:
    """GET-only adapter for the existing image-generation service API."""

    def __init__(self, base_url: str, bearer_token: str, *, timeout_seconds: float = 5.0) -> None:
        if not base_url.startswith(("http://", "https://")) or not bearer_token:
            raise ValueError("image-generation GET adapter configuration is invalid")
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            headers={"Authorization": f"Bearer {bearer_token}"},
        )

    def get_by_request(
        self,
        generation_request_id: UUID,
        *,
        contract_version: str = IMAGE_GENERATION_CONTRACT_V2,
    ) -> Mapping[str, object]:
        version = _api_version(contract_version)
        return self._get(f"/{version}/generation-requests/{generation_request_id}")

    def get_by_batch(
        self,
        batch_id: str,
        *,
        contract_version: str = IMAGE_GENERATION_CONTRACT_V2,
    ) -> Mapping[str, object]:
        version = _api_version(contract_version)
        allowed_chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
        if not batch_id or any(char not in allowed_chars for char in batch_id):
            raise ImageGenerationReadError("IMAGE_SERVICE_BATCH_ID_INVALID")
        return self._get(f"/{version}/generation-batches/{batch_id}")

    def _get(self, path: str) -> Mapping[str, object]:
        try:
            response = self._client.get(self._base_url + path)
        except httpx.HTTPError:
            raise ImageGenerationReadError("IMAGE_SERVICE_READ_UNAVAILABLE") from None
        if response.status_code == 404:
            raise ImageGenerationReadError("IMAGE_SERVICE_REQUEST_NOT_FOUND", not_found=True)
        if response.status_code >= 500 or response.status_code in {408, 429}:
            raise ImageGenerationReadError("IMAGE_SERVICE_READ_UNAVAILABLE")
        if not response.is_success:
            raise ImageGenerationReadError("IMAGE_SERVICE_READ_REJECTED")
        try:
            body = response.json()
        except ValueError:
            raise ImageGenerationReadError("IMAGE_SERVICE_RESPONSE_INVALID") from None
        if not isinstance(body, Mapping):
            raise ImageGenerationReadError("IMAGE_SERVICE_RESPONSE_INVALID")
        return body


class UnavailableGenerationStatusPort:
    def get_by_request(
        self,
        generation_request_id: UUID,
        *,
        contract_version: str = IMAGE_GENERATION_CONTRACT_V2,
    ) -> Mapping[str, object]:
        del generation_request_id
        _api_version(contract_version)
        raise ImageGenerationReadError("IMAGE_SERVICE_NOT_CONFIGURED")

    def get_by_batch(
        self,
        batch_id: str,
        *,
        contract_version: str = IMAGE_GENERATION_CONTRACT_V2,
    ) -> Mapping[str, object]:
        del batch_id
        _api_version(contract_version)
        raise ImageGenerationReadError("IMAGE_SERVICE_NOT_CONFIGURED")


def _api_version(contract_version: str) -> str:
    if contract_version == IMAGE_GENERATION_CONTRACT_V2:
        return "v2"
    if contract_version == IMAGE_GENERATION_CONTRACT_V3:
        return "v3"
    raise ImageGenerationReadError("IMAGE_SERVICE_CONTRACT_UNSUPPORTED")


def generation_action_id(generation_request_id: UUID, sequence: int) -> UUID:
    return uuid5(GENERATION_RECOVERY_NAMESPACE, f"{generation_request_id}:{sequence}")


def generation_action_digest(
    *, generation_request_id: UUID, task_id: UUID, session_id: UUID, sequence: int
) -> bytes:
    value = {
        "action_kind": GENERATION_RECOVERY_ACTION,
        "generation_request_id": str(generation_request_id),
        "task_id": str(task_id),
        "business_session_id": str(session_id),
        "sequence": sequence,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).digest()


def ensure_generation_recovery_action(
    db: Session,
    *,
    request: ChatflowGenerationRequest,
    task_id: UUID,
    session_id: UUID,
    tracking: ChatflowGenerationTracking,
    due_at,
) -> Action | None:
    """Schedule one bounded read-only query for the stable H4 request."""
    if (
        tracking.reconcile_state in {"complete", "unknown"}
        or tracking.reconcile_query_count >= MAX_RECONCILE_QUERIES
    ):
        return None
    existing = (
        db.execute(
            select(Action)
            .where(
                Action.scope_kind == "generation_request",
                Action.generation_request_id == request.id,
                Action.sequence_namespace == GENERATION_RECOVERY_SEQUENCE_NAMESPACE,
                Action.status.in_(("scheduled", "claimed")),
            )
            .order_by(Action.sequence.desc())
            .with_for_update()
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing
    latest_sequence = db.execute(
        select(func.max(Action.sequence)).where(
            Action.scope_kind == "generation_request",
            Action.scope_id == request.id,
            Action.sequence_namespace == GENERATION_RECOVERY_SEQUENCE_NAMESPACE,
        )
    ).scalar_one()
    sequence = (latest_sequence or 0) + 1
    action = schedule_action_in_transaction(
        db,
        action_id=generation_action_id(request.id, sequence),
        task_id=task_id,
        scope_kind="generation_request",
        scope_id=request.id,
        action_kind=GENERATION_RECOVERY_ACTION,
        sequence=sequence,
        due_at=due_at,
        input_digest_version=1,
        input_digest=generation_action_digest(
            generation_request_id=request.id,
            task_id=task_id,
            session_id=session_id,
            sequence=sequence,
        ),
        business_session_id=session_id,
        generation_request_id=request.id,
    )
    tracking.reconcile_state = "scheduled"
    tracking.next_reconcile_at = due_at
    return action


def build_generation_recovery_registration(
    port: GenerationStatusPort,
) -> ActionRegistration:
    return ActionRegistration(
        kind=GENERATION_RECOVERY_ACTION,
        prepare=prepare_generation_recovery,
        execute=lambda action_input: execute_generation_recovery(port, action_input),
        recover=lambda action_input: execute_generation_recovery(port, action_input),
        finalize=finalize_generation_recovery,
        scope_kinds=frozenset({"generation_request"}),
    )


def prepare_generation_recovery(db: Session, action: Action) -> ActionInput:
    if (
        action.action_kind != GENERATION_RECOVERY_ACTION
        or action.sequence_namespace != GENERATION_RECOVERY_SEQUENCE_NAMESPACE
        or action.scope_kind != "generation_request"
        or action.generation_request_id is None
        or action.scope_id != action.generation_request_id
        or action.task_id is None
        or action.business_session_id is None
        or action.sequence < 1
    ):
        raise ValueError("generation recovery Action identity is invalid")
    request = db.execute(
        select(ChatflowGenerationRequest).where(
            ChatflowGenerationRequest.id == action.generation_request_id,
            ChatflowGenerationRequest.task_id == action.task_id,
            ChatflowGenerationRequest.business_session_id == action.business_session_id,
        )
    ).scalar_one_or_none()
    business_session = db.get(BusinessSession, action.business_session_id)
    tracking = db.execute(
        select(ChatflowGenerationTracking).where(
            ChatflowGenerationTracking.generation_request_id == action.generation_request_id
        )
    ).scalar_one_or_none()
    permit = db.execute(
        select(ChatflowGenerationPermit).where(
            ChatflowGenerationPermit.generation_request_id == action.generation_request_id
        )
    ).scalar_one_or_none()
    slots = (
        db.execute(
            select(ChatflowGenerationRequestSlot)
            .where(
                ChatflowGenerationRequestSlot.generation_request_id == action.generation_request_id
            )
            .order_by(ChatflowGenerationRequestSlot.position, ChatflowGenerationRequestSlot.slot_id)
        )
        .scalars()
        .all()
    )
    if (
        request is None
        or business_session is None
        or tracking is None
        or permit is None
        or not slots
    ):
        raise ValueError("generation recovery parents are incomplete")
    service_contract_version = image_generation_contract_for_flow_version(
        business_session.flow_version
    )
    if (
        tracking.reconcile_state in {"complete", "unknown"}
        or tracking.reconcile_query_count >= MAX_RECONCILE_QUERIES
    ):
        should_query = False
    else:
        should_query = True
    if action.input_digest != generation_action_digest(
        generation_request_id=request.id,
        task_id=request.task_id,
        session_id=request.business_session_id,
        sequence=action.sequence,
    ):
        raise ValueError("generation recovery Action digest is invalid")
    return ActionInput(
        action_id=action.id,
        task_id=action.task_id,
        scope_kind=action.scope_kind,
        scope_id=action.scope_id,
        action_kind=action.action_kind,
        sequence=action.sequence,
        input_digest_version=action.input_digest_version,
        input_digest=action.input_digest,
        payload={
            "generation_request_id": str(request.id),
            "task_id": str(request.task_id),
            "business_session_id": str(request.business_session_id),
            "dify_run_id": request.dify_run_id,
            "package_digest": request.package_digest.hex(),
            "slot_mapping_digest": permit.slot_mapping_digest.hex(),
            "reference_manifest_digest": permit.reference_manifest_digest.hex(),
            "image_generation_contract_version": service_contract_version,
            "batch_id": tracking.batch_id,
            "slots": [
                {
                    "slot_id": slot.slot_id,
                    "task_item_id": str(slot.task_item_id),
                    "attempt_id": str(slot.attempt_id),
                    "position": slot.position,
                    "image_type": slot.image_type,
                }
                for slot in slots
            ],
            "query_number": tracking.reconcile_query_count + 1,
            "should_query": should_query,
        },
        business_session_id=request.business_session_id,
        generation_request_id=request.id,
    )


def execute_generation_recovery(
    port: GenerationStatusPort, action_input: ActionInput
) -> ActionExecutionResult:
    payload = action_input.payload
    if not isinstance(payload, Mapping) or not payload.get("should_query"):
        return ActionExecutionResult("accepted", value={"queried": False})
    try:
        batch_id = payload.get("batch_id")
        contract_version = str(
            payload.get("image_generation_contract_version", IMAGE_GENERATION_CONTRACT_V2)
        )
        snapshot = (
            port.get_by_batch(str(batch_id), contract_version=contract_version)
            if isinstance(batch_id, str) and batch_id
            else port.get_by_request(
                UUID(str(payload["generation_request_id"])), contract_version=contract_version
            )
        )
        normalized = _validate_service_snapshot(snapshot, payload)
    except ImageGenerationReadError as exc:
        number = int(payload.get("query_number", 1))
        if number < MAX_RECONCILE_QUERIES:
            delay = RECONCILE_BACKOFF_SECONDS[min(number - 1, len(RECONCILE_BACKOFF_SECONDS) - 1)]
            return ActionExecutionResult(
                "safe_retry",
                safe_reason_code=exc.code,
                value={"queried": True, "snapshot": None},
                retry_after_seconds=delay,
            )
        return ActionExecutionResult(
            "unknown",
            safe_reason_code=exc.code,
            value={"queried": True, "snapshot": None},
        )
    except (TypeError, ValueError, KeyError):
        return ActionExecutionResult(
            "unknown",
            safe_reason_code="IMAGE_SERVICE_SNAPSHOT_INVALID",
            value={"queried": True, "snapshot": None},
        )

    number = int(payload.get("query_number", 1))
    if not normalized["terminal"] and number < MAX_RECONCILE_QUERIES:
        delay = RECONCILE_BACKOFF_SECONDS[min(number - 1, len(RECONCILE_BACKOFF_SECONDS) - 1)]
        return ActionExecutionResult(
            "safe_retry",
            safe_reason_code="GENERATION_STILL_RUNNING",
            value={"queried": True, "snapshot": normalized},
            retry_after_seconds=delay,
        )
    if not normalized["terminal"]:
        return ActionExecutionResult(
            "unknown",
            safe_reason_code="GENERATION_RECONCILE_LIMIT_REACHED",
            value={"queried": True, "snapshot": normalized},
        )
    return ActionExecutionResult("accepted", value={"queried": True, "snapshot": normalized})


def finalize_generation_recovery(
    db: Session, action: Action, action_input: ActionInput, result: ActionExecutionResult
) -> None:
    request_id = action.generation_request_id
    if request_id is None or action.task_id is None or action.business_session_id is None:
        raise ValueError("generation recovery Action linkage is missing")
    tracking = db.execute(
        select(ChatflowGenerationTracking)
        .where(ChatflowGenerationTracking.generation_request_id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if tracking is None:
        raise ValueError("generation tracking row is missing")
    value = result.value if isinstance(result.value, Mapping) else {}
    snapshot = value.get("snapshot")
    queried = bool(value.get("queried"))
    now = db.scalar(select(func.clock_timestamp()))
    if queried:
        tracking.reconcile_query_count += 1
        tracking.last_reconcile_at = now
    if isinstance(snapshot, Mapping):
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
    tracking.last_error_code = result.safe_reason_code
    if result.outcome == "safe_retry":
        due_at = now + timedelta(seconds=result.retry_after_seconds or 1)
        tracking.reconcile_state = "scheduled"
        tracking.next_reconcile_at = due_at
        db.execute(
            select(ChatflowGenerationRequest).where(ChatflowGenerationRequest.id == request_id)
        ).scalar_one()
        next_sequence = action.sequence + 1
        schedule_action_in_transaction(
            db,
            action_id=generation_action_id(request_id, next_sequence),
            task_id=action.task_id,
            scope_kind="generation_request",
            scope_id=request_id,
            action_kind=GENERATION_RECOVERY_ACTION,
            sequence=next_sequence,
            due_at=due_at,
            input_digest_version=1,
            input_digest=generation_action_digest(
                generation_request_id=request_id,
                task_id=action.task_id,
                session_id=action.business_session_id,
                sequence=next_sequence,
            ),
            business_session_id=action.business_session_id,
            generation_request_id=request_id,
        )
    elif result.outcome == "unknown":
        tracking.reconcile_state = "unknown"
        tracking.next_reconcile_at = None
        if tracking.batch_id is None:
            tracking.status = "submission_unknown"
    else:
        tracking.reconcile_state = "complete"
        tracking.next_reconcile_at = None
    tracking.updated_at = now
    if (
        tracking.reconcile_query_count == MAX_RECONCILE_QUERIES
        and tracking.reconcile_state == "unknown"
    ):
        from app.services.chatflow_generation_expiry import (
            ensure_accepted_batch_followup_from_tracking,
        )

        ensure_accepted_batch_followup_from_tracking(db, request_id)


def _validate_service_snapshot(
    snapshot: Mapping[str, object], expected: Mapping[str, object]
) -> dict[str, object]:
    for key in (
        "generation_request_id",
        "task_id",
        "dify_run_id",
        "package_digest",
        "slot_mapping_digest",
        "reference_manifest_digest",
    ):
        if snapshot.get(key) != expected.get(key):
            raise ValueError("image-generation request identity mismatch")
    expected_contract = expected.get(
        "image_generation_contract_version", IMAGE_GENERATION_CONTRACT_V2
    )
    if snapshot.get("contract_version") != expected_contract or snapshot.get("permit_version") != 2:
        raise ValueError("image-generation contract version mismatch")
    batch_id = snapshot.get("batch_id")
    expected_batch_id = expected.get("accepted_batch_id", expected.get("batch_id"))
    if expected_batch_id is not None and batch_id != expected_batch_id:
        raise ValueError("image-generation batch identity mismatch")
    source_sequence = snapshot.get("source_sequence")
    raw_slots = snapshot.get("slots")
    expected_slots = expected.get("slots")
    if (
        not isinstance(batch_id, str)
        or not batch_id
        or not isinstance(source_sequence, int)
        or isinstance(source_sequence, bool)
        or source_sequence < 1
        or not isinstance(raw_slots, Sequence)
        or isinstance(raw_slots, (str, bytes))
        or not isinstance(expected_slots, Sequence)
        or len(raw_slots) != len(expected_slots)
    ):
        raise ValueError("image-generation snapshot shape is invalid")
    slots: list[dict[str, object]] = []
    artifact_refs_by_slot: dict[str, object] = {}
    typed_slots: list[ChatflowGenerationSlotEvent] = []
    terminal_statuses = {"completed", "failed", "cancelled", "protocol_error"}
    for raw, expected_slot in zip(raw_slots, expected_slots, strict=True):
        if not isinstance(raw, Mapping) or not isinstance(expected_slot, Mapping):
            raise ValueError("image-generation slot shape is invalid")
        for key in ("slot_id", "task_item_id", "attempt_id", "position", "image_type"):
            if raw.get(key) != expected_slot.get(key):
                raise ValueError("image-generation slot mapping mismatch")
        slot = ChatflowGenerationSlotEvent.model_validate(
            {
                "slot_id": expected_slot["slot_id"],
                "task_item_id": UUID(str(expected_slot["task_item_id"])),
                "attempt_id": UUID(str(expected_slot["attempt_id"])),
                "position": expected_slot["position"],
                "image_type": expected_slot["image_type"],
                "status": raw.get("status"),
                "status_version": raw.get("status_version"),
                "error_code": raw.get("error_code"),
                "known_retry_count": (
                    None
                    if raw.get("status") in {"submitting", "submission_unknown"}
                    else raw.get("known_retry_count")
                ),
            },
            strict=True,
        )
        typed_slots.append(slot)
        normalized = slot.model_dump(mode="json")
        artifact_ref = raw.get("artifact_ref", [])
        artifact_refs_by_slot[slot.slot_id] = (
            artifact_ref
            if isinstance(artifact_ref, Sequence) and not isinstance(artifact_ref, (str, bytes))
            else []
        )
        slots.append(normalized)
    status = snapshot.get("status")
    if status != service_generation_status(typed_slots):
        raise ValueError("image-generation aggregate status does not match slot statuses")
    return {
        "generation_request_id": expected["generation_request_id"],
        "task_id": expected["task_id"],
        "dify_run_id": expected["dify_run_id"],
        "batch_id": batch_id,
        "package_digest": expected["package_digest"],
        "slot_mapping_digest": expected["slot_mapping_digest"],
        "reference_manifest_digest": expected["reference_manifest_digest"],
        "source_sequence": source_sequence,
        "status": status,
        "slots": slots,
        "artifact_refs_by_slot": artifact_refs_by_slot,
        "terminal": all(slot["status"] in terminal_statuses for slot in slots),
    }


def _apply_service_snapshot(
    db: Session,
    tracking: ChatflowGenerationTracking,
    action_input: ActionInput,
    snapshot: Mapping[str, object],
    *,
    now,
) -> None:
    payload = action_input.payload
    if not isinstance(payload, Mapping):
        raise ValueError("generation Action input is invalid")
    request_id = UUID(str(payload["generation_request_id"]))
    batch_id = str(snapshot["batch_id"])
    if tracking.batch_id is not None and tracking.batch_id != batch_id:
        raise ValueError("original generation request resolved to a different batch")
    current_sequence = tracking.source_sequence or 0
    incoming_sequence = int(snapshot["source_sequence"])
    incoming_slots = [
        ChatflowGenerationSlotEvent.model_validate(slot, strict=False) for slot in snapshot["slots"]
    ]
    incoming_digest = generation_snapshot_digest(
        batch_id=batch_id,
        source_sequence=incoming_sequence,
        slots=incoming_slots,
    )
    if incoming_sequence < current_sequence:
        return
    if incoming_sequence == current_sequence:
        if tracking.status_digest is None or tracking.status_digest != incoming_digest:
            raise ValueError("same generation source sequence contains different snapshot data")
        return
    current_rows = (
        db.execute(
            select(ChatflowGenerationSlotState)
            .where(ChatflowGenerationSlotState.generation_request_id == request_id)
            .order_by(ChatflowGenerationSlotState.slot_id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    states = {row.slot_id: row for row in current_rows}
    for slot in incoming_slots:
        previous = states.get(slot.slot_id)
        digest = canonical_sha256(slot.model_dump(mode="json"))
        if previous is not None:
            if slot.status_version < previous.status_version:
                continue
            if slot.status_version == previous.status_version:
                if previous.status_digest != digest:
                    raise ValueError("same slot status version contains different data")
                continue
            if previous.status in {"completed", "failed", "cancelled", "protocol_error"}:
                continue
        retry_count = (
            None if slot.status in {"submitting", "submission_unknown"} else slot.known_retry_count
        )
        if previous is None:
            db.add(
                ChatflowGenerationSlotState(
                    generation_request_id=request_id,
                    slot_id=slot.slot_id,
                    status=slot.status,
                    status_version=slot.status_version,
                    status_digest=digest,
                    error_code=slot.error_code,
                    known_retry_count=retry_count,
                )
            )
        else:
            previous.status = slot.status
            previous.status_version = slot.status_version
            previous.status_digest = digest
            previous.error_code = slot.error_code
            previous.known_retry_count = retry_count
    tracking.batch_id = batch_id
    tracking.source_sequence = incoming_sequence
    db.flush()
    expected_by_id = {str(slot["slot_id"]): slot for slot in payload["slots"]}
    effective_rows = (
        db.execute(
            select(ChatflowGenerationSlotState)
            .where(ChatflowGenerationSlotState.generation_request_id == request_id)
            .order_by(ChatflowGenerationSlotState.slot_id)
        )
        .scalars()
        .all()
    )
    effective_slots = [
        ChatflowGenerationSlotEvent.model_validate(
            {
                **{
                    "slot_id": expected_by_id[row.slot_id]["slot_id"],
                    "task_item_id": UUID(str(expected_by_id[row.slot_id]["task_item_id"])),
                    "attempt_id": UUID(str(expected_by_id[row.slot_id]["attempt_id"])),
                    "position": expected_by_id[row.slot_id]["position"],
                    "image_type": expected_by_id[row.slot_id]["image_type"],
                },
                "status": row.status,
                "status_version": row.status_version,
                "error_code": row.error_code,
                "known_retry_count": row.known_retry_count,
            },
            strict=True,
        )
        for row in effective_rows
    ]
    tracking.status_digest = incoming_digest
    tracking.status = aggregate_generation_status(effective_slots)
    tracking.last_reconcile_at = now
    tracking.updated_at = now
