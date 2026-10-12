from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, timedelta
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.business_session_models import BusinessSession
from app.db.chatflow_generation_models import (
    ChatflowGenerationPermit,
    ChatflowGenerationRequest,
    ChatflowPromptPackage,
)
from app.db.chatflow_v34_models import (
    ChatflowV34RunEndEvidence,
    ChatflowV34SessionState,
    ChatflowV34StageEventReceipt,
)
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ImageAsset, ReferenceSet, ReferenceSetImage
from app.db.task_models import Task, TaskItem, TaskItemAttempt
from app.integrations.dify.chatflow_v34_http import DifyV34FormInput
from app.schemas.business_sessions import (
    BusinessSessionSelectionRequest,
    ChatflowRecommendationPayload,
    ChatflowReferenceIdentity,
    DifyOriginalRecommendation,
)
from app.schemas.chatflow_v34 import (
    ChatflowV34StageEvent,
    ChatflowV34StageEventAccepted,
    V34IncompletePayload,
    V34ParsedSelection,
    V34PromptContextResponse,
    V34PromptReference,
    V34PromptResolvedPreferences,
    V34PromptSelection,
    V34RecommendationReadyPayload,
    V34SelectionAppliedPayload,
    V34TerminalPayload,
    V34WorkflowFailurePayload,
    chatflow_v34_stage_event_digest,
)
from app.schemas.recommendations import IMAGE_TYPES, PLATFORMS
from app.services.chatflow_actions import (
    CHATFLOW_RUN_OBSERVATION_SEQUENCE_BASE,
    chatflow_v34_selection_action_digest,
    schedule_chatflow_v34_run_observation_in_transaction,
)
from app.services.chatflow_v34_conversion import recommendation_from_prepare_audit
from app.services.chatflow_v34_prompt_contract import (
    prompt_package_submission_id,
    selection_evidence_matches_action,
)
from app.services.tasks import validate_dify_chatflow_task_snapshot

_FORM_FIELDS: tuple[tuple[str, str], ...] = (
    ("choice_1", "select"),
    ("custom_1", "paragraph"),
    ("choice_2", "select"),
    ("choice_3", "select"),
    ("custom_3", "paragraph"),
    ("choice_4", "select"),
    ("custom_4", "paragraph"),
    ("choice_5", "select"),
    ("custom_5", "paragraph"),
    ("image_types", "paragraph"),
)
_CUSTOM_OPTION = "custom|||自定义"
_DESIGN_OPTION = "D|||由视觉审核 Agent 自由设计"


def v34_form_definition_digest(
    fields: Sequence[DifyV34FormInput],
    _action_ids: Sequence[str] = (),
    _expiration_time: int | None = None,
) -> bytes:
    canonical = json.dumps(
        [
            {
                "name": item.output_variable_name,
                "type": item.input_type,
                "options": list(item.option_values),
            }
            for item in fields
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def validate_v34_form_definition(
    fields: Sequence[DifyV34FormInput],
    action_ids: Sequence[str],
    *,
    recommendation: Recommendation | None,
) -> bool:
    if len(fields) != len(_FORM_FIELDS) or not {"submit", "cancel"}.issubset(action_ids):
        return False
    for field, (expected_name, expected_type) in zip(fields, _FORM_FIELDS, strict=True):
        if field.output_variable_name != expected_name or field.input_type != expected_type:
            return False
        if len(field.option_values) != len(set(field.option_values)):
            return False
        if expected_type == "paragraph" and field.option_values:
            return False
        if expected_type == "select" and not field.option_values:
            return False
    expected = _expected_form_fields(recommendation) if recommendation is not None else None
    if expected is None:
        return True
    return (
        tuple((item.output_variable_name, item.input_type, item.option_values) for item in fields)
        == expected
    )


def recommendation_form_digest(recommendation: Recommendation) -> bytes | None:
    expected = _expected_form_fields(recommendation)
    if expected is None:
        return None
    # The fixed DSL feeds image_types through a paragraph's variable default.
    # Backend submission validation separately checks its trusted value set;
    # Dify's actual FormDefinition for that paragraph contains no options.
    return v34_form_definition_digest(
        tuple(
            DifyV34FormInput(
                output_variable_name=name,
                input_type=input_type,
                option_values=option_values if input_type == "select" else (),
            )
            for name, input_type, option_values in expected
        )
    )


def _expected_form_fields(
    recommendation: Recommendation | None,
) -> tuple[tuple[str, str, tuple[str, ...]], ...] | None:
    if recommendation is None:
        return None
    if recommendation.status != "ready" or recommendation.original_recommendation is None:
        return None
    try:
        original = DifyOriginalRecommendation.model_validate(
            recommendation.original_recommendation, strict=True
        )
    except (TypeError, ValueError):
        return None
    fields: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for choice_id, recommendation_field in (
        ("choice_1", "language"),
        ("choice_3", "usage_scene"),
        ("choice_4", "visual_style"),
        ("choice_5", "selling_points"),
    ):
        values = original.field_texts[recommendation_field]
        options = tuple(
            [f"{letter}|||{value}" for letter, value in zip("ABC", values, strict=True)]
            + [_DESIGN_OPTION, _CUSTOM_OPTION]
        )
        fields[choice_id] = (choice_id, "select", options)
    fields["choice_2"] = (
        "choice_2",
        "select",
        tuple(f"PLATFORM|||{platform}" for platform in PLATFORMS),
    )
    for name in ("custom_1", "custom_3", "custom_4", "custom_5", "image_types"):
        fields[name] = (name, "paragraph", ())
    return tuple(fields[name] for name, _ in _FORM_FIELDS)


class ChatflowV34StageEventService:
    """Persist v3.4 stage events and process recommendation receipts after run binding."""

    def __init__(self, engine) -> None:
        from sqlalchemy.orm import sessionmaker

        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    def accept(self, event: ChatflowV34StageEvent) -> ChatflowV34StageEventAccepted:
        if not isinstance(event, ChatflowV34StageEvent):
            raise DomainError(422, "CALLBACK_INVALID", "回传内容无效")
        digest = chatflow_v34_stage_event_digest(event).hex()
        if event.digest_version != 1 or digest != event.digest:
            raise DomainError(422, "CALLBACK_INVALID", "回传内容无效")
        try:
            with self._sessions() as db, db.begin():
                session_hint = db.execute(
                    select(BusinessSession.owner_user_id, BusinessSession.reference_set_id).where(
                        BusinessSession.id == event.business_session_id
                    )
                ).one_or_none()
                if session_hint is None:
                    raise DomainError(404, "BUSINESS_SESSION_NOT_FOUND", "流程不可用")
                owner_user_id, reference_set_id = session_hint
                from app.db.models import User

                owner = db.execute(
                    select(User)
                    .where(User.id == owner_user_id, User.status == "active")
                    .with_for_update()
                ).scalar_one_or_none()
                reference_set = db.execute(
                    select(ReferenceSet)
                    .where(
                        ReferenceSet.id == reference_set_id,
                        ReferenceSet.owner_user_id == owner_user_id,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                session = db.execute(
                    select(BusinessSession)
                    .where(
                        BusinessSession.id == event.business_session_id,
                        BusinessSession.owner_user_id == owner_user_id,
                        BusinessSession.reference_set_id == reference_set_id,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                recommendation = db.execute(
                    select(Recommendation)
                    .where(
                        Recommendation.id == event.recommendation_id,
                        Recommendation.business_session_id == event.business_session_id,
                        Recommendation.owner_user_id == owner_user_id,
                        Recommendation.reference_set_id == reference_set_id,
                        Recommendation.source_kind == "dify_chatflow",
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                state = db.execute(
                    select(ChatflowV34SessionState)
                    .where(ChatflowV34SessionState.business_session_id == event.business_session_id)
                    .with_for_update()
                ).scalar_one_or_none()
                locked_rows = (owner, reference_set, session, recommendation, state)
                if any(item is None for item in locked_rows):
                    raise DomainError(404, "BUSINESS_SESSION_NOT_FOUND", "流程不可用")
                assert reference_set is not None
                assert session is not None
                assert recommendation is not None
                assert state is not None
                if (
                    session.flow_version != event.flow_version
                    or recommendation.id != event.recommendation_id
                    or recommendation.revision != event.recommendation_revision
                    or state.flow_version != event.flow_version
                    or state.dify_app_id == ""
                    or session.dify_user_id != f"bs-{session.id.hex}"
                ):
                    raise DomainError(409, "CALLBACK_STALE", "回传内容已过期")
                existing = db.execute(
                    select(ChatflowV34StageEventReceipt)
                    .where(
                        ChatflowV34StageEventReceipt.business_session_id == session.id,
                        ChatflowV34StageEventReceipt.event_key == event.event_key,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                event_digest = bytes.fromhex(event.digest)
                if existing is not None:
                    return _existing_stage_event_response(existing, event_digest)
                if event.event_type == "recommendation.ready":
                    accepted_recommendation_event = db.execute(
                        select(ChatflowV34StageEventReceipt.id).where(
                            ChatflowV34StageEventReceipt.business_session_id == session.id,
                            ChatflowV34StageEventReceipt.event_type == "recommendation.ready",
                            ChatflowV34StageEventReceipt.disposition == "accepted",
                        )
                    ).first()
                    if accepted_recommendation_event is not None:
                        raise DomainError(
                            409,
                            "RECOMMENDATION_ALREADY_RECEIVED",
                            "当前推荐已收到回传",
                        )
                now = db.scalar(select(func.clock_timestamp()))
                if now is None:
                    raise DomainError(503, "DATABASE_UNAVAILABLE", "流程暂时无法读取")
                disposition = "received_pending"
                reason_code = "V34_RUN_BINDING_PENDING"
                if session.dify_run_id is not None:
                    if session.dify_run_id != event.dify_run_id:
                        disposition = "rejected"
                        reason_code = "DIFY_RUN_ID_MISMATCH"
                    else:
                        disposition, reason_code = _process_stage_event(
                            db,
                            event=event,
                            session=session,
                            reference_set=reference_set,
                            recommendation=recommendation,
                            state=state,
                            now=now,
                        )
                receipt = ChatflowV34StageEventReceipt(
                    business_session_id=session.id,
                    flow_version=event.flow_version,
                    recommendation_id=event.recommendation_id,
                    recommendation_revision=event.recommendation_revision,
                    dify_run_id=event.dify_run_id,
                    node_id=event.node_id,
                    event_key=event.event_key,
                    event_type=event.event_type,
                    event_digest=event_digest,
                    payload=event.payload,
                    disposition=disposition,
                    safe_reason_code=reason_code,
                    processed_at=(now if disposition != "received_pending" else None),
                )
                db.add(receipt)
                db.flush()
                return _accepted_stage_event(receipt)
        except DomainError:
            raise
        except Exception as exc:
            from sqlalchemy.exc import SQLAlchemyError

            if isinstance(exc, SQLAlchemyError):
                raise DomainError(503, "DATABASE_UNAVAILABLE", "回传内容暂时无法保存") from None
            raise


def v34_first_submission_during_run_observation(
    db: Session, *, session: BusinessSession, task: Task
) -> bool:
    """Recognize only the transient observer state of this accepted v3.4 run."""
    from app.schemas.chatflow_v34 import V34_FLOW_VERSION

    if (
        session.flow_version != V34_FLOW_VERSION
        or session.status != "reconciling"
        or task.status != "reconciling"
        or task.terminal_at is not None
    ):
        return False
    state = db.execute(
        select(ChatflowV34SessionState).where(
            ChatflowV34SessionState.business_session_id == session.id
        )
    ).scalar_one_or_none()
    return bool(
        state is not None
        and state.flow_version == V34_FLOW_VERSION
        and state.selection_status == "accepted"
        and state.selection_evidence_kind in {"http_200", "selection_applied"}
        and state.selection_submission_id is not None
        and state.selection_input_digest is not None
        and state.safe_reason_code == "V34_RUN_TERMINAL_NOT_OBSERVED"
    )


class ChatflowV34ContextService:
    """Return private, phase-scoped context only after durable selection creation."""

    def __init__(
        self,
        engine,
        *,
        dify_app_id: str | None = None,
        dify_workflow_id: str | None = None,
    ) -> None:
        from sqlalchemy.orm import sessionmaker

        self._dify_app_id = dify_app_id
        self._dify_workflow_id = dify_workflow_id
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    def get_selection_context(
        self,
        *,
        business_session_id: UUID,
        workflow_run_id: str,
        phase: str,
    ):
        from app.schemas.chatflow_v34 import V34SelectionContextResponse
        from app.services.chatflow_actions import chatflow_v34_selection_action_digest
        from app.services.tasks import validate_dify_chatflow_task_snapshot

        if phase != "selection" or not workflow_run_id or len(workflow_run_id) > 320:
            raise DomainError(422, "V34_CONTEXT_INVALID", "上下文请求无效")
        with self._sessions() as db:
            session = db.get(BusinessSession, business_session_id)
            if session is None or session.flow_version != "visual-v3.4-single-selection":
                raise DomainError(404, "V34_CONTEXT_NOT_FOUND", "流程上下文不可用")
            recommendation = db.execute(
                select(Recommendation).where(
                    Recommendation.business_session_id == session.id,
                    Recommendation.source_kind == "dify_chatflow",
                )
            ).scalar_one_or_none()
            reference_set = db.get(ReferenceSet, session.reference_set_id)
            state = db.execute(
                select(ChatflowV34SessionState).where(
                    ChatflowV34SessionState.business_session_id == session.id
                )
            ).scalar_one_or_none()
            task = db.execute(
                select(Task).where(
                    Task.business_session_id == session.id,
                    Task.owner_user_id == session.owner_user_id,
                    Task.provenance_kind == "dify_chatflow",
                )
            ).scalar_one_or_none()
            if (
                recommendation is None
                or reference_set is None
                or state is None
                or task is None
                or session.dify_run_id != workflow_run_id
                or recommendation.dify_run_id != workflow_run_id
                or recommendation.status != "ready"
                or recommendation.revision != task.recommendation_revision
                or state.form_status != "consumed"
                or state.selection_status not in {"pending", "unknown", "accepted"}
                or state.selection_submission_id is None
            ):
                raise DomainError(409, "V34_CONTEXT_NOT_READY", "选择上下文尚未持久确认")
            action = db.execute(
                select(Action).where(Action.id == state.selection_submission_id)
            ).scalar_one_or_none()
            if (
                action is None
                or action.task_id != task.id
                or action.business_session_id != session.id
                or action.scope_kind != "business_session"
                or action.action_kind != "dify.chatflow.continue.v1"
                or action.sequence != 2
                or action.status not in {"claimed", "unknown", "completed"}
                or action.input_digest
                != chatflow_v34_selection_action_digest(
                    session=session,
                    recommendation=recommendation,
                    state=state,
                    task=task,
                )
            ):
                raise DomainError(409, "V34_CONTEXT_NOT_READY", "选择上下文尚未持久确认")
            try:
                validate_dify_chatflow_task_snapshot(task, session)
            except DomainError:
                raise DomainError(409, "V34_CONTEXT_INVALID", "选择上下文无效") from None
            deadline = min(
                item
                for item in (
                    session.expires_at,
                    reference_set.expires_at,
                    recommendation.valid_until,
                    recommendation.data_expires_at,
                    state.form_expires_at,
                )
                if item is not None
            )
            return V34SelectionContextResponse(
                contract_version="visual-v34-stage-v1",
                flow_version="visual-v3.4-single-selection",
                phase="selection",
                business_session_id=session.id,
                recommendation_id=recommendation.id,
                recommendation_revision=recommendation.revision,
                dify_run_id=session.dify_run_id,
                task_id=task.id,
                selection_submission_id=state.selection_submission_id,
                selection_status=state.selection_status,
                selection_input_digest=state.selection_input_digest.hex(),
                expires_at=deadline.astimezone(UTC),
            )

    def get_prompt_context(
        self,
        *,
        business_session_id: UUID,
        workflow_run_id: str,
        phase: str,
    ) -> V34PromptContextResponse:
        """Return the immutable H4 input context only after accepted selection evidence."""
        from app.core.domain_errors import DomainError
        from app.db.action_models import Action
        from app.db.chatflow_v34_models import ChatflowV34SessionState
        from app.db.recommendation_models import Recommendation
        from app.db.task_models import Task, TaskItem
        from app.schemas.business_sessions import BusinessSessionSelectionRequest
        from app.schemas.chatflow_v34 import V34_FLOW_VERSION

        if phase != "prompt" or not workflow_run_id or len(workflow_run_id) > 320:
            raise DomainError(422, "V34_CONTEXT_INVALID", "上下文请求无效")
        with self._sessions() as db:
            session = db.get(BusinessSession, business_session_id)
            if session is None or session.flow_version != V34_FLOW_VERSION:
                raise DomainError(404, "V34_CONTEXT_NOT_FOUND", "流程上下文不可用")
            recommendation = db.execute(
                select(Recommendation).where(
                    Recommendation.business_session_id == session.id,
                    Recommendation.source_kind == "dify_chatflow",
                )
            ).scalar_one_or_none()
            reference_set = db.get(ReferenceSet, session.reference_set_id)
            state = db.execute(
                select(ChatflowV34SessionState).where(
                    ChatflowV34SessionState.business_session_id == session.id
                )
            ).scalar_one_or_none()
            task = db.execute(
                select(Task).where(
                    Task.business_session_id == session.id,
                    Task.owner_user_id == session.owner_user_id,
                    Task.provenance_kind == "dify_chatflow",
                )
            ).scalar_one_or_none()
            task_items = (
                db.execute(
                    select(TaskItem)
                    .where(TaskItem.task_id == task.id)
                    .order_by(TaskItem.position, TaskItem.id)
                )
                .scalars()
                .all()
                if task is not None
                else []
            )
            pending_v34_run_observation = bool(
                task is not None
                and v34_first_submission_during_run_observation(db, session=session, task=task)
            )
            if pending_v34_run_observation and task is not None:
                task_item_ids = [item.id for item in task_items]
                has_package = (
                    db.execute(
                        select(ChatflowPromptPackage.id)
                        .where(ChatflowPromptPackage.task_id == task.id)
                        .limit(1)
                    ).first()
                    is not None
                )
                has_generation_request = (
                    db.execute(
                        select(ChatflowGenerationRequest.id)
                        .where(ChatflowGenerationRequest.task_id == task.id)
                        .limit(1)
                    ).first()
                    is not None
                )
                has_generation_permit = (
                    db.execute(
                        select(ChatflowGenerationPermit.permit_id)
                        .where(ChatflowGenerationPermit.task_id == task.id)
                        .limit(1)
                    ).first()
                    is not None
                )
                has_attempt = bool(task_item_ids) and (
                    db.execute(
                        select(TaskItemAttempt.id)
                        .where(TaskItemAttempt.task_item_id.in_(task_item_ids))
                        .limit(1)
                    ).first()
                    is not None
                )
                pending_v34_run_observation = (
                    not has_package
                    and not has_generation_request
                    and not has_generation_permit
                    and not has_attempt
                    and bool(task_items)
                    and all(
                        item.status == "queued" and item.image_id is None and not item.preview_ready
                        for item in task_items
                    )
                )
            if (
                recommendation is None
                or reference_set is None
                or state is None
                or task is None
                or session.status != "task_created"
                and not pending_v34_run_observation
                or session.start_outcome != "accepted"
                or session.dify_run_id != workflow_run_id
                or recommendation.dify_run_id != workflow_run_id
                or recommendation.status != "ready"
                or recommendation.valid_until is None
                or recommendation.data_expires_at is None
                or recommendation.revision != task.recommendation_revision
                or reference_set.status != "ready"
                or reference_set.owner_user_id != session.owner_user_id
                or task.status not in {"queued", "prompting"}
                and not pending_v34_run_observation
                or task.terminal_at is not None
                or state.form_status != "consumed"
                or state.selection_status != "accepted"
                or state.selection_submission_id is None
                or state.selection_input_digest is None
                or state.selection_evidence_kind not in {"http_200", "selection_applied"}
                or self._dify_app_id is None
                or self._dify_workflow_id is None
                or state.dify_app_id != self._dify_app_id
                or state.dify_workflow_id != self._dify_workflow_id
            ):
                raise DomainError(409, "V34_CONTEXT_NOT_READY", "提示词上下文尚未确认")

            continuation = db.execute(
                select(Action).where(Action.id == state.selection_submission_id)
            ).scalar_one_or_none()
            if (
                continuation is None
                or continuation.task_id != task.id
                or continuation.business_session_id != session.id
                or continuation.scope_kind != "business_session"
                or continuation.scope_id != session.id
                or continuation.action_kind != "dify.chatflow.continue.v1"
                or continuation.sequence != 2
                or continuation.input_digest_version != 1
                or continuation.input_digest
                != chatflow_v34_selection_action_digest(
                    session=session,
                    recommendation=recommendation,
                    state=state,
                    task=task,
                )
                or not selection_evidence_matches_action(
                    evidence_kind=state.selection_evidence_kind,
                    action_status=continuation.status,
                    last_outcome=continuation.last_outcome,
                )
            ):
                raise DomainError(409, "V34_CONTEXT_NOT_READY", "提示词上下文尚未确认")

            try:
                validate_dify_chatflow_task_snapshot(task, session)
                selection_snapshot = task.request_snapshot.get("selection")
                if not isinstance(selection_snapshot, dict) or set(selection_snapshot) != {
                    "recommendation_id",
                    "recommendation_revision",
                    "preferences",
                    "image_types",
                }:
                    raise ValueError("Task selection snapshot is malformed")
                selection = BusinessSessionSelectionRequest.model_validate_json(
                    json.dumps(
                        selection_snapshot,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                    strict=True,
                )
                original = DifyOriginalRecommendation.model_validate(
                    recommendation.original_recommendation, strict=True
                )
                task_original = task.recommendation_snapshot.get("original_recommendation")
                if task_original != original.model_dump(mode="json"):
                    raise ValueError("Task recommendation snapshot changed")
                resolved_preferences = V34PromptResolvedPreferences.model_validate(
                    task.resolved_preferences, strict=True
                )
                if (
                    selection.recommendation_id != recommendation.id
                    or selection.recommendation_revision != recommendation.revision
                    or selection.image_types != task.image_types
                    or task.planned_count != len(task.image_types)
                    or [item.position for item in task_items]
                    != list(range(1, task.planned_count + 1))
                    or [item.image_type for item in task_items] != list(task.image_types)
                ):
                    raise ValueError("Task selection linkage changed")
            except (TypeError, ValueError, DomainError):
                raise DomainError(409, "V34_CONTEXT_INVALID", "提示词上下文无效") from None

            snapshots = task.reference_assets_snapshot
            if (
                not isinstance(snapshots, list)
                or snapshots != session.reference_assets_snapshot
                or not 1 <= len(snapshots) <= 4
            ):
                raise DomainError(409, "V34_CONTEXT_INVALID", "参考图清单无效")
            rows = db.execute(
                select(ReferenceSetImage, ImageAsset)
                .join(ImageAsset, ImageAsset.id == ReferenceSetImage.model_asset_id)
                .where(ReferenceSetImage.reference_set_id == reference_set.id)
                .order_by(ReferenceSetImage.position, ReferenceSetImage.id)
            ).all()
            if len(rows) != len(snapshots):
                raise DomainError(409, "V34_CONTEXT_INVALID", "参考图清单无效")
            reference_ids = ("PRIMARY", "AUX01", "AUX02", "AUX03")
            references: list[V34PromptReference] = []
            for position, ((reference, asset), snapshot) in enumerate(
                zip(rows, snapshots, strict=True)
            ):
                ref_id = reference_ids[position]
                if (
                    not isinstance(snapshot, dict)
                    or reference.owner_user_id != task.owner_user_id
                    or reference.reference_set_id != reference_set.id
                    or reference.position != position
                    or reference.slot != ref_id
                    or reference.model_asset_id != asset.id
                    or asset.owner_user_id != task.owner_user_id
                    or asset.asset_kind != "reference_model_input"
                    or reference.content_sha256 != asset.content_sha256
                    or str(asset.id) != snapshot.get("model_asset_id")
                    or asset.content_sha256 != snapshot.get("content_sha256")
                ):
                    raise DomainError(409, "V34_CONTEXT_INVALID", "参考图清单无效")
                references.append(
                    V34PromptReference(
                        ref_id=ref_id,
                        order=position + 1,
                        asset_id=asset.id,
                        content_sha256=asset.content_sha256,
                    )
                )

            now = _v34_db_now(db)
            deadlines = [
                session.expires_at,
                reference_set.expires_at,
                recommendation.valid_until,
                recommendation.data_expires_at,
            ]
            if task.expires_at is not None:
                deadlines.append(task.expires_at)
            if state.form_expires_at is not None:
                deadlines.append(state.form_expires_at)
            deadline = min(deadlines)
            if deadline <= now:
                raise DomainError(410, "V34_CONTEXT_EXPIRED", "提示词上下文已失效")

            return V34PromptContextResponse(
                contract_version="visual-v34-stage-v1",
                flow_version=V34_FLOW_VERSION,
                phase="prompt",
                business_session_id=session.id,
                recommendation_id=recommendation.id,
                recommendation_revision=recommendation.revision,
                dify_run_id=session.dify_run_id,
                task_id=task.id,
                selection_submission_id=state.selection_submission_id,
                selection_status="accepted",
                selection_evidence_kind=state.selection_evidence_kind,
                selection_input_digest=state.selection_input_digest.hex(),
                prompt_submission_id=prompt_package_submission_id(state.selection_submission_id),
                selection=V34PromptSelection(
                    preferences=selection.preferences,
                    image_types=selection.image_types,
                ),
                resolved_preferences=resolved_preferences,
                original_recommendation=original,
                image_types=list(task.image_types),
                references=references,
                expires_at=deadline.astimezone(UTC),
            )


def _v34_db_now(db: Session):
    value = db.scalar(select(func.clock_timestamp()))
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        raise DomainError(503, "DATABASE_UNAVAILABLE", "流程暂时无法读取")
    return value.astimezone(UTC)


def process_pending_v34_stage_events(
    db: Session,
    *,
    session: BusinessSession,
    reference_set: ReferenceSet,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
) -> None:
    receipts = (
        db.execute(
            select(ChatflowV34StageEventReceipt)
            .where(
                ChatflowV34StageEventReceipt.business_session_id == session.id,
                ChatflowV34StageEventReceipt.disposition == "received_pending",
            )
            .order_by(ChatflowV34StageEventReceipt.received_at, ChatflowV34StageEventReceipt.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    now = db.scalar(select(func.clock_timestamp()))
    if now is None:
        raise ValueError("database time is unavailable")
    for receipt in receipts:
        if receipt.dify_run_id != session.dify_run_id:
            receipt.disposition = "rejected"
            receipt.safe_reason_code = "DIFY_RUN_ID_MISMATCH"
            receipt.processed_at = now
            continue
        event_payload = receipt.payload
        if receipt.event_type == "recommendation.ready":
            disposition, reason_code = _accept_recommendation(
                db,
                payload=event_payload,
                session=session,
                reference_set=reference_set,
                recommendation=recommendation,
                state=state,
                now=now,
            )
        elif receipt.event_type == "selection.applied":
            disposition, reason_code = _accept_selection_applied(
                db,
                payload=event_payload,
                event_contract_version=receipt.flow_version,
                business_session_id=session.id,
                recommendation_id=recommendation.id,
                recommendation_revision=recommendation.revision,
                dify_run_id=receipt.dify_run_id,
                session=session,
                recommendation=recommendation,
                state=state,
                now=now,
            )
        elif receipt.event_type in {
            "pre_generation.incomplete",
            "selection.cancelled",
            "selection.expired",
            "workflow.failed",
        }:
            disposition, reason_code = _process_v34_terminal_event(
                db,
                event_type=receipt.event_type,
                payload=event_payload,
                node_id=receipt.node_id,
                session=session,
                reference_set=reference_set,
                recommendation=recommendation,
                state=state,
                dify_run_id=receipt.dify_run_id,
                recommendation_revision=receipt.recommendation_revision,
                now=now,
            )
        else:
            # Later V4-V6 events are held until their package owns the transition.
            continue
        receipt.disposition = disposition
        receipt.safe_reason_code = reason_code
        receipt.processed_at = now


def _process_stage_event(
    db: Session,
    *,
    event: ChatflowV34StageEvent,
    session: BusinessSession,
    reference_set: ReferenceSet,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
    now,
) -> tuple[str, str | None]:
    if event.event_type == "recommendation.ready":
        return _accept_recommendation(
            db,
            payload=event.payload,
            session=session,
            reference_set=reference_set,
            recommendation=recommendation,
            state=state,
            now=now,
        )
    if event.event_type == "selection.applied":
        return _accept_selection_applied(
            db,
            payload=event.payload,
            event_contract_version=event.contract_version,
            business_session_id=event.business_session_id,
            recommendation_id=event.recommendation_id,
            recommendation_revision=event.recommendation_revision,
            dify_run_id=event.dify_run_id,
            session=session,
            recommendation=recommendation,
            state=state,
            now=now,
        )
    if event.event_type in {
        "pre_generation.incomplete",
        "selection.cancelled",
        "selection.expired",
        "workflow.failed",
    }:
        return _process_v34_terminal_event(
            db,
            event_type=event.event_type,
            payload=event.payload,
            node_id=event.node_id,
            session=session,
            reference_set=reference_set,
            recommendation=recommendation,
            state=state,
            dify_run_id=event.dify_run_id,
            recommendation_revision=event.recommendation_revision,
            now=now,
        )
    return "received_pending", "V34_EVENT_HANDLER_PENDING"


def _process_v34_terminal_event(
    db: Session,
    *,
    event_type: str,
    payload: dict[str, object],
    node_id: str,
    session: BusinessSession,
    reference_set: ReferenceSet,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
    dify_run_id: str,
    recommendation_revision: int,
    now,
) -> tuple[str, str | None]:
    if (
        session.flow_version != "visual-v3.4-single-selection"
        or session.dify_run_id != dify_run_id
        or recommendation.dify_run_id != dify_run_id
        or recommendation.revision != recommendation_revision
        or state.flow_version != session.flow_version
    ):
        return "rejected", "V34_TERMINAL_EVENT_BINDING_MISMATCH"

    # A late event from the same run must not replace an already committed
    # terminal outcome (for example, prepare failure arriving after cancel).
    if session.status in {"failed", "expired", "cancelled"} or state.form_status in {
        "expired",
        "cancelled",
    }:
        return "rejected", "V34_SESSION_ALREADY_TERMINAL"

    if event_type == "selection.cancelled":
        try:
            terminal = V34TerminalPayload.model_validate(payload, strict=True)
        except (TypeError, ValueError):
            return "rejected", "V34_CANCEL_PAYLOAD_INVALID"
        if (
            terminal.reason_code != "FORM_CANCELLED"
            or node_id != "selection_cancelled_callback"
            or state.selection_status != "not_started"
            or state.form_status != "cancelling"
            or recommendation.status != "ready"
        ):
            return "rejected", "V34_CANCEL_STATE_MISMATCH"
        if _v34_preselection_task_or_evidence_exists(db, session.id):
            return "rejected", "V34_CANCEL_AFTER_SELECTION"
        from app.services.chatflow_actions import (
            CHATFLOW_CANCEL_ACTION,
            CHATFLOW_CANCEL_SEQUENCE,
            chatflow_action_id,
            chatflow_v34_cancel_action_digest,
        )

        cancel_action = db.execute(
            select(Action)
            .where(
                Action.business_session_id == session.id,
                Action.scope_kind == "business_session",
                Action.scope_id == session.id,
                Action.action_kind == CHATFLOW_CANCEL_ACTION,
                Action.sequence == CHATFLOW_CANCEL_SEQUENCE,
            )
            .with_for_update()
        ).scalar_one_or_none()
        try:
            expected_cancel_digest = chatflow_v34_cancel_action_digest(
                session=session, recommendation=recommendation, state=state
            )
        except ValueError:
            expected_cancel_digest = None
        if (
            cancel_action is None
            or cancel_action.id
            != chatflow_action_id(
                business_session_id=session.id, action_kind=CHATFLOW_CANCEL_ACTION
            )
            or cancel_action.task_id is not None
            or cancel_action.status not in {"claimed", "unknown", "completed"}
            or cancel_action.input_digest != expected_cancel_digest
        ):
            return "rejected", "V34_CANCEL_INTENT_MISSING"
        if cancel_action.status == "unknown":
            # The Dify callback is durable acceptance evidence for the one
            # already-dispatched cancel step; do not leave it blocking restart.
            cancel_action.status = "completed"
            cancel_action.updated_at = now
        if _v34_generation_tracking_exists(db, session.id):
            return "rejected", "V34_GENERATION_ALREADY_STARTED"
        _cancel_v34_pending_cancel_recoveries(
            db,
            session=session,
            recommendation=recommendation,
            state=state,
            now=now,
        )
        state.form_status = "cancelled"
        state.safe_reason_code = None
        state.updated_at = now
        session.status = "cancelled"
        session.updated_at = now
        return "accepted", None

    if event_type == "selection.expired":
        try:
            terminal = V34TerminalPayload.model_validate(payload, strict=True)
        except (TypeError, ValueError):
            return "rejected", "V34_EXPIRY_PAYLOAD_INVALID"
        if (
            terminal.reason_code != "FORM_EXPIRED"
            or node_id != "selection_expired_callback"
            or state.selection_status != "not_started"
            or state.form_status not in {"ready", "cancelling"}
            or state.form_expires_at is None
            or state.form_expires_at > now
            or recommendation.status != "ready"
        ):
            return "rejected", "V34_EXPIRY_STATE_MISMATCH"
        if _v34_preselection_task_or_evidence_exists(db, session.id):
            return "rejected", "V34_EXPIRY_AFTER_SELECTION"
        if _v34_generation_tracking_exists(db, session.id):
            return "rejected", "V34_GENERATION_ALREADY_STARTED"
        _cancel_v34_pending_cancel_recoveries(
            db,
            session=session,
            recommendation=recommendation,
            state=state,
            now=now,
        )
        if state.form_status == "cancelling":
            from app.services.chatflow_actions import (
                CHATFLOW_CANCEL_ACTION,
                CHATFLOW_CANCEL_SEQUENCE,
                chatflow_action_id,
                chatflow_v34_cancel_action_digest,
            )

            cancel_action = db.execute(
                select(Action)
                .where(
                    Action.business_session_id == session.id,
                    Action.scope_kind == "business_session",
                    Action.scope_id == session.id,
                    Action.action_kind == CHATFLOW_CANCEL_ACTION,
                    Action.sequence == CHATFLOW_CANCEL_SEQUENCE,
                )
                .with_for_update()
            ).scalar_one_or_none()
            try:
                expected_cancel_digest = chatflow_v34_cancel_action_digest(
                    session=session, recommendation=recommendation, state=state
                )
            except ValueError:
                expected_cancel_digest = None
            if (
                cancel_action is not None
                and cancel_action.id
                == chatflow_action_id(
                    business_session_id=session.id, action_kind=CHATFLOW_CANCEL_ACTION
                )
                and cancel_action.task_id is None
                and cancel_action.input_digest == expected_cancel_digest
                and cancel_action.status == "unknown"
            ):
                # A durable terminal expiry receipt makes a previous cancel
                # response ambiguity irrelevant; no POST retry is permitted.
                cancel_action.status = "completed"
                cancel_action.updated_at = now
        state.form_status = "expired"
        state.safe_reason_code = "V34_FORM_EXPIRED"
        state.updated_at = now
        session.status = "expired"
        session.updated_at = now
        recommendation.status = "expired"
        recommendation.updated_at = now
        return "accepted", "V34_FORM_EXPIRED"

    if event_type == "pre_generation.incomplete":
        try:
            incomplete = V34IncompletePayload.model_validate(payload, strict=True)
        except (TypeError, ValueError):
            return "rejected", "V34_INCOMPLETE_PAYLOAD_INVALID"
        if node_id != "ready_0" or incomplete.reason_code != "PRODUCT_IDENTITY_CONFLICT":
            return "rejected", "V34_INCOMPLETE_STAGE_MISMATCH"
        task = db.execute(
            select(Task)
            .where(
                Task.business_session_id == session.id,
                Task.owner_user_id == session.owner_user_id,
                Task.reference_set_id == reference_set.id,
                Task.recommendation_id == recommendation.id,
                Task.provenance_kind == "dify_chatflow",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if (
            task is None
            or state.selection_status != "accepted"
            or state.selection_submission_id is None
            or state.selection_input_digest is None
            or recommendation.status != "ready"
        ):
            return "rejected", "V34_INCOMPLETE_TASK_MISMATCH"
        if not _terminalize_v34_task_without_generation(
            db,
            task=task,
            session=session,
            recommendation=recommendation,
            state=state,
            reason_code="PRODUCT_IDENTITY_CONFLICT",
            now=now,
        ):
            return "rejected", "V34_GENERATION_ALREADY_STARTED"
        return "accepted", "PRODUCT_IDENTITY_CONFLICT"

    try:
        failure = V34WorkflowFailurePayload.model_validate(payload, strict=True)
    except (TypeError, ValueError):
        return "rejected", "V34_FAILURE_PAYLOAD_INVALID"
    if node_id != "workflow_failed_callback" or failure.failed_node_id != failure.phase:
        return "rejected", "V34_FAILURE_STAGE_MISMATCH"
    safe_reason = _V34_PHASE_FAILURE_REASONS[failure.phase]
    task = db.execute(
        select(Task)
        .where(
            Task.business_session_id == session.id,
            Task.owner_user_id == session.owner_user_id,
            Task.reference_set_id == reference_set.id,
            Task.recommendation_id == recommendation.id,
            Task.provenance_kind == "dify_chatflow",
        )
        .with_for_update()
    ).scalar_one_or_none()
    if failure.phase in {"audit", "prepare"}:
        if task is not None or recommendation.status not in {"analyzing", "ready"}:
            return "rejected", "V34_FAILURE_STATE_MISMATCH"
        if _v34_generation_tracking_exists(db, session.id):
            return "rejected", "V34_GENERATION_ALREADY_STARTED"
        _fail_v34_recommendation_and_session(
            session=session,
            recommendation=recommendation,
            state=state,
            safe_reason=safe_reason,
            now=now,
        )
        return "accepted", safe_reason
    if task is None or state.selection_status != "accepted" or recommendation.status != "ready":
        return "rejected", "V34_FAILURE_TASK_MISMATCH"
    if not _terminalize_v34_task_without_generation(
        db,
        task=task,
        session=session,
        recommendation=recommendation,
        state=state,
        reason_code=safe_reason,
        now=now,
    ):
        return "rejected", "V34_GENERATION_ALREADY_STARTED"
    return "accepted", safe_reason


_V34_PHASE_FAILURE_REASONS = {
    "audit": "V34_AUDIT_FAILED",
    "prepare": "V34_PREPARE_FAILED",
    "resolve_0": "V34_RESOLVE_FAILED",
    "review_0": "V34_REVIEW_FAILED",
    "design": "V34_DESIGN_FAILED",
    "deliver": "V34_DELIVER_FAILED",
}


def _cancel_v34_pending_cancel_recoveries(
    db: Session,
    *,
    session: BusinessSession,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
    now,
) -> None:
    """Retire unclaimed GET-only recovery steps after a durable terminal receipt."""
    if state.form_status != "cancelling":
        return
    from uuid import uuid5

    from app.services.chatflow_actions import (
        CHATFLOW_ACTION_NAMESPACE,
        CHATFLOW_CANCEL_RECOVERY_ACTION,
        CHATFLOW_CANCEL_RECOVERY_MAX_SEQUENCE,
        CHATFLOW_CANCEL_RECOVERY_SEQUENCE_BASE,
        chatflow_v34_cancel_recovery_action_digest,
    )

    recoveries = (
        db.execute(
            select(Action)
            .where(
                Action.business_session_id == session.id,
                Action.scope_kind == "business_session",
                Action.scope_id == session.id,
                Action.action_kind == CHATFLOW_CANCEL_RECOVERY_ACTION,
                Action.sequence.between(
                    CHATFLOW_CANCEL_RECOVERY_SEQUENCE_BASE,
                    CHATFLOW_CANCEL_RECOVERY_MAX_SEQUENCE,
                ),
            )
            .order_by(Action.sequence, Action.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    for action in recoveries:
        expected_id = uuid5(
            CHATFLOW_ACTION_NAMESPACE,
            f"{CHATFLOW_CANCEL_RECOVERY_ACTION}:{session.id}:{action.sequence}",
        )
        try:
            expected_digest = chatflow_v34_cancel_recovery_action_digest(
                session=session,
                recommendation=recommendation,
                state=state,
                sequence=action.sequence,
            )
        except ValueError:
            continue
        if (
            action.id != expected_id
            or action.task_id is not None
            or action.input_digest != expected_digest
        ):
            continue
        if action.status in {"scheduled", "unknown"}:
            action.status = "cancelled"
            action.last_outcome = "accepted"
            action.last_error_code = None
            action.claim_token = None
            action.lease_until = None
            action.completed_at = now
            action.updated_at = now


def _v34_preselection_task_or_evidence_exists(db: Session, business_session_id: UUID) -> bool:
    task_exists = db.execute(
        select(Task.id).where(Task.business_session_id == business_session_id).limit(1)
    ).first()
    if task_exists is not None:
        return True
    selection_receipt_exists = db.execute(
        select(ChatflowV34StageEventReceipt.id)
        .where(
            ChatflowV34StageEventReceipt.business_session_id == business_session_id,
            ChatflowV34StageEventReceipt.event_type == "selection.applied",
        )
        .limit(1)
    ).first()
    return selection_receipt_exists is not None


def _v34_generation_tracking_exists(db: Session, business_session_id: UUID) -> bool:
    request_ids = select(ChatflowGenerationRequest.id).where(
        ChatflowGenerationRequest.business_session_id == business_session_id
    )
    if db.execute(request_ids.limit(1)).first() is not None:
        return True
    permit = db.execute(
        select(ChatflowGenerationPermit.permit_id)
        .join(
            ChatflowGenerationRequest,
            ChatflowGenerationRequest.id == ChatflowGenerationPermit.generation_request_id,
        )
        .where(ChatflowGenerationRequest.business_session_id == business_session_id)
        .limit(1)
    ).first()
    if permit is not None:
        return True
    generation_action = db.execute(
        select(Action.id)
        .where(
            Action.business_session_id == business_session_id,
            Action.action_kind.like("chatflow.generation.%"),
        )
        .limit(1)
    ).first()
    return generation_action is not None


def _terminalize_v34_task_without_generation(
    db: Session,
    *,
    task: Task,
    session: BusinessSession,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
    reason_code: str,
    item_error_code: str = "V34_PRE_GENERATION_FAILED",
    now,
) -> bool:
    if _v34_generation_tracking_exists(db, session.id):
        return False
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
    if (
        len(items) != task.planned_count
        or any(item.image_id is not None or item.status == "succeeded" for item in items)
        or task.status not in {"queued", "prompting", "needs_input", "reconciling"}
    ):
        return False
    task.status = "failed"
    task.terminal_at = now
    task.expires_at = now + timedelta(days=task.retention_days)
    task.updated_at = now
    for item in items:
        if item.status not in {"succeeded", "failed"}:
            item.status = "failed"
            item.error_code = item_error_code
            item.updated_at = now
    recommendation.status = "failed"
    recommendation.failure_code = reason_code
    recommendation.state = {"stage": "failed", "safe_reason_code": reason_code}
    recommendation.updated_at = now
    session.status = "failed"
    session.updated_at = now
    state.safe_reason_code = reason_code
    state.updated_at = now
    return True


def record_v34_observed_workflow_failure(
    db: Session,
    *,
    task: Task,
    session: BusinessSession,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
    dify_run_id: str,
    terminal_event: str,
    workflow_status: str | None,
    now,
) -> bool:
    """Persist an internal, same-run terminal observation without widening callbacks."""
    if not (
        (terminal_event == "workflow_finished" and workflow_status == "failed")
        or (terminal_event == "workflow_failed" and workflow_status in {None, "failed"})
    ):
        return False
    if (
        session.flow_version != "visual-v3.4-single-selection"
        or session.dify_run_id != dify_run_id
        or recommendation.business_session_id != session.id
        or recommendation.dify_run_id != dify_run_id
        or recommendation.status != "ready"
        or recommendation.revision != task.recommendation_revision
        or task.business_session_id != session.id
        or task.owner_user_id != session.owner_user_id
        or task.recommendation_id != recommendation.id
        or task.provenance_kind != "dify_chatflow"
        or state.business_session_id != session.id
        or state.flow_version != session.flow_version
        or state.selection_status != "accepted"
        or state.selection_evidence_kind not in {"http_200", "selection_applied"}
        or state.selection_submission_id is None
        or state.selection_input_digest is None
        or session.status in {"failed", "expired", "cancelled"}
    ):
        return False
    if _v34_generation_tracking_exists(db, session.id):
        return False

    reason_code = "V34_DIFY_WORKFLOW_FAILED"
    payload = {
        "source": "dify_sse_terminal_observation_v1",
        "terminal_event": terminal_event,
        "workflow_status": workflow_status,
        "reason_code": reason_code,
    }
    event_key = f"observed-workflow-failure:{hashlib.sha256(dify_run_id.encode()).hexdigest()}"
    canonical = json.dumps(
        {
            "business_session_id": str(session.id),
            "recommendation_id": str(recommendation.id),
            "recommendation_revision": recommendation.revision,
            "dify_run_id": dify_run_id,
            "node_id": "workflow_finished",
            "event_key": event_key,
            "event_type": "workflow.failed",
            "payload": payload,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    event_digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    existing = db.execute(
        select(ChatflowV34StageEventReceipt)
        .where(
            ChatflowV34StageEventReceipt.business_session_id == session.id,
            ChatflowV34StageEventReceipt.event_key == event_key,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        return (
            existing.event_type == "workflow.failed"
            and existing.node_id == "workflow_finished"
            and existing.event_digest == event_digest
            and existing.disposition == "accepted"
        )
    if not _terminalize_v34_task_without_generation(
        db,
        task=task,
        session=session,
        recommendation=recommendation,
        state=state,
        reason_code=reason_code,
        now=now,
    ):
        return False
    db.add(
        ChatflowV34StageEventReceipt(
            business_session_id=session.id,
            flow_version=session.flow_version,
            recommendation_id=recommendation.id,
            recommendation_revision=recommendation.revision,
            dify_run_id=dify_run_id,
            node_id="workflow_finished",
            event_key=event_key,
            event_type="workflow.failed",
            event_digest=event_digest,
            payload=payload,
            disposition="accepted",
            safe_reason_code=reason_code,
            processed_at=now,
        )
    )
    db.flush()
    return True


def record_v34_observed_workflow_end_without_generation(
    db: Session,
    *,
    task: Task,
    session: BusinessSession,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
    dify_run_id: str,
    terminal_event: str,
    workflow_status: str | None,
    now,
) -> bool:
    """Close a bound selection only after a trusted same-run partial success."""
    reason_code = "V34_WORKFLOW_ENDED_WITHOUT_GENERATION"
    if not (
        terminal_event == "workflow_finished"
        and workflow_status in {"partial-succeeded", "succeeded"}
    ):
        return False
    if (
        session.flow_version != "visual-v3.4-single-selection"
        or session.dify_run_id != dify_run_id
        or recommendation.business_session_id != session.id
        or recommendation.dify_run_id != dify_run_id
        or recommendation.status != "ready"
        or recommendation.revision != task.recommendation_revision
        or task.business_session_id != session.id
        or task.owner_user_id != session.owner_user_id
        or task.reference_set_id != session.reference_set_id
        or task.recommendation_id != recommendation.id
        or task.provenance_kind != "dify_chatflow"
        or recommendation.owner_user_id != session.owner_user_id
        or recommendation.reference_set_id != session.reference_set_id
        or state.business_session_id != session.id
        or state.flow_version != session.flow_version
        or not state.dify_app_id
        or not state.dify_workflow_id
        or state.selection_status != "accepted"
        or state.selection_evidence_kind not in {"http_200", "selection_applied"}
        or state.selection_submission_id is None
        or state.selection_input_digest is None
        or session.status not in {"task_created", "reconciling"}
        or state.safe_reason_code not in {None, "V34_RUN_TERMINAL_NOT_OBSERVED"}
    ):
        return False

    canonical = json.dumps(
        {
            "business_session_id": str(session.id),
            "task_id": str(task.id),
            "recommendation_id": str(recommendation.id),
            "recommendation_revision": recommendation.revision,
            "owner_user_id": str(session.owner_user_id),
            "reference_set_id": str(session.reference_set_id),
            "flow_version": session.flow_version,
            "dify_app_id": state.dify_app_id,
            "dify_published_workflow_id": state.dify_workflow_id,
            "dify_run_id": dify_run_id,
            "selection_submission_id": str(state.selection_submission_id),
            "selection_input_digest": state.selection_input_digest.hex(),
            "terminal_event": terminal_event,
            "terminal_status": workflow_status,
            "business_reason_code": reason_code,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    event_digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    existing = db.execute(
        select(ChatflowV34RunEndEvidence)
        .where(
            ChatflowV34RunEndEvidence.business_session_id == session.id,
            ChatflowV34RunEndEvidence.dify_run_id == dify_run_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        return (
            existing.task_id == task.id
            and existing.recommendation_id == recommendation.id
            and existing.recommendation_revision == recommendation.revision
            and existing.selection_submission_id == state.selection_submission_id
            and existing.selection_input_digest == state.selection_input_digest
            and existing.terminal_event == terminal_event
            and existing.terminal_status == workflow_status
            and existing.event_digest == event_digest
            and task.status == "failed"
            and session.status == "failed"
            and recommendation.failure_code == reason_code
        )

    if _v34_generation_tracking_exists(db, session.id):
        return False
    if db.execute(
        select(ChatflowPromptPackage.id)
        .where(ChatflowPromptPackage.business_session_id == session.id)
        .limit(1)
    ).first():
        return False
    if db.execute(
        select(TaskItemAttempt.id)
        .join(TaskItem, TaskItem.id == TaskItemAttempt.task_item_id)
        .where(TaskItem.task_id == task.id)
        .limit(1)
    ).first():
        return False
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
    if len(items) != task.planned_count or any(
        item.image_id is not None or item.status not in {"queued", "running", "reconciling"}
        for item in items
    ):
        return False

    if not _terminalize_v34_task_without_generation(
        db,
        task=task,
        session=session,
        recommendation=recommendation,
        state=state,
        reason_code=reason_code,
        item_error_code=reason_code,
        now=now,
    ):
        return False
    db.add(
        ChatflowV34RunEndEvidence(
            business_session_id=session.id,
            task_id=task.id,
            recommendation_id=recommendation.id,
            recommendation_revision=recommendation.revision,
            owner_user_id=session.owner_user_id,
            reference_set_id=session.reference_set_id,
            flow_version=session.flow_version,
            dify_app_id=state.dify_app_id,
            dify_published_workflow_id=state.dify_workflow_id,
            dify_run_id=dify_run_id,
            selection_submission_id=state.selection_submission_id,
            selection_input_digest=state.selection_input_digest,
            terminal_event=terminal_event,
            terminal_status=workflow_status,
            event_digest=event_digest,
            observed_at=now,
            business_reason_code=reason_code,
            closure_outcome="failed_without_generation",
        )
    )
    db.flush()
    return True


def _fail_v34_recommendation_and_session(
    *,
    session: BusinessSession,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
    safe_reason: str,
    now,
) -> None:
    recommendation.status = "failed"
    recommendation.failure_code = safe_reason
    recommendation.state = {"stage": "failed", "safe_reason_code": safe_reason}
    recommendation.updated_at = now
    session.status = "failed"
    session.updated_at = now
    state.safe_reason_code = safe_reason
    state.updated_at = now


def _accept_selection_applied(
    db: Session,
    *,
    payload: dict[str, object],
    event_contract_version: str,
    business_session_id: UUID,
    recommendation_id: UUID,
    recommendation_revision: int,
    dify_run_id: str,
    session: BusinessSession,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
    now,
) -> tuple[str, str | None]:
    if (
        event_contract_version != "visual-v34-stage-v1"
        or session.id != business_session_id
        or session.flow_version != "visual-v3.4-single-selection"
        or session.dify_run_id != dify_run_id
        or recommendation.id != recommendation_id
        or recommendation.revision != recommendation_revision
        or recommendation.status != "ready"
        or recommendation.dify_run_id != dify_run_id
        or state.flow_version != session.flow_version
        or state.form_status != "consumed"
        or state.selection_status not in {"pending", "unknown", "accepted"}
    ):
        return "rejected", "V34_SELECTION_BINDING_MISMATCH"
    try:
        applied = V34SelectionAppliedPayload.model_validate_json(
            json.dumps(payload, ensure_ascii=False, allow_nan=False), strict=True
        )
        submission_id = applied.selection_submission_id
    except (TypeError, ValueError):
        return "rejected", "V34_SELECTION_PAYLOAD_INVALID"
    if (
        state.selection_submission_id != submission_id
        or submission_id is None
        or state.selection_inputs is None
        or state.selection_input_digest is None
    ):
        return "rejected", "V34_SELECTION_SUBMISSION_MISMATCH"

    task = db.execute(
        select(Task)
        .where(
            Task.business_session_id == session.id,
            Task.owner_user_id == session.owner_user_id,
            Task.reference_set_id == session.reference_set_id,
            Task.recommendation_id == recommendation.id,
            Task.provenance_kind == "dify_chatflow",
        )
        .with_for_update()
    ).scalar_one_or_none()
    if task is None or task.recommendation_revision != recommendation.revision:
        return "rejected", "V34_SELECTION_TASK_MISMATCH"
    action = db.execute(
        select(Action).where(Action.id == submission_id).with_for_update()
    ).scalar_one_or_none()
    if (
        action is None
        or action.task_id != task.id
        or action.business_session_id != session.id
        or action.scope_kind != "business_session"
        or action.scope_id != session.id
        or action.action_kind != "dify.chatflow.continue.v1"
        or action.sequence != 2
        or action.status not in {"claimed", "unknown", "completed"}
        or action.last_outcome == "definitely_rejected"
    ):
        return "rejected", "V34_SELECTION_ACTION_NOT_SUBMITTED"

    try:
        from app.services.chatflow_actions import chatflow_v34_selection_action_digest
        from app.services.chatflow_v34_conversion import (
            form_0_definition_for_recommendation,
            order_form_0_submission,
            selection_to_form_0,
            validate_form_0_submission,
        )
        from app.services.tasks import validate_dify_chatflow_task_snapshot

        if action.input_digest != chatflow_v34_selection_action_digest(
            session=session,
            recommendation=recommendation,
            state=state,
            task=task,
        ):
            raise ValueError("selection Action digest differs")
        snapshot = validate_dify_chatflow_task_snapshot(task, session)
        request = BusinessSessionSelectionRequest.model_validate_json(
            json.dumps(
                task.request_snapshot.get("selection"),
                ensure_ascii=False,
                allow_nan=False,
            ),
            strict=True,
        )
        original = DifyOriginalRecommendation.model_validate(
            recommendation.original_recommendation, strict=True
        )
        conversion = selection_to_form_0(request, original)
        validate_form_0_submission(
            conversion.form_values,
            form_0_definition_for_recommendation(original),
        )
        stored_form_values = order_form_0_submission(state.selection_inputs)
        expected_parse_state = V34ParsedSelection(
            preferences=conversion.normalized_preferences,
            image_types=request.image_types,
        )
        if (
            snapshot.get("dify_run_id") != dify_run_id
            or task.request_digest is None
            or stored_form_values != conversion.form_values
            or applied.parse_state.model_dump(mode="json")
            != expected_parse_state.model_dump(mode="json")
        ):
            raise ValueError("selection parse state differs from the stored Task")
    except (TypeError, ValueError, DomainError):
        return "rejected", "V34_SELECTION_PARSE_STATE_MISMATCH"

    if state.selection_status == "accepted":
        if state.selection_evidence_kind == "selection_applied" and action.status == "unknown":
            action.status = "completed"
            action.updated_at = now
        schedule_chatflow_v34_run_observation_in_transaction(
            db,
            session=session,
            recommendation=recommendation,
            state=state,
            task=task,
            sequence=CHATFLOW_RUN_OBSERVATION_SEQUENCE_BASE,
            due_at=now,
        )
        return "accepted", None
    generation_request_exists = db.execute(
        select(ChatflowGenerationRequest.id)
        .where(ChatflowGenerationRequest.task_id == task.id)
        .limit(1)
    ).first()
    generation_action_exists = db.execute(
        select(Action.id)
        .where(
            Action.task_id == task.id,
            or_(
                Action.action_kind.like("generation.%"),
                Action.action_kind.like("chatflow.generation.%"),
            ),
        )
        .limit(1)
    ).first()
    if (
        task.status not in {"queued", "reconciling"}
        or generation_request_exists is not None
        or generation_action_exists is not None
    ):
        return "rejected", "V34_SELECTION_TASK_NOT_PENDING"
    state.selection_status = "accepted"
    state.selection_evidence_kind = "selection_applied"
    state.selection_accepted_at = now
    state.safe_reason_code = None
    state.updated_at = now
    session.status = "task_created"
    session.updated_at = now
    task.status = "queued"
    task.updated_at = now
    if action.status == "unknown":
        action.status = "completed"
        action.updated_at = now
    schedule_chatflow_v34_run_observation_in_transaction(
        db,
        session=session,
        recommendation=recommendation,
        state=state,
        task=task,
        sequence=CHATFLOW_RUN_OBSERVATION_SEQUENCE_BASE,
        due_at=now,
    )
    return "accepted", None


def _accept_recommendation(
    db: Session,
    *,
    payload: dict[str, object],
    session: BusinessSession,
    reference_set: ReferenceSet,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
    now,
) -> tuple[str, str | None]:
    if recommendation.status != "analyzing":
        return "rejected", "V34_RECOMMENDATION_ALREADY_ACCEPTED"
    if (
        session.expires_at <= now
        or recommendation.data_expires_at <= now
        or reference_set.expires_at is None
        or reference_set.expires_at <= now
        or session.status not in {"starting", "awaiting_recommendation", "reconciling"}
    ):
        return "rejected", "V34_RECOMMENDATION_EXPIRED"
    try:
        ready = V34RecommendationReadyPayload.model_validate(payload, strict=True)
        parsed = recommendation_from_prepare_audit(ready.audit)
        if parsed.original_recommendation.category_snapshot != reference_set.product_category:
            raise ValueError("recommendation category differs from the saved category")
        references = [
            ChatflowReferenceIdentity.model_validate(
                {
                    key: (
                        UUID(str(reference[key]))
                        if key
                        in {
                            "reference_set_image_id",
                            "source_asset_id",
                            "model_asset_id",
                        }
                        else reference[key]
                    )
                    for key in (
                        "reference_set_image_id",
                        "position",
                        "slot",
                        "source_asset_id",
                        "model_asset_id",
                        "content_sha256",
                        "source_sha256",
                    )
                },
                strict=True,
            )
            for reference in session.reference_assets_snapshot
        ]
        valid_until = min(
            session.expires_at,
            reference_set.expires_at,
            recommendation.data_expires_at,
        ).astimezone(UTC)
        callback_payload = ChatflowRecommendationPayload(
            reference_set_id=session.reference_set_id,
            reference_asset_digest=session.reference_asset_digest.hex(),
            reference_assets=references,
            original_recommendation=parsed.original_recommendation,
            public_options=parsed.public_options,
            image_types=list(IMAGE_TYPES),
            valid_until=valid_until,
        )
        from app.services.business_sessions import BusinessSessionService

        BusinessSessionService._validate_callback_identity(
            session, reference_set, callback_payload, now=now
        )
    except (TypeError, ValueError, DomainError):
        recommendation.status = "failed"
        recommendation.failure_code = "V34_RECOMMENDATION_INVALID"
        recommendation.state = {
            "stage": "failed",
            "safe_reason_code": "V34_RECOMMENDATION_INVALID",
        }
        session.status = "failed"
        state.safe_reason_code = "V34_RECOMMENDATION_INVALID"
        return "rejected", "V34_RECOMMENDATION_INVALID"

    recommendation.status = "ready"
    recommendation.original_recommendation = parsed.original_recommendation.model_dump(mode="json")
    recommendation.public_options = parsed.public_options.model_dump(mode="json")
    recommendation.image_types = list(IMAGE_TYPES)
    recommendation.valid_until = valid_until
    recommendation.ready_at = now
    recommendation.audit = {
        "source_kind": "dify_chatflow",
        "contract_version": "visual-v34-stage-v1",
    }
    recommendation.state = {
        "stage": "ready",
        "provenance_kind": "dify_chatflow",
        "reference_asset_digest": session.reference_asset_digest.hex(),
        "stage_contract": "visual-v34-stage-v1",
    }
    recommendation.updated_at = now
    if session.start_outcome == "accepted":
        session.status = "recommendation_ready"
    else:
        session.status = "reconciling"
    session.updated_at = now
    expected_digest = recommendation_form_digest(recommendation)
    if (
        state.form_token is not None
        and state.form_definition_digest is not None
        and expected_digest is not None
    ):
        if state.form_definition_digest != expected_digest:
            state.form_status = "rejected"
            state.safe_reason_code = "V34_FORM_DEFINITION_MISMATCH"
            session.status = "failed"
            return "rejected", "V34_FORM_DEFINITION_MISMATCH"
        if state.form_expires_at is not None and state.form_expires_at <= now:
            state.form_status = "expired"
            state.safe_reason_code = "V34_FORM_EXPIRED"
            session.status = "expired"
            return "accepted", "V34_FORM_EXPIRED"
        state.form_status = "ready"
        state.safe_reason_code = None
    return "accepted", None


def _accepted_stage_event(
    receipt: ChatflowV34StageEventReceipt, *, replayed: bool = False
) -> ChatflowV34StageEventAccepted:
    status = "replayed" if replayed and receipt.disposition == "accepted" else receipt.disposition
    return ChatflowV34StageEventAccepted(
        status=status,
        business_session_id=receipt.business_session_id,
        event_key=receipt.event_key,
    )


def _existing_stage_event_response(
    receipt: ChatflowV34StageEventReceipt, event_digest: bytes
) -> ChatflowV34StageEventAccepted:
    if receipt.event_digest != event_digest:
        raise DomainError(409, "EVENT_CONTENT_CONFLICT", "事件内容与已保存记录冲突")
    return _accepted_stage_event(receipt, replayed=True)


__all__ = [
    "ChatflowV34ContextService",
    "ChatflowV34StageEventService",
    "process_pending_v34_stage_events",
    "recommendation_form_digest",
    "validate_v34_form_definition",
    "v34_form_definition_digest",
]
