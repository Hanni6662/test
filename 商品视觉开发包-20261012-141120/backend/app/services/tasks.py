from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import BinaryIO, Literal
from uuid import UUID, uuid4, uuid5

from sqlalchemy import Engine, and_, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, aliased, sessionmaker

from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.business_session_models import BusinessSession
from app.db.generation_models import GenerationAttemptContext, GenerationResult
from app.db.models import User
from app.db.outbox_models import OutboxEvent
from app.db.prompt_models import TaskPromptItem, TaskPromptPackage
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ImageAsset, ReferenceSet, ReferenceSetImage
from app.db.task_models import (
    Task,
    TaskIdempotencyRecord,
    TaskItem,
    TaskItemAttempt,
    TaskRecommendationConsumption,
)
from app.integrations.generation.protocol import GenerationCommand
from app.integrations.storage.protocol import Storage, StorageError
from app.schemas.business_sessions import (
    BusinessSessionSelectionRequest,
    DifyOriginalRecommendation,
    chatflow_reference_snapshot_digest,
)
from app.schemas.recommendations import IMAGE_TYPES, RecommendationPublicOptions
from app.schemas.task_history import (
    TaskHistoryEntry,
    TaskHistoryPage,
    TaskRetryAccepted,
    TaskRetryAttempt,
    TaskRetryRequest,
)
from app.schemas.tasks import (
    CustomPreference,
    PreferenceMode,
    TaskAccepted,
    TaskCounts,
    TaskCreateRequest,
    TaskItemPublicError,
    TaskItemResponse,
    TaskResponse,
)
from app.services.generation_retry_policy import is_resolved_submit_unknown, is_unknown_action
from app.services.original_downloads import (
    DownloadLease,
    OriginalDownload,
    prepare_original_download,
)
from app.services.outbox import enqueue_in_transaction
from app.services.prompt_validation import (
    PROMPT_PACKAGE_LEGACY_VALIDATOR_VERSIONS,
    PROMPT_PACKAGE_VALIDATOR_VERSION,
)
from app.services.prompt_workflows import (
    PromptWorkflowUnavailable,
    create_initial_prompt_submission_in_transaction,
)
from app.services.retention_policy import (
    RetentionPolicyUnavailable,
    load_retention_policy_in_transaction,
)
from app.services.trusted_analysis import (
    TRUSTED_ANALYSIS_STATE_KEY,
    TrustedAnalysisSnapshot,
    TrustedAnalysisSnapshotError,
    load_trusted_analysis_snapshot,
)
from app.workers.action_processor import schedule_action_in_transaction
from app.workers.messages import OutboxMessage
from app.workers.processor import HandlerRegistration, PermanentEventFailure

TASK_CREATE_OPERATION = "tasks.create.v2"
CHATFLOW_SELECTION_OPERATION = "chatflow.selection.create.v1"
TASK_PROMPT_EVENT_TYPE = "task.prompt.v1"
TASK_RETRY_OPERATION = "tasks.retry.v1"
REQUEST_DIGEST_VERSION = 1
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[!-~]{1,200}$")
_RETRY_ATTEMPT_NAMESPACE = UUID("867fd8ac-f99b-4ce6-b0a9-1cd755e74a36")
_HISTORY_CURSOR_VERSION = 1
_HISTORY_CURSOR_MAX_LENGTH = 256
_HISTORY_DEFAULT_LIMIT = 20
_HISTORY_MAX_LIMIT = 50
_GENERATION_ACTION_KIND = "generation.submit.v1"
_GENERATION_ACTION_DIGEST_VERSION = 1

_FIELD_NAMES = ("language", "usage_scene", "visual_style", "selling_points")
_ITEM_PUBLIC_ERRORS: dict[str, TaskItemPublicError] = {
    "IMAGE_GENERATION_FAILED": TaskItemPublicError(
        code="IMAGE_GENERATION_FAILED", message="本张图片生成失败，可单独重试。"
    ),
    "V34_PRE_GENERATION_FAILED": TaskItemPublicError(
        code="V34_PRE_GENERATION_FAILED", message="商品身份核对未通过，未发起生图。"
    ),
    "V34_WORKFLOW_ENDED_WITHOUT_GENERATION": TaskItemPublicError(
        code="V34_WORKFLOW_ENDED_WITHOUT_GENERATION",
        message="流程已结束，但未取得生图许可，本次未发起生图。",
    ),
    "GENERATION_PERMIT_EXPIRED_BEFORE_ACCEPTANCE": TaskItemPublicError(
        code="GENERATION_PERMIT_EXPIRED_BEFORE_ACCEPTANCE",
        message="生图许可已过期，本次未发起生图。",
    ),
}


@dataclass(frozen=True, slots=True)
class TaskOptions:
    schema_version: int = 2
    workflow_version: str = "business-task-v1"
    billing_mode: Literal["disabled"] = "disabled"
    billing_policy_version: str = "disabled-v1"
    retention_days: int = 60
    retention_policy_revision: int = 1
    event_operation_version: int = 1
    event_schema_version: int = 1
    idempotency_key_max_length: int = 200
    max_nonterminal_per_owner: int = 2

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValueError("only task schema version 2 is implemented")
        if not self.workflow_version or len(self.workflow_version) > 80:
            raise ValueError("workflow_version must contain 1-80 characters")
        if self.billing_mode != "disabled":
            raise ValueError("only disabled billing is implemented")
        if not self.billing_policy_version or len(self.billing_policy_version) > 80:
            raise ValueError("billing_policy_version must contain 1-80 characters")
        if self.retention_days < 1 or self.retention_policy_revision < 1:
            raise ValueError("task retention values must be positive")
        if self.event_operation_version != 1 or self.event_schema_version != 1:
            raise ValueError("only task prompt event version 1 is implemented")
        if not 1 <= self.idempotency_key_max_length <= 200:
            raise ValueError("idempotency key max length must be between 1 and 200")
        if self.max_nonterminal_per_owner < 1:
            raise ValueError("max_nonterminal_per_owner must be positive")


@dataclass(frozen=True, slots=True)
class _ExpiredReplay:
    task_id: UUID


@dataclass(frozen=True, slots=True)
class _HistoryCursor:
    created_at: datetime
    task_id: UUID


@dataclass(frozen=True, slots=True)
class PreviewStream:
    body: BinaryIO
    mime_type: str
    size_bytes: int


type TaskCreationResult = TaskAccepted | _ExpiredReplay


def process_task_prompt_event(db: Session, event: OutboxEvent, message: OutboxMessage) -> None:
    """Create the first prompt submission inside the task-intent transaction.

    EventProcessor already owns the event and receipt locks. This handler then
    locks the S6 business parents in their frozen order before delegating to the
    pure DB helper; it performs no provider, model, or image I/O.
    """
    if (
        event.event_type != TASK_PROMPT_EVENT_TYPE
        or event.operation_version != 1
        or event.schema_version != 1
        or event.aggregate_id != message.aggregate_id
        or event.payload != {}
        or message.payload != {}
    ):
        raise PermanentEventFailure("TASK_PROMPT_EVENT_INVALID")

    task_hint = db.execute(
        select(Task.owner_user_id, Task.reference_set_id, Task.recommendation_id).where(
            Task.id == event.aggregate_id
        )
    ).one_or_none()
    if task_hint is None:
        raise PermanentEventFailure("TASK_PROMPT_TASK_MISSING")
    owner_user_id, reference_set_id, recommendation_id = task_hint

    owner = db.execute(
        select(User).where(User.id == owner_user_id).with_for_update()
    ).scalar_one_or_none()
    reference_set = db.execute(
        select(ReferenceSet)
        .where(
            ReferenceSet.id == reference_set_id,
            ReferenceSet.owner_user_id == owner_user_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    recommendation = db.execute(
        select(Recommendation)
        .where(
            Recommendation.id == recommendation_id,
            Recommendation.owner_user_id == owner_user_id,
            Recommendation.reference_set_id == reference_set_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    task = db.execute(
        select(Task).where(Task.id == event.aggregate_id).with_for_update()
    ).scalar_one_or_none()
    if (
        owner is None
        or reference_set is None
        or recommendation is None
        or task is None
        or task.owner_user_id != owner.id
        or task.reference_set_id != reference_set.id
        or task.recommendation_id != recommendation.id
    ):
        raise PermanentEventFailure("TASK_PROMPT_PARENT_INVALID")

    try:
        create_initial_prompt_submission_in_transaction(db, task=task)
    except PromptWorkflowUnavailable:
        raise PermanentEventFailure("TASK_PROMPT_STATE_INVALID") from None


def task_prompt_handlers() -> dict[str, HandlerRegistration]:
    """Return the exact task-intent handler set used to gate dispatcher release."""
    return {TASK_PROMPT_EVENT_TYPE: HandlerRegistration(process_task_prompt_event, frozenset({1}))}


def normalize_task_request(request: TaskCreateRequest) -> dict[str, object]:
    """Return the JSON v2 semantic request with image types in contract order."""
    payload = request.model_dump(mode="json")
    requested_types = set(payload["image_types"])
    payload["image_types"] = [
        image_type for image_type in IMAGE_TYPES if image_type in requested_types
    ]
    return payload


def canonical_request_digest(request: TaskCreateRequest) -> bytes:
    canonical_json = json.dumps(
        normalize_task_request(request),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).digest()


def chatflow_selection_digest(
    *,
    business_session_id: UUID,
    reference_asset_digest: bytes,
    normalized_selection: dict[str, object],
) -> bytes:
    if len(reference_asset_digest) != 32:
        raise ValueError("reference_asset_digest must contain a SHA-256 digest")
    canonical = json.dumps(
        {
            "contract_version": "chatflow-handoff-v1",
            "business_session_id": str(business_session_id),
            "reference_asset_digest": reference_asset_digest.hex(),
            "selection": normalized_selection,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def validate_dify_chatflow_task_snapshot(
    task: Task, business_session: BusinessSession
) -> dict[str, object]:
    """Validate the H2 provenance and exact H1 complete-reference snapshot."""
    recommendation_snapshot = task.recommendation_snapshot
    request_snapshot = task.request_snapshot
    chatflow_snapshot = (
        recommendation_snapshot.get("dify_chatflow_snapshot")
        if isinstance(recommendation_snapshot, dict)
        else None
    )
    references = (
        chatflow_snapshot.get("references") if isinstance(chatflow_snapshot, dict) else None
    )
    if (
        task.provenance_kind != "dify_chatflow"
        or task.business_session_id != business_session.id
        or task.recommendation_snapshot_version != "dify-chatflow-recommendation-v1"
        or not isinstance(request_snapshot, dict)
        or request_snapshot.get("business_session_id") != str(business_session.id)
        or request_snapshot.get("reference_asset_digest")
        != business_session.reference_asset_digest.hex()
        or not isinstance(chatflow_snapshot, dict)
        or chatflow_snapshot.get("schema_version") != 1
        or chatflow_snapshot.get("provenance_kind") != "dify_chatflow"
        or chatflow_snapshot.get("business_session_id") != str(business_session.id)
        or chatflow_snapshot.get("recommendation_id") != str(task.recommendation_id)
        or chatflow_snapshot.get("recommendation_revision") != task.recommendation_revision
        or chatflow_snapshot.get("dify_run_id") != business_session.dify_run_id
        or chatflow_snapshot.get("reference_set_id") != str(task.reference_set_id)
        or chatflow_snapshot.get("reference_asset_digest")
        != business_session.reference_asset_digest.hex()
        or not isinstance(references, list)
        or references != task.reference_assets_snapshot
        or "trusted_analysis_snapshot" in recommendation_snapshot
    ):
        raise DomainError(409, "RECOMMENDATION_UNAVAILABLE", "推荐快照暂不可用")
    expected_slots = ("PRIMARY", "AUX01", "AUX02", "AUX03")[: len(references)]
    if not 1 <= len(references) <= 4:
        raise DomainError(409, "REFERENCE_SET_UNAVAILABLE", "参考组资产暂不可用")
    for position, (entry, expected_slot) in enumerate(zip(references, expected_slots, strict=True)):
        if (
            not isinstance(entry, dict)
            or entry.get("position") != position
            or entry.get("slot") != expected_slot
            or not _is_uuid_text(entry.get("reference_set_image_id"))
            or not _is_uuid_text(entry.get("source_asset_id"))
            or not _is_uuid_text(entry.get("model_asset_id"))
            or entry.get("content_sha256") != entry.get("model_sha256")
            or not _is_sha256_text(entry.get("content_sha256"))
            or not _is_sha256_text(entry.get("source_sha256"))
        ):
            raise DomainError(409, "REFERENCE_SET_UNAVAILABLE", "参考组资产暂不可用")
    try:
        digest = bytes.fromhex(chatflow_reference_snapshot_digest(references))
    except Exception:
        raise DomainError(409, "REFERENCE_SET_UNAVAILABLE", "参考组资产暂不可用") from None
    if digest != business_session.reference_asset_digest:
        raise DomainError(409, "REFERENCE_SET_UNAVAILABLE", "参考组资产暂不可用")
    try:
        DifyOriginalRecommendation.model_validate(
            recommendation_snapshot.get("original_recommendation"), strict=True
        )
    except Exception:
        raise DomainError(409, "RECOMMENDATION_UNAVAILABLE", "推荐快照暂不可用") from None
    original = recommendation_snapshot.get("original_recommendation")
    if isinstance(original, dict) and "fixture_id" in original:
        raise DomainError(409, "RECOMMENDATION_UNAVAILABLE", "推荐快照暂不可用")
    return chatflow_snapshot


def _is_uuid_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        UUID(value)
    except (TypeError, ValueError):
        return False
    return True


def _is_sha256_text(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def idempotency_key_hash(value: str, *, maximum_length: int = 200) -> bytes:
    if len(value) > maximum_length or _IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
        raise DomainError(400, "IDEMPOTENCY_KEY_INVALID", "幂等键格式无效")
    return hashlib.sha256(value.encode("ascii")).digest()


def resolve_preferences(
    request: TaskCreateRequest,
    *,
    public_options_value: object,
    original_recommendation: object,
    category: str,
    recommendation_revision: int,
) -> dict[str, object]:
    """Resolve selection codes against this exact saved recommendation snapshot."""
    try:
        public_options = RecommendationPublicOptions.model_validate(public_options_value)
    except Exception:
        raise DomainError(
            409, "RECOMMENDATION_UNAVAILABLE", "推荐内容暂不可用，请重新获取推荐"
        ) from None
    if not isinstance(original_recommendation, dict):
        raise DomainError(409, "RECOMMENDATION_UNAVAILABLE", "推荐内容暂不可用，请重新获取推荐")
    if original_recommendation.get("category_snapshot") != category:
        raise DomainError(409, "RECOMMENDATION_UNAVAILABLE", "推荐内容与参考组不匹配")
    field_texts = original_recommendation.get("field_texts")
    if not isinstance(field_texts, dict):
        raise DomainError(409, "RECOMMENDATION_UNAVAILABLE", "推荐内容暂不可用，请重新获取推荐")

    resolved: dict[str, object] = {}
    for field_name in _FIELD_NAMES:
        selection = getattr(request.preferences, field_name)
        public_field = getattr(public_options, field_name)
        texts = field_texts.get(field_name)
        if (
            not isinstance(texts, list)
            or len(texts) != 3
            or any(not isinstance(text, str) or not text for text in texts)
            or [choice.text for choice in public_field.choices] != texts
        ):
            raise DomainError(409, "RECOMMENDATION_UNAVAILABLE", "推荐内容暂不可用，请重新获取推荐")

        if isinstance(selection, PreferenceMode):
            if selection.mode in {"A", "B", "C"}:
                index = ord(selection.mode) - ord("A")
                resolved[field_name] = {
                    "mode": selection.mode,
                    "text": texts[index],
                    "source": "recommendation_original",
                    "recommendation_revision": recommendation_revision,
                }
            else:
                if not public_field.allow_agent_design:
                    raise DomainError(422, "PREFERENCE_NOT_ALLOWED", "当前推荐不支持自由设计选项")
                resolved[field_name] = {
                    "mode": "D",
                    "kind": "agent_design",
                    "instruction": "由智能体自由设计，不得将未提供的信息表述为事实。",
                }
        elif isinstance(selection, CustomPreference):
            if not public_field.allow_custom:
                raise DomainError(422, "PREFERENCE_NOT_ALLOWED", "当前推荐不支持自定义内容")
            resolved[field_name] = {"mode": "custom", "text": selection.text}
        else:  # pragma: no cover - guarded by the strict Pydantic request model
            raise DomainError(422, "PREFERENCE_INVALID", "偏好选项无效")

    platform = request.preferences.platform
    if platform not in public_options.platform:
        raise DomainError(422, "PLATFORM_NOT_ALLOWED", "所选平台不在当前推荐范围内")
    resolved["platform"] = {"mode": "platform", "value": platform}
    return resolved


def _load_task_analysis_snapshot(
    recommendation: Recommendation,
    *,
    reference_set_id: UUID,
    category: str,
    reference_assets: list[dict[str, object]],
) -> TrustedAnalysisSnapshot:
    state = recommendation.state
    raw_snapshot = state.get(TRUSTED_ANALYSIS_STATE_KEY) if isinstance(state, dict) else None
    try:
        return load_trusted_analysis_snapshot(
            raw_snapshot,
            reference_set_id=reference_set_id,
            recommendation_id=recommendation.id,
            recommendation_revision=recommendation.revision,
            category=category,
            expected_references=reference_assets,
        )
    except TrustedAnalysisSnapshotError:
        raise DomainError(
            409, "RECOMMENDATION_UNAVAILABLE", "推荐内容暂不可用，请重新获取推荐"
        ) from None


class TaskService:
    """S6 task creation and owner-scoped projection; each method owns its DB Session."""

    def __init__(self, engine: Engine, options: TaskOptions) -> None:
        self.options = options
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    def create_task(
        self,
        *,
        owner_user_id: UUID,
        idempotency_key: str,
        request: TaskCreateRequest,
    ) -> TaskAccepted:
        key_hash = idempotency_key_hash(
            idempotency_key, maximum_length=self.options.idempotency_key_max_length
        )
        request_digest = canonical_request_digest(request)
        normalized_request = normalize_task_request(request)
        try:
            result = self._create_in_transaction(
                owner_user_id=owner_user_id,
                key_hash=key_hash,
                request_digest=request_digest,
                normalized_request=normalized_request,
                request=request,
            )
        except DomainError:
            raise
        except IntegrityError:
            recovered = self._recover_submission(
                owner_user_id=owner_user_id,
                key_hash=key_hash,
                request_digest=request_digest,
                recommendation_id=request.recommendation_id,
            )
            if recovered is not None:
                if isinstance(recovered, _ExpiredReplay):
                    raise DomainError(410, "TASK_EXPIRED", "任务已过期，无法恢复") from None
                return recovered
            raise DomainError(
                409, "TASK_SUBMISSION_CONFLICT", "任务提交状态冲突，请重新读取"
            ) from None
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "任务暂时无法创建") from None

        if isinstance(result, _ExpiredReplay):
            raise DomainError(410, "TASK_EXPIRED", "任务已过期，无法恢复")
        return result

    def create_chatflow_task_in_transaction(
        self,
        db: Session,
        *,
        owner_user_id: UUID,
        business_session: BusinessSession,
        reference_set: ReferenceSet,
        recommendation: Recommendation,
        idempotency_key: str,
        request: BusinessSessionSelectionRequest,
        now: datetime,
    ) -> tuple[TaskAccepted, bool]:
        """Create the Dify selection Task inside the caller's locked transaction.

        The caller must already hold User → ReferenceSet → BusinessSession →
        Recommendation locks. This method deliberately does no I/O and does not
        enqueue the legacy task.prompt.v1 intent.
        """
        if (
            business_session.owner_user_id != owner_user_id
            or reference_set.owner_user_id != owner_user_id
            or reference_set.id != business_session.reference_set_id
            or recommendation.owner_user_id != owner_user_id
            or recommendation.reference_set_id != reference_set.id
            or recommendation.business_session_id != business_session.id
            or recommendation.source_kind != "dify_chatflow"
            or recommendation.dify_run_id != business_session.dify_run_id
            or business_session.dify_run_id is None
        ):
            raise DomainError(409, "RECOMMENDATION_UNAVAILABLE", "推荐内容暂不可用")
        if request.recommendation_id != recommendation.id:
            raise DomainError(409, "RECOMMENDATION_STALE", "推荐版本已变化，请重新获取")
        if request.recommendation_revision != recommendation.revision:
            raise DomainError(409, "RECOMMENDATION_STALE", "推荐版本已变化，请重新获取")

        key_hash = idempotency_key_hash(
            idempotency_key, maximum_length=self.options.idempotency_key_max_length
        )
        normalized_selection = request.model_dump(mode="json")
        normalized_selection["image_types"] = [
            image_type for image_type in IMAGE_TYPES if image_type in set(request.image_types)
        ]
        selection_digest = chatflow_selection_digest(
            business_session_id=business_session.id,
            reference_asset_digest=business_session.reference_asset_digest,
            normalized_selection=normalized_selection,
        )
        key_record = db.execute(
            select(TaskIdempotencyRecord)
            .where(
                TaskIdempotencyRecord.owner_user_id == owner_user_id,
                TaskIdempotencyRecord.operation == CHATFLOW_SELECTION_OPERATION,
                TaskIdempotencyRecord.key_hash == key_hash,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if key_record is not None:
            if key_record.request_digest != selection_digest:
                raise DomainError(409, "IDEMPOTENCY_CONFLICT", "相同幂等键不能用于不同请求")
            task = db.execute(
                select(Task).where(
                    Task.id == key_record.task_id,
                    Task.owner_user_id == owner_user_id,
                    Task.business_session_id == business_session.id,
                )
            ).scalar_one_or_none()
            if self._task_is_expired(task, now):
                raise DomainError(410, "TASK_EXPIRED", "任务已过期，无法恢复")
            return self._accepted(task), True

        existing_task = db.execute(
            select(Task).where(Task.business_session_id == business_session.id).with_for_update()
        ).scalar_one_or_none()
        if existing_task is not None:
            if existing_task.request_digest != selection_digest:
                raise DomainError(409, "SESSION_SELECTION_CONFLICT", "此流程已确认不同的选项")
            if self._task_is_expired(existing_task, now):
                raise DomainError(410, "TASK_EXPIRED", "任务已过期，无法恢复")
            self._insert_chatflow_idempotency_record(
                db,
                owner_user_id=owner_user_id,
                key_hash=key_hash,
                request_digest=selection_digest,
                task=existing_task,
                now=now,
            )
            return self._accepted(existing_task), True

        if (
            business_session.status != "recommendation_ready"
            or business_session.expires_at <= now
            or recommendation.status != "ready"
            or recommendation.valid_until is None
            or recommendation.valid_until <= now
            or recommendation.data_expires_at <= now
            or reference_set.status != "ready"
            or reference_set.expires_at is None
            or reference_set.expires_at <= now
        ):
            raise DomainError(409, "RECOMMENDATION_EXPIRED", "推荐已失效或版本已变化，请重新获取")

        consumption = db.execute(
            select(TaskRecommendationConsumption)
            .where(TaskRecommendationConsumption.recommendation_id == recommendation.id)
            .with_for_update()
        ).scalar_one_or_none()
        if consumption is not None:
            if consumption.request_digest != selection_digest:
                raise DomainError(409, "RECOMMENDATION_ALREADY_USED", "此推荐已用于不同的任务输入")
            raise DomainError(409, "SESSION_SELECTION_CONFLICT", "任务状态正在确认，请重新读取流程")

        retention_policy = load_retention_policy_in_transaction(db)
        nonterminal_count = db.scalar(
            select(func.count(Task.id)).where(
                Task.owner_user_id == owner_user_id,
                Task.status.not_in({"succeeded", "partially_succeeded", "failed"}),
            )
        )
        if (
            nonterminal_count is not None
            and nonterminal_count >= self.options.max_nonterminal_per_owner
        ):
            raise DomainError(
                429, "TASK_ACTIVE_LIMIT_REACHED", "当前未完成任务数量已达上限，请稍后再试"
            )

        reference_assets = self._reference_asset_snapshot(
            db, owner_user_id=owner_user_id, reference_set_id=reference_set.id
        )
        if (
            not reference_assets
            or chatflow_reference_snapshot_digest(reference_assets)
            != business_session.reference_asset_digest.hex()
        ):
            raise DomainError(409, "REFERENCE_SET_UNAVAILABLE", "参考组资产暂不可用")
        if (
            recommendation.state.get("reference_asset_digest")
            != business_session.reference_asset_digest.hex()
        ):
            raise DomainError(409, "RECOMMENDATION_UNAVAILABLE", "推荐与参考组不匹配")

        try:
            original_model = DifyOriginalRecommendation.model_validate(
                recommendation.original_recommendation, strict=True
            )
            original_snapshot = original_model.model_dump(mode="json")
        except Exception:
            raise DomainError(
                409, "RECOMMENDATION_UNAVAILABLE", "推荐内容暂不可用，请重新获取推荐"
            ) from None
        resolved_preferences = resolve_preferences(
            TaskCreateRequest(
                schema_version=2,
                reference_set_id=reference_set.id,
                recommendation_id=recommendation.id,
                recommendation_revision=recommendation.revision,
                preferences=request.preferences,
                image_types=request.image_types,
            ),
            public_options_value=recommendation.public_options,
            original_recommendation=original_snapshot,
            category=reference_set.product_category,
            recommendation_revision=recommendation.revision,
        )
        if any(item not in recommendation.image_types for item in request.image_types):
            raise DomainError(422, "IMAGE_TYPE_NOT_ALLOWED", "所选图片类型不在当前推荐范围内")

        task_id = uuid4()
        planned_types = list(normalized_selection["image_types"])
        selection_snapshot = {
            "contract_version": "chatflow-handoff-v1",
            "business_session_id": str(business_session.id),
            "reference_asset_digest": business_session.reference_asset_digest.hex(),
            "selection": normalized_selection,
        }
        chatflow_snapshot: dict[str, object] = {
            "schema_version": 1,
            "provenance_kind": "dify_chatflow",
            "business_session_id": str(business_session.id),
            "recommendation_id": str(recommendation.id),
            "recommendation_revision": recommendation.revision,
            "dify_run_id": business_session.dify_run_id,
            "reference_set_id": str(reference_set.id),
            "reference_asset_digest": business_session.reference_asset_digest.hex(),
            "references": copy.deepcopy(reference_assets),
        }
        recommendation_snapshot = {
            "recommendation_id": str(recommendation.id),
            "revision": recommendation.revision,
            "snapshot_version": "dify-chatflow-recommendation-v1",
            "provenance_kind": "dify_chatflow",
            "dify_chatflow_snapshot": chatflow_snapshot,
            "original_recommendation": copy.deepcopy(original_snapshot),
            "public_options": copy.deepcopy(recommendation.public_options),
            "valid_until": recommendation.valid_until.isoformat(),
            "data_expires_at": recommendation.data_expires_at.isoformat(),
        }
        billing_snapshot: dict[str, object] = {
            "mode": self.options.billing_mode,
            "policy_version": self.options.billing_policy_version,
            "planned_units": 0,
            "reserved_units": 0,
            "charged_units": 0,
            "currency": None,
            "items": [{"image_type": item, "billable_units": 0} for item in planned_types],
        }
        task = Task(
            id=task_id,
            owner_user_id=owner_user_id,
            reference_set_id=reference_set.id,
            recommendation_id=recommendation.id,
            business_session_id=business_session.id,
            provenance_kind="dify_chatflow",
            recommendation_revision=recommendation.revision,
            recommendation_snapshot_version="dify-chatflow-recommendation-v1",
            schema_version=self.options.schema_version,
            workflow_version="chatflow-handoff-v1",
            request_digest=selection_digest,
            request_digest_version=REQUEST_DIGEST_VERSION,
            request_snapshot=selection_snapshot,
            resolved_preferences=resolved_preferences,
            recommendation_snapshot=recommendation_snapshot,
            reference_assets_snapshot=copy.deepcopy(reference_assets),
            category_snapshot=reference_set.product_category,
            image_types=planned_types,
            planned_count=len(planned_types),
            billing_mode=self.options.billing_mode,
            billing_policy_version=self.options.billing_policy_version,
            billing_snapshot=billing_snapshot,
            status="queued",
            retention_days=retention_policy.task_retention_days,
            retention_policy_revision=retention_policy.revision,
            created_at=now,
            updated_at=now,
        )
        db.add(task)
        db.flush()
        db.add_all(
            TaskItem(
                task_id=task_id,
                position=position,
                image_type=image_type,
                status="queued",
                preview_ready=False,
                created_at=now,
                updated_at=now,
            )
            for position, image_type in enumerate(planned_types, start=1)
        )
        db.add(
            TaskRecommendationConsumption(
                recommendation_id=recommendation.id,
                owner_user_id=owner_user_id,
                request_digest=selection_digest,
                digest_version=REQUEST_DIGEST_VERSION,
                task_id=task_id,
                planned_count=len(planned_types),
                state="active",
                created_at=now,
                updated_at=now,
            )
        )
        self._insert_chatflow_idempotency_record(
            db,
            owner_user_id=owner_user_id,
            key_hash=key_hash,
            request_digest=selection_digest,
            task=task,
            now=now,
        )
        db.flush()
        return self._accepted(task), False

    @staticmethod
    def _insert_chatflow_idempotency_record(
        db: Session,
        *,
        owner_user_id: UUID,
        key_hash: bytes,
        request_digest: bytes,
        task: Task,
        now: datetime,
    ) -> None:
        db.add(
            TaskIdempotencyRecord(
                owner_user_id=owner_user_id,
                operation=CHATFLOW_SELECTION_OPERATION,
                key_hash=key_hash,
                request_digest=request_digest,
                digest_version=REQUEST_DIGEST_VERSION,
                task_id=task.id,
                planned_count=task.planned_count,
                state="active",
                created_at=now,
                updated_at=now,
            )
        )
        db.flush()

    def _create_in_transaction(
        self,
        *,
        owner_user_id: UUID,
        key_hash: bytes,
        request_digest: bytes,
        normalized_request: dict[str, object],
        request: TaskCreateRequest,
    ) -> TaskCreationResult:
        with self._sessions() as db, db.begin():
            # S6 checks the idempotency key before taking resource locks, then rechecks it
            # authoritatively after serializing on the owner row.
            self._find_idempotency_record(
                db,
                owner_user_id=owner_user_id,
                key_hash=key_hash,
                for_update=False,
            )

            owner = db.execute(
                select(User)
                .where(User.id == owner_user_id, User.status == "active")
                .with_for_update()
            ).scalar_one_or_none()
            if owner is None:
                raise DomainError(401, "IDENTITY_NOT_FOUND", "需要有效身份")
            now = db.scalar(select(func.clock_timestamp()))

            # Recheck after the per-owner row lock so concurrent retries see the committed row.
            key_record = self._find_idempotency_record(
                db,
                owner_user_id=owner_user_id,
                key_hash=key_hash,
                for_update=True,
            )
            if key_record is not None:
                return self._resolve_idempotency_replay(
                    db, key_record, request_digest=request_digest, now=now
                )

            # Retention is a per-task immutable snapshot of the singleton policy.
            # The read is intentionally unlocked and happens before resource locks.
            try:
                retention_policy = load_retention_policy_in_transaction(db)
            except RetentionPolicyUnavailable:
                raise DomainError(503, "RETENTION_POLICY_UNAVAILABLE", "任务服务暂不可用") from None

            consumption = db.execute(
                select(TaskRecommendationConsumption).where(
                    TaskRecommendationConsumption.recommendation_id == request.recommendation_id,
                    TaskRecommendationConsumption.owner_user_id == owner_user_id,
                )
            ).scalar_one_or_none()
            if consumption is not None:
                return self._resolve_recommendation_replay(
                    db,
                    consumption,
                    owner_user_id=owner_user_id,
                    key_hash=key_hash,
                    request_digest=request_digest,
                    now=now,
                )

            reference_set = db.execute(
                select(ReferenceSet)
                .where(
                    ReferenceSet.id == request.reference_set_id,
                    ReferenceSet.owner_user_id == owner_user_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if reference_set is None:
                raise DomainError(404, "REFERENCE_SET_NOT_FOUND", "参考组不可用")

            recommendation = db.execute(
                select(Recommendation)
                .where(
                    Recommendation.id == request.recommendation_id,
                    Recommendation.owner_user_id == owner_user_id,
                    Recommendation.reference_set_id == request.reference_set_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if recommendation is None:
                raise DomainError(404, "RECOMMENDATION_NOT_FOUND", "推荐记录不可用")
            now = db.scalar(select(func.clock_timestamp()))

            # Recheck key/consumption while holding the owner, reference, and recommendation locks.
            key_record = self._find_idempotency_record(
                db,
                owner_user_id=owner_user_id,
                key_hash=key_hash,
                for_update=True,
            )
            if key_record is not None:
                return self._resolve_idempotency_replay(
                    db, key_record, request_digest=request_digest, now=now
                )
            consumption = db.execute(
                select(TaskRecommendationConsumption)
                .where(TaskRecommendationConsumption.recommendation_id == recommendation.id)
                .with_for_update()
            ).scalar_one_or_none()
            if consumption is not None:
                return self._resolve_recommendation_replay(
                    db,
                    consumption,
                    owner_user_id=owner_user_id,
                    key_hash=key_hash,
                    request_digest=request_digest,
                    now=now,
                )

            if (
                reference_set.status != "ready"
                or reference_set.expires_at is None
                or reference_set.expires_at <= now
            ):
                raise DomainError(409, "REFERENCE_SET_UNAVAILABLE", "参考组已失效，请重新上传")
            if (
                recommendation.status != "ready"
                or recommendation.revision != request.recommendation_revision
                or recommendation.valid_until is None
                or recommendation.valid_until <= now
                or recommendation.data_expires_at <= now
            ):
                raise DomainError(
                    409, "RECOMMENDATION_EXPIRED", "推荐已失效或版本已变化，请重新获取"
                )

            nonterminal_count = db.scalar(
                select(func.count(Task.id)).where(
                    Task.owner_user_id == owner_user_id,
                    Task.status.not_in({"succeeded", "partially_succeeded", "failed"}),
                )
            )
            if nonterminal_count is not None and nonterminal_count >= (
                self.options.max_nonterminal_per_owner
            ):
                raise DomainError(
                    429,
                    "TASK_ACTIVE_LIMIT_REACHED",
                    "当前未完成任务数量已达上限，请稍后再试",
                )

            reference_assets = self._reference_asset_snapshot(
                db, owner_user_id=owner_user_id, reference_set_id=reference_set.id
            )
            if not reference_assets:
                raise DomainError(409, "REFERENCE_SET_UNAVAILABLE", "参考组资产暂不可用")

            trusted_analysis_snapshot = _load_task_analysis_snapshot(
                recommendation,
                reference_set_id=reference_set.id,
                category=reference_set.product_category,
                reference_assets=reference_assets,
            )

            resolved_preferences = resolve_preferences(
                request,
                public_options_value=recommendation.public_options,
                original_recommendation=recommendation.original_recommendation,
                category=reference_set.product_category,
                recommendation_revision=recommendation.revision,
            )
            original_snapshot = recommendation.original_recommendation
            if not isinstance(original_snapshot, dict):
                raise DomainError(
                    409, "RECOMMENDATION_UNAVAILABLE", "推荐内容暂不可用，请重新获取推荐"
                )
            fixture_id = original_snapshot.get("fixture_id")
            if not isinstance(fixture_id, str) or not fixture_id or len(fixture_id) > 120:
                raise DomainError(
                    409, "RECOMMENDATION_UNAVAILABLE", "推荐版本信息暂不可用，请重新获取推荐"
                )
            recommended_types = recommendation.image_types or []
            if any(
                image_type not in recommended_types
                for image_type in normalized_request["image_types"]
            ):
                raise DomainError(422, "IMAGE_TYPE_NOT_ALLOWED", "所选图片类型不在当前推荐范围内")
            original_types = original_snapshot.get("image_types")
            if not isinstance(original_types, list) or any(
                image_type not in original_types for image_type in normalized_request["image_types"]
            ):
                raise DomainError(422, "IMAGE_TYPE_NOT_ALLOWED", "所选图片类型不在当前推荐范围内")

            task_id = uuid4()
            planned_types = list(normalized_request["image_types"])
            recommendation_snapshot = {
                "recommendation_id": str(recommendation.id),
                "revision": recommendation.revision,
                "snapshot_version": fixture_id,
                "trusted_analysis_snapshot_id": str(trusted_analysis_snapshot.snapshot_id),
                "trusted_analysis_snapshot_digest": trusted_analysis_snapshot.snapshot_digest,
                "trusted_analysis_snapshot": trusted_analysis_snapshot.model_dump(mode="json"),
                "original_recommendation": copy.deepcopy(original_snapshot),
                "public_options": copy.deepcopy(recommendation.public_options),
                "valid_until": recommendation.valid_until.isoformat(),
                "data_expires_at": recommendation.data_expires_at.isoformat(),
            }
            billing_snapshot: dict[str, object] = {
                "mode": self.options.billing_mode,
                "policy_version": self.options.billing_policy_version,
                "planned_units": 0,
                "reserved_units": 0,
                "charged_units": 0,
                "currency": None,
                "items": [
                    {"image_type": image_type, "billable_units": 0} for image_type in planned_types
                ],
            }
            task = Task(
                id=task_id,
                owner_user_id=owner_user_id,
                reference_set_id=reference_set.id,
                recommendation_id=recommendation.id,
                recommendation_revision=recommendation.revision,
                recommendation_snapshot_version=fixture_id,
                schema_version=self.options.schema_version,
                workflow_version=self.options.workflow_version,
                request_digest=request_digest,
                request_digest_version=REQUEST_DIGEST_VERSION,
                request_snapshot=copy.deepcopy(normalized_request),
                resolved_preferences=resolved_preferences,
                recommendation_snapshot=recommendation_snapshot,
                reference_assets_snapshot=reference_assets,
                category_snapshot=reference_set.product_category,
                image_types=planned_types,
                planned_count=len(planned_types),
                billing_mode=self.options.billing_mode,
                billing_policy_version=self.options.billing_policy_version,
                billing_snapshot=billing_snapshot,
                status="queued",
                retention_days=retention_policy.task_retention_days,
                retention_policy_revision=retention_policy.revision,
                created_at=now,
                updated_at=now,
            )
            db.add(task)
            db.flush()
            db.add_all(
                TaskItem(
                    task_id=task_id,
                    position=position,
                    image_type=image_type,
                    status="queued",
                    preview_ready=False,
                    created_at=now,
                    updated_at=now,
                )
                for position, image_type in enumerate(planned_types, start=1)
            )
            db.add(
                TaskRecommendationConsumption(
                    recommendation_id=recommendation.id,
                    owner_user_id=owner_user_id,
                    request_digest=request_digest,
                    digest_version=REQUEST_DIGEST_VERSION,
                    task_id=task_id,
                    planned_count=len(planned_types),
                    state="active",
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                TaskIdempotencyRecord(
                    owner_user_id=owner_user_id,
                    operation=TASK_CREATE_OPERATION,
                    key_hash=key_hash,
                    request_digest=request_digest,
                    digest_version=REQUEST_DIGEST_VERSION,
                    task_id=task_id,
                    planned_count=len(planned_types),
                    state="active",
                    created_at=now,
                    updated_at=now,
                )
            )
            db.flush()
            enqueue_in_transaction(
                db,
                event_type=TASK_PROMPT_EVENT_TYPE,
                aggregate_id=task_id,
                operation_version=self.options.event_operation_version,
                schema_version=self.options.event_schema_version,
                payload={},
            )
            return self._accepted(task)

    @staticmethod
    def _find_idempotency_record(
        db: Session,
        *,
        owner_user_id: UUID,
        key_hash: bytes,
        for_update: bool,
    ) -> TaskIdempotencyRecord | None:
        statement = select(TaskIdempotencyRecord).where(
            TaskIdempotencyRecord.owner_user_id == owner_user_id,
            TaskIdempotencyRecord.operation == TASK_CREATE_OPERATION,
            TaskIdempotencyRecord.key_hash == key_hash,
        )
        if for_update:
            statement = statement.with_for_update()
        return db.execute(statement).scalar_one_or_none()

    def _resolve_idempotency_replay(
        self,
        db: Session,
        record: TaskIdempotencyRecord,
        *,
        request_digest: bytes,
        now: datetime,
    ) -> TaskCreationResult:
        if record.request_digest != request_digest:
            raise DomainError(409, "IDEMPOTENCY_CONFLICT", "相同幂等键不能用于不同请求")
        task = db.execute(
            select(Task).where(
                Task.id == record.task_id,
                Task.owner_user_id == record.owner_user_id,
            )
        ).scalar_one_or_none()
        if record.state == "expired" or self._task_is_expired(task, now):
            record.state = "expired"
            record.updated_at = now
            consumption = db.execute(
                select(TaskRecommendationConsumption).where(
                    TaskRecommendationConsumption.task_id == record.task_id
                )
            ).scalar_one_or_none()
            if consumption is not None:
                consumption.state = "expired"
                consumption.updated_at = now
            return _ExpiredReplay(record.task_id)
        return self._accepted(task)

    def _resolve_recommendation_replay(
        self,
        db: Session,
        consumption: TaskRecommendationConsumption,
        *,
        owner_user_id: UUID,
        key_hash: bytes,
        request_digest: bytes,
        now: datetime,
    ) -> TaskCreationResult:
        if consumption.owner_user_id != owner_user_id:
            raise DomainError(404, "RECOMMENDATION_NOT_FOUND", "推荐记录不可用")
        if consumption.request_digest != request_digest:
            raise DomainError(409, "RECOMMENDATION_ALREADY_USED", "此推荐已用于不同的任务输入")
        task = db.execute(
            select(Task).where(
                Task.id == consumption.task_id,
                Task.owner_user_id == owner_user_id,
            )
        ).scalar_one_or_none()
        if consumption.state == "expired" or self._task_is_expired(task, now):
            consumption.state = "expired"
            consumption.updated_at = now
            return _ExpiredReplay(consumption.task_id)

        db.add(
            TaskIdempotencyRecord(
                owner_user_id=owner_user_id,
                operation=TASK_CREATE_OPERATION,
                key_hash=key_hash,
                request_digest=request_digest,
                digest_version=REQUEST_DIGEST_VERSION,
                task_id=consumption.task_id,
                planned_count=consumption.planned_count,
                state="active",
                created_at=now,
                updated_at=now,
            )
        )
        db.flush()
        return self._accepted(task)

    @staticmethod
    def _task_is_expired(task: Task | None, now: datetime) -> bool:
        return task is None or (task.expires_at is not None and task.expires_at <= now)

    @staticmethod
    def _accepted(task: Task | None) -> TaskAccepted:
        if task is None:
            raise DomainError(410, "TASK_EXPIRED", "任务已过期，无法恢复")
        return TaskAccepted(
            task_id=task.id,
            status=task.status,  # type: ignore[arg-type]
            planned_count=task.planned_count,
        )

    @staticmethod
    def _reference_asset_snapshot(
        db: Session, *, owner_user_id: UUID, reference_set_id: UUID
    ) -> list[dict[str, object]]:
        source_asset = aliased(ImageAsset)
        model_asset = aliased(ImageAsset)
        rows = db.execute(
            select(ReferenceSetImage, source_asset, model_asset)
            .join(
                source_asset,
                and_(
                    ReferenceSetImage.source_asset_id == source_asset.id,
                    ReferenceSetImage.owner_user_id == source_asset.owner_user_id,
                    source_asset.asset_kind == "reference_source",
                ),
            )
            .join(
                model_asset,
                and_(
                    ReferenceSetImage.model_asset_id == model_asset.id,
                    ReferenceSetImage.owner_user_id == model_asset.owner_user_id,
                    model_asset.asset_kind == "reference_model_input",
                ),
            )
            .where(
                ReferenceSetImage.reference_set_id == reference_set_id,
                ReferenceSetImage.owner_user_id == owner_user_id,
            )
            .order_by(ReferenceSetImage.position)
        ).all()
        if not 1 <= len(rows) <= 4:
            return []
        slots = [row[0].slot for row in rows]
        if slots[0] != "PRIMARY" or slots != ["PRIMARY", "AUX01", "AUX02", "AUX03"][: len(slots)]:
            return []
        snapshots: list[dict[str, object]] = []
        for reference_image, source, model in rows:
            if reference_image.content_sha256 != model.content_sha256:
                return []
            snapshots.append(
                {
                    "reference_set_image_id": str(reference_image.id),
                    "position": reference_image.position,
                    "slot": reference_image.slot,
                    "source_asset_id": str(source.id),
                    "model_asset_id": str(model.id),
                    "content_sha256": reference_image.content_sha256,
                    "source_sha256": source.content_sha256,
                    "model_sha256": model.content_sha256,
                    "source_mime_type": source.mime_type,
                    "model_mime_type": model.mime_type,
                    "source_size_bytes": source.size_bytes,
                    "model_size_bytes": model.size_bytes,
                    "model_width": model.width,
                    "model_height": model.height,
                }
            )
        return snapshots

    def get_task(self, *, owner_user_id: UUID, task_id: UUID) -> TaskResponse:
        try:
            with self._sessions() as db:
                now = db.scalar(select(func.clock_timestamp()))
                task = db.execute(
                    select(Task).where(
                        Task.id == task_id,
                        Task.owner_user_id == owner_user_id,
                        Task.status != "deleting",
                    )
                ).scalar_one_or_none()
                if task is None:
                    raise DomainError(404, "TASK_NOT_FOUND", "任务不可用")
                if task.expires_at is not None and task.expires_at <= now:
                    raise DomainError(410, "TASK_EXPIRED", "任务已过期")
                items = (
                    db.execute(
                        select(TaskItem)
                        .where(TaskItem.task_id == task.id)
                        .order_by(TaskItem.position)
                    )
                    .scalars()
                    .all()
                )
                return self._to_public_response(task, items)
        except DomainError:
            raise
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "任务暂时无法读取") from None

    def list_tasks(
        self,
        *,
        owner_user_id: UUID,
        limit: int = _HISTORY_DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> TaskHistoryPage:
        if isinstance(limit, bool) or not 1 <= limit <= _HISTORY_MAX_LIMIT:
            raise DomainError(422, "HISTORY_LIMIT_INVALID", "历史分页数量无效")
        try:
            decoded_cursor = _decode_history_cursor(cursor) if cursor is not None else None
        except ValueError:
            raise DomainError(400, "HISTORY_CURSOR_INVALID", "历史游标无效") from None

        try:
            with self._sessions() as db:
                now = db.scalar(select(func.clock_timestamp()))
                filters = [
                    Task.owner_user_id == owner_user_id,
                    Task.status != "deleting",
                    or_(Task.expires_at.is_(None), Task.expires_at > now),
                ]
                if decoded_cursor is not None:
                    filters.append(
                        or_(
                            Task.created_at < decoded_cursor.created_at,
                            and_(
                                Task.created_at == decoded_cursor.created_at,
                                Task.id < decoded_cursor.task_id,
                            ),
                        )
                    )
                rows = (
                    db.execute(
                        select(Task)
                        .where(*filters)
                        .order_by(Task.created_at.desc(), Task.id.desc())
                        .limit(limit + 1)
                    )
                    .scalars()
                    .all()
                )
                has_more = len(rows) > limit
                tasks = rows[:limit]
                task_ids = [task.id for task in tasks]
                task_items: dict[UUID, list[TaskItem]] = {task_id: [] for task_id in task_ids}
                if task_ids:
                    item_rows = (
                        db.execute(
                            select(TaskItem)
                            .where(TaskItem.task_id.in_(task_ids))
                            .order_by(TaskItem.task_id, TaskItem.position, TaskItem.id)
                        )
                        .scalars()
                        .all()
                    )
                    for item in item_rows:
                        task_items[item.task_id].append(item)

                entries = [self._to_history_entry(task, task_items[task.id]) for task in tasks]
                next_cursor = (
                    _encode_history_cursor(tasks[-1].created_at, tasks[-1].id)
                    if has_more and tasks
                    else None
                )
                return TaskHistoryPage(items=entries, next_cursor=next_cursor)
        except DomainError:
            raise
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "历史记录暂时无法读取") from None

    def open_preview(
        self,
        *,
        owner_user_id: UUID,
        task_id: UUID,
        image_id: UUID,
        storage: Storage,
    ) -> PreviewStream:
        try:
            with self._sessions() as db:
                now = db.scalar(select(func.clock_timestamp()))
                original_asset = aliased(ImageAsset)
                preview_asset = aliased(ImageAsset)
                row = db.execute(
                    select(
                        preview_asset.object_key,
                        preview_asset.mime_type,
                        preview_asset.size_bytes,
                    )
                    .select_from(Task)
                    .join(TaskItem, TaskItem.task_id == Task.id)
                    .join(
                        GenerationResult,
                        and_(
                            GenerationResult.task_id == Task.id,
                            GenerationResult.task_item_id == TaskItem.id,
                            GenerationResult.owner_user_id == owner_user_id,
                        ),
                    )
                    .join(
                        original_asset,
                        and_(
                            original_asset.id == GenerationResult.original_asset_id,
                            original_asset.owner_user_id == owner_user_id,
                            original_asset.asset_kind == "generation_original",
                        ),
                    )
                    .join(
                        preview_asset,
                        and_(
                            preview_asset.id == GenerationResult.preview_asset_id,
                            preview_asset.owner_user_id == owner_user_id,
                            preview_asset.asset_kind == "generation_preview",
                            preview_asset.mime_type == "image/webp",
                        ),
                    )
                    .where(
                        Task.id == task_id,
                        Task.owner_user_id == owner_user_id,
                        Task.status != "deleting",
                        or_(Task.expires_at.is_(None), Task.expires_at > now),
                        TaskItem.status == "succeeded",
                        TaskItem.preview_ready.is_(True),
                        TaskItem.image_id == image_id,
                        GenerationResult.original_asset_id == image_id,
                        original_asset.id == image_id,
                    )
                ).one_or_none()
                if row is None:
                    raise DomainError(404, "PREVIEW_NOT_FOUND", "预览图片不可用")
                object_key, mime_type, size_bytes = row
        except DomainError:
            raise
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "预览图片暂时无法读取") from None

        try:
            body = storage.open_reader(object_key)
        except StorageError:
            raise DomainError(503, "PREVIEW_UNAVAILABLE", "预览图片暂时无法读取") from None
        return PreviewStream(body=body, mime_type=mime_type, size_bytes=size_bytes)

    def open_original(
        self,
        *,
        owner_user_id: UUID,
        task_id: UUID,
        image_id: UUID,
        storage: Storage,
        lease: DownloadLease,
        identity_recheck: Callable[[], bool],
    ) -> OriginalDownload:
        """Open one authorized original, validate it, then recheck access before return."""

        reader: BinaryIO | None = None
        download: OriginalDownload | None = None
        handed_off = False

        def close_resources() -> None:
            if download is not None:
                try:
                    download.close()
                except BaseException:
                    pass
            elif reader is not None:
                try:
                    if not getattr(reader, "closed", False):
                        reader.close()
                except BaseException:
                    pass
            try:
                lease.release()
            except BaseException:
                pass

        try:
            try:
                first_snapshot = self._original_snapshot(
                    owner_user_id=owner_user_id,
                    task_id=task_id,
                    image_id=image_id,
                )
            except SQLAlchemyError:
                raise DomainError(
                    503, "DATABASE_UNAVAILABLE", "原图暂时无法读取"
                ) from None
            if first_snapshot is None:
                raise DomainError(404, "ORIGINAL_NOT_FOUND", "原图不可用")

            object_key, mime_type, size_bytes, sha256 = first_snapshot
            try:
                reader = storage.open_reader(object_key)
            except Exception:
                raise DomainError(503, "ORIGINAL_UNAVAILABLE", "原图暂时无法读取") from None

            download = prepare_original_download(
                reader=reader,
                mime_type=mime_type,
                expected_size=size_bytes,
                expected_sha256=sha256,
                task_id=task_id,
                image_id=image_id,
                lease=lease,
            )

            try:
                identity_is_current = identity_recheck()
            except SQLAlchemyError:
                raise DomainError(
                    503, "DATABASE_UNAVAILABLE", "当前身份暂时无法验证"
                ) from None
            if not identity_is_current:
                raise DomainError(401, "AUTH_REQUIRED", "需要有效身份")

            # Resource admission must observe changes committed during the identity lookup.
            try:
                final_snapshot = self._original_snapshot(
                    owner_user_id=owner_user_id,
                    task_id=task_id,
                    image_id=image_id,
                )
            except SQLAlchemyError:
                raise DomainError(
                    503, "DATABASE_UNAVAILABLE", "原图暂时无法读取"
                ) from None
            if final_snapshot is None or final_snapshot != first_snapshot:
                raise DomainError(404, "ORIGINAL_NOT_FOUND", "原图不可用")
            lease.check_deadline()

            handed_off = True
            return download
        except BaseException:
            if not handed_off:
                close_resources()
            raise

    def _original_snapshot(
        self,
        *,
        owner_user_id: UUID,
        task_id: UUID,
        image_id: UUID,
    ) -> tuple[str, str, int, str] | None:
        """Read the committed original asset snapshot with every ownership edge fixed."""

        with self._sessions() as db:
            row = db.execute(
                select(
                    ImageAsset.object_key,
                    ImageAsset.mime_type,
                    ImageAsset.size_bytes,
                    ImageAsset.content_sha256,
                )
                .select_from(Task)
                .join(TaskItem, TaskItem.task_id == Task.id)
                .join(
                    GenerationResult,
                    and_(
                        GenerationResult.task_id == Task.id,
                        GenerationResult.task_item_id == TaskItem.id,
                        GenerationResult.owner_user_id == owner_user_id,
                    ),
                )
                .join(
                    TaskItemAttempt,
                    and_(
                        TaskItemAttempt.id == GenerationResult.attempt_id,
                        TaskItemAttempt.task_item_id == TaskItem.id,
                        TaskItemAttempt.status == "completed",
                        TaskItemAttempt.completed_at.is_not(None),
                    ),
                )
                .join(
                    ImageAsset,
                    and_(
                        ImageAsset.id == GenerationResult.original_asset_id,
                        ImageAsset.owner_user_id == owner_user_id,
                        ImageAsset.asset_kind == "generation_original",
                    ),
                )
                .where(
                    Task.id == task_id,
                    Task.owner_user_id == owner_user_id,
                    Task.status != "deleting",
                    or_(Task.expires_at.is_(None), Task.expires_at > func.clock_timestamp()),
                    TaskItem.status == "succeeded",
                    TaskItem.image_id == image_id,
                    GenerationResult.original_asset_id == image_id,
                    ImageAsset.id == image_id,
                )
            ).one_or_none()
            if row is None:
                return None
            return (row[0], row[1], row[2], row[3])

    def retry_failed_items(
        self,
        *,
        owner_user_id: UUID,
        task_id: UUID,
        idempotency_key: str,
        request: TaskRetryRequest,
    ) -> TaskRetryAccepted:
        key_hash = idempotency_key_hash(
            idempotency_key, maximum_length=self.options.idempotency_key_max_length
        )
        item_ids = tuple(sorted(request.failed_item_ids, key=str))
        request_digest = retry_request_digest(task_id, item_ids)
        try:
            return self._retry_failed_items_in_transaction(
                owner_user_id=owner_user_id,
                task_id=task_id,
                key_hash=key_hash,
                request_digest=request_digest,
                item_ids=item_ids,
            )
        except DomainError:
            raise
        except IntegrityError:
            replay = self._recover_retry_replay(
                owner_user_id=owner_user_id,
                task_id=task_id,
                key_hash=key_hash,
                request_digest=request_digest,
                item_ids=item_ids,
            )
            if replay is not None:
                return replay
            raise DomainError(409, "RETRY_CONFLICT", "重试状态冲突，请重新读取任务") from None
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "任务暂时无法重试") from None

    def _retry_failed_items_in_transaction(
        self,
        *,
        owner_user_id: UUID,
        task_id: UUID,
        key_hash: bytes,
        request_digest: bytes,
        item_ids: tuple[UUID, ...],
    ) -> TaskRetryAccepted:
        with self._sessions() as db, db.begin():
            owner = db.execute(
                select(User)
                .where(User.id == owner_user_id, User.status == "active")
                .with_for_update()
            ).scalar_one_or_none()
            if owner is None:
                raise DomainError(401, "IDENTITY_NOT_FOUND", "需要有效身份")

            # Locking the owner serializes retries that share an owner-scoped
            # idempotency key, so a duplicate cannot create a second attempt. Check
            # this tombstone before looking up Task: hard deletion intentionally
            # retains the key mapping so a replay returns 410 rather than 404.
            replay_record = db.execute(
                select(TaskIdempotencyRecord).where(
                    TaskIdempotencyRecord.owner_user_id == owner_user_id,
                    TaskIdempotencyRecord.operation == TASK_RETRY_OPERATION,
                    TaskIdempotencyRecord.key_hash == key_hash,
                )
            ).scalar_one_or_none()
            if replay_record is not None and (
                replay_record.task_id != task_id or replay_record.request_digest != request_digest
            ):
                raise DomainError(409, "IDEMPOTENCY_CONFLICT", "幂等键已用于其他重试请求")
            if replay_record is not None:
                now = db.scalar(select(func.clock_timestamp()))
                if replay_record.state != "active" or replay_record.planned_count != len(item_ids):
                    raise DomainError(410, "TASK_EXPIRED", "任务已过期，无法重试")
                replay_task = db.execute(
                    select(Task).where(
                        Task.id == task_id,
                        Task.owner_user_id == owner_user_id,
                    )
                ).scalar_one_or_none()
                if (
                    replay_task is None
                    or replay_task.status == "deleting"
                    or (replay_task.expires_at is not None and replay_task.expires_at <= now)
                ):
                    raise DomainError(410, "TASK_EXPIRED", "任务已过期，无法重试")
                return _retry_accepted(task_id, item_ids, key_hash)

            task_hint = db.execute(
                select(Task.owner_user_id, Task.reference_set_id, Task.recommendation_id).where(
                    Task.id == task_id,
                    Task.owner_user_id == owner_user_id,
                )
            ).one_or_none()
            if task_hint is None:
                raise DomainError(404, "TASK_NOT_FOUND", "任务不可用")
            owner_id, reference_set_id, recommendation_id = task_hint

            reference_set = db.execute(
                select(ReferenceSet)
                .where(
                    ReferenceSet.id == reference_set_id,
                    ReferenceSet.owner_user_id == owner_user_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            recommendation = db.execute(
                select(Recommendation)
                .where(
                    Recommendation.id == recommendation_id,
                    Recommendation.owner_user_id == owner_user_id,
                    Recommendation.reference_set_id == reference_set_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            task = db.execute(
                select(Task)
                .where(
                    Task.id == task_id,
                    Task.owner_user_id == owner_user_id,
                    Task.reference_set_id == reference_set_id,
                    Task.recommendation_id == recommendation_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if reference_set is None or recommendation is None or task is None:
                raise DomainError(404, "TASK_NOT_FOUND", "任务不可用")
            if task.status == "deleting":
                raise DomainError(409, "TASK_DELETING", "任务正在清理，无法重试")
            now = db.scalar(select(func.clock_timestamp()))
            if task.expires_at is not None and task.expires_at <= now:
                raise DomainError(410, "TASK_EXPIRED", "任务已过期，无法重试")

            # The generation lock contract requires all task item rows first,
            # then all selected attempt rows, before any context/action locks.
            items = (
                db.execute(
                    select(TaskItem)
                    .where(TaskItem.task_id == task.id)
                    .order_by(TaskItem.position, TaskItem.id)
                    .with_for_update()
                )
                .scalars()
                .all()
            )
            if len(items) != task.planned_count:
                raise DomainError(503, "TASK_STATE_UNAVAILABLE", "任务状态暂不可用")
            item_by_id = {item.id: item for item in items}
            if any(item_id not in item_by_id for item_id in item_ids):
                raise DomainError(409, "ITEMS_NOT_RETRYABLE", "所选失败项不可重试")
            selected_items = sorted(
                (item_by_id[item_id] for item_id in item_ids),
                key=lambda item: (item.position, item.id),
            )
            if task.business_session_id is not None and task.provenance_kind == "dify_chatflow":
                flow_version = db.scalar(
                    select(BusinessSession.flow_version).where(
                        BusinessSession.id == task.business_session_id,
                        BusinessSession.owner_user_id == owner_user_id,
                    )
                )
                if flow_version == "visual-v3.4-single-selection" and any(
                    item.error_code
                    in {
                        "V34_PRE_GENERATION_FAILED",
                        "V34_WORKFLOW_ENDED_WITHOUT_GENERATION",
                        "GENERATION_PERMIT_EXPIRED_BEFORE_ACCEPTANCE",
                    }
                    for item in selected_items
                ):
                    raise DomainError(
                        409,
                        "V34_TASK_NOT_RETRYABLE",
                        "未发起生图的 v3.4 任务不能按失败图片重试",
                    )
            if any(
                item.status != "failed" or item.preview_ready or item.image_id is not None
                for item in selected_items
            ):
                raise DomainError(409, "ITEMS_NOT_RETRYABLE", "所选失败项不可重试")

            attempts_by_item: dict[UUID, list[TaskItemAttempt]] = {}
            for item in selected_items:
                attempts_by_item[item.id] = (
                    db.execute(
                        select(TaskItemAttempt)
                        .where(TaskItemAttempt.task_item_id == item.id)
                        .order_by(TaskItemAttempt.attempt_no, TaskItemAttempt.id)
                        .with_for_update()
                    )
                    .scalars()
                    .all()
                )

            reference_snapshot_aliases = _task_reference_asset_aliases(task)
            validated: list[
                tuple[
                    TaskItem, TaskItemAttempt, TaskPromptPackage, TaskPromptItem, GenerationCommand
                ]
            ] = []
            for item in selected_items:
                attempts = attempts_by_item[item.id]
                if not attempts:
                    raise DomainError(409, "ITEMS_NOT_RETRYABLE", "所选失败项不可重试")
                last_attempt = attempts[-1]
                context = db.execute(
                    select(GenerationAttemptContext)
                    .where(GenerationAttemptContext.attempt_id == last_attempt.id)
                    .with_for_update()
                ).scalar_one_or_none()
                actions = (
                    db.execute(
                        select(Action)
                        .where(Action.attempt_id == last_attempt.id)
                        .order_by(Action.sequence, Action.id)
                        .with_for_update()
                    )
                    .scalars()
                    .all()
                )
                if (
                    last_attempt.status != "failed"
                    or context is None
                    or context.task_id != task.id
                    or context.task_item_id != item.id
                    or context.owner_user_id != owner_user_id
                    or context.save_state != "failed"
                    or not actions
                    or actions[-1].status != "rejected"
                    or actions[-1].last_outcome != "definitely_rejected"
                    or any(action.status in {"scheduled", "claimed"} for action in actions)
                    or any(
                        is_unknown_action(action)
                        and not is_resolved_submit_unknown(action, last_attempt, context, actions)
                        for action in actions
                    )
                ):
                    raise DomainError(409, "ITEMS_NOT_RETRYABLE", "所选失败项不可重试")

                prompt_item = db.execute(
                    select(TaskPromptItem).where(
                        TaskPromptItem.task_id == task.id,
                        TaskPromptItem.task_item_id == item.id,
                    )
                ).scalar_one_or_none()
                if prompt_item is None:
                    raise DomainError(409, "PROMPT_NOT_RETRYABLE", "任务提示词不可用于重试")
                package = db.execute(
                    select(TaskPromptPackage).where(
                        TaskPromptPackage.id == prompt_item.package_id,
                        TaskPromptPackage.task_id == task.id,
                    )
                ).scalar_one_or_none()
                if package is None:
                    raise DomainError(409, "PROMPT_NOT_RETRYABLE", "任务提示词不可用于重试")
                command = _retry_generation_command(
                    task=task,
                    item=item,
                    package=package,
                    prompt_item=prompt_item,
                    request_key=_retry_attempt_id(task.id, item.id, key_hash),
                    snapshot_aliases=reference_snapshot_aliases,
                )
                if (
                    context.prompt_package_id != package.id
                    or context.prompt_item_id != prompt_item.id
                    or context.package_digest_version != package.package_digest_version
                    or bytes(context.package_digest) != bytes(package.package_digest)
                    or context.request_digest_version != command.request_digest_version
                    or bytes(context.request_digest) != command.request_digest
                    or context.prompt_package_version != command.prompt_package_version
                    or context.image_type != command.image_type
                    or context.prompt_text != command.prompt_text
                    or context.reference_asset_ids
                    != [str(asset_id) for asset_id in command.reference_asset_ids]
                    or context.excluded_reference_aliases
                    != list(prompt_item.excluded_reference_aliases)
                ):
                    raise DomainError(409, "PROMPT_NOT_RETRYABLE", "任务提示词不可用于重试")
                validated.append((item, last_attempt, package, prompt_item, command))

            # All failure, action, prompt, package, and reference checks have
            # passed; inserts and aggregate updates below commit atomically.
            retry_attempts: list[
                tuple[
                    TaskItem, TaskItemAttempt, TaskPromptPackage, TaskPromptItem, GenerationCommand
                ]
            ] = []
            for item, previous, package, prompt_item, command in validated:
                attempt_id = command.request_key
                attempt = TaskItemAttempt(
                    id=attempt_id,
                    task_item_id=item.id,
                    attempt_no=previous.attempt_no + 1,
                    idempotency_key=attempt_id,
                    status="prepared",
                    channel_id=previous.channel_id or "mock",
                    model_id=previous.model_id or "mock-generation",
                    model_version=command.prompt_package_version,
                    started_at=now,
                    next_action_at=now,
                )
                db.add(attempt)
                retry_attempts.append((item, attempt, package, prompt_item, command))
            db.flush()

            for item, attempt, package, prompt_item, command in retry_attempts:
                db.add(
                    GenerationAttemptContext(
                        attempt_id=attempt.id,
                        task_id=task.id,
                        task_item_id=item.id,
                        owner_user_id=owner_user_id,
                        prompt_package_id=package.id,
                        prompt_item_id=prompt_item.id,
                        package_digest_version=package.package_digest_version,
                        package_digest=bytes(package.package_digest),
                        request_digest_version=command.request_digest_version,
                        request_digest=command.request_digest,
                        prompt_package_version=command.prompt_package_version,
                        image_type=command.image_type,
                        prompt_text=command.prompt_text,
                        reference_asset_ids=[
                            str(asset_id) for asset_id in command.reference_asset_ids
                        ],
                        excluded_reference_aliases=list(prompt_item.excluded_reference_aliases),
                        original_object_key=f"generated/{attempt.id}/original",
                        preview_object_key=f"generated/{attempt.id}/preview.webp",
                        save_state="awaiting_result",
                    )
                )
            db.flush()

            for item, attempt, _package, _prompt_item, command in retry_attempts:
                action_digest = _generation_action_input_digest(
                    attempt_id=attempt.id,
                    request_digest=command.request_digest,
                )
                schedule_action_in_transaction(
                    db,
                    action_id=uuid4(),
                    task_id=task.id,
                    scope_kind="attempt",
                    scope_id=attempt.id,
                    action_kind=_GENERATION_ACTION_KIND,
                    sequence=1,
                    due_at=now,
                    input_digest_version=_GENERATION_ACTION_DIGEST_VERSION,
                    input_digest=action_digest,
                    task_item_id=item.id,
                    attempt_id=attempt.id,
                )
                item.status = "running"
                item.error_code = None
                item.preview_ready = False
                item.image_id = None
                item.width = None
                item.height = None
                item.updated_at = now

            db.add(
                TaskIdempotencyRecord(
                    owner_user_id=owner_user_id,
                    operation=TASK_RETRY_OPERATION,
                    key_hash=key_hash,
                    request_digest=request_digest,
                    digest_version=REQUEST_DIGEST_VERSION,
                    task_id=task.id,
                    planned_count=len(item_ids),
                    state="active",
                    created_at=now,
                    updated_at=now,
                )
            )
            task.terminal_at = None
            task.expires_at = None
            if task.status != "reconciling":
                task.status = "generating"
            task.updated_at = now
            return _retry_accepted(task.id, item_ids, key_hash)

    def _recover_retry_replay(
        self,
        *,
        owner_user_id: UUID,
        task_id: UUID,
        key_hash: bytes,
        request_digest: bytes,
        item_ids: tuple[UUID, ...],
    ) -> TaskRetryAccepted | None:
        with self._sessions() as db:
            now = db.scalar(select(func.clock_timestamp()))
            record = db.execute(
                select(TaskIdempotencyRecord).where(
                    TaskIdempotencyRecord.owner_user_id == owner_user_id,
                    TaskIdempotencyRecord.operation == TASK_RETRY_OPERATION,
                    TaskIdempotencyRecord.key_hash == key_hash,
                )
            ).scalar_one_or_none()
            if record is None:
                return None
            if record.task_id != task_id or record.request_digest != request_digest:
                raise DomainError(409, "IDEMPOTENCY_CONFLICT", "幂等键已用于其他重试请求")
            if record.state != "active" or record.planned_count != len(item_ids):
                raise DomainError(410, "TASK_EXPIRED", "任务已过期，无法重试")
            task = db.execute(
                select(Task).where(
                    Task.id == task_id,
                    Task.owner_user_id == owner_user_id,
                )
            ).scalar_one_or_none()
            if (
                task is None
                or task.status == "deleting"
                or (task.expires_at is not None and task.expires_at <= now)
            ):
                raise DomainError(410, "TASK_EXPIRED", "任务已过期，无法重试")
            return _retry_accepted(task_id, item_ids, key_hash)

    @staticmethod
    def _to_public_response(task: Task, items: list[TaskItem]) -> TaskResponse:
        if len(items) != task.planned_count:
            raise DomainError(503, "TASK_STATE_UNAVAILABLE", "任务状态暂不可用")
        succeeded = sum(item.status == "succeeded" for item in items)
        failed = sum(item.status == "failed" for item in items)
        unresolved = sum(item.status in {"reconciling", "unknown"} for item in items)
        pending = sum(item.status in {"queued", "running", "saving"} for item in items)
        if succeeded + failed + pending + unresolved != task.planned_count:
            raise DomainError(503, "TASK_STATE_UNAVAILABLE", "任务状态暂不可用")
        public_items = [
            TaskItemResponse(
                task_item_id=item.id,
                position=item.position,
                image_type=item.image_type,
                status=("reconciling" if item.status == "unknown" else item.status),  # type: ignore[arg-type]
                image_id=item.image_id,
                preview_ready=item.preview_ready,
                width=item.width,
                height=item.height,
                error=_ITEM_PUBLIC_ERRORS.get(item.error_code) if item.error_code else None,
            )
            for item in items
        ]
        return TaskResponse(
            task_id=task.id,
            status=task.status,  # type: ignore[arg-type]
            counts=TaskCounts(
                planned=task.planned_count,
                succeeded=succeeded,
                failed=failed,
                pending=pending,
                unresolved=unresolved,
            ),
            items=public_items,
        )

    @classmethod
    def _to_history_entry(cls, task: Task, items: list[TaskItem]) -> TaskHistoryEntry:
        public = cls._to_public_response(task, items)
        return TaskHistoryEntry(
            task_id=public.task_id,
            status=public.status,
            created_at=task.created_at,
            counts=public.counts,
            items=public.items,
        )

    def _recover_submission(
        self,
        *,
        owner_user_id: UUID,
        key_hash: bytes,
        request_digest: bytes,
        recommendation_id: UUID,
    ) -> TaskAccepted | _ExpiredReplay | None:
        with self._sessions() as db, db.begin():
            owner = db.execute(
                select(User)
                .where(User.id == owner_user_id, User.status == "active")
                .with_for_update()
            ).scalar_one_or_none()
            if owner is None:
                raise DomainError(401, "IDENTITY_NOT_FOUND", "需要有效身份")
            now = db.scalar(select(func.clock_timestamp()))
            record = self._find_idempotency_record(
                db,
                owner_user_id=owner_user_id,
                key_hash=key_hash,
                for_update=True,
            )
            if record is not None:
                return self._resolve_idempotency_replay(
                    db, record, request_digest=request_digest, now=now
                )
            consumption = db.execute(
                select(TaskRecommendationConsumption)
                .where(
                    TaskRecommendationConsumption.recommendation_id == recommendation_id,
                    TaskRecommendationConsumption.owner_user_id == owner_user_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if consumption is None:
                return None
            if consumption.request_digest != request_digest:
                raise DomainError(409, "RECOMMENDATION_ALREADY_USED", "此推荐已用于不同的任务输入")
            task = db.execute(
                select(Task).where(
                    Task.id == consumption.task_id,
                    Task.owner_user_id == owner_user_id,
                )
            ).scalar_one_or_none()
            if self._task_is_expired(task, now) or consumption.state == "expired":
                consumption.state = "expired"
                consumption.updated_at = now
                return _ExpiredReplay(consumption.task_id)
            db.add(
                TaskIdempotencyRecord(
                    owner_user_id=owner_user_id,
                    operation=TASK_CREATE_OPERATION,
                    key_hash=key_hash,
                    request_digest=request_digest,
                    digest_version=REQUEST_DIGEST_VERSION,
                    task_id=consumption.task_id,
                    planned_count=consumption.planned_count,
                    state="active",
                    created_at=now,
                    updated_at=now,
                )
            )
            db.flush()
            return self._accepted(task)


def retry_request_digest(task_id: UUID, item_ids: tuple[UUID, ...]) -> bytes:
    canonical = json.dumps(
        {
            "digest_version": REQUEST_DIGEST_VERSION,
            "task_id": str(task_id),
            "failed_item_ids": sorted(str(item_id) for item_id in item_ids),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def _retry_attempt_id(task_id: UUID, item_id: UUID, key_hash: bytes) -> UUID:
    return uuid5(
        _RETRY_ATTEMPT_NAMESPACE,
        f"{task_id}:{item_id}:{key_hash.hex()}",
    )


def _retry_accepted(
    task_id: UUID,
    item_ids: tuple[UUID, ...],
    key_hash: bytes,
) -> TaskRetryAccepted:
    return TaskRetryAccepted(
        task_id=task_id,
        attempts=[
            TaskRetryAttempt(
                task_item_id=item_id,
                attempt_id=_retry_attempt_id(task_id, item_id, key_hash),
            )
            for item_id in item_ids
        ],
    )


def _encode_history_cursor(created_at: datetime, task_id: UUID) -> str:
    if created_at.tzinfo is None or created_at.utcoffset() != timedelta(0):
        raise ValueError("history cursor timestamp must be UTC")
    payload = json.dumps(
        [
            _HISTORY_CURSOR_VERSION,
            created_at.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            str(task_id),
        ],
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_history_cursor(value: str) -> _HistoryCursor:
    if not value or len(value) > _HISTORY_CURSOR_MAX_LENGTH:
        raise ValueError("history cursor has an invalid length")
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("ascii"))
        if (
            not isinstance(payload, list)
            or len(payload) != 3
            or isinstance(payload[0], bool)
            or payload[0] != _HISTORY_CURSOR_VERSION
            or not isinstance(payload[1], str)
            or not isinstance(payload[2], str)
        ):
            raise ValueError
        timestamp_text = payload[1]
        if not timestamp_text.endswith("Z"):
            raise ValueError
        created_at = datetime.fromisoformat(timestamp_text[:-1] + "+00:00")
        if created_at.utcoffset() != timedelta(0):
            raise ValueError
        return _HistoryCursor(created_at=created_at, task_id=UUID(payload[2]))
    except (binascii.Error, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("history cursor is malformed") from None


def _task_reference_asset_aliases(task: Task) -> dict[str, UUID]:
    snapshots = task.reference_assets_snapshot
    if not isinstance(snapshots, list) or not 1 <= len(snapshots) <= 4:
        raise DomainError(409, "PROMPT_NOT_RETRYABLE", "任务提示词不可用于重试")
    try:
        expected_slots = ("PRIMARY", "AUX01", "AUX02", "AUX03")[: len(snapshots)]
        aliases: dict[str, UUID] = {}
        for position, (entry, expected_slot) in enumerate(
            zip(snapshots, expected_slots, strict=True)
        ):
            if (
                not isinstance(entry, dict)
                or isinstance(entry.get("position"), bool)
                or entry.get("position") != position
                or entry.get("slot") != expected_slot
                or not isinstance(entry.get("model_asset_id"), str)
            ):
                raise ValueError
            aliases[expected_slot] = UUID(entry["model_asset_id"])
    except (TypeError, ValueError):
        raise DomainError(409, "PROMPT_NOT_RETRYABLE", "任务提示词不可用于重试") from None
    if len(set(aliases.values())) != len(snapshots):
        raise DomainError(409, "PROMPT_NOT_RETRYABLE", "任务提示词不可用于重试")
    return aliases


def _retry_generation_command(
    *,
    task: Task,
    item: TaskItem,
    package: TaskPromptPackage,
    prompt_item: TaskPromptItem,
    request_key: UUID,
    snapshot_aliases: dict[str, UUID],
) -> GenerationCommand:
    raw_reference_ids = prompt_item.allowed_reference_asset_ids
    allowed_aliases = prompt_item.allowed_reference_aliases
    excluded_aliases = prompt_item.excluded_reference_aliases
    if (
        prompt_item.task_id != task.id
        or prompt_item.task_item_id != item.id
        or prompt_item.image_type != item.image_type
        or prompt_item.position != item.position
        or package.id != prompt_item.package_id
        or package.task_id != task.id
        or not isinstance(package.validator_version, str)
        or (
            package.validator_version != PROMPT_PACKAGE_VALIDATOR_VERSION
            and package.validator_version not in PROMPT_PACKAGE_LEGACY_VALIDATOR_VERSIONS
        )
        or package.package_digest_version < 1
        or len(package.package_digest) != 32
        or not package.chatflow_contract_version.strip()
        or not prompt_item.design_prompt.strip()
        or not prompt_item.identity_prompt.strip()
        or not isinstance(raw_reference_ids, list)
        or not isinstance(allowed_aliases, list)
        or not isinstance(excluded_aliases, list)
        or any(not isinstance(alias, str) for alias in allowed_aliases + excluded_aliases)
        or allowed_aliases != list(snapshot_aliases)
        or excluded_aliases
        or len(raw_reference_ids) != len(snapshot_aliases)
        or any(not isinstance(value, str) for value in raw_reference_ids)
    ):
        raise DomainError(409, "PROMPT_NOT_RETRYABLE", "任务提示词不可用于重试")
    try:
        reference_ids = tuple(UUID(value) for value in raw_reference_ids)
        expected_aliases = {asset_id: alias for alias, asset_id in snapshot_aliases.items()}
        if (
            reference_ids != tuple(snapshot_aliases.values())
            or [expected_aliases[asset_id] for asset_id in reference_ids] != allowed_aliases
            or prompt_item.identity_anchor_asset_id != snapshot_aliases["PRIMARY"]
        ):
            raise ValueError
        return GenerationCommand(
            request_key=request_key,
            image_type=item.image_type,
            prompt_package_version=package.chatflow_contract_version,
            prompt_text=f"{prompt_item.design_prompt}\n\n{prompt_item.identity_prompt}",
            reference_asset_ids=reference_ids,
        )
    except (TypeError, ValueError):
        raise DomainError(409, "PROMPT_NOT_RETRYABLE", "任务提示词不可用于重试") from None


def _generation_action_input_digest(*, attempt_id: UUID, request_digest: bytes) -> bytes:
    # Keep this byte-for-byte compatible with the R5 generation action digest.
    canonical = json.dumps(
        {
            "action_kind": _GENERATION_ACTION_KIND,
            "attempt_id": str(attempt_id),
            "request_digest": request_digest.hex(),
            "sequence": 1,
            "extra": {},
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).digest()
