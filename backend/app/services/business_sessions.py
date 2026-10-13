from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.business_session_models import BusinessSession
from app.db.chatflow_generation_models import (
    ChatflowGenerationAcceptanceEvidence,
    ChatflowGenerationDelivery,
    ChatflowGenerationEventReceipt,
    ChatflowGenerationPermit,
    ChatflowGenerationRequest,
    ChatflowGenerationResultManifest,
    ChatflowGenerationTracking,
)
from app.db.chatflow_reference_files import ChatflowDifyReferenceFile
from app.db.chatflow_v34_models import (
    ChatflowV34SessionState,
    ChatflowV34StageEventReceipt,
)
from app.db.models import User
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ReferenceSet
from app.db.task_models import Task, TaskIdempotencyRecord, TaskItem, TaskItemAttempt
from app.schemas.business_sessions import (
    BusinessSelectionAccepted,
    BusinessSessionCancelResponse,
    BusinessSessionCreated,
    BusinessSessionHistoryPage,
    BusinessSessionRecommendation,
    BusinessSessionResponse,
    BusinessSessionSelectionRequest,
    BusinessSessionV34Created,
    BusinessSessionV34Response,
    ChatflowBusinessEvent,
    ChatflowBusinessEventAccepted,
    ChatflowFailurePayload,
    ChatflowRecommendationPayload,
    DifyOriginalRecommendation,
    chatflow_business_event_digest,
    chatflow_reference_asset_digest,
    chatflow_reference_snapshot_digest,
)
from app.schemas.chatflow_v34 import (
    V34_FLOW_VERSION,
)
from app.schemas.recommendations import IMAGE_TYPES, RecommendationPublicOptions
from app.services.business_questions import BusinessQuestionService, project_business_question
from app.services.chatflow_actions import (
    CHATFLOW_CANCEL_ACTION,
    CHATFLOW_CANCEL_SEQUENCE,
    CHATFLOW_CONTINUE_ACTION,
    CHATFLOW_CONTINUE_SEQUENCE,
    chatflow_action_digest,
    chatflow_action_id,
    chatflow_reference_upload_action_id,
    chatflow_v34_cancel_action_digest,
    chatflow_v34_selection_action_digest,
    schedule_chatflow_reference_upload_in_transaction,
    schedule_chatflow_v34_cancel_in_transaction,
    set_chatflow_v34_selection_input_digest,
)
from app.services.retention_policy import (
    RetentionPolicyUnavailable,
    load_retention_policy_in_transaction,
)
from app.services.session_history import can_end_unstarted, list_session_history
from app.services.tasks import (
    TaskOptions,
    TaskService,
    chatflow_selection_digest,
    idempotency_key_hash,
)
from app.workers.action_processor import schedule_action_in_transaction

_SESSION_DIGEST_VERSION = 1


@dataclass(frozen=True, slots=True)
class BusinessSessionOptions:
    max_active_per_owner: int = 1
    idempotency_key_max_length: int = 200
    session_retention_days: int = 60
    dify_app_id: str = "mock-chatflow-v1"
    flow_version: str = "legacy"
    dify_workflow_id: str | None = None

    def __post_init__(self) -> None:
        if self.max_active_per_owner < 1:
            raise ValueError("max_active_per_owner must be positive")
        if not 1 <= self.idempotency_key_max_length <= 200:
            raise ValueError("idempotency key max length must be between 1 and 200")
        if self.session_retention_days < 1:
            raise ValueError("session_retention_days must be positive")
        if not self.dify_app_id or len(self.dify_app_id) > 160:
            raise ValueError("dify_app_id must be between 1 and 160 characters")
        if self.flow_version not in {"legacy", V34_FLOW_VERSION}:
            raise ValueError("unsupported Chatflow flow version")
        if self.flow_version == V34_FLOW_VERSION and (
            not self.dify_workflow_id
            or len(self.dify_workflow_id) > 160
            or self.dify_workflow_id != self.dify_workflow_id.strip()
        ):
            raise ValueError("v3.4 flow requires a fixed Dify workflow identity")


class BusinessSessionService:
    """Persist-before-I/O Chatflow start, callback, selection and public projection."""

    def __init__(
        self,
        engine: Engine,
        *,
        task_service: TaskService | None = None,
        question_service: BusinessQuestionService | None = None,
        options: BusinessSessionOptions | None = None,
    ) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        self.options = options or BusinessSessionOptions()
        self.task_service = task_service or TaskService(engine, TaskOptions())
        self.question_service = question_service or BusinessQuestionService(engine)

    def create_session(
        self, *, owner_user_id: UUID, reference_set_id: UUID, idempotency_key: str
    ) -> BusinessSessionCreated | BusinessSessionV34Created:
        key_hash = idempotency_key_hash(
            idempotency_key, maximum_length=self.options.idempotency_key_max_length
        )
        request_digest = _session_request_digest(reference_set_id, self.options.flow_version)
        try:
            with self._sessions() as db, db.begin():
                owner = db.execute(
                    select(User)
                    .where(User.id == owner_user_id, User.status == "active")
                    .with_for_update()
                ).scalar_one_or_none()
                if owner is None:
                    raise DomainError(401, "IDENTITY_NOT_FOUND", "需要有效身份")
                reference_set = db.execute(
                    select(ReferenceSet)
                    .where(
                        ReferenceSet.id == reference_set_id,
                        ReferenceSet.owner_user_id == owner_user_id,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if reference_set is None:
                    raise DomainError(404, "REFERENCE_SET_NOT_FOUND", "参考组不可用")
                now = _db_now(db)
                existing = db.execute(
                    select(BusinessSession)
                    .where(
                        BusinessSession.owner_user_id == owner_user_id,
                        BusinessSession.idempotency_key_hash == key_hash,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.request_digest != request_digest:
                        raise DomainError(409, "IDEMPOTENCY_CONFLICT", "相同幂等键不能用于不同请求")
                    recommendation = self._recommendation_for_session(db, existing.id)
                    if recommendation is None:
                        raise DomainError(
                            409, "SESSION_STATE_INVALID", "流程状态暂不可用，请重新读取"
                        )
                    return _project_created_session(existing, recommendation.id, "replayed")
                if (
                    reference_set.status != "ready"
                    or reference_set.expires_at is None
                    or reference_set.expires_at <= now
                ):
                    raise DomainError(409, "REFERENCE_SET_UNAVAILABLE", "参考组已失效，请重新上传")
                if self.options.flow_version == V34_FLOW_VERSION:
                    unresolved_v34 = db.execute(
                        select(BusinessSession.id)
                        .where(
                            BusinessSession.owner_user_id == owner_user_id,
                            BusinessSession.reference_set_id == reference_set.id,
                            BusinessSession.flow_version == V34_FLOW_VERSION,
                            BusinessSession.status.in_(
                                {
                                    "starting",
                                    "awaiting_recommendation",
                                    "recommendation_ready",
                                    "task_created",
                                    "reconciling",
                                }
                            ),
                        )
                        .limit(1)
                    ).first()
                    if unresolved_v34 is not None:
                        raise DomainError(
                            429,
                            "BUSINESS_SESSION_RECONCILING",
                            "上一流程仍在确认，暂不能重新开始",
                        )
                    terminal_v34_ids = select(BusinessSession.id).where(
                        BusinessSession.owner_user_id == owner_user_id,
                        BusinessSession.reference_set_id == reference_set.id,
                        BusinessSession.flow_version == V34_FLOW_VERSION,
                        BusinessSession.status.in_({"failed", "expired", "cancelled"}),
                    )
                    generation_requests = (
                        db.execute(
                            select(ChatflowGenerationRequest).where(
                                ChatflowGenerationRequest.business_session_id.in_(terminal_v34_ids)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    closed_request_ids = {
                        request.id
                        for request in generation_requests
                        if _generation_request_closed_for_restart(db, request)
                    }
                    actions = (
                        db.execute(
                            select(Action).where(Action.business_session_id.in_(terminal_v34_ids))
                        )
                        .scalars()
                        .all()
                    )
                    action_blocks_restart = False
                    for action in actions:
                        if action.status in {"scheduled", "claimed"}:
                            action_blocks_restart = True
                            break
                        is_generation_action = action.action_kind.startswith(
                            ("generation.", "chatflow.generation.")
                        )
                        if not is_generation_action and action.status != "unknown":
                            continue
                        if (
                            action.scope_kind == "generation_request"
                            and action.generation_request_id in closed_request_ids
                            and action.action_kind
                            in {
                                "chatflow.generation.recover.v1",
                                "chatflow.generation.expiry_assess.v1",
                            }
                            and action.status in {"unknown", "completed", "cancelled", "rejected"}
                        ):
                            continue
                        action_blocks_restart = True
                        break
                    if action_blocks_restart or len(closed_request_ids) != len(generation_requests):
                        raise DomainError(
                            409,
                            "V34_RESTART_REQUIRES_RECONCILIATION",
                            "上一流程仍有待核记录，不能重新开始",
                        )
                active_count = db.scalar(
                    select(func.count(BusinessSession.id)).where(
                        BusinessSession.owner_user_id == owner_user_id,
                        BusinessSession.expires_at > now,
                        BusinessSession.status.in_(
                            {
                                "starting",
                                "awaiting_recommendation",
                                "recommendation_ready",
                                "task_created",
                                "reconciling",
                            }
                        ),
                        # Session transport state can remain task_created or
                        # reconciling after its Task definitively completes.
                        # Missing or nonterminal Tasks still occupy the slot.
                        ~select(Task.id)
                        .where(
                            Task.business_session_id == BusinessSession.id,
                            Task.status.in_({"succeeded", "partially_succeeded", "failed"}),
                        )
                        .exists(),
                    )
                )
                if (active_count or 0) >= self.options.max_active_per_owner:
                    raise DomainError(429, "BUSINESS_SESSION_LIMIT_REACHED", "已有流程正在处理")

                reference_assets = TaskService._reference_asset_snapshot(
                    db, owner_user_id=owner_user_id, reference_set_id=reference_set.id
                )
                if not reference_assets:
                    raise DomainError(409, "REFERENCE_SET_UNAVAILABLE", "参考组资产暂不可用")
                reference_digest = bytes.fromhex(
                    chatflow_reference_snapshot_digest(reference_assets)
                )
                try:
                    retention = load_retention_policy_in_transaction(db)
                except RetentionPolicyUnavailable:
                    raise DomainError(
                        503, "RETENTION_POLICY_UNAVAILABLE", "流程暂时无法创建"
                    ) from None

                session_id = uuid4()
                recommendation_id = uuid4()
                expiry = min(
                    reference_set.expires_at,
                    now + timedelta(days=self.options.session_retention_days),
                )
                dify_user_id = f"bs-{session_id.hex}"
                business_session = BusinessSession(
                    id=session_id,
                    owner_user_id=owner_user_id,
                    reference_set_id=reference_set.id,
                    idempotency_key_hash=key_hash,
                    request_digest=request_digest,
                    digest_version=_SESSION_DIGEST_VERSION,
                    reference_asset_digest=reference_digest,
                    reference_assets_snapshot=reference_assets,
                    status="starting",
                    flow_version=self.options.flow_version,
                    start_outcome="pending",
                    dify_user_id=dify_user_id,
                    run_revision=1,
                    retention_days=self.options.session_retention_days,
                    retention_policy_revision=retention.revision,
                    expires_at=expiry,
                    created_at=now,
                    updated_at=now,
                )
                recommendation = Recommendation(
                    id=recommendation_id,
                    owner_user_id=owner_user_id,
                    reference_set_id=reference_set.id,
                    business_session_id=session_id,
                    source_kind="dify_chatflow",
                    status="analyzing",
                    revision=1,
                    image_types=list(IMAGE_TYPES),
                    state={
                        "stage": "starting",
                        "provenance_kind": "dify_chatflow",
                        "reference_asset_digest": reference_digest.hex(),
                    },
                    data_expires_at=min(
                        reference_set.expires_at,
                        now + timedelta(days=retention.recommendation_retention_days),
                    ),
                    retention_days=retention.recommendation_retention_days,
                    retention_policy_revision=retention.revision,
                    created_at=now,
                    updated_at=now,
                )
                db.add(business_session)
                db.add(recommendation)
                db.flush()
                if self.options.flow_version == V34_FLOW_VERSION:
                    assert self.options.dify_workflow_id is not None
                    db.add(
                        ChatflowV34SessionState(
                            business_session_id=session_id,
                            flow_version=V34_FLOW_VERSION,
                            dify_app_id=self.options.dify_app_id,
                            dify_workflow_id=self.options.dify_workflow_id,
                            form_status="awaiting_event",
                            selection_status="not_started",
                        )
                    )
                reference_file_rows: list[ChatflowDifyReferenceFile] = []
                for asset in reference_assets:
                    position = int(asset["position"])
                    reference_file_rows.append(
                        ChatflowDifyReferenceFile(
                            business_session_id=session_id,
                            owner_user_id=owner_user_id,
                            reference_set_id=reference_set.id,
                            reference_set_image_id=UUID(str(asset["reference_set_image_id"])),
                            model_asset_id=UUID(str(asset["model_asset_id"])),
                            slot=str(asset["slot"]),
                            position=position,
                            content_sha256=str(asset["content_sha256"]),
                            dify_app_id=self.options.dify_app_id,
                            dify_user_id=dify_user_id,
                            upload_action_id=chatflow_reference_upload_action_id(
                                business_session_id=session_id, position=position
                            ),
                            status="pending",
                        )
                    )
                db.add_all(reference_file_rows)
                db.flush()
                if not reference_file_rows or reference_file_rows[0].slot != "PRIMARY":
                    raise DomainError(409, "REFERENCE_SET_UNAVAILABLE", "参考组资产暂不可用")
                schedule_chatflow_reference_upload_in_transaction(
                    db, reference_file_rows[0], due_at=now
                )
                return _project_created_session(business_session, recommendation_id, "starting")
        except DomainError:
            raise
        except IntegrityError:
            return self._recover_create_race(owner_user_id, key_hash, request_digest)
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "流程暂时无法创建") from None

    def list_sessions(
        self, *, owner_user_id: UUID, limit: int = 20, cursor: str | None = None
    ) -> BusinessSessionHistoryPage:
        if isinstance(limit, bool) or not 1 <= limit <= 50:
            raise DomainError(422, "HISTORY_LIMIT_INVALID", "历史分页数量无效")
        try:
            with self._sessions() as db:
                return list_session_history(db, owner_user_id, _db_now(db), limit, cursor)
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "创作记录暂时无法读取") from None

    def end_unstarted(
        self, *, owner_user_id: UUID, business_session_id: UUID
    ) -> BusinessSessionV34Response:
        """Close a file-upload-only unknown session without issuing any Dify POST."""
        try:
            with self._sessions() as db, db.begin():
                locked = self._lock_session_for_selection(db, owner_user_id, business_session_id)
                if locked is None:
                    raise DomainError(404, "BUSINESS_SESSION_NOT_FOUND", "流程不可用")
                session, _reference, recommendation, now = locked
                state = db.scalar(
                    select(ChatflowV34SessionState)
                    .where(ChatflowV34SessionState.business_session_id == session.id)
                    .with_for_update()
                )
                # Same owner/reference/session lock order as Worker and callbacks.
                db.scalars(
                    select(Action).where(Action.business_session_id == session.id).with_for_update()
                ).all()
                if state is not None and state.safe_reason_code == "PRESTART_UPLOAD_ABANDONED":
                    return self._project_v34_session(db, session, now)
                if not can_end_unstarted(db, session, recommendation):
                    raise DomainError(
                        409,
                        "SESSION_ALREADY_STARTED_OR_ACTIVE",
                        "流程已启动或仍有动作执行，不能结束为未启动流程",
                    )
                session.status = "failed"
                session.start_outcome = "rejected"
                session.updated_at = now
                recommendation.status = "failed"
                recommendation.failure_code = "PRESTART_UPLOAD_ABANDONED"
                recommendation.state = {
                    "stage": "failed",
                    "failure_code": "PRESTART_UPLOAD_ABANDONED",
                    "previous_stage": "reference_upload_unknown",
                    "ended_before_start_at": now.isoformat(),
                    "ended_by_owner": str(owner_user_id),
                }
                state.safe_reason_code = "PRESTART_UPLOAD_ABANDONED"
                state.form_status = "rejected"
                for row in db.scalars(
                    select(ChatflowDifyReferenceFile).where(
                        ChatflowDifyReferenceFile.business_session_id == session.id,
                        ChatflowDifyReferenceFile.status.in_({"pending", "unknown"}),
                    )
                ):
                    row.status = "rejected"
                db.flush()
                return self._project_v34_session(db, session, now)
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "流程暂时无法处理") from None

    def get_session(
        self, *, owner_user_id: UUID, business_session_id: UUID
    ) -> BusinessSessionResponse | BusinessSessionV34Response:
        try:
            with self._sessions() as db:
                now = _db_now(db)
                session = db.execute(
                    select(BusinessSession).where(
                        BusinessSession.id == business_session_id,
                        BusinessSession.owner_user_id == owner_user_id,
                    )
                ).scalar_one_or_none()
                if session is None:
                    raise DomainError(404, "BUSINESS_SESSION_NOT_FOUND", "流程不可用")
                if session.flow_version == V34_FLOW_VERSION:
                    return self._project_v34_session(db, session, now)
                recommendation = self._recommendation_for_session(db, session.id)
                task = db.execute(
                    select(Task).where(Task.business_session_id == session.id)
                ).scalar_one_or_none()
                public_question = project_business_question(
                    db,
                    session=session,
                    recommendation=recommendation,
                    task=task,
                    now=now,
                )
                status = session.status
                if session.expires_at <= now or (
                    recommendation is not None
                    and recommendation.status == "ready"
                    and recommendation.valid_until is not None
                    and recommendation.valid_until <= now
                ):
                    status = "expired"
                public_recommendation = None
                if (
                    recommendation is not None
                    and recommendation.status == "ready"
                    and recommendation.valid_until is not None
                    and recommendation.valid_until > now
                    and recommendation.original_recommendation is not None
                    and recommendation.public_options is not None
                ):
                    try:
                        public_recommendation = BusinessSessionRecommendation(
                            recommendation_id=recommendation.id,
                            recommendation_revision=recommendation.revision,
                            original_recommendation=DifyOriginalRecommendation.model_validate(
                                recommendation.original_recommendation, strict=True
                            ),
                            public_options=RecommendationPublicOptions.model_validate(
                                recommendation.public_options, strict=True
                            ),
                            image_types=recommendation.image_types,
                            valid_until=recommendation.valid_until,
                        )
                    except Exception:
                        raise DomainError(
                            503, "RECOMMENDATION_UNAVAILABLE", "推荐信息暂时无法读取"
                        ) from None
                return BusinessSessionResponse(
                    business_session_id=session.id,
                    status=status,  # type: ignore[arg-type]
                    recommendation=public_recommendation,
                    task_id=task.id if task is not None else None,
                    question=public_question,
                )
        except DomainError:
            raise
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "流程暂时无法读取") from None

    @staticmethod
    def _project_v34_session(
        db: Session, session: BusinessSession, now: datetime
    ) -> BusinessSessionV34Response:
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session.id
            )
        ).scalar_one_or_none()
        if state is None:
            raise DomainError(503, "SESSION_STATE_INVALID", "流程状态暂不可用")
        recommendation = BusinessSessionService._recommendation_for_session(db, session.id)
        task = db.execute(
            select(Task).where(Task.business_session_id == session.id)
        ).scalar_one_or_none()
        expired = session.status not in {"cancelled", "failed", "expired"} and (
            session.expires_at <= now
            or (
                recommendation is not None
                and recommendation.status == "ready"
                and recommendation.valid_until is not None
                and recommendation.valid_until <= now
            )
        )
        status = "expired" if expired else session.status
        public_recommendation = None
        if (
            not expired
            and recommendation is not None
            and recommendation.status == "ready"
            and recommendation.valid_until is not None
            and recommendation.original_recommendation is not None
            and recommendation.public_options is not None
        ):
            try:
                public_recommendation = BusinessSessionRecommendation(
                    recommendation_id=recommendation.id,
                    recommendation_revision=recommendation.revision,
                    original_recommendation=DifyOriginalRecommendation.model_validate(
                        recommendation.original_recommendation, strict=True
                    ),
                    public_options=RecommendationPublicOptions.model_validate(
                        recommendation.public_options, strict=True
                    ),
                    image_types=recommendation.image_types,
                    valid_until=recommendation.valid_until,
                )
            except Exception:
                raise DomainError(
                    503, "RECOMMENDATION_UNAVAILABLE", "推荐信息暂时无法读取"
                ) from None
        phase = _v34_public_phase(session, state, expired=expired)
        from app.services.chatflow_generation_closure import project_generation_completion

        completion = project_generation_completion(db, task.id if task is not None else None)
        closed = completion is not None and completion["reconciliation_status"] in {
            "completed", "completed_with_execution_warning"
        }
        selection_ready = (
            not expired
            and session.status == "recommendation_ready"
            and public_recommendation is not None
            and state.form_status == "ready"
            and state.form_node_id == "form_0"
            and state.form_expires_at is not None
            and state.form_expires_at > now
            and state.selection_status == "not_started"
        )
        return BusinessSessionV34Response(
            business_session_id=session.id,
            flow_version=V34_FLOW_VERSION,
            status=status,  # type: ignore[arg-type]
            phase=phase,
            completion=completion,
            recommendation=public_recommendation,
            task_id=task.id if task is not None else None,
            selection_ready=selection_ready,
            can_end_before_start=(
                recommendation is not None and can_end_unstarted(db, session, recommendation)
            ),
            safe_reason_code=(
                completion["reason_code"] if closed else state.safe_reason_code
                or (
                    "DIFY_REFERENCE_UPLOAD_OUTCOME_UNKNOWN"
                    if recommendation is not None
                    and (recommendation.state or {}).get("stage") == "reference_upload_unknown"
                    else recommendation.failure_code
                    if recommendation is not None
                    else None
                )
            ),
        )

    def ensure_h3_allowed(self, *, owner_user_id: UUID, business_session_id: UUID) -> None:
        """Keep legacy question routes available while rejecting v3.4 H3 traffic."""
        try:
            with self._sessions() as db:
                flow_version = db.scalar(
                    select(BusinessSession.flow_version).where(
                        BusinessSession.id == business_session_id,
                        BusinessSession.owner_user_id == owner_user_id,
                    )
                )
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "流程暂时无法读取") from None
        if flow_version == V34_FLOW_VERSION:
            raise DomainError(409, "V34_H3_EVENT_REJECTED", "此流程不支持补问")

    def accept_business_event(self, event: ChatflowBusinessEvent) -> ChatflowBusinessEventAccepted:
        if not isinstance(event, ChatflowBusinessEvent):
            raise DomainError(422, "CALLBACK_INVALID", "回传内容无效")
        if chatflow_business_event_digest(event) != event.digest:
            raise DomainError(422, "CALLBACK_INVALID", "回传内容无效")
        self._ensure_legacy_callback_allowed(event.business_session_id)
        if event.event_type == "human_input_required":
            return self.question_service.accept_human_input_event(event)
        if event.event_type == "recommendation.ready":
            try:
                payload = ChatflowRecommendationPayload.model_validate_json(
                    json.dumps(event.payload, ensure_ascii=False), strict=True
                )
            except Exception:
                raise DomainError(422, "CALLBACK_INVALID", "回传内容无效") from None
            self._validate_ready_payload(payload)
        else:
            try:
                payload = ChatflowFailurePayload.model_validate_json(
                    json.dumps(event.payload, ensure_ascii=False), strict=True
                )
            except Exception:
                raise DomainError(422, "CALLBACK_INVALID", "回传内容无效") from None

        try:
            with self._sessions() as db, db.begin():
                locked = self._lock_callback_parents(db, event)
                if locked is None:
                    raise DomainError(404, "BUSINESS_SESSION_NOT_FOUND", "流程不可用")
                session, reference_set, recommendation = locked
                if recommendation.callback_event_id is not None:
                    if recommendation.callback_event_id == event.event_id:
                        if (
                            recommendation.callback_digest_version == event.digest_version
                            and recommendation.callback_digest == bytes.fromhex(event.digest)
                        ):
                            return self._callback_accepted(event, "replayed")
                        raise DomainError(409, "EVENT_CONTENT_CONFLICT", "回传内容与已保存事件冲突")
                    raise DomainError(409, "RECOMMENDATION_ALREADY_RECEIVED", "当前推荐已收到回传")

                now = _db_now(db)
                if (
                    event.contract_version != "chatflow-handoff-v1"
                    or event.source != "dify"
                    or event.business_session_id != session.id
                    or event.recommendation_id != recommendation.id
                    or event.recommendation_revision != recommendation.revision
                    or event.dify_run_id != session.dify_run_id
                    or event.dify_run_id != recommendation.dify_run_id
                    or session.status != "awaiting_recommendation"
                    or session.start_outcome != "accepted"
                    or session.expires_at <= now
                    or recommendation.status != "analyzing"
                    or recommendation.data_expires_at <= now
                    or reference_set.status != "ready"
                    or reference_set.expires_at is None
                    or reference_set.expires_at <= now
                ):
                    raise DomainError(409, "CALLBACK_STALE", "推荐回传已过期")

                if event.event_type == "recommendation.failed":
                    assert isinstance(payload, ChatflowFailurePayload)
                    recommendation.status = "failed"
                    recommendation.failure_code = payload.failure_code
                    recommendation.callback_event_id = event.event_id
                    recommendation.callback_digest_version = event.digest_version
                    recommendation.callback_digest = bytes.fromhex(event.digest)
                    recommendation.state = {
                        "stage": "failed",
                        "provenance_kind": "dify_chatflow",
                        "reference_asset_digest": session.reference_asset_digest.hex(),
                        "callback_event_id": str(event.event_id),
                        "callback_source_sequence": event.source_sequence,
                    }
                    session.status = "failed"
                else:
                    assert isinstance(payload, ChatflowRecommendationPayload)
                    self._validate_callback_identity(session, reference_set, payload, now=now)
                    if payload.valid_until > recommendation.data_expires_at:
                        raise DomainError(422, "CALLBACK_INVALID", "回传内容无效")
                    recommendation.status = "ready"
                    recommendation.original_recommendation = (
                        payload.original_recommendation.model_dump(mode="json")
                    )
                    recommendation.public_options = payload.public_options.model_dump(mode="json")
                    recommendation.image_types = list(payload.image_types)
                    recommendation.valid_until = payload.valid_until.astimezone(UTC)
                    recommendation.ready_at = now
                    recommendation.audit = {
                        "source_kind": "dify_chatflow",
                        "contract_version": event.contract_version,
                    }
                    recommendation.callback_event_id = event.event_id
                    recommendation.callback_digest_version = event.digest_version
                    recommendation.callback_digest = bytes.fromhex(event.digest)
                    recommendation.state = {
                        "stage": "ready",
                        "provenance_kind": "dify_chatflow",
                        "reference_asset_digest": session.reference_asset_digest.hex(),
                        "callback_event_id": str(event.event_id),
                        "callback_source_sequence": event.source_sequence,
                        "callback_digest_version": event.digest_version,
                        "callback_digest": event.digest,
                    }
                    session.status = "recommendation_ready"
                recommendation.updated_at = now
                session.updated_at = now
                return self._callback_accepted(event, "accepted")
        except DomainError:
            raise
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "推荐回传暂时无法保存") from None

    def create_selection(
        self,
        *,
        owner_user_id: UUID,
        business_session_id: UUID,
        idempotency_key: str,
        request: BusinessSessionSelectionRequest,
    ) -> BusinessSelectionAccepted:
        try:
            with self._sessions() as db, db.begin():
                locked = self._lock_session_for_selection(db, owner_user_id, business_session_id)
                if locked is None:
                    raise DomainError(404, "BUSINESS_SESSION_NOT_FOUND", "流程不可用")
                session, reference_set, recommendation, now = locked
                if session.flow_version == V34_FLOW_VERSION:
                    return self._create_v34_selection_in_transaction(
                        db,
                        owner_user_id=owner_user_id,
                        session=session,
                        reference_set=reference_set,
                        recommendation=recommendation,
                        idempotency_key=idempotency_key,
                        request=request,
                        now=now,
                    )
                accepted, replayed = self.task_service.create_chatflow_task_in_transaction(
                    db,
                    owner_user_id=owner_user_id,
                    business_session=session,
                    reference_set=reference_set,
                    recommendation=recommendation,
                    idempotency_key=idempotency_key,
                    request=request,
                    now=now,
                )
                db.flush()
                task = db.execute(
                    select(Task)
                    .where(
                        Task.id == accepted.task_id,
                        Task.business_session_id == session.id,
                        Task.owner_user_id == owner_user_id,
                        Task.provenance_kind == "dify_chatflow",
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if task is None:
                    raise DomainError(409, "SESSION_SELECTION_CONFLICT", "任务状态正在确认")
                action_id = chatflow_action_id(
                    business_session_id=session.id, action_kind=CHATFLOW_CONTINUE_ACTION
                )
                action_digest = chatflow_action_digest(
                    action_kind=CHATFLOW_CONTINUE_ACTION,
                    business_session_id=session.id,
                    task_id=task.id,
                    request_digest=task.request_digest,
                )
                if replayed:
                    continuation = db.execute(
                        select(Action)
                        .where(
                            Action.scope_kind == "business_session",
                            Action.scope_id == session.id,
                            Action.sequence == CHATFLOW_CONTINUE_SEQUENCE,
                        )
                        .with_for_update()
                    ).scalar_one_or_none()
                    if (
                        continuation is None
                        or continuation.id != action_id
                        or continuation.task_id != task.id
                        or continuation.business_session_id != session.id
                        or continuation.action_kind != CHATFLOW_CONTINUE_ACTION
                        or continuation.input_digest_version != 1
                        or continuation.input_digest != action_digest
                    ):
                        raise DomainError(
                            409,
                            "SESSION_SELECTION_CONFLICT",
                            "任务续接状态暂不可用，请重新读取流程",
                        )
                else:
                    schedule_action_in_transaction(
                        db,
                        action_id=action_id,
                        task_id=task.id,
                        business_session_id=session.id,
                        scope_kind="business_session",
                        scope_id=session.id,
                        action_kind=CHATFLOW_CONTINUE_ACTION,
                        sequence=CHATFLOW_CONTINUE_SEQUENCE,
                        due_at=now,
                        input_digest_version=1,
                        input_digest=action_digest,
                    )
                if session.status == "recommendation_ready":
                    session.status = "task_created"
                    session.updated_at = now
                return BusinessSelectionAccepted(
                    business_session_id=session.id,
                    task_id=accepted.task_id,
                    status="replayed" if replayed else "queued",
                    planned_count=accepted.planned_count,
                )
        except DomainError:
            raise
        except IntegrityError:
            # The same session/key can be retried to read a committed Task.
            return self._recover_selection(
                owner_user_id, business_session_id, idempotency_key, request
            )
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "选择状态暂时无法确认") from None

    def cancel_before_selection(
        self, *, owner_user_id: UUID, business_session_id: UUID
    ) -> BusinessSessionCancelResponse:
        """Persist one pre-selection cancel intent under the selection row locks."""
        try:
            with self._sessions() as db, db.begin():
                locked = self._lock_session_for_selection(db, owner_user_id, business_session_id)
                if locked is None:
                    raise DomainError(404, "BUSINESS_SESSION_NOT_FOUND", "流程不可用")
                session, reference_set, recommendation, now = locked
                if session.flow_version != V34_FLOW_VERSION:
                    raise DomainError(409, "CANCEL_NOT_SUPPORTED", "当前流程不支持取消")
                state = db.execute(
                    select(ChatflowV34SessionState)
                    .where(ChatflowV34SessionState.business_session_id == session.id)
                    .with_for_update()
                ).scalar_one_or_none()
                task = db.execute(
                    select(Task).where(Task.business_session_id == session.id).limit(1)
                ).scalar_one_or_none()
                accepted_selection_evidence = db.execute(
                    select(ChatflowV34StageEventReceipt.id)
                    .where(
                        ChatflowV34StageEventReceipt.business_session_id == session.id,
                        ChatflowV34StageEventReceipt.event_type == "selection.applied",
                        ChatflowV34StageEventReceipt.disposition == "accepted",
                    )
                    .limit(1)
                ).first()
                if state is None:
                    raise DomainError(409, "SESSION_STATE_INVALID", "流程状态暂不可用")
                if (
                    task is not None
                    or accepted_selection_evidence is not None
                    or state.selection_status != "not_started"
                ):
                    raise DomainError(
                        409,
                        "V34_CANCEL_AFTER_SELECTION",
                        "选择已提交，不能按首次选择前取消",
                    )
                if state.form_status in {"cancelling", "cancelled"}:
                    action = db.execute(
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
                    if (
                        action is None
                        or action.id
                        != chatflow_action_id(
                            business_session_id=session.id,
                            action_kind=CHATFLOW_CANCEL_ACTION,
                        )
                        or action.task_id is not None
                        or action.input_digest
                        != chatflow_v34_cancel_action_digest(
                            session=session,
                            recommendation=recommendation,
                            state=state,
                        )
                    ):
                        raise DomainError(409, "V34_CANCEL_STATE_INVALID", "取消状态暂不可用")
                    return BusinessSessionCancelResponse(
                        business_session_id=session.id,
                        status=("cancelled" if state.form_status == "cancelled" else "cancelling"),
                        replayed=True,
                        safe_reason_code=state.safe_reason_code,
                    )
                if (
                    session.status != "recommendation_ready"
                    or recommendation.status != "ready"
                    or recommendation.dify_run_id != session.dify_run_id
                    or state.form_status != "ready"
                    or state.form_node_id != "form_0"
                    or state.form_token is None
                    or state.form_definition_digest is None
                    or state.form_expires_at is None
                    or session.dify_run_id is None
                    or session.expires_at <= now
                    or recommendation.data_expires_at <= now
                    or recommendation.valid_until is None
                    or recommendation.valid_until <= now
                    or reference_set.expires_at is None
                    or reference_set.expires_at <= now
                    or state.form_expires_at <= now
                ):
                    raise DomainError(409, "V34_CANCEL_NOT_READY", "当前表单不能取消")
                from app.services.chatflow_v34_events import recommendation_form_digest

                if state.form_definition_digest != recommendation_form_digest(recommendation):
                    raise DomainError(
                        409,
                        "V34_FORM_DEFINITION_MISMATCH",
                        "表单内容已变化，请重新读取",
                    )
                state.form_status = "cancelling"
                state.safe_reason_code = "DIFY_CANCEL_OUTCOME_PENDING"
                state.updated_at = now
                session.status = "reconciling"
                session.updated_at = now
                schedule_chatflow_v34_cancel_in_transaction(
                    db,
                    session=session,
                    recommendation=recommendation,
                    state=state,
                    due_at=now,
                )
                return BusinessSessionCancelResponse(
                    business_session_id=session.id,
                    status="cancelling",
                    replayed=False,
                    safe_reason_code=state.safe_reason_code,
                )
        except DomainError:
            raise
        except IntegrityError:
            return self._recover_v34_cancel(
                owner_user_id=owner_user_id,
                business_session_id=business_session_id,
            )
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "取消状态暂时无法确认") from None

    def _recover_v34_cancel(
        self, *, owner_user_id: UUID, business_session_id: UUID
    ) -> BusinessSessionCancelResponse:
        try:
            with self._sessions() as db:
                session = db.execute(
                    select(BusinessSession).where(
                        BusinessSession.id == business_session_id,
                        BusinessSession.owner_user_id == owner_user_id,
                    )
                ).scalar_one_or_none()
                state = db.execute(
                    select(ChatflowV34SessionState).where(
                        ChatflowV34SessionState.business_session_id == business_session_id
                    )
                ).scalar_one_or_none()
                recommendation = db.execute(
                    select(Recommendation).where(
                        Recommendation.business_session_id == business_session_id,
                        Recommendation.source_kind == "dify_chatflow",
                    )
                ).scalar_one_or_none()
                action = db.execute(
                    select(Action).where(
                        Action.business_session_id == business_session_id,
                        Action.scope_kind == "business_session",
                        Action.scope_id == business_session_id,
                        Action.action_kind == CHATFLOW_CANCEL_ACTION,
                        Action.sequence == CHATFLOW_CANCEL_SEQUENCE,
                    )
                ).scalar_one_or_none()
                task_exists = db.execute(
                    select(Task.id).where(Task.business_session_id == business_session_id).limit(1)
                ).first()
                accepted_selection_evidence = db.execute(
                    select(ChatflowV34StageEventReceipt.id)
                    .where(
                        ChatflowV34StageEventReceipt.business_session_id == business_session_id,
                        ChatflowV34StageEventReceipt.event_type == "selection.applied",
                        ChatflowV34StageEventReceipt.disposition == "accepted",
                    )
                    .limit(1)
                ).first()
                if (
                    session is None
                    or state is None
                    or recommendation is None
                    or session.flow_version != V34_FLOW_VERSION
                ):
                    raise DomainError(409, "V34_CANCEL_STATE_INVALID", "取消状态暂不可用")
                action_valid = False
                if action is not None:
                    try:
                        expected_digest = chatflow_v34_cancel_action_digest(
                            session=session,
                            recommendation=recommendation,
                            state=state,
                        )
                    except ValueError:
                        expected_digest = None
                    action_valid = (
                        action.id
                        == chatflow_action_id(
                            business_session_id=session.id,
                            action_kind=CHATFLOW_CANCEL_ACTION,
                        )
                        and action.task_id is None
                        and action.input_digest == expected_digest
                    )
                if (
                    state.form_status == "cancelled"
                    and session.status == "cancelled"
                    and state.selection_status == "not_started"
                    and task_exists is None
                    and accepted_selection_evidence is None
                    and action_valid
                ):
                    status = "cancelled"
                elif (
                    state.form_status == "cancelling"
                    and session.status == "reconciling"
                    and state.selection_status == "not_started"
                    and task_exists is None
                    and accepted_selection_evidence is None
                    and action_valid
                ):
                    status = "cancelling"
                else:
                    raise DomainError(409, "V34_CANCEL_STATE_INVALID", "取消状态暂不可用")
                return BusinessSessionCancelResponse(
                    business_session_id=session.id,
                    status=status,
                    replayed=True,
                    safe_reason_code=state.safe_reason_code,
                )
        except DomainError:
            raise
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "取消状态暂时无法确认") from None

    def _create_v34_selection_in_transaction(
        self,
        db: Session,
        *,
        owner_user_id: UUID,
        session: BusinessSession,
        reference_set: ReferenceSet,
        recommendation: Recommendation,
        idempotency_key: str,
        request: BusinessSessionSelectionRequest,
        now: datetime,
    ) -> BusinessSelectionAccepted:
        state = db.execute(
            select(ChatflowV34SessionState)
            .where(ChatflowV34SessionState.business_session_id == session.id)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            state is None
            or session.dify_run_id is None
            or recommendation.status != "ready"
            or recommendation.dify_run_id != session.dify_run_id
            or recommendation.revision != request.recommendation_revision
            or recommendation.id != request.recommendation_id
            or state.form_node_id != "form_0"
            or state.form_token is None
            or state.form_token_digest is None
            or hashlib.sha256(state.form_token.encode("utf-8")).digest() != state.form_token_digest
            or state.form_definition_digest is None
            or state.form_expires_at is None
        ):
            raise DomainError(409, "V34_SELECTION_NOT_READY", "选择交接尚未完成")
        from app.services.chatflow_v34_conversion import (
            form_0_definition_for_recommendation,
            selection_to_form_0,
            validate_form_0_submission,
        )
        from app.services.chatflow_v34_events import recommendation_form_digest

        try:
            original = DifyOriginalRecommendation.model_validate(
                recommendation.original_recommendation, strict=True
            )
            conversion = selection_to_form_0(request, original)
            validate_form_0_submission(
                conversion.form_values,
                form_0_definition_for_recommendation(original),
            )
        except (TypeError, ValueError):
            raise DomainError(422, "SELECTION_INVALID", "所选内容暂不可用") from None
        if state.form_definition_digest != recommendation_form_digest(recommendation):
            raise DomainError(409, "V34_FORM_DEFINITION_MISMATCH", "表单内容已变化，请重新读取")

        if state.selection_status == "not_started":
            if (
                session.status != "recommendation_ready"
                or state.form_status != "ready"
                or session.expires_at <= now
                or reference_set.expires_at is None
                or reference_set.expires_at <= now
                or recommendation.valid_until is None
                or recommendation.valid_until <= now
                or recommendation.data_expires_at <= now
                or state.form_expires_at <= now
            ):
                raise DomainError(409, "RECOMMENDATION_EXPIRED", "推荐已失效，请重新开始")
        elif state.selection_status not in {"pending", "unknown", "accepted"}:
            raise DomainError(409, "V34_SELECTION_TERMINAL", "本次选择已结束，请重新开始")

        accepted, replayed = self.task_service.create_chatflow_task_in_transaction(
            db,
            owner_user_id=owner_user_id,
            business_session=session,
            reference_set=reference_set,
            recommendation=recommendation,
            idempotency_key=idempotency_key,
            request=request,
            now=now,
        )
        task = db.execute(
            select(Task)
            .where(
                Task.id == accepted.task_id,
                Task.business_session_id == session.id,
                Task.owner_user_id == owner_user_id,
                Task.provenance_kind == "dify_chatflow",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if task is None:
            raise DomainError(409, "SESSION_SELECTION_CONFLICT", "任务状态正在确认")
        action_id = chatflow_action_id(
            business_session_id=session.id,
            action_kind=CHATFLOW_CONTINUE_ACTION,
        )
        if replayed:
            if (
                state.selection_status not in {"pending", "unknown", "accepted"}
                or state.form_status != "consumed"
                or state.selection_submission_id != action_id
                or state.selection_inputs != conversion.form_values
                or state.selection_input_digest
                != set_chatflow_v34_selection_input_digest(
                    session=session,
                    recommendation=recommendation,
                    task=task,
                    state=state,
                    form_inputs=conversion.form_values,
                )
            ):
                raise DomainError(409, "SESSION_SELECTION_CONFLICT", "选择交接状态暂不可用")
            action = db.execute(
                select(Action)
                .where(Action.id == action_id, Action.business_session_id == session.id)
                .with_for_update()
            ).scalar_one_or_none()
            expected_digest = chatflow_v34_selection_action_digest(
                session=session,
                recommendation=recommendation,
                state=state,
                task=task,
            )
            if (
                action is None
                or action.task_id != task.id
                or action.action_kind != CHATFLOW_CONTINUE_ACTION
                or action.sequence != CHATFLOW_CONTINUE_SEQUENCE
                or action.input_digest != expected_digest
            ):
                raise DomainError(409, "SESSION_SELECTION_CONFLICT", "任务续接状态暂不可用")
        else:
            if state.selection_status != "not_started" or state.form_status != "ready":
                raise DomainError(409, "SESSION_SELECTION_CONFLICT", "流程已提交其他选择")
            state.selection_submission_id = action_id
            state.selection_inputs = conversion.form_values
            state.selection_status = "pending"
            state.selection_evidence_kind = None
            state.selection_accepted_at = None
            state.form_status = "consumed"
            state.selection_input_digest = set_chatflow_v34_selection_input_digest(
                session=session,
                recommendation=recommendation,
                task=task,
                state=state,
                form_inputs=conversion.form_values,
            )
            action_digest = chatflow_v34_selection_action_digest(
                session=session,
                recommendation=recommendation,
                state=state,
                task=task,
            )
            schedule_action_in_transaction(
                db,
                action_id=action_id,
                task_id=task.id,
                business_session_id=session.id,
                scope_kind="business_session",
                scope_id=session.id,
                action_kind=CHATFLOW_CONTINUE_ACTION,
                sequence=CHATFLOW_CONTINUE_SEQUENCE,
                due_at=now,
                input_digest_version=1,
                input_digest=action_digest,
            )
        session.status = "task_created"
        session.updated_at = now
        state.updated_at = now
        return BusinessSelectionAccepted(
            business_session_id=session.id,
            task_id=task.id,
            status="replayed" if replayed else "queued",
            planned_count=accepted.planned_count,
        )

    def _recover_create_race(
        self, owner_user_id: UUID, key_hash: bytes, request_digest: bytes
    ) -> BusinessSessionCreated | BusinessSessionV34Created:
        try:
            with self._sessions() as db:
                session = db.execute(
                    select(BusinessSession).where(
                        BusinessSession.owner_user_id == owner_user_id,
                        BusinessSession.idempotency_key_hash == key_hash,
                    )
                ).scalar_one_or_none()
                recommendation = (
                    self._recommendation_for_session(db, session.id)
                    if session is not None
                    else None
                )
                if (
                    session is not None
                    and session.request_digest == request_digest
                    and recommendation
                ):
                    return _project_created_session(session, recommendation.id, "replayed")
        except SQLAlchemyError:
            pass
        raise DomainError(409, "BUSINESS_SESSION_CONFLICT", "流程状态冲突，请重新读取") from None

    def _ensure_legacy_callback_allowed(self, business_session_id: UUID) -> None:
        try:
            with self._sessions() as db:
                flow_version = db.scalar(
                    select(BusinessSession.flow_version).where(
                        BusinessSession.id == business_session_id
                    )
                )
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "流程暂时无法读取") from None
        if flow_version == V34_FLOW_VERSION:
            raise DomainError(409, "V34_LEGACY_CALLBACK_REJECTED", "流程回传与当前版本不匹配")

    def _recover_selection(
        self,
        owner_user_id: UUID,
        business_session_id: UUID,
        idempotency_key: str,
        request: BusinessSessionSelectionRequest,
    ) -> BusinessSelectionAccepted:
        key_hash = idempotency_key_hash(
            idempotency_key, maximum_length=self.options.idempotency_key_max_length
        )
        try:
            with self._sessions() as db:
                session = db.execute(
                    select(BusinessSession).where(
                        BusinessSession.id == business_session_id,
                        BusinessSession.owner_user_id == owner_user_id,
                    )
                ).scalar_one_or_none()
                task = db.execute(
                    select(Task).where(
                        Task.business_session_id == business_session_id,
                        Task.owner_user_id == owner_user_id,
                        Task.provenance_kind == "dify_chatflow",
                    )
                ).scalar_one_or_none()
                if session is not None and task is not None:
                    normalized = request.model_dump(mode="json")
                    normalized["image_types"] = [
                        item for item in IMAGE_TYPES if item in set(request.image_types)
                    ]
                    expected_digest = chatflow_selection_digest(
                        business_session_id=session.id,
                        reference_asset_digest=session.reference_asset_digest,
                        normalized_selection=normalized,
                    )
                    idem = db.execute(
                        select(TaskIdempotencyRecord).where(
                            TaskIdempotencyRecord.owner_user_id == owner_user_id,
                            TaskIdempotencyRecord.operation == "chatflow.selection.create.v1",
                            TaskIdempotencyRecord.key_hash == key_hash,
                            TaskIdempotencyRecord.task_id == task.id,
                        )
                    ).scalar_one_or_none()
                    if (
                        idem is not None
                        and idem.request_digest == expected_digest
                        and task.request_digest == expected_digest
                    ):
                        return BusinessSelectionAccepted(
                            business_session_id=session.id,
                            task_id=task.id,
                            status="replayed",
                            planned_count=task.planned_count,
                        )
        except SQLAlchemyError:
            pass
        raise DomainError(
            409, "SESSION_SELECTION_CONFLICT", "选择状态暂时无法确认，请重新读取流程"
        ) from None

    @staticmethod
    def _recommendation_for_session(
        db: Session, business_session_id: UUID
    ) -> Recommendation | None:
        return db.execute(
            select(Recommendation).where(
                Recommendation.business_session_id == business_session_id,
                Recommendation.source_kind == "dify_chatflow",
            )
        ).scalar_one_or_none()

    @staticmethod
    def _lock_callback_parents(
        db: Session, event: ChatflowBusinessEvent
    ) -> tuple[BusinessSession, ReferenceSet, Recommendation] | None:
        hint = db.execute(
            select(BusinessSession.owner_user_id, BusinessSession.reference_set_id).where(
                BusinessSession.id == event.business_session_id
            )
        ).one_or_none()
        if hint is None:
            return None
        owner_user_id, reference_set_id = hint
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
        if owner is None or reference_set is None or session is None or recommendation is None:
            return None
        return session, reference_set, recommendation

    def _lock_session_for_selection(
        self, db: Session, owner_user_id: UUID, business_session_id: UUID
    ) -> tuple[BusinessSession, ReferenceSet, Recommendation, datetime] | None:
        hint = db.execute(
            select(BusinessSession.reference_set_id).where(
                BusinessSession.id == business_session_id,
                BusinessSession.owner_user_id == owner_user_id,
            )
        ).scalar_one_or_none()
        if hint is None:
            return None
        owner = db.execute(
            select(User).where(User.id == owner_user_id, User.status == "active").with_for_update()
        ).scalar_one_or_none()
        reference_set = db.execute(
            select(ReferenceSet)
            .where(
                ReferenceSet.id == hint,
                ReferenceSet.owner_user_id == owner_user_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        session = db.execute(
            select(BusinessSession)
            .where(
                BusinessSession.id == business_session_id,
                BusinessSession.owner_user_id == owner_user_id,
                BusinessSession.reference_set_id == hint,
            )
            .with_for_update()
        ).scalar_one_or_none()
        recommendation = self._recommendation_for_session(db, business_session_id)
        if recommendation is not None:
            recommendation = db.execute(
                select(Recommendation)
                .where(
                    Recommendation.id == recommendation.id,
                    Recommendation.business_session_id == business_session_id,
                    Recommendation.owner_user_id == owner_user_id,
                    Recommendation.reference_set_id == hint,
                )
                .with_for_update()
            ).scalar_one_or_none()
        if owner is None or reference_set is None or session is None or recommendation is None:
            return None
        return session, reference_set, recommendation, _db_now(db)

    @staticmethod
    def _validate_ready_payload(payload: ChatflowRecommendationPayload) -> None:
        original = payload.original_recommendation
        for name in ("language", "usage_scene", "visual_style", "selling_points"):
            choices = getattr(payload.public_options, name).choices
            if [choice.text for choice in choices] != original.field_texts[name]:
                raise DomainError(422, "CALLBACK_INVALID", "回传内容无效")
        if payload.image_types != list(IMAGE_TYPES):
            raise DomainError(422, "CALLBACK_INVALID", "回传内容无效")

    @staticmethod
    def _validate_callback_identity(
        session: BusinessSession,
        reference_set: ReferenceSet,
        payload: ChatflowRecommendationPayload,
        *,
        now: datetime,
    ) -> None:
        expected_digest = session.reference_asset_digest.hex()
        expected_references = [
            {
                key: entry[key]
                for key in (
                    "reference_set_image_id",
                    "position",
                    "slot",
                    "source_asset_id",
                    "model_asset_id",
                    "content_sha256",
                    "source_sha256",
                )
            }
            for entry in session.reference_assets_snapshot
        ]
        actual_references = [item.model_dump(mode="json") for item in payload.reference_assets]
        if (
            payload.reference_set_id != session.reference_set_id
            or payload.reference_asset_digest != expected_digest
            or chatflow_reference_asset_digest(payload.reference_assets) != expected_digest
            or actual_references != expected_references
            or payload.original_recommendation.category_snapshot != reference_set.product_category
            or payload.valid_until.tzinfo is None
            or payload.valid_until.utcoffset() != timedelta(0)
            or payload.valid_until <= now
            or payload.valid_until > reference_set.expires_at
        ):
            raise DomainError(422, "CALLBACK_INVALID", "回传内容无效")

    @staticmethod
    def _callback_accepted(
        event: ChatflowBusinessEvent, status: str
    ) -> ChatflowBusinessEventAccepted:
        return ChatflowBusinessEventAccepted(
            status=status,  # type: ignore[arg-type]
            business_session_id=event.business_session_id,
            recommendation_id=event.recommendation_id,
            recommendation_revision=event.recommendation_revision,
        )


def _session_request_digest(reference_set_id: UUID, flow_version: str = "legacy") -> bytes:
    # Preserve the legacy digest bytes so pre-V1 idempotency replays remain valid.
    digest_contract: dict[str, str] = {
        "contract_version": "chatflow-handoff-v1",
        "reference_set_id": str(reference_set_id),
    }
    if flow_version == V34_FLOW_VERSION:
        digest_contract["flow_version"] = flow_version
    canonical = json.dumps(digest_contract, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def _project_created_session(
    session: BusinessSession,
    recommendation_id: UUID,
    status: str,
) -> BusinessSessionCreated | BusinessSessionV34Created:
    if session.flow_version == V34_FLOW_VERSION:
        return BusinessSessionV34Created(
            flow_version=V34_FLOW_VERSION,
            business_session_id=session.id,
            recommendation_id=recommendation_id,
            status=status,  # type: ignore[arg-type]
        )
    return BusinessSessionCreated(
        business_session_id=session.id,
        recommendation_id=recommendation_id,
        status=status,  # type: ignore[arg-type]
    )


def _v34_public_phase(
    session: BusinessSession,
    state: ChatflowV34SessionState,
    *,
    expired: bool,
) -> str:
    if session.status == "cancelled" or state.form_status == "cancelled":
        return "cancelled"
    if expired or state.form_status == "expired":
        return "expired"
    if state.form_status == "cancelling":
        return "reconciling"
    if session.status == "failed" or state.form_status == "rejected":
        return "failed"
    if state.selection_status in {"pending", "unknown"} or state.form_status == "unknown":
        return "reconciling"
    if session.status in {"task_created", "reconciling"} or state.selection_status == "accepted":
        return "reconciling"
    if state.form_status == "ready" and session.status == "recommendation_ready":
        return "awaiting_selection"
    if state.form_status in {"binding", "awaiting_event"} and session.status in {
        "awaiting_recommendation",
        "recommendation_ready",
    }:
        return "awaiting_form"
    if session.status in {"starting", "awaiting_recommendation", "failed"}:
        return session.status
    return "reconciling"


def _db_now(db: Session) -> datetime:
    value = db.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise DomainError(503, "DATABASE_UNAVAILABLE", "流程暂时无法读取")
    return value.astimezone(UTC)


def _generation_request_closed_for_restart(db: Session, request: ChatflowGenerationRequest) -> bool:
    """Allow explicit v3.4 restart only for the exact durable no-batch closure."""
    evidence = db.get(ChatflowGenerationAcceptanceEvidence, request.id)
    permit = db.execute(
        select(ChatflowGenerationPermit).where(
            ChatflowGenerationPermit.generation_request_id == request.id,
            ChatflowGenerationPermit.task_id == request.task_id,
        )
    ).scalar_one_or_none()
    tracking = db.get(ChatflowGenerationTracking, request.id)
    task = db.get(Task, request.task_id)
    session = db.get(BusinessSession, request.business_session_id)
    if any(row is None for row in (evidence, permit, tracking, task, session)):
        return False
    if (
        evidence.outcome != "rejected_expired"
        or evidence.closed_at is None
        or evidence.batch_id is not None
        or evidence.receipt_id is None
        or evidence.receipt_digest is None
        or evidence.service_authority_id is None
        or evidence.service_authority_id != request.service_authority_id
        or evidence.permit_id != permit.permit_id
        or evidence.permit_version != 2
        or evidence.permit_expires_at != permit.expires_at
        or permit.service_authority_id != request.service_authority_id
        or request.service_authority_id is None
        or tracking.status != "rejected_before_acceptance"
        or tracking.batch_id is not None
        or tracking.source_sequence is not None
        or task.status != "failed"
        or task.terminal_at is None
        or task.expires_at != task.terminal_at + timedelta(days=task.retention_days)
        or task.provenance_kind != "dify_chatflow"
        or session.status != "failed"
        or session.flow_version != V34_FLOW_VERSION
    ):
        return False
    items = (
        db.execute(select(TaskItem).where(TaskItem.task_id == task.id).order_by(TaskItem.position))
        .scalars()
        .all()
    )
    if len(items) != task.planned_count or any(
        item.status != "failed"
        or item.error_code != "GENERATION_PERMIT_EXPIRED_BEFORE_ACCEPTANCE"
        or item.image_id is not None
        or item.preview_ready
        for item in items
    ):
        return False
    attempts = (
        db.execute(
            select(TaskItemAttempt).where(
                TaskItemAttempt.task_item_id.in_([item.id for item in items])
            )
        )
        .scalars()
        .all()
    )
    if len(attempts) != len(items) or any(
        attempt.status != "failed"
        or attempt.failure_code != "GENERATION_PERMIT_EXPIRED_BEFORE_ACCEPTANCE"
        or attempt.completed_at is None
        for attempt in attempts
    ):
        return False
    if any(
        db.execute(query).first() is not None
        for query in (
            select(ChatflowGenerationDelivery.id).where(
                ChatflowGenerationDelivery.generation_request_id == request.id
            ),
            select(ChatflowGenerationEventReceipt.event_id).where(
                ChatflowGenerationEventReceipt.generation_request_id == request.id
            ),
            select(ChatflowGenerationResultManifest.generation_request_id).where(
                ChatflowGenerationResultManifest.generation_request_id == request.id
            ),
        )
    ):
        return False
    return True
