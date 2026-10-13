"""H7 fixed-source download and exact delivery-confirmation Actions."""

from __future__ import annotations

import hashlib
import io
from collections.abc import Mapping
from datetime import timedelta
from typing import Protocol
from urllib.parse import quote
from uuid import UUID, uuid5

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.action_models import Action
from app.db.business_session_models import BusinessSession
from app.db.chatflow_generation_models import (
    ChatflowGenerationDelivery,
    ChatflowGenerationRequest,
)
from app.db.generation_models import GenerationResult
from app.db.reference_models import ImageAsset
from app.db.task_models import Task, TaskItem, TaskItemAttempt
from app.integrations.storage.protocol import (
    ImmutableObjectConflict,
    Storage,
    StorageError,
)
from app.services.chatflow_generation_contracts import (
    IMAGE_GENERATION_CONTRACT_V2,
    IMAGE_GENERATION_CONTRACT_V3,
    image_generation_contract_for_flow_version,
)
from app.services.chatflow_generation_results import (
    DELIVERY_CONFIRM_ACTION,
    DELIVERY_DOWNLOAD_ACTION,
    _ensure_delivery_action,
)
from app.services.generation_image_saving import (
    MAX_GENERATION_IMAGE_BYTES,
    GenerationImageError,
    validate_and_render_generation_output,
)
from app.workers.action_processor import (
    ActionExecutionResult,
    ActionInput,
    ActionRegistration,
)

_ASSET_NAMESPACE = UUID("df2c1369-8087-4f3b-a219-9dbde02b3025")
_MAX_DOWNLOAD_ATTEMPTS = 5
_RETRY_DELAYS = (10, 30, 90, 240)


class ImageGenerationDeliveryPort(Protocol):
    def get_artifact(
        self,
        batch_id: str,
        slot_id: str,
        file_id: str,
        *,
        contract_version: str,
    ) -> tuple[bytes, str]: ...

    def confirm_delivery(
        self,
        batch_id: str,
        slot_id: str,
        payload: Mapping[str, str],
        *,
        contract_version: str,
    ) -> None: ...


class DeliveryPortError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = True) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class HttpImageGenerationDeliveryPort:
    """Use fixed authenticated API paths; manifest data never supplies a URL."""

    def __init__(self, base_url: str, bearer_token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Accept-Encoding": "identity",
            },
        )

    def get_artifact(
        self,
        batch_id: str,
        slot_id: str,
        file_id: str,
        *,
        contract_version: str = IMAGE_GENERATION_CONTRACT_V2,
    ) -> tuple[bytes, str]:
        version = _api_version(contract_version)
        path = (
            f"/{version}/generation-batches/{quote(batch_id, safe='')}/slots/"
            f"{quote(slot_id, safe='')}/artifacts/{quote(file_id, safe='')}"
        )
        try:
            with self._client.stream("GET", self._base_url + path) as response:
                if response.status_code == 404:
                    raise DeliveryPortError("IMAGE_SOURCE_UNAVAILABLE")
                if response.status_code in {408, 429} or response.status_code >= 500:
                    raise DeliveryPortError("IMAGE_SOURCE_TEMPORARY")
                if not response.is_success:
                    raise DeliveryPortError("IMAGE_SOURCE_REJECTED", retryable=False)
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise DeliveryPortError("IMAGE_SOURCE_ENCODING_INVALID", retryable=False)
                mime_type = (
                    response.headers.get("content-type", "").split(";", maxsplit=1)[0].lower()
                )
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_GENERATION_IMAGE_BYTES:
                        raise DeliveryPortError("IMAGE_SOURCE_TOO_LARGE", retryable=False)
                    chunks.append(chunk)
                return b"".join(chunks), mime_type
        except DeliveryPortError:
            raise
        except httpx.HTTPError:
            raise DeliveryPortError("IMAGE_SOURCE_TEMPORARY") from None

    def confirm_delivery(
        self,
        batch_id: str,
        slot_id: str,
        payload: Mapping[str, str],
        *,
        contract_version: str = IMAGE_GENERATION_CONTRACT_V2,
    ) -> None:
        version = _api_version(contract_version)
        path = (
            f"/{version}/generation-batches/{quote(batch_id, safe='')}/slots/"
            f"{quote(slot_id, safe='')}/delivery-confirmations"
        )
        try:
            response = self._client.post(self._base_url + path, json=dict(payload))
        except httpx.HTTPError:
            raise DeliveryPortError("IMAGE_DELIVERY_ACK_UNKNOWN") from None
        if response.is_success:
            try:
                body = response.json()
            except ValueError:
                raise DeliveryPortError("IMAGE_DELIVERY_ACK_UNKNOWN") from None
            if (
                not isinstance(body, Mapping)
                or set(body) != {"status"}
                or body.get("status") not in {"confirmed", "replayed"}
            ):
                raise DeliveryPortError("IMAGE_DELIVERY_ACK_UNKNOWN")
            return
        if response.status_code in {408, 429} or response.status_code >= 500:
            raise DeliveryPortError("IMAGE_DELIVERY_ACK_UNKNOWN")
        raise DeliveryPortError("IMAGE_DELIVERY_ACK_REJECTED", retryable=False)

    def close(self) -> None:
        self._client.close()


class UnavailableImageGenerationDeliveryPort:
    def get_artifact(
        self,
        batch_id: str,
        slot_id: str,
        file_id: str,
        *,
        contract_version: str = IMAGE_GENERATION_CONTRACT_V2,
    ):
        del batch_id, slot_id, file_id
        _api_version(contract_version)
        raise DeliveryPortError("IMAGE_SERVICE_NOT_CONFIGURED")

    def confirm_delivery(
        self,
        batch_id: str,
        slot_id: str,
        payload: Mapping[str, str],
        *,
        contract_version: str = IMAGE_GENERATION_CONTRACT_V2,
    ) -> None:
        del batch_id, slot_id, payload
        _api_version(contract_version)
        raise DeliveryPortError("IMAGE_SERVICE_NOT_CONFIGURED")


def _api_version(contract_version: str) -> str:
    if contract_version == IMAGE_GENERATION_CONTRACT_V2:
        return "v2"
    if contract_version == IMAGE_GENERATION_CONTRACT_V3:
        return "v3"
    raise DeliveryPortError("IMAGE_SERVICE_CONTRACT_UNSUPPORTED", retryable=False)


def build_generation_delivery_registrations(
    port: ImageGenerationDeliveryPort,
    storage: Storage,
) -> dict[str, ActionRegistration]:
    def execute(action_input: ActionInput) -> ActionExecutionResult:
        return execute_generation_delivery(port, storage, action_input)

    return {
        DELIVERY_DOWNLOAD_ACTION: ActionRegistration(
            kind=DELIVERY_DOWNLOAD_ACTION,
            prepare=prepare_generation_delivery,
            execute=execute,
            recover=execute,
            finalize=finalize_generation_delivery,
            scope_kinds=frozenset({"generation_delivery"}),
        ),
        DELIVERY_CONFIRM_ACTION: ActionRegistration(
            kind=DELIVERY_CONFIRM_ACTION,
            prepare=prepare_generation_delivery,
            execute=execute,
            recover=execute,
            finalize=finalize_generation_delivery,
            scope_kinds=frozenset({"generation_delivery"}),
        ),
    }


def prepare_generation_delivery(db: Session, action: Action) -> ActionInput:
    if (
        action.scope_kind != "generation_delivery"
        or action.generation_delivery_id is None
        or action.generation_request_id is None
        or action.task_id is None
        or action.business_session_id is None
        or action.task_item_id is None
        or action.attempt_id is None
        or action.scope_id != action.generation_delivery_id
        or action.action_kind not in {DELIVERY_DOWNLOAD_ACTION, DELIVERY_CONFIRM_ACTION}
    ):
        raise ValueError("generation delivery Action identity is invalid")
    delivery = db.execute(
        select(ChatflowGenerationDelivery).where(
            ChatflowGenerationDelivery.id == action.generation_delivery_id,
            ChatflowGenerationDelivery.generation_request_id == action.generation_request_id,
            ChatflowGenerationDelivery.task_id == action.task_id,
            ChatflowGenerationDelivery.task_item_id == action.task_item_id,
            ChatflowGenerationDelivery.attempt_id == action.attempt_id,
        )
    ).scalar_one_or_none()
    request = db.execute(
        select(ChatflowGenerationRequest).where(
            ChatflowGenerationRequest.id == action.generation_request_id,
            ChatflowGenerationRequest.task_id == action.task_id,
            ChatflowGenerationRequest.business_session_id == action.business_session_id,
        )
    ).scalar_one_or_none()
    business_session = (
        db.get(BusinessSession, request.business_session_id) if request is not None else None
    )
    if delivery is None or request is None or business_session is None:
        raise ValueError("generation delivery parents are incomplete")
    service_contract_version = image_generation_contract_for_flow_version(
        business_session.flow_version
    )
    if action.action_kind == DELIVERY_DOWNLOAD_ACTION:
        if action.sequence != delivery.download_action_sequence:
            raise ValueError("generation download Action sequence differs from its intent")
        should_execute = delivery.status == "pending_download"
    else:
        if action.sequence != delivery.confirmation_action_sequence:
            raise ValueError("generation confirmation Action sequence differs from its intent")
        should_execute = delivery.status == "awaiting_confirmation"
    payload = {
        "generation_request_id": str(request.id),
        "image_generation_contract_version": service_contract_version,
        "business_session_id": str(request.business_session_id),
        "task_id": str(delivery.task_id),
        "task_item_id": str(delivery.task_item_id),
        "attempt_id": str(delivery.attempt_id),
        "slot_id": delivery.slot_id,
        "batch_id": delivery.batch_id,
        "file_id": delivery.source_file_id,
        "sha256": delivery.source_sha256.hex() if delivery.source_sha256 else None,
        "mime_type": delivery.source_mime_type,
        "size_bytes": delivery.source_size_bytes,
        "download_attempt_count": delivery.download_attempt_count,
        "confirmation_attempt_count": delivery.confirmation_attempt_count,
        "original_object_key": delivery.original_object_key,
        "preview_object_key": delivery.preview_object_key,
        "status": delivery.status,
        "should_execute": should_execute,
    }
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
        task_item_id=action.task_item_id,
        attempt_id=action.attempt_id,
        business_session_id=action.business_session_id,
        generation_request_id=action.generation_request_id,
        generation_delivery_id=action.generation_delivery_id,
    )


def execute_generation_delivery(
    port: ImageGenerationDeliveryPort,
    storage: Storage,
    action_input: ActionInput,
) -> ActionExecutionResult:
    payload = _payload(action_input)
    if not payload.get("should_execute"):
        return ActionExecutionResult("accepted", value={"noop": True})
    if action_input.action_kind == DELIVERY_CONFIRM_ACTION:
        request_id = _text(payload, "generation_request_id")
        task_id = _text(payload, "task_id")
        item_id = _text(payload, "task_item_id")
        attempt_id = _text(payload, "attempt_id")
        file_id = _text(payload, "file_id")
        sha256 = _text(payload, "sha256")
        try:
            port.confirm_delivery(
                _text(payload, "batch_id"),
                _text(payload, "slot_id"),
                {
                    "generation_request_id": request_id,
                    "task_id": task_id,
                    "task_item_id": item_id,
                    "attempt_id": attempt_id,
                    "file_id": file_id,
                    "sha256": sha256,
                },
                contract_version=str(
                    payload.get("image_generation_contract_version", IMAGE_GENERATION_CONTRACT_V2)
                ),
            )
        except DeliveryPortError as exc:
            if exc.retryable:
                return _retry_or_unknown(
                    attempt_number=int(payload.get("confirmation_attempt_count", 0)) + 1,
                    error_code=exc.code,
                    stage="confirmation",
                )
            return ActionExecutionResult(
                "definitely_rejected",
                safe_reason_code=exc.code,
                value={"stage": "confirmation", "confirmation_attempted": True},
            )
        return ActionExecutionResult(
            "accepted", value={"stage": "confirmation", "confirmation_attempted": True}
        )

    try:
        raw, source_mime = port.get_artifact(
            _text(payload, "batch_id"),
            _text(payload, "slot_id"),
            _text(payload, "file_id"),
            contract_version=str(
                payload.get("image_generation_contract_version", IMAGE_GENERATION_CONTRACT_V2)
            ),
        )
    except DeliveryPortError as exc:
        if exc.retryable:
            return _retry_or_unknown(
                attempt_number=int(payload.get("download_attempt_count", 0)) + 1,
                error_code=exc.code,
                stage="download",
            )
        return ActionExecutionResult(
            "definitely_rejected",
            safe_reason_code=exc.code,
            value={"stage": "download", "download_attempted": True},
        )
    if (
        len(raw) != payload.get("size_bytes")
        or hashlib.sha256(raw).hexdigest() != payload.get("sha256")
        or source_mime != payload.get("mime_type")
    ):
        return ActionExecutionResult(
            "definitely_rejected",
            safe_reason_code="IMAGE_SOURCE_DIGEST_MISMATCH",
            value={"stage": "download", "download_attempted": True},
        )
    try:
        rendered = validate_and_render_generation_output(raw)
        if rendered.original_mime_type != payload.get("mime_type"):
            return ActionExecutionResult(
                "definitely_rejected",
                safe_reason_code="IMAGE_SOURCE_MIME_INVALID",
                value={"stage": "download", "download_attempted": True},
            )
        original_stat = storage.put_immutable(
            _text(payload, "original_object_key"),
            io.BytesIO(rendered.original_bytes),
            {
                "asset_kind": "generation_original",
                "mime_type": rendered.original_mime_type,
                "sha256": rendered.original_sha256.hex(),
                "source": "chatflow",
            },
        )
        preview_stat = storage.put_immutable(
            _text(payload, "preview_object_key"),
            io.BytesIO(rendered.preview_bytes),
            {
                "asset_kind": "generation_preview",
                "mime_type": rendered.preview_mime_type,
                "sha256": rendered.preview_sha256.hex(),
                "source": "chatflow",
            },
        )
    except GenerationImageError as exc:
        return ActionExecutionResult(
            "definitely_rejected",
            safe_reason_code=exc.reason_code,
            value={"stage": "download", "download_attempted": True},
        )
    except ImmutableObjectConflict:
        return ActionExecutionResult(
            "definitely_rejected",
            safe_reason_code="PRIVATE_OBJECT_CONFLICT",
            value={"stage": "download", "download_attempted": True},
        )
    except StorageError:
        return _retry_or_unknown(
            attempt_number=int(payload.get("download_attempt_count", 0)) + 1,
            error_code="PRIVATE_STORAGE_UNAVAILABLE",
            stage="download",
        )
    return ActionExecutionResult(
        "accepted",
        value={
            "stage": "download",
            "download_attempted": True,
            "original_sha256": original_stat.sha256,
            "original_size_bytes": original_stat.size_bytes,
            "original_mime_type": rendered.original_mime_type,
            "original_width": rendered.width,
            "original_height": rendered.height,
            "preview_sha256": preview_stat.sha256,
            "preview_size_bytes": preview_stat.size_bytes,
            "preview_width": rendered.preview_width,
            "preview_height": rendered.preview_height,
        },
    )


def finalize_generation_delivery(
    db: Session,
    action: Action,
    action_input: ActionInput,
    result: ActionExecutionResult,
) -> None:
    delivery_id = action.generation_delivery_id
    request_id = action.generation_request_id
    if (
        delivery_id is None
        or request_id is None
        or action.task_id is None
        or action.task_item_id is None
        or action.attempt_id is None
        or action.business_session_id is None
    ):
        raise ValueError("generation delivery Action linkage is incomplete")
    delivery = db.execute(
        select(ChatflowGenerationDelivery)
        .where(
            ChatflowGenerationDelivery.id == delivery_id,
            ChatflowGenerationDelivery.generation_request_id == request_id,
            ChatflowGenerationDelivery.task_id == action.task_id,
            ChatflowGenerationDelivery.task_item_id == action.task_item_id,
            ChatflowGenerationDelivery.attempt_id == action.attempt_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    task = db.execute(
        select(Task).where(Task.id == action.task_id).with_for_update()
    ).scalar_one_or_none()
    item = db.execute(
        select(TaskItem).where(TaskItem.id == action.task_item_id).with_for_update()
    ).scalar_one_or_none()
    attempt = db.execute(
        select(TaskItemAttempt)
        .where(
            TaskItemAttempt.id == action.attempt_id,
            TaskItemAttempt.task_item_id == action.task_item_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    request = db.execute(
        select(ChatflowGenerationRequest)
        .where(
            ChatflowGenerationRequest.id == request_id,
            ChatflowGenerationRequest.task_id == action.task_id,
            ChatflowGenerationRequest.business_session_id == action.business_session_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if any(row is None for row in (delivery, task, item, attempt, request)):
        raise ValueError("generation delivery parents are missing")
    now = db.scalar(select(func.clock_timestamp()))
    value = result.value if isinstance(result.value, Mapping) else {}
    if action.action_kind == DELIVERY_DOWNLOAD_ACTION:
        if value.get("download_attempted"):
            delivery.download_attempt_count += 1
        if result.outcome == "safe_retry":
            delivery.status = "pending_download"
            delivery.updated_at = now
            next_due = now + timedelta(seconds=result.retry_after_seconds or 1)
            _ensure_delivery_action(
                db,
                delivery=delivery,
                task=task,
                request=request,
                action_kind=DELIVERY_DOWNLOAD_ACTION,
                due_at=next_due,
                after_sequence=action.sequence,
            )
            return
        if result.outcome == "unknown":
            delivery.status = "source_unavailable"
            delivery.error_code = result.safe_reason_code or "IMAGE_SOURCE_UNAVAILABLE"
            delivery.updated_at = now
            item.status = "reconciling"
            item.preview_ready = False
            item.error_code = delivery.error_code
            attempt.status = "reconciling"
            attempt.failure_code = delivery.error_code
            task.status = "reconciling"
            task.terminal_at = None
            task.expires_at = None
            task.updated_at = now
            return
        if result.outcome == "definitely_rejected":
            delivery.status = "protocol_error"
            delivery.error_code = result.safe_reason_code or "CHATFLOW_RESULT_PROTOCOL_ERROR"
            delivery.updated_at = now
            item.status = "reconciling"
            item.preview_ready = False
            item.error_code = delivery.error_code
            attempt.status = "reconciling"
            attempt.failure_code = delivery.error_code
            task.status = "reconciling"
            task.terminal_at = None
            task.expires_at = None
            task.updated_at = now
            return
        if value.get("noop"):
            return
        _attach_saved_result(db, delivery, task, item, attempt, value, now)
        delivery.status = "awaiting_confirmation"
        delivery.error_code = None
        delivery.saved_at = now
        delivery.updated_at = now
        _ensure_delivery_action(
            db,
            delivery=delivery,
            task=task,
            request=request,
            action_kind=DELIVERY_CONFIRM_ACTION,
            due_at=now,
        )
        return
    if action.action_kind == DELIVERY_CONFIRM_ACTION:
        if value.get("confirmation_attempted"):
            delivery.confirmation_attempt_count += 1
        if result.outcome == "accepted":
            if value.get("noop"):
                from app.services.chatflow_generation_expiry import (
                    mark_accepted_batch_delivery_progress,
                )

                mark_accepted_batch_delivery_progress(db, request_id, now=now)
                return
            delivery.status = "confirmed"
            delivery.error_code = None
            delivery.confirmed_at = now
            delivery.updated_at = now
        elif result.outcome in {"safe_retry", "unknown"}:
            delivery.error_code = result.safe_reason_code or "IMAGE_DELIVERY_ACK_UNKNOWN"
            delivery.updated_at = now
            if result.outcome == "safe_retry":
                _ensure_delivery_action(
                    db,
                    delivery=delivery,
                    task=task,
                    request=request,
                    action_kind=DELIVERY_CONFIRM_ACTION,
                    due_at=now + timedelta(seconds=result.retry_after_seconds or 1),
                    after_sequence=action.sequence,
                )
        else:
            delivery.error_code = result.safe_reason_code or "IMAGE_DELIVERY_ACK_REJECTED"
            delivery.updated_at = now
        if delivery.status == "confirmed":
            from app.services.chatflow_generation_expiry import (
                mark_accepted_batch_delivery_progress,
            )

            mark_accepted_batch_delivery_progress(db, request_id, now=now)
            from app.services.chatflow_generation_closure import ensure_generation_close_action

            ensure_generation_close_action(db, request_id, now)


def _attach_saved_result(
    db: Session,
    delivery: ChatflowGenerationDelivery,
    task: Task,
    item: TaskItem,
    attempt: TaskItemAttempt,
    value: Mapping[str, object],
    now,
) -> None:
    original_id = uuid5(_ASSET_NAMESPACE, f"{attempt.id}:original")
    preview_id = uuid5(_ASSET_NAMESPACE, f"{attempt.id}:preview")
    metadata = {
        "original": {
            "id": original_id,
            "kind": "generation_original",
            "object_key": delivery.original_object_key,
            "sha256": _required_text(value, "original_sha256"),
            "mime_type": _required_text(value, "original_mime_type"),
            "size_bytes": _required_int(value, "original_size_bytes"),
            "width": _required_int(value, "original_width"),
            "height": _required_int(value, "original_height"),
        },
        "preview": {
            "id": preview_id,
            "kind": "generation_preview",
            "object_key": delivery.preview_object_key,
            "sha256": _required_text(value, "preview_sha256"),
            "mime_type": "image/webp",
            "size_bytes": _required_int(value, "preview_size_bytes"),
            "width": _required_int(value, "preview_width"),
            "height": _required_int(value, "preview_height"),
        },
    }
    assets: dict[str, ImageAsset] = {}
    for role, spec in metadata.items():
        existing = db.execute(
            select(ImageAsset).where(ImageAsset.id == spec["id"]).with_for_update()
        ).scalar_one_or_none()
        if existing is None:
            existing = ImageAsset(
                id=spec["id"],
                owner_user_id=task.owner_user_id,
                asset_kind=spec["kind"],
                object_key=spec["object_key"],
                content_sha256=spec["sha256"],
                mime_type=spec["mime_type"],
                size_bytes=spec["size_bytes"],
                width=spec["width"],
                height=spec["height"],
            )
            db.add(existing)
        elif (
            existing.owner_user_id != task.owner_user_id
            or existing.asset_kind != spec["kind"]
            or existing.object_key != spec["object_key"]
            or existing.content_sha256 != spec["sha256"]
            or existing.mime_type != spec["mime_type"]
            or existing.size_bytes != spec["size_bytes"]
        ):
            raise ValueError("deterministic generation asset association conflicts")
        assets[role] = existing
    db.flush()
    result_row = db.execute(
        select(GenerationResult).where(GenerationResult.attempt_id == attempt.id).with_for_update()
    ).scalar_one_or_none()
    if result_row is None:
        db.add(
            GenerationResult(
                attempt_id=attempt.id,
                task_id=task.id,
                task_item_id=item.id,
                owner_user_id=task.owner_user_id,
                original_asset_id=original_id,
                preview_asset_id=preview_id,
            )
        )
    elif (
        result_row.task_id != task.id
        or result_row.task_item_id != item.id
        or result_row.owner_user_id != task.owner_user_id
        or result_row.original_asset_id != original_id
        or result_row.preview_asset_id != preview_id
    ):
        raise ValueError("generation result association conflicts")
    delivery.original_asset_id = original_id
    delivery.preview_asset_id = preview_id
    delivery.original_size_bytes = metadata["original"]["size_bytes"]
    delivery.original_sha256 = metadata["original"]["sha256"]
    delivery.original_mime_type = metadata["original"]["mime_type"]
    delivery.original_width = metadata["original"]["width"]
    delivery.original_height = metadata["original"]["height"]
    delivery.preview_size_bytes = metadata["preview"]["size_bytes"]
    delivery.preview_sha256 = metadata["preview"]["sha256"]
    delivery.preview_width = metadata["preview"]["width"]
    delivery.preview_height = metadata["preview"]["height"]
    item.image_id = original_id
    item.preview_ready = True
    item.width = metadata["original"]["width"]
    item.height = metadata["original"]["height"]
    item.status = "succeeded"
    item.error_code = None
    item.updated_at = now
    attempt.status = "completed"
    attempt.failure_code = None
    attempt.completed_at = now
    attempt.next_action_at = None
    attempt.updated_at = now
    _aggregate_task(db, task, now)
    from app.services.chatflow_generation_closure import ensure_generation_close_action

    ensure_generation_close_action(db, delivery.generation_request_id, now)


def _aggregate_task(db: Session, task: Task, now) -> None:
    items = (
        db.execute(select(TaskItem).where(TaskItem.task_id == task.id).order_by(TaskItem.position))
        .scalars()
        .all()
    )
    statuses = [item.status for item in items]
    if any(status == "reconciling" for status in statuses):
        task.status = "reconciling"
        task.terminal_at = None
        task.expires_at = None
    elif all(status in {"succeeded", "failed"} for status in statuses):
        succeeded = statuses.count("succeeded")
        task.status = (
            "succeeded"
            if succeeded == len(statuses)
            else "failed"
            if succeeded == 0
            else "partially_succeeded"
        )
        task.terminal_at = now
        task.expires_at = now + timedelta(days=task.retention_days)
    else:
        task.status = "generating"
        task.terminal_at = None
        task.expires_at = None
    task.updated_at = now


def _retry_or_unknown(*, attempt_number: int, error_code: str, stage: str):
    value = {
        "stage": stage,
        "download_attempted": stage == "download",
        "confirmation_attempted": stage == "confirmation",
    }
    if attempt_number < _MAX_DOWNLOAD_ATTEMPTS:
        delay = _RETRY_DELAYS[min(attempt_number - 1, len(_RETRY_DELAYS) - 1)]
        return ActionExecutionResult(
            "safe_retry",
            safe_reason_code=error_code,
            value=value,
            retry_after_seconds=delay,
        )
    return ActionExecutionResult(
        "unknown",
        safe_reason_code=error_code,
        value=value,
    )


def _payload(action_input: ActionInput) -> Mapping[str, object]:
    if not isinstance(action_input.payload, Mapping):
        raise ValueError("generation delivery Action payload is invalid")
    return action_input.payload


def _text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"generation delivery {key} is invalid")
    return value


def _required_text(payload: Mapping[str, object], key: str) -> str:
    return _text(payload, key)


def _required_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"generation delivery {key} is invalid")
    return value
