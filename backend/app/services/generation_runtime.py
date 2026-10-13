"""Persistent, one-step orchestration for private mock image generation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from typing import Final, cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.action_models import Action
from app.db.generation_models import (
    GenerationAttemptContext,
    GenerationCapacityControl,
    GenerationCapacityReservation,
    GenerationResult,
)
from app.db.models import User
from app.db.outbox_models import OutboxEvent
from app.db.prompt_models import TaskPromptItem, TaskPromptPackage
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ImageAsset, ReferenceSet
from app.db.task_models import Task, TaskItem, TaskItemAttempt
from app.integrations.generation.protocol import (
    GenerationAccepted,
    GenerationCommand,
    GenerationDefinitelyRejected,
    GenerationDownloadError,
    GenerationDownloadTemporaryError,
    GenerationIdempotencyConflict,
    GenerationPort,
    GenerationPortError,
    GenerationQueryFailed,
    GenerationQueryPending,
    GenerationQuerySucceeded,
    GenerationQueryTemporaryError,
    GenerationQueryUnknown,
    GenerationRecoveryDefinitelyNotFound,
    GenerationRecoveryFound,
    GenerationRecoveryUnknown,
    GenerationRecoveryUnsupported,
    GenerationUnknown,
)
from app.integrations.storage.protocol import Storage
from app.services.generation_image_saving import (
    MAX_GENERATION_IMAGE_BYTES,
    GenerationImageError,
    validate_and_render_generation_output,
)
from app.services.prompt_validation import (
    PROMPT_PACKAGE_LEGACY_VALIDATOR_VERSIONS,
    PROMPT_PACKAGE_VALIDATOR_VERSION,
)
from app.workers.action_processor import (
    ActionExecutionResult,
    ActionInput,
    ActionRegistration,
    schedule_action_in_transaction,
)
from app.workers.messages import OutboxMessage
from app.workers.processor import PermanentEventFailure

GENERATION_START_EVENT_TYPE: Final[str] = "task.generation.start.v1"
GENERATION_ACTION_KINDS: Final[tuple[str, ...]] = (
    "generation.submit.v1",
    "generation.recover.v1",
    "generation.query.v1",
    "generation.download.v1",
    "generation.save.v1",
)
GENERATION_CAPACITY_CONTROL_ID: Final[int] = 1
DEFAULT_GLOBAL_GENERATION_LIMIT: Final[int] = 2
GENERATION_RETRY_SECONDS: Final[int] = 5
GENERATION_RECOVER_SECONDS: Final[int] = 15
_DIGEST_VERSION: Final[int] = 1
_ASSET_NAMESPACE: Final[UUID] = UUID("df2c1369-8087-4f3b-a219-9dbde02b3025")


class GenerationRuntimeError(RuntimeError):
    """A generation invariant failed without exposing prompt or provider data."""


class GenerationRuntimeConflict(GenerationRuntimeError):
    """Persisted generation state conflicts with the immutable item contract."""


@dataclass(frozen=True, slots=True)
class GenerationRuntimeOptions:
    """Private runtime defaults; production wiring must supply policy outside user JSON."""

    global_capacity: int = DEFAULT_GLOBAL_GENERATION_LIMIT
    channel_id: str = "mock"
    model_id: str = "mock-generation"

    def __post_init__(self) -> None:
        if isinstance(self.global_capacity, bool) or self.global_capacity < 1:
            raise ValueError("global_capacity must be positive")
        if not self.channel_id or len(self.channel_id) > 100:
            raise ValueError("channel_id must be bounded text")
        if not self.model_id or len(self.model_id) > 100:
            raise ValueError("model_id must be bounded text")


def handle_generation_start_event(
    db: Session,
    event: OutboxEvent,
    message: OutboxMessage,
    *,
    options: GenerationRuntimeOptions | None = None,
) -> None:
    """Create one durable attempt/action from a trusted prompt callback event.

    EventProcessor owns the event/receipt transaction. This function then acquires
    the business parents in R0 order and writes the attempt, private context, Action,
    and its outbox receipt in that same transaction.
    """

    if event.event_type != GENERATION_START_EVENT_TYPE:
        raise PermanentEventFailure("GENERATION_EVENT_TYPE_MISMATCH")
    task_item_id = message.payload.get("task_item_id")
    if (
        not isinstance(task_item_id, UUID)
        or event.aggregate_id != task_item_id
        or message.aggregate_id != task_item_id
    ):
        raise PermanentEventFailure("GENERATION_EVENT_PAYLOAD_INVALID")
    try:
        start_generation_item(db, task_item_id, options=options)
    except GenerationRuntimeConflict:
        raise PermanentEventFailure("GENERATION_INPUT_CONFLICT") from None


def start_generation_item(
    db: Session,
    task_item_id: UUID,
    *,
    options: GenerationRuntimeOptions | None = None,
) -> UUID | None:
    """Persist a stable attempt and first submit action for one ready TaskItem."""

    runtime_options = options or GenerationRuntimeOptions()
    item_hint = db.execute(
        select(TaskItem.id, TaskItem.task_id).where(TaskItem.id == task_item_id)
    ).one_or_none()
    if item_hint is None:
        return None

    parent_hint = db.execute(
        select(Task.owner_user_id, Task.reference_set_id, Task.recommendation_id).where(
            Task.id == item_hint.task_id
        )
    ).one_or_none()
    if parent_hint is None:
        return None
    owner_id, reference_set_id, recommendation_id = parent_hint

    owner = db.execute(
        select(User).where(User.id == owner_id).with_for_update()
    ).scalar_one_or_none()
    reference_set = db.execute(
        select(ReferenceSet)
        .where(ReferenceSet.id == reference_set_id)
        .with_for_update()
    ).scalar_one_or_none()
    recommendation = db.execute(
        select(Recommendation)
        .where(Recommendation.id == recommendation_id)
        .with_for_update()
    ).scalar_one_or_none()
    task = db.execute(
        select(Task).where(Task.id == item_hint.task_id).with_for_update()
    ).scalar_one_or_none()
    if (
        owner is None
        or reference_set is None
        or recommendation is None
        or task is None
        or task.owner_user_id != owner.id
        or task.reference_set_id != reference_set.id
        or task.recommendation_id != recommendation.id
        or owner.status != "active"
    ):
        return None

    item = db.execute(
        select(TaskItem)
        .where(TaskItem.id == task_item_id, TaskItem.task_id == task.id)
        .with_for_update()
    ).scalar_one_or_none()
    if item is None:
        return None

    existing_attempts = (
        db.execute(
            select(TaskItemAttempt)
            .where(TaskItemAttempt.task_item_id == item.id)
            .order_by(TaskItemAttempt.attempt_no)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    if existing_attempts:
        # Duplicate generation-start delivery is an idempotent no-op. Retries are
        # a separate R6 operation and must allocate a new attempt UUID.
        if len(existing_attempts) > 1:
            raise GenerationRuntimeConflict("multiple attempts already exist")
        return existing_attempts[0].id
    if item.status != "queued" or task.status in {"succeeded", "partially_succeeded", "failed"}:
        raise GenerationRuntimeConflict("generation item is not queued")

    prompt_item = db.execute(
        select(TaskPromptItem).where(TaskPromptItem.task_item_id == item.id)
    ).scalar_one_or_none()
    if prompt_item is None or prompt_item.task_id != task.id:
        raise GenerationRuntimeConflict("trusted prompt item is missing")
    package = db.execute(
        select(TaskPromptPackage).where(
            TaskPromptPackage.id == prompt_item.package_id,
            TaskPromptPackage.task_id == task.id,
        )
    ).scalar_one_or_none()
    if package is None or not _valid_prompt_item(prompt_item, item, task, package):
        raise GenerationRuntimeConflict("trusted prompt package is inconsistent")

    reference_ids = _parse_allowed_reference_ids(prompt_item.allowed_reference_asset_ids)
    prompt_text = f"{prompt_item.design_prompt}\n\n{prompt_item.identity_prompt}"
    prompt_version = package.chatflow_contract_version
    attempt_id = uuid4()
    command = GenerationCommand(
        request_key=attempt_id,
        image_type=item.image_type,
        prompt_package_version=prompt_version,
        prompt_text=prompt_text,
        reference_asset_ids=reference_ids,
    )
    now = _db_now(db)
    attempt = TaskItemAttempt(
        id=attempt_id,
        task_item_id=item.id,
        attempt_no=1,
        idempotency_key=attempt_id,
        status="prepared",
        channel_id=runtime_options.channel_id,
        model_id=runtime_options.model_id,
        model_version=prompt_version,
        started_at=now,
        next_action_at=now,
    )
    db.add(attempt)
    db.flush()

    context = GenerationAttemptContext(
        attempt_id=attempt_id,
        task_id=task.id,
        task_item_id=item.id,
        owner_user_id=owner.id,
        prompt_package_id=package.id,
        prompt_item_id=prompt_item.id,
        package_digest_version=package.package_digest_version,
        package_digest=bytes(package.package_digest),
        request_digest_version=command.request_digest_version,
        request_digest=command.request_digest,
        prompt_package_version=prompt_version,
        image_type=command.image_type,
        prompt_text=command.prompt_text,
        reference_asset_ids=[str(asset_id) for asset_id in reference_ids],
        excluded_reference_aliases=list(prompt_item.excluded_reference_aliases),
        original_object_key=f"generated/{attempt_id}/original",
        preview_object_key=f"generated/{attempt_id}/preview.webp",
        save_state="awaiting_result",
    )
    db.add(context)
    db.flush()

    _schedule_generation_action(
        db,
        task_id=task.id,
        task_item_id=item.id,
        attempt_id=attempt_id,
        sequence=1,
        action_kind="generation.submit.v1",
        due_at=now,
        request_digest=command.request_digest,
    )
    item.status = "running"
    item.error_code = None
    item.updated_at = now
    if task.status != "reconciling":
        task.status = "generating"
    task.updated_at = now
    return attempt_id


class GenerationActionRuntime:
    """Builds one-call Action hooks around GenerationPort and private Storage."""

    def __init__(
        self,
        port: GenerationPort,
        storage: Storage,
        *,
        options: GenerationRuntimeOptions | None = None,
    ) -> None:
        self._port = port
        self._storage = storage
        self._options = options or GenerationRuntimeOptions()

    def registrations(self) -> Mapping[str, ActionRegistration]:
        registrations: dict[str, ActionRegistration] = {}
        for kind in GENERATION_ACTION_KINDS:
            registrations[kind] = ActionRegistration(
                kind=kind,
                prepare=lambda db, action, action_kind=kind: self.prepare(
                    db, action, action_kind
                ),
                execute=self.execute,
                finalize=self.finalize,
                recover=self.recover,
                scope_kinds=frozenset({"attempt"}),
            )
        return registrations

    def prepare(self, db: Session, action: Action, kind: str | None = None) -> ActionInput:
        action_kind = kind or action.action_kind
        if (
            action.scope_kind != "attempt"
            or action.attempt_id is None
            or action.scope_id != action.attempt_id
            or action.task_item_id is None
            or action.action_kind != action_kind
        ):
            raise GenerationRuntimeConflict("generation Action scope is invalid")
        context = db.execute(
            select(GenerationAttemptContext).where(
                GenerationAttemptContext.attempt_id == action.attempt_id
            )
        ).scalar_one_or_none()
        attempt = db.execute(
            select(TaskItemAttempt).where(
                TaskItemAttempt.id == action.attempt_id,
                TaskItemAttempt.task_item_id == action.task_item_id,
            )
        ).scalar_one_or_none()
        if (
            context is None
            or attempt is None
            or context.task_id != action.task_id
            or context.task_item_id != action.task_item_id
            or attempt.idempotency_key != context.attempt_id
        ):
            raise GenerationRuntimeConflict("generation attempt context is missing")

        if action_kind == "generation.submit.v1":
            granted = _reserve_capacity_if_available(
                db,
                context,
                capacity_limit=self._options.global_capacity,
            )
            payload: dict[str, object] = {
                "capacity_granted": granted,
                "request_key": str(context.attempt_id),
                "image_type": context.image_type,
                "prompt_package_version": context.prompt_package_version,
                "prompt_text": context.prompt_text,
                "reference_asset_ids": tuple(context.reference_asset_ids),
            }
        elif action_kind in {"generation.recover.v1", "generation.submit.v1"}:
            payload = {"request_key": str(context.attempt_id)}
        elif action_kind == "generation.query.v1":
            if attempt.provider_job_id is None:
                raise GenerationRuntimeConflict("provider job reference is missing")
            payload = {"provider_job_id": attempt.provider_job_id}
        elif action_kind == "generation.download.v1":
            if (
                attempt.provider_result_ref is None
                or context.result_digest is None
                or context.save_state not in {"download_pending", "original_stored"}
            ):
                raise GenerationRuntimeConflict("generation result is not ready to download")
            payload = {
                "result_ref": attempt.provider_result_ref,
                "result_digest": bytes(context.result_digest),
                "original_object_key": context.original_object_key,
            }
        elif action_kind == "generation.save.v1":
            if (
                context.save_state not in {"original_stored", "attached"}
                or context.original_sha256 is None
            ):
                raise GenerationRuntimeConflict("generation original is not stored")
            payload = {
                "original_object_key": context.original_object_key,
                "preview_object_key": context.preview_object_key,
                "original_sha256": context.original_sha256,
            }
        else:
            raise GenerationRuntimeConflict("unsupported generation Action kind")

        return ActionInput(
            action_id=action.id,
            task_id=action.task_id,
            scope_kind=action.scope_kind,
            scope_id=action.scope_id,
            action_kind=action.action_kind,
            sequence=action.sequence,
            input_digest_version=action.input_digest_version,
            input_digest=bytes(action.input_digest),
            payload=payload,
            task_item_id=action.task_item_id,
            attempt_id=action.attempt_id,
        )

    def execute(self, action_input: ActionInput) -> ActionExecutionResult:
        kind = action_input.action_kind
        payload = _payload(action_input)
        if kind == "generation.submit.v1":
            return self._submit(payload)
        if kind == "generation.recover.v1":
            return self._recover_request(payload)
        if kind == "generation.query.v1":
            return self._query(payload)
        if kind == "generation.download.v1":
            return self._download(payload)
        if kind == "generation.save.v1":
            return self._save(payload)
        return ActionExecutionResult("definitely_rejected", safe_reason_code="ACTION_KIND_INVALID")

    def recover(self, action_input: ActionInput) -> ActionExecutionResult:
        """Repeat only idempotent recovery/read/save steps after an expired lease."""

        if action_input.action_kind == "generation.submit.v1":
            return self._recover_request(_payload(action_input))
        # query and download are read-only; save uses deterministic immutable keys.
        return self.execute(action_input)

    def finalize(
        self,
        db: Session,
        action: Action,
        action_input: ActionInput,
        result: ActionExecutionResult,
    ) -> None:
        context = db.execute(
            select(GenerationAttemptContext).where(
                GenerationAttemptContext.attempt_id == action.attempt_id
            )
        ).scalar_one_or_none()
        attempt = db.execute(
            select(TaskItemAttempt).where(TaskItemAttempt.id == action.attempt_id)
        ).scalar_one_or_none()
        item = db.execute(
            select(TaskItem).where(TaskItem.id == action.task_item_id)
        ).scalar_one_or_none()
        task = db.execute(select(Task).where(Task.id == action.task_id)).scalar_one_or_none()
        if context is None or attempt is None or item is None or task is None:
            raise GenerationRuntimeConflict("generation finalize scope is missing")
        if (
            context.task_id != task.id
            or context.task_item_id != item.id
            or attempt.task_item_id != item.id
            or item.task_id != task.id
        ):
            raise GenerationRuntimeConflict("generation finalize identity changed")

        value = result.value if isinstance(result.value, Mapping) else {}
        result_kind = value.get("kind")
        if result.outcome == "accepted":
            self._finalize_accepted(db, action, context, attempt, item, task, value)
        elif result.outcome == "definitely_rejected":
            self._fail_attempt(
                db,
                context,
                attempt,
                item,
                task,
                result.safe_reason_code or _safe_reason(value.get("reason_code")),
            )
        elif result.outcome == "safe_retry":
            self._finalize_safe_retry(
                db,
                action,
                context,
                attempt,
                item,
                task,
                result.safe_reason_code or "GENERATION_STEP_RETRY",
                result.retry_after_seconds or GENERATION_RETRY_SECONDS,
            )
        else:
            self._finalize_unknown(
                db,
                action,
                context,
                attempt,
                item,
                result.safe_reason_code or "GENERATION_OUTCOME_UNKNOWN",
                result_kind=result_kind,
            )

    def _submit(self, payload: Mapping[str, object]) -> ActionExecutionResult:
        if payload.get("capacity_granted") is not True:
            return ActionExecutionResult(
                "safe_retry",
                safe_reason_code="GENERATION_CAPACITY_FULL",
                retry_after_seconds=GENERATION_RETRY_SECONDS,
            )
        try:
            command = _command_from_payload(payload)
            submitted = self._port.submit(command)
        except GenerationIdempotencyConflict:
            return ActionExecutionResult(
                "definitely_rejected", safe_reason_code="GENERATION_IDEMPOTENCY_CONFLICT"
            )
        except GenerationPortError as exc:
            return ActionExecutionResult(
                "unknown", safe_reason_code=exc.reason_code, value={"kind": "submit_unknown"}
            )
        if isinstance(submitted, GenerationAccepted):
            return ActionExecutionResult(
                "accepted",
                value={
                    "kind": "job_accepted",
                    "provider_job_id": submitted.provider_job_id,
                    "delay_seconds": max(1, submitted.poll_after_seconds),
                },
            )
        if isinstance(submitted, GenerationDefinitelyRejected):
            return ActionExecutionResult(
                "definitely_rejected",
                safe_reason_code=submitted.reason_code,
                value={"kind": "submit_rejected"},
            )
        if isinstance(submitted, GenerationUnknown):
            return ActionExecutionResult(
                "unknown",
                safe_reason_code=submitted.reason_code,
                value={
                    "kind": "submit_unknown",
                    "delay_seconds": submitted.reconcile_after_seconds,
                },
            )
        return ActionExecutionResult(
            "unknown",
            safe_reason_code="GENERATION_RESULT_INVALID",
            value={"kind": "submit_unknown"},
        )

    def _recover_request(self, payload: Mapping[str, object]) -> ActionExecutionResult:
        request_key = _required_uuid(payload, "request_key")
        try:
            recovered = self._port.recover(request_key)
        except GenerationPortError as exc:
            return ActionExecutionResult("unknown", safe_reason_code=exc.reason_code)
        if recovered.request_key != request_key:
            return ActionExecutionResult(
                "unknown", safe_reason_code="GENERATION_RECOVERY_KEY_MISMATCH"
            )
        if isinstance(recovered, GenerationRecoveryFound):
            return ActionExecutionResult(
                "accepted",
                value={
                    "kind": "job_recovered",
                    "provider_job_id": recovered.provider_job_id,
                    "delay_seconds": GENERATION_RETRY_SECONDS,
                },
            )
        if isinstance(recovered, GenerationRecoveryDefinitelyNotFound):
            return ActionExecutionResult(
                "safe_retry",
                safe_reason_code="GENERATION_NOT_FOUND_CONFIRMED",
                retry_after_seconds=GENERATION_RETRY_SECONDS,
            )
        if isinstance(recovered, GenerationRecoveryUnknown):
            return ActionExecutionResult("unknown", safe_reason_code=recovered.reason_code)
        if isinstance(recovered, GenerationRecoveryUnsupported):
            return ActionExecutionResult("unknown", safe_reason_code=recovered.reason_code)
        return ActionExecutionResult("unknown", safe_reason_code="GENERATION_RECOVERY_INVALID")

    def _query(self, payload: Mapping[str, object]) -> ActionExecutionResult:
        provider_job_id = _required_text(payload, "provider_job_id", 320)
        try:
            queried = self._port.query(provider_job_id)
        except GenerationQueryTemporaryError as exc:
            return ActionExecutionResult(
                "safe_retry",
                safe_reason_code=exc.reason_code,
                retry_after_seconds=GENERATION_RETRY_SECONDS,
            )
        except GenerationPortError as exc:
            return ActionExecutionResult("unknown", safe_reason_code=exc.reason_code)
        if isinstance(queried, GenerationQueryPending):
            if queried.provider_job_id != provider_job_id:
                return ActionExecutionResult(
                    "unknown", safe_reason_code="GENERATION_PROVIDER_JOB_MISMATCH"
                )
            return ActionExecutionResult(
                "accepted",
                value={
                    "kind": "job_pending",
                    "provider_job_id": queried.provider_job_id,
                    "delay_seconds": max(1, queried.poll_after_seconds),
                },
            )
        if isinstance(queried, GenerationQuerySucceeded):
            if queried.provider_job_id != provider_job_id:
                return ActionExecutionResult(
                    "unknown", safe_reason_code="GENERATION_PROVIDER_JOB_MISMATCH"
                )
            return ActionExecutionResult(
                "accepted",
                value={
                    "kind": "job_succeeded",
                    "provider_job_id": queried.provider_job_id,
                    "result_ref": queried.result_ref,
                    "result_digest": queried.result_digest,
                },
            )
        if isinstance(queried, GenerationQueryFailed):
            if queried.provider_job_id != provider_job_id:
                return ActionExecutionResult(
                    "unknown", safe_reason_code="GENERATION_PROVIDER_JOB_MISMATCH"
                )
            return ActionExecutionResult(
                "definitely_rejected",
                safe_reason_code=queried.reason_code,
                value={"kind": "job_failed"},
            )
        if isinstance(queried, GenerationQueryUnknown):
            if queried.provider_job_id != provider_job_id:
                return ActionExecutionResult(
                    "unknown", safe_reason_code="GENERATION_PROVIDER_JOB_MISMATCH"
                )
            return ActionExecutionResult(
                "unknown", safe_reason_code=queried.reason_code, value={"kind": "query_unknown"}
            )
        return ActionExecutionResult("unknown", safe_reason_code="GENERATION_QUERY_INVALID")

    def _download(self, payload: Mapping[str, object]) -> ActionExecutionResult:
        result_ref = _required_text(payload, "result_ref", 640)
        expected_digest = payload.get("result_digest")
        object_key = _required_text(payload, "original_object_key", 512)
        if not isinstance(expected_digest, bytes) or len(expected_digest) != 32:
            return ActionExecutionResult(
                "definitely_rejected", safe_reason_code="GENERATION_RESULT_DIGEST_INVALID"
            )
        try:
            raw = self._port.download(result_ref)
        except GenerationDownloadTemporaryError as exc:
            return ActionExecutionResult(
                "safe_retry",
                safe_reason_code=exc.reason_code,
                retry_after_seconds=GENERATION_RETRY_SECONDS,
            )
        except GenerationDownloadError as exc:
            return ActionExecutionResult("definitely_rejected", safe_reason_code=exc.reason_code)
        except GenerationPortError as exc:
            return ActionExecutionResult("unknown", safe_reason_code=exc.reason_code)
        try:
            rendered = validate_and_render_generation_output(raw)
        except GenerationImageError as exc:
            return ActionExecutionResult("definitely_rejected", safe_reason_code=exc.reason_code)
        if rendered.original_sha256 != expected_digest:
            return ActionExecutionResult(
                "definitely_rejected", safe_reason_code="GENERATION_RESULT_DIGEST_MISMATCH"
            )
        stat = self._storage.put_immutable(
            object_key,
            BytesIO(rendered.original_bytes),
            {
                "asset_kind": "generation_original",
                "sha256": rendered.original_sha256.hex(),
                "mime_type": rendered.original_mime_type,
                "width": rendered.width,
                "height": rendered.height,
            },
        )
        if stat.size_bytes != len(raw) or stat.sha256 != rendered.original_sha256.hex():
            return ActionExecutionResult(
                "unknown", safe_reason_code="GENERATION_ORIGINAL_STORAGE_MISMATCH"
            )
        return ActionExecutionResult(
            "accepted",
            value={
                "kind": "original_stored",
                "sha256": rendered.original_sha256.hex(),
                "size_bytes": stat.size_bytes,
                "mime_type": rendered.original_mime_type,
                "width": rendered.width,
                "height": rendered.height,
            },
        )

    def _save(self, payload: Mapping[str, object]) -> ActionExecutionResult:
        original_key = _required_text(payload, "original_object_key", 512)
        preview_key = _required_text(payload, "preview_object_key", 512)
        expected_sha256 = _required_text(payload, "original_sha256", 64)
        try:
            with self._storage.open_reader(original_key) as source:
                raw = _read_bounded_image(source)
        except Exception:
            return ActionExecutionResult(
                "unknown", safe_reason_code="GENERATION_ORIGINAL_READ_FAILED"
            )
        if len(raw) > MAX_GENERATION_IMAGE_BYTES:
            return ActionExecutionResult(
                "definitely_rejected", safe_reason_code="GENERATION_IMAGE_TOO_LARGE"
            )
        try:
            rendered = validate_and_render_generation_output(raw)
        except GenerationImageError as exc:
            return ActionExecutionResult("definitely_rejected", safe_reason_code=exc.reason_code)
        if rendered.original_sha256.hex() != expected_sha256:
            return ActionExecutionResult(
                "definitely_rejected", safe_reason_code="GENERATION_ORIGINAL_DIGEST_MISMATCH"
            )
        stat = self._storage.put_immutable(
            preview_key,
            BytesIO(rendered.preview_bytes),
            {
                "asset_kind": "generation_preview",
                "sha256": rendered.preview_sha256.hex(),
                "mime_type": "image/webp",
                "width": rendered.preview_width,
                "height": rendered.preview_height,
            },
        )
        if (
            stat.size_bytes != len(rendered.preview_bytes)
            or stat.sha256 != rendered.preview_sha256.hex()
        ):
            return ActionExecutionResult(
                "unknown", safe_reason_code="GENERATION_PREVIEW_STORAGE_MISMATCH"
            )
        return ActionExecutionResult(
            "accepted",
            value={
                "kind": "preview_stored",
                "preview_sha256": rendered.preview_sha256.hex(),
                "preview_size_bytes": stat.size_bytes,
                "preview_width": rendered.preview_width,
                "preview_height": rendered.preview_height,
            },
        )

    def _finalize_accepted(
        self,
        db: Session,
        action: Action,
        context: GenerationAttemptContext,
        attempt: TaskItemAttempt,
        item: TaskItem,
        task: Task,
        value: Mapping[str, object],
    ) -> None:
        kind = value.get("kind")
        now = _db_now(db)
        if kind in {"job_accepted", "job_recovered"}:
            provider_job_id = _required_text(value, "provider_job_id", 320)
            attempt.provider_job_id = provider_job_id
            attempt.status = "submitted"
            attempt.next_action_at = _schedule_generation_action(
                db,
                task_id=task.id,
                task_item_id=item.id,
                attempt_id=attempt.id,
                sequence=action.sequence + 1,
                action_kind="generation.query.v1",
                due_at=now + timedelta(seconds=_safe_delay(value.get("delay_seconds"))),
                request_digest=context.request_digest,
                provider_job_id=provider_job_id,
            ).due_at
            item.status = "running"
        elif kind == "job_pending":
            provider_job_id = _required_text(value, "provider_job_id", 320)
            attempt.provider_job_id = provider_job_id
            attempt.status = "polling"
            attempt.next_action_at = _schedule_generation_action(
                db,
                task_id=task.id,
                task_item_id=item.id,
                attempt_id=attempt.id,
                sequence=action.sequence + 1,
                action_kind="generation.query.v1",
                due_at=now + timedelta(seconds=_safe_delay(value.get("delay_seconds"))),
                request_digest=context.request_digest,
                provider_job_id=provider_job_id,
            ).due_at
            item.status = "running"
        elif kind == "job_succeeded":
            provider_job_id = _required_text(value, "provider_job_id", 320)
            result_ref = _required_text(value, "result_ref", 640)
            result_digest = value.get("result_digest")
            if not isinstance(result_digest, bytes) or len(result_digest) != 32:
                raise GenerationRuntimeConflict("provider result digest is invalid")
            attempt.provider_job_id = provider_job_id
            attempt.provider_result_ref = result_ref
            attempt.status = "saving"
            attempt.next_action_at = None
            context.result_digest = bytes(result_digest)
            context.save_state = "download_pending"
            item.status = "saving"
            attempt.next_action_at = _schedule_generation_action(
                db,
                task_id=task.id,
                task_item_id=item.id,
                attempt_id=attempt.id,
                sequence=action.sequence + 1,
                action_kind="generation.download.v1",
                due_at=now,
                request_digest=context.request_digest,
                result_ref=result_ref,
                result_digest=result_digest.hex(),
            ).due_at
        elif kind == "original_stored":
            original_sha256 = _required_text(value, "sha256", 64)
            context.original_sha256 = original_sha256
            context.original_size_bytes = _required_int(value, "size_bytes", minimum=1)
            context.original_mime_type = _required_text(value, "mime_type", 32)
            context.original_width = _required_int(value, "width", minimum=1)
            context.original_height = _required_int(value, "height", minimum=1)
            context.save_state = "original_stored"
            attempt.status = "saving"
            item.status = "saving"
            attempt.next_action_at = _schedule_generation_action(
                db,
                task_id=task.id,
                task_item_id=item.id,
                attempt_id=attempt.id,
                sequence=action.sequence + 1,
                action_kind="generation.save.v1",
                due_at=now,
                request_digest=context.request_digest,
                original_sha256=original_sha256,
            ).due_at
        elif kind == "preview_stored":
            self._complete_item(db, context, attempt, item, task, value)
            attempt.next_action_at = None
        else:
            raise GenerationRuntimeConflict("accepted generation result kind is invalid")
        attempt.updated_at = now
        item.updated_at = now
        task.updated_at = now
        _aggregate_task(db, task)

    def _finalize_safe_retry(
        self,
        db: Session,
        action: Action,
        context: GenerationAttemptContext,
        attempt: TaskItemAttempt,
        item: TaskItem,
        task: Task,
        reason_code: str,
        delay_seconds: int,
    ) -> None:
        kind = action.action_kind
        retry_kind = kind
        if kind == "generation.recover.v1":
            # The adapter explicitly proved that this stable request key was
            # not accepted. A new Action may safely submit the same attempt key.
            retry_kind = "generation.submit.v1"
            attempt.status = "prepared"
            item.status = "running"
        elif kind == "generation.query.v1":
            attempt.status = "polling"
            item.status = "running"
        elif kind == "generation.submit.v1":
            attempt.status = "prepared"
            item.status = "running"
        elif kind in {"generation.download.v1", "generation.save.v1"}:
            attempt.status = "saving"
            item.status = "saving"
        else:
            raise GenerationRuntimeConflict("safe retry was returned for an unsupported action")
        due_at = _db_now(db) + timedelta(seconds=delay_seconds + 1)
        next_action = _schedule_generation_action(
            db,
            task_id=action.task_id,
            task_item_id=item.id,
            attempt_id=attempt.id,
            sequence=action.sequence + 1,
            action_kind=retry_kind,
            due_at=due_at,
            request_digest=context.request_digest,
            retry_reason=reason_code,
        )
        attempt.next_action_at = next_action.due_at
        attempt.updated_at = _db_now(db)
        item.updated_at = attempt.updated_at
        _aggregate_task(db, task)

    def _finalize_unknown(
        self,
        db: Session,
        action: Action,
        context: GenerationAttemptContext,
        attempt: TaskItemAttempt,
        item: TaskItem,
        reason_code: str,
        *,
        result_kind: object,
    ) -> None:
        kind = action.action_kind
        # The ActionProcessor will mark this attempt/item/task reconciling after
        # this hook returns. Persist an explicit next recovery step first, except
        # for unsupported provider recovery, which requires operator disposition.
        if kind == "generation.submit.v1":
            next_kind = "generation.recover.v1"
        elif kind == "generation.recover.v1":
            if reason_code == "RECOVERY_UNSUPPORTED":
                attempt.next_action_at = None
                return
            next_kind = "generation.recover.v1"
        elif kind in {
            "generation.query.v1",
            "generation.download.v1",
        }:
            next_kind = kind
        elif kind == "generation.save.v1":
            next_kind = (
                "generation.download.v1"
                if reason_code == "GENERATION_ORIGINAL_READ_FAILED"
                else "generation.save.v1"
            )
        else:
            raise GenerationRuntimeConflict("unknown generation Action kind")
        now = _db_now(db)
        next_action = _schedule_generation_action(
            db,
            task_id=action.task_id,
            task_item_id=item.id,
            attempt_id=attempt.id,
            sequence=action.sequence + 1,
            action_kind=next_kind,
            due_at=now + timedelta(seconds=GENERATION_RECOVER_SECONDS),
            request_digest=context.request_digest,
            recovery_reason=reason_code,
            result_kind=str(result_kind or "unknown"),
        )
        attempt.next_action_at = next_action.due_at
        attempt.updated_at = now

    def _fail_attempt(
        self,
        db: Session,
        context: GenerationAttemptContext,
        attempt: TaskItemAttempt,
        item: TaskItem,
        task: Task,
        reason_code: str,
    ) -> None:
        now = _db_now(db)
        attempt.status = "failed"
        attempt.failure_code = reason_code
        attempt.next_action_at = None
        attempt.completed_at = now
        attempt.updated_at = now
        item.status = "failed"
        item.error_code = reason_code
        item.preview_ready = False
        item.updated_at = now
        context.save_state = "failed"
        context.updated_at = now
        _release_capacity(db, attempt.id)
        _aggregate_task(db, task)

    def _complete_item(
        self,
        db: Session,
        context: GenerationAttemptContext,
        attempt: TaskItemAttempt,
        item: TaskItem,
        task: Task,
        value: Mapping[str, object],
    ) -> None:
        original_key = context.original_object_key
        preview_key = context.preview_object_key
        original_id = uuid5(_ASSET_NAMESPACE, f"{attempt.id}:original")
        preview_id = uuid5(_ASSET_NAMESPACE, f"{attempt.id}:preview")
        original_sha = context.original_sha256
        preview_sha = _required_text(value, "preview_sha256", 64)
        preview_size = _required_int(value, "preview_size_bytes", minimum=1)
        preview_width = _required_int(value, "preview_width", minimum=1)
        preview_height = _required_int(value, "preview_height", minimum=1)
        if (
            original_sha is None
            or context.original_size_bytes is None
            or context.original_mime_type is None
            or context.original_width is None
            or context.original_height is None
        ):
            raise GenerationRuntimeConflict("stored original metadata is incomplete")

        # Capacity serialization/release precedes every ImageAsset lookup or
        # insert, following the shared Action → Capacity → Asset lock order.
        _release_capacity(db, attempt.id)

        original_asset = _asset_by_deterministic_id(db, original_id)
        if original_asset is None:
            original_asset = ImageAsset(
                id=original_id,
                owner_user_id=context.owner_user_id,
                asset_kind="generation_original",
                object_key=original_key,
                content_sha256=original_sha,
                mime_type=context.original_mime_type,
                size_bytes=context.original_size_bytes,
                width=context.original_width,
                height=context.original_height,
            )
            db.add(original_asset)
        elif not _same_asset(
            original_asset,
            owner_user_id=context.owner_user_id,
            asset_kind="generation_original",
            object_key=original_key,
            content_sha256=original_sha,
        ):
            raise GenerationRuntimeConflict("original asset identity conflicts")

        preview_asset = _asset_by_deterministic_id(db, preview_id)
        if preview_asset is None:
            preview_asset = ImageAsset(
                id=preview_id,
                owner_user_id=context.owner_user_id,
                asset_kind="generation_preview",
                object_key=preview_key,
                content_sha256=preview_sha,
                mime_type="image/webp",
                size_bytes=preview_size,
                width=preview_width,
                height=preview_height,
            )
            db.add(preview_asset)
        elif not _same_asset(
            preview_asset,
            owner_user_id=context.owner_user_id,
            asset_kind="generation_preview",
            object_key=preview_key,
            content_sha256=preview_sha,
        ):
            raise GenerationRuntimeConflict("preview asset identity conflicts")
        db.flush()

        existing_result = db.get(GenerationResult, attempt.id)
        if existing_result is None:
            db.add(
                GenerationResult(
                    attempt_id=attempt.id,
                    task_id=task.id,
                    task_item_id=item.id,
                    owner_user_id=context.owner_user_id,
                    original_asset_id=original_id,
                    preview_asset_id=preview_id,
                )
            )
        elif (
            existing_result.original_asset_id != original_id
            or existing_result.preview_asset_id != preview_id
        ):
            raise GenerationRuntimeConflict("generation result association conflicts")

        context.preview_size_bytes = preview_size
        context.preview_sha256 = preview_sha
        context.preview_width = preview_width
        context.preview_height = preview_height
        context.save_state = "attached"
        context.updated_at = _db_now(db)
        attempt.status = "completed"
        attempt.failure_code = None
        attempt.completed_at = context.updated_at
        attempt.updated_at = context.updated_at
        item.image_id = original_id
        item.preview_ready = True
        item.width = context.original_width
        item.height = context.original_height
        item.status = "succeeded"
        item.error_code = None
        item.updated_at = context.updated_at
        _aggregate_task(db, task)


def _valid_prompt_item(
    prompt_item: TaskPromptItem,
    item: TaskItem,
    task: Task,
    package: TaskPromptPackage,
) -> bool:
    if (
        prompt_item.task_item_id != item.id
        or prompt_item.task_id != item.task_id
        or prompt_item.image_type != item.image_type
        or package.id != prompt_item.package_id
        or package.task_id != task.id
        or item.task_id != task.id
        or not isinstance(package.validator_version, str)
        or (
            package.validator_version != PROMPT_PACKAGE_VALIDATOR_VERSION
            and package.validator_version not in PROMPT_PACKAGE_LEGACY_VALIDATOR_VERSIONS
        )
        or not prompt_item.design_prompt.strip()
        or not prompt_item.identity_prompt.strip()
        or not isinstance(prompt_item.allowed_reference_asset_ids, list)
        or not isinstance(prompt_item.allowed_reference_aliases, list)
        or not isinstance(prompt_item.excluded_reference_aliases, list)
        or any(not isinstance(alias, str) for alias in prompt_item.allowed_reference_aliases)
        or prompt_item.excluded_reference_aliases
    ):
        return False
    try:
        expected_aliases, expected_ids = _task_snapshot_reference_assets(task)
        reference_ids = _parse_allowed_reference_ids(prompt_item.allowed_reference_asset_ids)
        if (
            prompt_item.allowed_reference_aliases != list(expected_aliases)
            or reference_ids != expected_ids
            or prompt_item.identity_anchor_asset_id != expected_ids[0]
        ):
            return False
    except (GenerationRuntimeConflict, TypeError, ValueError):
        return False
    return True


def _task_snapshot_reference_assets(task: Task) -> tuple[tuple[str, ...], tuple[UUID, ...]]:
    snapshots = task.reference_assets_snapshot
    if not isinstance(snapshots, list) or not 1 <= len(snapshots) <= 4:
        raise GenerationRuntimeConflict("task reference snapshot is invalid")
    expected_slots = ("PRIMARY", "AUX01", "AUX02", "AUX03")[: len(snapshots)]
    aliases: list[str] = []
    asset_ids: list[UUID] = []
    for position, (entry, expected_slot) in enumerate(zip(snapshots, expected_slots, strict=True)):
        if (
            not isinstance(entry, dict)
            or isinstance(entry.get("position"), bool)
            or entry.get("position") != position
            or entry.get("slot") != expected_slot
            or not isinstance(entry.get("model_asset_id"), str)
        ):
            raise GenerationRuntimeConflict("task reference snapshot is invalid")
        aliases.append(expected_slot)
        asset_ids.append(UUID(entry["model_asset_id"]))
    if len(set(asset_ids)) != len(asset_ids):
        raise GenerationRuntimeConflict("task reference snapshot is invalid")
    return tuple(aliases), tuple(asset_ids)


def _parse_allowed_reference_ids(raw_ids: object) -> tuple[UUID, ...]:
    if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= 4:
        raise GenerationRuntimeConflict("allowed reference list is invalid")
    try:
        ids = tuple(UUID(value) for value in raw_ids if isinstance(value, str))
    except (TypeError, ValueError):
        raise GenerationRuntimeConflict("allowed reference list is invalid") from None
    if len(ids) != len(raw_ids) or len(set(ids)) != len(ids):
        raise GenerationRuntimeConflict("allowed reference list is invalid")
    return ids


def _command_from_payload(payload: Mapping[str, object]) -> GenerationCommand:
    request_key = _required_uuid(payload, "request_key")
    image_type = _required_text(payload, "image_type", 80)
    package_version = _required_text(payload, "prompt_package_version", 120)
    prompt_text = payload.get("prompt_text")
    raw_refs = payload.get("reference_asset_ids")
    if not isinstance(prompt_text, str) or not isinstance(raw_refs, tuple):
        raise GenerationRuntimeConflict("generation command is malformed")
    try:
        refs = tuple(UUID(value) for value in raw_refs if isinstance(value, str))
    except (TypeError, ValueError):
        raise GenerationRuntimeConflict("generation reference list is malformed") from None
    if len(refs) != len(raw_refs):
        raise GenerationRuntimeConflict("generation reference list is malformed")
    return GenerationCommand(
        request_key=request_key,
        image_type=image_type,
        prompt_package_version=package_version,
        prompt_text=prompt_text,
        reference_asset_ids=refs,
    )


def _reserve_capacity_if_available(
    db: Session,
    context: GenerationAttemptContext,
    *,
    capacity_limit: int,
) -> bool:
    control = db.execute(
        select(GenerationCapacityControl)
        .where(GenerationCapacityControl.id == GENERATION_CAPACITY_CONTROL_ID)
        .with_for_update()
    ).scalar_one_or_none()
    if control is None:
        raise GenerationRuntimeError("generation capacity control is not seeded")
    reservation = db.execute(
        select(GenerationCapacityReservation).where(
            GenerationCapacityReservation.attempt_id == context.attempt_id
        )
    ).scalar_one_or_none()
    if reservation is not None:
        return reservation.status == "active"
    active = db.scalar(
        select(func.count(GenerationCapacityReservation.id)).where(
            GenerationCapacityReservation.status == "active"
        )
    )
    if int(active or 0) >= capacity_limit:
        return False
    db.add(
        GenerationCapacityReservation(
            attempt_id=context.attempt_id,
            task_id=context.task_id,
            task_item_id=context.task_item_id,
            owner_user_id=context.owner_user_id,
            status="active",
        )
    )
    db.flush()
    return True


def _release_capacity(db: Session, attempt_id: UUID) -> None:
    control = db.execute(
        select(GenerationCapacityControl)
        .where(GenerationCapacityControl.id == GENERATION_CAPACITY_CONTROL_ID)
        .with_for_update()
    ).scalar_one_or_none()
    if control is None:
        raise GenerationRuntimeError("generation capacity control is not seeded")
    reservation = db.execute(
        select(GenerationCapacityReservation)
        .where(GenerationCapacityReservation.attempt_id == attempt_id)
        .with_for_update()
    ).scalar_one_or_none()
    if reservation is not None and reservation.status == "active":
        reservation.status = "released"
        reservation.released_at = _db_now(db)


def _schedule_generation_action(
    db: Session,
    *,
    task_id: UUID,
    task_item_id: UUID,
    attempt_id: UUID,
    sequence: int,
    action_kind: str,
    due_at,
    request_digest: bytes,
    **extra: object,
) -> Action:
    digest_document = {
        "action_kind": action_kind,
        "attempt_id": str(attempt_id),
        "request_digest": request_digest.hex(),
        "sequence": sequence,
        "extra": _canonical_action_value(extra),
    }
    input_digest = hashlib.sha256(
        json.dumps(
            digest_document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).digest()
    return schedule_action_in_transaction(
        db,
        action_id=uuid4(),
        task_id=task_id,
        scope_kind="attempt",
        scope_id=attempt_id,
        action_kind=action_kind,
        sequence=sequence,
        due_at=due_at,
        input_digest_version=_DIGEST_VERSION,
        input_digest=input_digest,
        task_item_id=task_item_id,
        attempt_id=attempt_id,
    )


def _canonical_action_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Mapping):
        return {str(key): _canonical_action_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical_action_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise GenerationRuntimeConflict("generation action digest input is invalid")


def _aggregate_task(db: Session, task: Task) -> None:
    items = (
        db.execute(
            select(TaskItem)
            .where(TaskItem.task_id == task.id)
            .order_by(TaskItem.position)
        )
        .scalars()
        .all()
    )
    if len(items) != task.planned_count:
        raise GenerationRuntimeConflict("TaskItem count differs from planned count")
    statuses = [item.status for item in items]
    succeeded = statuses.count("succeeded")
    failed = statuses.count("failed")
    unresolved = statuses.count("reconciling")
    pending = len(statuses) - succeeded - failed - unresolved
    if len(statuses) != succeeded + failed + pending + unresolved:
        raise GenerationRuntimeConflict("TaskItem aggregation is inconsistent")

    if pending == 0 and unresolved == 0:
        now = _db_now(db)
        task.status = (
            "succeeded" if succeeded == len(items) else
            "failed" if failed == len(items) else
            "partially_succeeded"
        )
        task.terminal_at = now
        task.expires_at = now + timedelta(days=task.retention_days)
        task.updated_at = now
    elif unresolved:
        task.status = "reconciling"
        task.terminal_at = None
        task.expires_at = None
    else:
        task.status = "generating"
        task.terminal_at = None
        task.expires_at = None


def _asset_by_deterministic_id(db: Session, asset_id: UUID) -> ImageAsset | None:
    return db.execute(
        select(ImageAsset).where(ImageAsset.id == asset_id)
    ).scalar_one_or_none()


def _same_asset(
    asset: ImageAsset,
    *,
    owner_user_id: UUID,
    asset_kind: str,
    object_key: str,
    content_sha256: str,
) -> bool:
    return (
        asset.owner_user_id == owner_user_id
        and asset.asset_kind == asset_kind
        and asset.object_key == object_key
        and asset.content_sha256 == content_sha256
    )


def _payload(action_input: ActionInput) -> Mapping[str, object]:
    if not isinstance(action_input.payload, Mapping):
        raise GenerationRuntimeConflict("generation action payload is invalid")
    return cast(Mapping[str, object], action_input.payload)


def _read_bounded_image(source) -> bytes:
    chunks: list[bytes] = []
    remaining = MAX_GENERATION_IMAGE_BYTES + 1
    while remaining > 0:
        chunk = source.read(min(128 * 1024, remaining))
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise GenerationRuntimeError("storage returned an invalid image stream")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _required_uuid(payload: Mapping[str, object], key: str) -> UUID:
    value = payload.get(key)
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            pass
    raise GenerationRuntimeConflict(f"generation {key} is invalid")


def _required_text(payload: Mapping[str, object], key: str, max_length: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise GenerationRuntimeConflict(f"generation {key} is invalid")
    return value


def _required_int(payload: Mapping[str, object], key: str, *, minimum: int) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise GenerationRuntimeConflict(f"generation {key} is invalid")
    return value


def _safe_reason(value: object) -> str:
    if isinstance(value, str) and value[:1].isalpha() and value.replace("_", "").isalnum():
        normalized = value.upper()[:80]
        if normalized and normalized[0].isalpha():
            return normalized
    return "GENERATION_FAILED"


def _safe_delay(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return GENERATION_RETRY_SECONDS
    return max(1, min(value, 3600))


def _db_now(db: Session):
    now = db.scalar(select(func.clock_timestamp()))
    if now is None or now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise GenerationRuntimeError("database clock must return aware UTC")
    return now
