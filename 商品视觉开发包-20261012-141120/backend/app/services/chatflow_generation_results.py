"""Durable receipt and private-save intents for H7 Chatflow result artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.chatflow_generation_models import (
    ChatflowGenerationDelivery,
    ChatflowGenerationRequest,
    ChatflowGenerationRequestSlot,
    ChatflowGenerationSlotState,
    ChatflowGenerationTracking,
)
from app.db.chatflow_generation_models import (
    ChatflowGenerationResultManifest as ResultManifestRow,
)
from app.db.task_models import Task, TaskItem, TaskItemAttempt
from app.schemas.chatflow_generation import (
    ChatflowGenerationEvent,
    ChatflowGenerationSlotEvent,
    generation_snapshot_digest,
)
from app.schemas.chatflow_generation_results import (
    ChatflowGenerationManifestAccepted,
    ChatflowGenerationManifestSlot,
    ChatflowGenerationResultManifest,
    ChatflowGenerationSourceArtifact,
)
from app.services.chatflow_generation import _lock_generation_event_parents
from app.workers.action_processor import schedule_action_in_transaction

_DELIVERY_NAMESPACE = UUID("25a02b20-5e4d-42b7-9273-1478849d35c7")
DELIVERY_DOWNLOAD_ACTION = "chatflow.generation.delivery.download.v1"
DELIVERY_CONFIRM_ACTION = "chatflow.generation.delivery.confirm.v1"


class ChatflowGenerationResultService:
    """Validate an authenticated Dify result and commit receipt plus download intents."""

    def __init__(self, engine: Engine, *, generation_event_service=None) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        if generation_event_service is None:
            from app.services.chatflow_generation import ChatflowGenerationEventService

            generation_event_service = ChatflowGenerationEventService(engine)
        self._generation_event_service = generation_event_service

    def accept(
        self, manifest: ChatflowGenerationResultManifest
    ) -> ChatflowGenerationManifestAccepted:
        # The H6 identity/status receipt is committed first. If the H7 transaction
        # fails, Dify can replay the same event and complete H7 without another run.
        self._generation_event_service.accept(manifest.status_event)
        event = manifest.status_event
        artifacts = {
            slot.slot_id: slot.original_artifact.model_dump(mode="json")
            if slot.original_artifact is not None
            else None
            for slot in manifest.slots
        }
        return self._accept_snapshot(
            event=event,
            manifest_slots=list(manifest.slots),
            artifact_by_slot=artifacts,
        )

    def _accept_snapshot(
        self,
        *,
        event: ChatflowGenerationEvent,
        manifest_slots: list[ChatflowGenerationManifestSlot],
        artifact_by_slot: Mapping[str, dict[str, object] | None],
    ) -> ChatflowGenerationManifestAccepted:
        status_digest = generation_snapshot_digest(
            batch_id=event.batch_id,
            source_sequence=event.source_sequence,
            slots=event.slots,
        )
        status_by_id = {slot.slot_id: slot.status for slot in event.slots}
        manifest_digest = _manifest_digest(
            generation_request_id=event.generation_request_id,
            task_id=event.task_id,
            session_id=event.business_session_id,
            batch_id=event.batch_id,
            source_sequence=event.source_sequence,
            status_digest=status_digest,
            slots=[
                {
                    "slot_id": slot.slot_id,
                    "status": status_by_id[slot.slot_id],
                    "status_version": slot.status_version,
                    "artifact": artifact_by_slot.get(slot.slot_id),
                }
                for slot in manifest_slots
            ],
        )
        try:
            with self._sessions() as db, db.begin():
                request, task, tracking, request_slots, _permit, _references, states = (
                    _lock_generation_event_parents(db, event)
                )
                return _record_manifest_and_intents(
                    db,
                    request=request,
                    task=task,
                    tracking=tracking,
                    request_slots=request_slots,
                    slot_states=states,
                    batch_id=event.batch_id,
                    source_sequence=event.source_sequence,
                    status_digest=status_digest,
                    manifest_digest=manifest_digest,
                    slots=manifest_slots,
                    slot_status_by_id={slot.slot_id: slot.status for slot in event.slots},
                    artifact_by_slot=artifact_by_slot,
                )
        except DomainError:
            raise
        except IntegrityError:
            with self._sessions() as db:
                saved = db.execute(
                    select(ResultManifestRow).where(
                        ResultManifestRow.generation_request_id == event.generation_request_id,
                        ResultManifestRow.source_sequence == event.source_sequence,
                    )
                ).scalar_one_or_none()
                if saved is not None and saved.manifest_digest == manifest_digest:
                    return _accepted_response(
                        db,
                        event.generation_request_id,
                        event.batch_id,
                        event.source_sequence,
                        replayed=True,
                    )
            raise DomainError(
                409, "GENERATION_RESULT_CONFLICT", "结果清单发生并发冲突"
            ) from None
        except SQLAlchemyError:
            raise DomainError(
                503, "GENERATION_RESULT_UNAVAILABLE", "结果清单暂时无法保存"
            ) from None


def ingest_reconciled_snapshot(
    db: Session,
    *,
    generation_request_id: UUID,
    task_id: UUID,
    session_id: UUID,
    snapshot: Mapping[str, object],
    now,
) -> None:
    """Use the same receipt/intents transaction for an H6 read-only GET snapshot."""
    request = db.execute(
        select(ChatflowGenerationRequest).where(
            ChatflowGenerationRequest.id == generation_request_id,
            ChatflowGenerationRequest.task_id == task_id,
            ChatflowGenerationRequest.business_session_id == session_id,
        )
    ).scalar_one_or_none()
    task = db.execute(select(Task).where(Task.id == task_id)).scalar_one_or_none()
    tracking = db.execute(
        select(ChatflowGenerationTracking).where(
            ChatflowGenerationTracking.generation_request_id == generation_request_id
        )
    ).scalar_one_or_none()
    if request is None or task is None or tracking is None:
        raise ValueError("H7 reconciliation parents are missing")
    batch_id = str(snapshot["batch_id"])
    source_sequence = int(snapshot["source_sequence"])
    raw_slots = snapshot["slots"]
    artifact_refs_by_slot = snapshot.get("artifact_refs_by_slot", {})
    request_slots = db.execute(
        select(ChatflowGenerationRequestSlot)
        .where(ChatflowGenerationRequestSlot.generation_request_id == generation_request_id)
        .order_by(ChatflowGenerationRequestSlot.position, ChatflowGenerationRequestSlot.slot_id)
    ).scalars().all()
    if not isinstance(raw_slots, (list, tuple)) or len(raw_slots) != len(request_slots):
        raise ValueError("H7 reconciliation slot mapping is invalid")
    slot_by_id = {slot.slot_id: slot for slot in request_slots}
    model_slots: list[ChatflowGenerationSlotEvent] = []
    manifest_slots: list[ChatflowGenerationManifestSlot] = []
    artifact_by_slot: dict[str, dict[str, object] | None] = {}
    for raw in raw_slots:
        if not isinstance(raw, Mapping):
            raise ValueError("H7 reconciliation slot is invalid")
        mapping = slot_by_id.get(str(raw.get("slot_id")))
        if mapping is None:
            raise ValueError("H7 reconciliation slot mapping is unknown")
        slot = ChatflowGenerationSlotEvent(
            slot_id=mapping.slot_id,
            task_item_id=mapping.task_item_id,
            attempt_id=mapping.attempt_id,
            position=mapping.position,
            image_type=mapping.image_type,
            status=raw["status"],
            status_version=raw["status_version"],
            error_code=raw.get("error_code"),
            known_retry_count=raw.get("known_retry_count"),
        )
        model_slots.append(slot)
        artifact_ref = raw.get("artifact_ref")
        if artifact_ref is None and isinstance(artifact_refs_by_slot, Mapping):
            artifact_ref = artifact_refs_by_slot.get(slot.slot_id)
        artifact, protocol_error = _artifact_from_service_refs(artifact_ref, slot)
        artifact_by_slot[slot.slot_id] = artifact
        manifest_slots.append(
            ChatflowGenerationManifestSlot(
                slot_id=slot.slot_id,
                task_item_id=slot.task_item_id,
                attempt_id=slot.attempt_id,
                position=slot.position,
                image_type=slot.image_type,
                status_version=slot.status_version,
                original_artifact=(
                    ChatflowGenerationSourceArtifact.model_validate(artifact)
                    if artifact is not None and not protocol_error
                    else None
                ),
            )
        )
    status_digest = generation_snapshot_digest(
        batch_id=batch_id,
        source_sequence=source_sequence,
        slots=model_slots,
    )
    manifest_digest = _manifest_digest(
        generation_request_id=generation_request_id,
        task_id=task_id,
        session_id=session_id,
        batch_id=batch_id,
        source_sequence=source_sequence,
        status_digest=status_digest,
        slots=[
            {
                "slot_id": slot.slot_id,
                "status": slot.status,
                "status_version": slot.status_version,
                "artifact": artifact_by_slot.get(slot.slot_id),
            }
            for slot in model_slots
        ],
    )
    states = db.execute(
        select(ChatflowGenerationSlotState)
        .where(ChatflowGenerationSlotState.generation_request_id == generation_request_id)
        .order_by(ChatflowGenerationSlotState.slot_id)
    ).scalars().all()
    _record_manifest_and_intents(
        db,
        request=request,
        task=task,
        tracking=tracking,
        request_slots=request_slots,
        slot_states=states,
        batch_id=batch_id,
        source_sequence=source_sequence,
        status_digest=status_digest,
        manifest_digest=manifest_digest,
        slots=manifest_slots,
        slot_status_by_id={slot.slot_id: slot.status for slot in model_slots},
        artifact_by_slot=artifact_by_slot,
        protocol_error_slots={
            slot.slot_id
            for raw, slot in zip(raw_slots, model_slots, strict=True)
            if _artifact_from_service_refs(
                raw.get("artifact_ref")
                if raw.get("artifact_ref") is not None
                else artifact_refs_by_slot.get(slot.slot_id)
                if isinstance(artifact_refs_by_slot, Mapping)
                else None,
                slot,
            )[1]
        },
        now=now,
    )


def _record_manifest_and_intents(
    db: Session,
    *,
    request: ChatflowGenerationRequest,
    task: Task,
    tracking: ChatflowGenerationTracking,
    request_slots: list[ChatflowGenerationRequestSlot],
    slot_states: list[ChatflowGenerationSlotState],
    batch_id: str,
    source_sequence: int,
    status_digest: bytes,
    manifest_digest: bytes,
    slots: list[ChatflowGenerationManifestSlot],
    slot_status_by_id: Mapping[str, str],
    artifact_by_slot: Mapping[str, dict[str, object] | None],
    protocol_error_slots: set[str] | None = None,
    now=None,
) -> ChatflowGenerationManifestAccepted:
    if (
        request.task_id != task.id
        or request.business_session_id != task.business_session_id
        or (tracking.batch_id is not None and tracking.batch_id != batch_id)
    ):
        raise DomainError(409, "GENERATION_RESULT_CONFLICT", "结果清单关联不一致")
    existing_manifest = db.execute(
        select(ResultManifestRow)
        .where(
            ResultManifestRow.generation_request_id == request.id,
            ResultManifestRow.source_sequence == source_sequence,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if existing_manifest is not None:
        if (
            existing_manifest.task_id != task.id
            or existing_manifest.business_session_id != task.business_session_id
            or existing_manifest.batch_id != batch_id
            or existing_manifest.status_digest != status_digest
            or existing_manifest.manifest_digest != manifest_digest
        ):
            raise DomainError(409, "GENERATION_RESULT_CONFLICT", "同一结果版本内容不同")
        return _accepted_response(
            db, request.id, batch_id, source_sequence, replayed=True
        )
    db.add(
        ResultManifestRow(
            generation_request_id=request.id,
            source_sequence=source_sequence,
            task_id=task.id,
            business_session_id=task.business_session_id,
            batch_id=batch_id,
            status_digest=status_digest,
            manifest_digest=manifest_digest,
        )
    )
    db.flush()
    mapping_by_id = {slot.slot_id: slot for slot in request_slots}
    states_by_id = {state.slot_id: state for state in slot_states}
    protocol_error_slots = protocol_error_slots or set()
    recorded_at = now or db.scalar(select(func.clock_timestamp()))
    for result_slot in slots:
        mapping = mapping_by_id.get(result_slot.slot_id)
        state = states_by_id.get(result_slot.slot_id)
        if (
            mapping is None
            or mapping.task_id != task.id
            or mapping.task_item_id != result_slot.task_item_id
            or mapping.attempt_id != result_slot.attempt_id
            or mapping.position != result_slot.position
            or mapping.image_type != result_slot.image_type
        ):
            raise DomainError(409, "GENERATION_RESULT_CONFLICT", "图位关联不一致")
        # An old GET/callback cannot introduce an asset after a newer H6 slot
        # state or change an already completed/private result.
        if (
            state is None
            or slot_status_by_id.get(result_slot.slot_id) != "completed"
            or state.status != "completed"
            or state.status_version != result_slot.status_version
        ):
            continue
        source = artifact_by_slot.get(result_slot.slot_id)
        protocol_error = result_slot.slot_id in protocol_error_slots
        if source is None and not protocol_error:
            continue
        delivery = db.execute(
            select(ChatflowGenerationDelivery)
            .where(
                ChatflowGenerationDelivery.generation_request_id == request.id,
                ChatflowGenerationDelivery.slot_id == result_slot.slot_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if delivery is None:
            delivery = ChatflowGenerationDelivery(
                id=uuid5(_DELIVERY_NAMESPACE, f"{request.id}:{result_slot.slot_id}"),
                generation_request_id=request.id,
                slot_id=result_slot.slot_id,
                task_id=task.id,
                task_item_id=mapping.task_item_id,
                attempt_id=mapping.attempt_id,
                owner_user_id=task.owner_user_id,
                batch_id=batch_id,
                source_sequence=source_sequence,
                source_file_id=None,
                source_sha256=None,
                source_mime_type=None,
                source_size_bytes=None,
                source_expires_at=None,
                original_object_key=f"generated/{mapping.attempt_id}/original",
                preview_object_key=f"generated/{mapping.attempt_id}/preview.webp",
                status="protocol_error" if protocol_error else "pending_download",
                error_code=("CHATFLOW_RESULT_PROTOCOL_ERROR" if protocol_error else None),
                download_attempt_count=0,
                updated_at=recorded_at,
            )
            if source is not None and not protocol_error:
                _set_source_identity(delivery, source)
            db.add(delivery)
            db.flush()
        else:
            if (
                delivery.task_id != task.id
                or delivery.task_item_id != mapping.task_item_id
                or delivery.attempt_id != mapping.attempt_id
                or delivery.owner_user_id != task.owner_user_id
                or delivery.batch_id != batch_id
            ):
                raise DomainError(409, "GENERATION_RESULT_CONFLICT", "既有下载关联不同")
            if source is not None and not protocol_error:
                if delivery.source_file_id is not None and not _same_source(delivery, source):
                    raise DomainError(409, "GENERATION_RESULT_CONFLICT", "同一图位结果文件不同")
                if delivery.status in {"saved", "awaiting_confirmation", "confirmed"}:
                    continue
                if delivery.source_file_id is None or delivery.status == "protocol_error":
                    _set_source_identity(delivery, source)
                    delivery.status = "pending_download"
                    delivery.error_code = None
                delivery.source_sequence = max(delivery.source_sequence, source_sequence)
                delivery.updated_at = recorded_at
            elif protocol_error and delivery.status not in {
                "saved", "awaiting_confirmation", "confirmed"
            }:
                delivery.status = "protocol_error"
                delivery.error_code = "CHATFLOW_RESULT_PROTOCOL_ERROR"
                delivery.source_sequence = max(delivery.source_sequence, source_sequence)
                delivery.updated_at = recorded_at
        if delivery.status == "pending_download" and delivery.source_file_id is not None:
            item = next(
                item for item in _task_items(db, task.id) if item.id == delivery.task_item_id
            )
            attempt = db.execute(
                select(TaskItemAttempt).where(
                    TaskItemAttempt.id == delivery.attempt_id,
                    TaskItemAttempt.task_item_id == delivery.task_item_id,
                )
            ).scalar_one()
            item.status = "saving"
            item.preview_ready = False
            item.error_code = None
            attempt.status = "saving"
            attempt.failure_code = None
            _ensure_delivery_action(
                db,
                delivery=delivery,
                task=task,
                request=request,
                action_kind=DELIVERY_DOWNLOAD_ACTION,
                due_at=recorded_at,
            )
    if any(
        slot_status_by_id.get(slot.slot_id) == "completed"
        and slot.slot_id in protocol_error_slots
        for slot in slots
    ):
        for slot in slots:
            if slot.slot_id not in protocol_error_slots:
                continue
            item = db.execute(
                select(TaskItem).where(TaskItem.id == slot.task_item_id).with_for_update()
            ).scalar_one_or_none()
            attempt = db.execute(
                select(TaskItemAttempt).where(
                    TaskItemAttempt.id == slot.attempt_id,
                    TaskItemAttempt.task_item_id == slot.task_item_id,
                ).with_for_update()
            ).scalar_one_or_none()
            if item is not None and item.status not in {"succeeded", "failed"}:
                item.status = "reconciling"
                item.preview_ready = False
                item.error_code = "CHATFLOW_RESULT_PROTOCOL_ERROR"
            if attempt is not None and attempt.status != "completed":
                attempt.status = "reconciling"
                attempt.failure_code = "CHATFLOW_RESULT_PROTOCOL_ERROR"
        task.status = "reconciling"
        task.terminal_at = None
        task.expires_at = None
    elif any(
        delivery.status == "pending_download"
        for delivery in _request_deliveries(db, request.id)
    ):
        task.status = "generating"
        task.terminal_at = None
        task.expires_at = None
    return _accepted_response(db, request.id, batch_id, source_sequence, replayed=False)


def _ensure_delivery_action(
    db: Session,
    *,
    delivery: ChatflowGenerationDelivery,
    task: Task,
    request: ChatflowGenerationRequest,
    action_kind: str,
    due_at,
    after_sequence: int | None = None,
) -> Action:
    sequence_attr = (
        "download_action_sequence"
        if action_kind == DELIVERY_DOWNLOAD_ACTION
        else "confirmation_action_sequence"
    )
    live_conditions = [
            Action.scope_kind == "generation_delivery",
            Action.scope_id == delivery.id,
            Action.action_kind == action_kind,
            Action.status.in_(("scheduled", "claimed")),
    ]
    if after_sequence is not None:
        live_conditions.append(Action.sequence > after_sequence)
    live = db.execute(
        select(Action)
        .where(*live_conditions)
        .order_by(Action.sequence.desc())
        .with_for_update()
    ).scalars().first()
    if live is not None:
        setattr(delivery, sequence_attr, live.sequence)
        return live
    latest = db.execute(
        select(func.max(Action.sequence)).where(
            Action.scope_kind == "generation_delivery",
            Action.scope_id == delivery.id,
        )
    ).scalar_one()
    sequence = (latest or 0) + 1
    digest = _delivery_action_digest(delivery, task, request, action_kind, sequence)
    action = schedule_action_in_transaction(
        db,
        action_id=uuid5(_DELIVERY_NAMESPACE, f"{delivery.id}:{sequence}"),
        task_id=task.id,
        scope_kind="generation_delivery",
        scope_id=delivery.id,
        action_kind=action_kind,
        sequence=sequence,
        due_at=due_at,
        input_digest_version=1,
        input_digest=digest,
        task_item_id=delivery.task_item_id,
        attempt_id=delivery.attempt_id,
        business_session_id=request.business_session_id,
        generation_request_id=request.id,
        generation_delivery_id=delivery.id,
    )
    setattr(delivery, sequence_attr, sequence)
    return action


def _delivery_action_digest(
    delivery: ChatflowGenerationDelivery,
    task: Task,
    request: ChatflowGenerationRequest,
    action_kind: str,
    sequence: int,
) -> bytes:
    value = {
        "action_kind": action_kind,
        "generation_delivery_id": str(delivery.id),
        "generation_request_id": str(request.id),
        "task_id": str(task.id),
        "business_session_id": str(request.business_session_id),
        "task_item_id": str(delivery.task_item_id),
        "attempt_id": str(delivery.attempt_id),
        "slot_id": delivery.slot_id,
        "source_file_id": delivery.source_file_id,
        "source_sha256": delivery.source_sha256.hex() if delivery.source_sha256 else None,
        "sequence": sequence,
    }
    return hashlib.sha256(_canonical_json(value)).digest()


def _manifest_digest(
    *,
    generation_request_id: UUID,
    task_id: UUID,
    session_id: UUID,
    batch_id: str,
    source_sequence: int,
    status_digest: bytes,
    slots: list[dict[str, object]],
) -> bytes:
    value = {
        "generation_request_id": str(generation_request_id),
        "task_id": str(task_id),
        "business_session_id": str(session_id),
        "batch_id": batch_id,
        "source_sequence": source_sequence,
        "status_digest": status_digest.hex(),
        "slots": slots,
    }
    return hashlib.sha256(_canonical_json(value)).digest()


def _artifact_from_service_refs(raw: object, slot: ChatflowGenerationSlotEvent):
    if slot.status != "completed":
        return None, bool(raw)
    if not isinstance(raw, (list, tuple)):
        return None, True
    originals = [
        item for item in raw
        if isinstance(item, Mapping) and item.get("kind") == "original"
    ]
    if len(originals) != 1:
        return None, True
    item = originals[0]
    try:
        artifact = ChatflowGenerationSourceArtifact.model_validate(
            {
                "file_id": item.get("file_id"),
                "sha256": item.get("sha256"),
                "mime_type": item.get("mime_type"),
                "size_bytes": item.get("size_bytes"),
                "expires_at": item.get("expires_at"),
            }
        )
    except Exception:
        return None, True
    if not artifact.file_id.startswith(slot.slot_id + "-"):
        return None, True
    return artifact.model_dump(mode="json"), False


def _set_source_identity(
    delivery: ChatflowGenerationDelivery, source: dict[str, object]
) -> None:
    artifact = ChatflowGenerationSourceArtifact.model_validate(source)
    delivery.source_file_id = artifact.file_id
    delivery.source_sha256 = bytes.fromhex(artifact.sha256)
    delivery.source_mime_type = artifact.mime_type
    delivery.source_size_bytes = artifact.size_bytes
    delivery.source_expires_at = artifact.expires_at


def _same_source(delivery: ChatflowGenerationDelivery, source: dict[str, object]) -> bool:
    artifact = ChatflowGenerationSourceArtifact.model_validate(source)
    return (
        delivery.source_file_id == artifact.file_id
        and delivery.source_sha256 == bytes.fromhex(artifact.sha256)
        and delivery.source_mime_type == artifact.mime_type
        and delivery.source_size_bytes == artifact.size_bytes
    )


def _accepted_response(
    db: Session,
    request_id: UUID,
    batch_id: str,
    source_sequence: int,
    *,
    replayed: bool,
) -> ChatflowGenerationManifestAccepted:
    pending = db.execute(
        select(ChatflowGenerationDelivery.id).where(
            ChatflowGenerationDelivery.generation_request_id == request_id,
            ChatflowGenerationDelivery.status.in_(
                ("pending_download", "saved", "awaiting_confirmation")
            ),
        )
    ).first()
    return ChatflowGenerationManifestAccepted(
        status="replayed" if replayed else "accepted",
        image_save_status="pending" if pending else "not_required",
        generation_request_id=request_id,
        batch_id=batch_id,
        source_sequence=source_sequence,
    )


def _task_items(db: Session, task_id: UUID) -> list[TaskItem]:
    return db.execute(
        select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.position)
    ).scalars().all()


def _request_deliveries(db: Session, request_id: UUID) -> list[ChatflowGenerationDelivery]:
    return db.execute(
        select(ChatflowGenerationDelivery).where(
            ChatflowGenerationDelivery.generation_request_id == request_id
        )
    ).scalars().all()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
