from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, aliased, sessionmaker

from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.business_session_models import BusinessSession
from app.db.chatflow_generation_models import (
    ChatflowGenerationPermit,
    ChatflowGenerationRequest,
    ChatflowGenerationRequestSlot,
    ChatflowGenerationTracking,
    ChatflowPromptImage,
    ChatflowPromptPackage,
    ChatflowPromptReference,
)
from app.db.chatflow_v34_models import ChatflowV34SessionState
from app.db.models import User
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ImageAsset, ReferenceSet, ReferenceSetImage
from app.db.task_models import Task, TaskItem, TaskItemAttempt
from app.schemas.chatflow_prompt_packages import (
    ChatflowPromptPackageAccepted,
    ChatflowPromptPackageSubmission,
    ChatflowPromptSlot,
    GenerationPermit,
)
from app.schemas.chatflow_prompt_packages import (
    ChatflowPromptReference as PromptReferenceSubmission,
)
from app.schemas.chatflow_v34 import V34_FLOW_VERSION
from app.schemas.prompt_workflow import canonical_sha256
from app.services.chatflow_actions import (
    CHATFLOW_CONTINUE_ACTION,
    CHATFLOW_CONTINUE_SEQUENCE,
    chatflow_action_digest,
    chatflow_v34_selection_action_digest,
)
from app.services.chatflow_generation_actions import ensure_generation_recovery_action
from app.services.chatflow_generation_expiry import ensure_generation_expiry_assessment_action
from app.services.chatflow_v34_events import v34_first_submission_during_run_observation
from app.services.chatflow_v34_prompt_contract import (
    prompt_package_submission_id,
    selection_evidence_matches_action,
)
from app.services.prompt_validation import (
    PromptPackageValidationError,
    validate_prompt_package,
)
from app.services.tasks import validate_dify_chatflow_task_snapshot

_RUN_ID = re.compile(r"^[A-Za-z0-9._:-]{1,320}$")
_CONTRACT_VERSION = "chatflow-handoff-v1"
_PACKAGE_VERSION = "chatflow-prompt-package-v1"
_PERMIT_VERSION = 2
_PERMIT_CONTRACT_VERSION = "chatflow-generation-permit-v2"
_REQUEST_DIGEST_VERSION = 1
_PERMIT_TTL = timedelta(minutes=5)
_REFERENCE_IDS = ("PRIMARY", "AUX01", "AUX02", "AUX03")
_PREFERENCE_FIELDS = {
    "1": ("language", "语言种类"),
    "3": ("usage_scene", "使用场景"),
    "4": ("visual_style", "视觉风格"),
    "5": ("selling_points", "商品卖点"),
}


class ChatflowPromptPackageService:
    """Persist the H4 package and permit; deliberately has no generation port."""

    def __init__(
        self,
        engine: Engine,
        *,
        permit_secret: SecretStr,
        service_authority_id: UUID | None = None,
        permit_ttl: timedelta = _PERMIT_TTL,
    ) -> None:
        key = permit_secret.get_secret_value()
        if len(key) < 32 or not key.isascii() or any(char.isspace() for char in key):
            raise ValueError("generation permit secret must be 32+ non-whitespace ASCII chars")
        if permit_ttl <= timedelta(0) or permit_ttl > timedelta(hours=1):
            raise ValueError("generation permit TTL must be positive and at most one hour")
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        self._permit_key = key.encode("ascii")
        self._permit_ttl = permit_ttl
        self._service_authority_id = service_authority_id

    def accept(self, submission: ChatflowPromptPackageSubmission) -> ChatflowPromptPackageAccepted:
        package_value = submission.prompt_package.model_dump(mode="json")
        try:
            package_digest = canonical_sha256(package_value)
            request_digest = canonical_sha256(submission.model_dump(mode="json"))
        except ValueError:
            raise DomainError(422, "PROMPT_PACKAGE_INVALID", "提示词包内容无效") from None
        if not hmac.compare_digest(package_digest.hex(), submission.package_digest):
            raise DomainError(422, "PROMPT_PACKAGE_DIGEST_INVALID", "提示词包摘要无效")

        task_id = submission.task_id
        submission_id = submission.submission_id
        is_replay = False
        try:
            with self._sessions() as db, db.begin():
                owner, reference_set, session, recommendation, task, task_items = (
                    self._lock_handoff_parents(db, submission)
                )
                if session.flow_version == V34_FLOW_VERSION and self._service_authority_id is None:
                    raise DomainError(
                        503,
                        "GENERATION_AUTHORITY_UNAVAILABLE",
                        "生图服务身份尚未配置，暂时无法交接",
                    )
                attempts = self._lock_task_attempts(db, task_items)

                existing_submission = db.execute(
                    select(ChatflowPromptPackage).where(
                        ChatflowPromptPackage.submission_id == submission_id
                    )
                ).scalar_one_or_none()
                if existing_submission is not None:
                    if (
                        existing_submission.task_id != task.id
                        or existing_submission.request_digest != request_digest
                        or existing_submission.package_digest != package_digest
                    ):
                        raise _content_conflict()
                    now = _db_now(db)
                    self._validate_live_handoff(
                        db,
                        submission=submission,
                        owner=owner,
                        reference_set=reference_set,
                        session=session,
                        recommendation=recommendation,
                        task=task,
                        task_items=task_items,
                        now=now,
                        allow_replay=True,
                    )
                    expected_references = self._validate_reference_manifest(
                        db=db,
                        submission=submission,
                        task=task,
                        session=session,
                        reference_set=reference_set,
                    )
                    self._validate_package(
                        submission=submission,
                        task=task,
                        reference_set=reference_set,
                        expected_references=expected_references,
                    )
                    self._validate_continuation(
                        db,
                        submission=submission,
                        session=session,
                        recommendation=recommendation,
                        task=task,
                    )
                    self._upgrade_existing_permit_v2(
                        db,
                        task_id=task.id,
                        submission=existing_submission,
                        task_items=task_items,
                        expected_references=expected_references,
                        now=now,
                        expected_service_authority_id=(
                            self._service_authority_id
                            if session.flow_version == V34_FLOW_VERSION
                            else None
                        ),
                    )
                    is_replay = True
                else:
                    existing_task_package = db.execute(
                        select(ChatflowPromptPackage).where(
                            ChatflowPromptPackage.task_id == task.id
                        )
                    ).scalar_one_or_none()
                    existing_request = db.execute(
                        select(ChatflowGenerationRequest).where(
                            ChatflowGenerationRequest.task_id == task.id
                        )
                    ).scalar_one_or_none()
                    if existing_task_package is not None or existing_request is not None:
                        raise _content_conflict()
                    if attempts:
                        raise _linkage_conflict()

                    now = _db_now(db)
                    self._validate_live_handoff(
                        db,
                        submission=submission,
                        owner=owner,
                        reference_set=reference_set,
                        session=session,
                        recommendation=recommendation,
                        task=task,
                        task_items=task_items,
                        now=now,
                    )
                    expected_references = self._validate_reference_manifest(
                        db=db,
                        submission=submission,
                        task=task,
                        session=session,
                        reference_set=reference_set,
                    )
                    validated_package = self._validate_package(
                        submission=submission,
                        task=task,
                        reference_set=reference_set,
                        expected_references=expected_references,
                    )
                    self._validate_continuation(
                        db,
                        submission=submission,
                        session=session,
                        recommendation=recommendation,
                        task=task,
                    )

                    package_id = uuid5(task.id, f"chatflow-prompt-package:{submission_id}")
                    generation_request_id = uuid5(
                        task.id, f"chatflow-generation-request:{submission_id}"
                    )
                    slot_mapping = _slot_mapping_for_items(generation_request_id, task_items)
                    slot_mapping_digest = canonical_sha256(slot_mapping)
                    reference_manifest_digest = canonical_sha256(
                        _reference_manifest_for_expected(expected_references)
                    )
                    permit_id = uuid5(generation_request_id, "generation-permit-v1")
                    expires_at = min(
                        now + self._permit_ttl,
                        session.expires_at,
                        reference_set.expires_at,
                        recommendation.valid_until,
                        task.expires_at or now + self._permit_ttl,
                    )
                    if expires_at <= now:
                        raise _expired()
                    token = _sign_permit(
                        self._permit_key,
                        permit_id=permit_id,
                        generation_request_id=generation_request_id,
                        task_id=task.id,
                        package_digest=package_digest,
                        slot_mapping_digest=slot_mapping_digest,
                        reference_manifest_digest=reference_manifest_digest,
                        service_authority_id=(
                            self._service_authority_id
                            if session.flow_version == V34_FLOW_VERSION
                            else None
                        ),
                        expires_at=expires_at,
                    )
                    token_digest = hashlib.sha256(token.encode("ascii")).digest()

                    package_row = ChatflowPromptPackage(
                        id=package_id,
                        submission_id=submission_id,
                        task_id=task.id,
                        business_session_id=session.id,
                        recommendation_id=recommendation.id,
                        owner_user_id=owner.id,
                        reference_set_id=reference_set.id,
                        recommendation_revision=recommendation.revision,
                        dify_run_id=session.dify_run_id,
                        contract_version=_CONTRACT_VERSION,
                        package_version=submission.package_version,
                        package_digest_version=submission.package_digest_version,
                        package_digest=package_digest,
                        request_digest_version=_REQUEST_DIGEST_VERSION,
                        request_digest=request_digest,
                        image_types=list(task.image_types),
                        package=package_value,
                        created_at=now,
                    )
                    db.add(package_row)
                    db.flush()
                    db.add(
                        ChatflowGenerationRequest(
                            id=generation_request_id,
                            package_id=package_id,
                            task_id=task.id,
                            business_session_id=session.id,
                            service_authority_id=(
                                self._service_authority_id
                                if session.flow_version == V34_FLOW_VERSION
                                else None
                            ),
                            dify_run_id=session.dify_run_id,
                            package_digest=package_digest,
                            request_digest=request_digest,
                            created_at=now,
                        )
                    )
                    db.flush()
                    db.add(
                        ChatflowGenerationPermit(
                            permit_id=permit_id,
                            generation_request_id=generation_request_id,
                            task_id=task.id,
                            service_authority_id=(
                                self._service_authority_id
                                if session.flow_version == V34_FLOW_VERSION
                                else None
                            ),
                            package_digest=package_digest,
                            contract_version=_CONTRACT_VERSION,
                            permit_version=_PERMIT_VERSION,
                            slot_mapping_digest=slot_mapping_digest,
                            reference_manifest_digest=reference_manifest_digest,
                            token_digest=token_digest,
                            expires_at=expires_at,
                            created_at=now,
                        )
                    )
                    for reference in expected_references:
                        db.add(
                            ChatflowPromptReference(
                                id=uuid5(package_id, f"reference:{reference.ref_id}"),
                                package_id=package_id,
                                task_id=task.id,
                                ref_id=reference.ref_id,
                                asset_id=reference.asset_id,
                                position=reference.order,
                                content_sha256=reference.content_sha256,
                                created_at=now,
                            )
                        )
                    for item, prompt_image in zip(
                        task_items, validated_package.images, strict=True
                    ):
                        attempt_id = uuid5(generation_request_id, f"task-item-attempt:{item.id}")
                        db.add(
                            TaskItemAttempt(
                                id=attempt_id,
                                task_item_id=item.id,
                                attempt_no=1,
                                idempotency_key=attempt_id,
                                status="prepared",
                            )
                        )
                        db.add(
                            ChatflowPromptImage(
                                id=uuid5(package_id, f"task-item:{item.id}"),
                                package_id=package_id,
                                task_id=task.id,
                                task_item_id=item.id,
                                position=item.position,
                                image_type=item.image_type,
                                prompt_image=prompt_image.model_dump(mode="json"),
                                created_at=now,
                            )
                        )
                    db.flush()
                    for item in task_items:
                        attempt_id = uuid5(generation_request_id, f"task-item-attempt:{item.id}")
                        db.add(
                            ChatflowGenerationRequestSlot(
                                generation_request_id=generation_request_id,
                                slot_id=f"slot_{item.position:02d}",
                                task_id=task.id,
                                task_item_id=item.id,
                                attempt_id=attempt_id,
                                position=item.position,
                                image_type=item.image_type,
                                created_at=now,
                            )
                        )
                    # The transaction contains all package, image, reference,
                    # request, slot/attempt, permit, and first H6 read-only
                    # reconciliation Action. No image-generation POST is part
                    # of this transaction.
                    db.flush()
                request_row = db.execute(
                    select(ChatflowGenerationRequest).where(
                        ChatflowGenerationRequest.task_id == task.id
                    )
                ).scalar_one_or_none()
                if request_row is None:
                    raise _linkage_conflict()
                tracking = db.execute(
                    select(ChatflowGenerationTracking)
                    .where(ChatflowGenerationTracking.generation_request_id == request_row.id)
                    .with_for_update()
                ).scalar_one_or_none()
                if tracking is None:
                    tracking = ChatflowGenerationTracking(
                        generation_request_id=request_row.id,
                        task_id=task.id,
                        status="awaiting_batch",
                        reconcile_state="scheduled",
                        reconcile_query_count=0,
                    )
                    db.add(tracking)
                    db.flush()
                ensure_generation_recovery_action(
                    db,
                    request=request_row,
                    task_id=task.id,
                    session_id=session.id,
                    tracking=tracking,
                    due_at=now + timedelta(seconds=10),
                )
                if (
                    session.flow_version == V34_FLOW_VERSION
                    and request_row.service_authority_id == self._service_authority_id
                    and self._service_authority_id is not None
                ):
                    permit_row = db.execute(
                        select(ChatflowGenerationPermit)
                        .where(ChatflowGenerationPermit.generation_request_id == request_row.id)
                        .with_for_update()
                    ).scalar_one()
                    if permit_row.service_authority_id == self._service_authority_id:
                        ensure_generation_expiry_assessment_action(
                            db,
                            request=request_row,
                            permit=permit_row,
                            task_id=task.id,
                            session_id=session.id,
                            tracking=tracking,
                        )
        except DomainError:
            raise
        except IntegrityError:
            recovered = self._confirm_original_submission(
                task_id=task_id,
                submission_id=submission_id,
                request_digest=request_digest,
                package_digest=package_digest,
                status="replayed",
            )
            if recovered is not None:
                return recovered
            raise _content_conflict() from None
        except SQLAlchemyError:
            recovered = self._confirm_original_submission(
                task_id=task_id,
                submission_id=submission_id,
                request_digest=request_digest,
                package_digest=package_digest,
                status="replayed",
            )
            if recovered is not None:
                return recovered
            raise DomainError(503, "DATABASE_UNAVAILABLE", "提示词许可暂时无法确认") from None

        # Re-read only the original Task + submission after COMMIT. This is the
        # confirmation gate for both a fresh response and a response-loss replay.
        response_status = "replayed" if is_replay else "accepted"
        confirmed = self._confirm_original_submission(
            task_id=task_id,
            submission_id=submission_id,
            request_digest=request_digest,
            package_digest=package_digest,
            status=response_status,
        )
        if confirmed is None:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "提示词许可暂时无法确认")
        return confirmed

    def _lock_handoff_parents(
        self, db: Session, submission: ChatflowPromptPackageSubmission
    ) -> tuple[User, ReferenceSet, BusinessSession, Recommendation, Task, list[TaskItem]]:
        hint = db.execute(
            select(
                Task.owner_user_id,
                Task.reference_set_id,
                Task.recommendation_id,
                Task.business_session_id,
            ).where(Task.id == submission.task_id)
        ).one_or_none()
        if hint is None:
            raise DomainError(404, "TASK_NOT_FOUND", "任务不可用")
        owner_id, reference_set_id, recommendation_id, session_id = hint
        if session_id is None:
            raise _linkage_conflict()

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
        session = db.execute(
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
            )
            .with_for_update()
        ).scalar_one_or_none()
        task = db.execute(
            select(Task)
            .where(
                Task.id == submission.task_id,
                Task.owner_user_id == owner_id,
                Task.reference_set_id == reference_set_id,
                Task.recommendation_id == recommendation_id,
                Task.business_session_id == session_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if any(row is None for row in (owner, reference_set, session, recommendation, task)):
            raise DomainError(404, "TASK_NOT_FOUND", "任务不可用")
        assert owner is not None
        assert reference_set is not None
        assert session is not None
        assert recommendation is not None
        assert task is not None
        task_items = (
            db.execute(
                select(TaskItem)
                .where(TaskItem.task_id == task.id)
                .order_by(TaskItem.position, TaskItem.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        return owner, reference_set, session, recommendation, task, task_items

    @staticmethod
    def _lock_task_attempts(db: Session, task_items: list[TaskItem]) -> list[TaskItemAttempt]:
        if not task_items:
            return []
        return (
            db.execute(
                select(TaskItemAttempt)
                .where(TaskItemAttempt.task_item_id.in_([item.id for item in task_items]))
                .order_by(
                    TaskItemAttempt.task_item_id,
                    TaskItemAttempt.attempt_no,
                    TaskItemAttempt.id,
                )
                .with_for_update()
            )
            .scalars()
            .all()
        )

    def _validate_live_handoff(
        self,
        db: Session,
        *,
        submission: ChatflowPromptPackageSubmission,
        owner: User,
        reference_set: ReferenceSet,
        session: BusinessSession,
        recommendation: Recommendation,
        task: Task,
        task_items: list[TaskItem],
        now: datetime,
        allow_replay: bool = False,
    ) -> None:
        if (
            session.expires_at <= now
            or reference_set.expires_at is None
            or reference_set.expires_at <= now
            or recommendation.valid_until is None
            or recommendation.valid_until <= now
            or recommendation.data_expires_at <= now
            or task.expires_at is not None
            and task.expires_at <= now
        ):
            raise _expired()

        allowed_task_statuses = (
            {
                "queued",
                "needs_input",
                "prompting",
                "generating",
                "reconciling",
                "succeeded",
                "partially_succeeded",
                "failed",
            }
            if allow_replay
            else {"queued", "needs_input", "prompting"}
        )
        pending_v34_run_observation = (
            not allow_replay
            and v34_first_submission_during_run_observation(db, session=session, task=task)
        )
        if pending_v34_run_observation:
            allowed_task_statuses.add("reconciling")
        if (
            submission.contract_version != _CONTRACT_VERSION
            or submission.package_version != _PACKAGE_VERSION
            or not _RUN_ID.fullmatch(submission.dify_run_id)
            or submission.business_session_id != session.id
            or submission.recommendation_id != recommendation.id
            or submission.recommendation_revision != recommendation.revision
            or submission.dify_run_id != session.dify_run_id
            or submission.dify_run_id != recommendation.dify_run_id
            or task.provenance_kind != "dify_chatflow"
            or task.business_session_id != session.id
            or task.owner_user_id != owner.id
            or task.reference_set_id != reference_set.id
            or task.recommendation_id != recommendation.id
            or task.recommendation_revision != recommendation.revision
            or session.status
            not in (
                {"task_created", "reconciling"}
                if allow_replay or pending_v34_run_observation
                else {"task_created"}
            )
            or session.start_outcome != "accepted"
            or session.dify_run_id is None
            or session.run_revision < 1
            or recommendation.source_kind != "dify_chatflow"
            or recommendation.status != "ready"
            or recommendation.valid_until is None
            or task.status not in allowed_task_statuses
            or not allow_replay
            and task.terminal_at is not None
            or task.planned_count != len(task_items)
            or [item.position for item in task_items] != list(range(1, task.planned_count + 1))
            or [item.image_type for item in task_items] != task.image_types
            or [item.image_type for item in task_items] != submission.image_types
            or not allow_replay
            and any(
                item.status != "queued" or item.image_id is not None or item.preview_ready
                for item in task_items
            )
            or reference_set.status != "ready"
        ):
            raise _linkage_conflict()
        if session.reference_assets_snapshot != task.reference_assets_snapshot:
            raise _linkage_conflict()
        try:
            validate_dify_chatflow_task_snapshot(task, session)
        except DomainError:
            raise _linkage_conflict() from None
        if (
            task.request_snapshot.get("business_session_id") != str(session.id)
            or task.request_snapshot.get("reference_asset_digest")
            != session.reference_asset_digest.hex()
        ):
            raise _linkage_conflict()

    @staticmethod
    def _validate_continuation(
        db: Session,
        *,
        submission: ChatflowPromptPackageSubmission,
        session: BusinessSession,
        recommendation: Recommendation,
        task: Task,
    ) -> None:
        continuation = db.execute(
            select(Action)
            .where(
                Action.scope_kind == "business_session",
                Action.scope_id == session.id,
                Action.business_session_id == session.id,
                Action.task_id == task.id,
                Action.action_kind == CHATFLOW_CONTINUE_ACTION,
                Action.sequence == CHATFLOW_CONTINUE_SEQUENCE,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if continuation is None:
            raise _linkage_conflict()
        if session.flow_version == "visual-v3.4-single-selection":
            state = db.execute(
                select(ChatflowV34SessionState).where(
                    ChatflowV34SessionState.business_session_id == session.id
                )
            ).scalar_one_or_none()
            if (
                state is None
                or state.selection_status != "accepted"
                or state.selection_submission_id != continuation.id
                or state.selection_evidence_kind not in {"http_200", "selection_applied"}
                or state.selection_input_digest is None
                or state.selection_submission_id is None
                or submission.submission_id
                != prompt_package_submission_id(state.selection_submission_id)
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
                raise _linkage_conflict()
            return

        if (
            continuation.status != "completed"
            or continuation.last_outcome != "accepted"
            or continuation.input_digest_version != 1
            or continuation.input_digest
            != chatflow_action_digest(
                action_kind=CHATFLOW_CONTINUE_ACTION,
                business_session_id=session.id,
                task_id=task.id,
                request_digest=task.request_digest,
            )
        ):
            raise _linkage_conflict()

    def _validate_reference_manifest(
        self,
        *,
        db: Session,
        submission: ChatflowPromptPackageSubmission,
        task: Task,
        session: BusinessSession,
        reference_set: ReferenceSet,
    ) -> list[PromptReferenceSubmission]:
        source_asset = aliased(ImageAsset)
        model_asset = aliased(ImageAsset)
        rows = db.execute(
            select(ReferenceSetImage, source_asset, model_asset)
            .join(source_asset, source_asset.id == ReferenceSetImage.source_asset_id)
            .join(model_asset, model_asset.id == ReferenceSetImage.model_asset_id)
            .where(ReferenceSetImage.reference_set_id == reference_set.id)
            .order_by(ReferenceSetImage.position, ReferenceSetImage.id)
        ).all()
        snapshots = task.reference_assets_snapshot
        if (
            not isinstance(snapshots, list)
            or not 1 <= len(snapshots) <= 4
            or len(rows) != len(snapshots)
            or len(submission.references) != len(snapshots)
        ):
            raise _linkage_conflict()

        expected: list[PromptReferenceSubmission] = []
        for position, (row, saved) in enumerate(zip(rows, snapshots, strict=True)):
            reference, source, model = row
            alias = _REFERENCE_IDS[position]
            if (
                not isinstance(saved, dict)
                or reference.owner_user_id != task.owner_user_id
                or reference.reference_set_id != task.reference_set_id
                or reference.position != position
                or reference.slot != alias
                or source.owner_user_id != task.owner_user_id
                or source.asset_kind != "reference_source"
                or model.owner_user_id != task.owner_user_id
                or model.asset_kind != "reference_model_input"
                or reference.source_asset_id != source.id
                or reference.model_asset_id != model.id
                or reference.content_sha256 != model.content_sha256
                or str(reference.id) != saved.get("reference_set_image_id")
                or str(source.id) != saved.get("source_asset_id")
                or str(model.id) != saved.get("model_asset_id")
                or source.content_sha256 != saved.get("source_sha256")
                or model.content_sha256 != saved.get("model_sha256")
                or reference.content_sha256 != saved.get("content_sha256")
            ):
                raise _linkage_conflict()
            expected.append(
                PromptReferenceSubmission(
                    ref_id=alias,
                    asset_id=model.id,
                    order=position + 1,
                    content_sha256=model.content_sha256,
                )
            )
        if [item.model_dump(mode="json") for item in expected] != [
            item.model_dump(mode="json") for item in submission.references
        ]:
            raise _linkage_conflict()
        return expected

    @staticmethod
    def _validate_package(
        *,
        submission: ChatflowPromptPackageSubmission,
        task: Task,
        reference_set: ReferenceSet,
        expected_references: list[PromptReferenceSubmission],
    ):
        resolved = task.resolved_preferences
        platform_entry = resolved.get("platform") if isinstance(resolved, dict) else None
        platform = platform_entry.get("value") if isinstance(platform_entry, dict) else None
        package = submission.prompt_package
        if (
            not isinstance(platform, str)
            or package.basis.declared_category != task.category_snapshot
            or task.category_snapshot != reference_set.product_category
            or list(submission.image_types) != list(task.image_types)
            or package.basis.image_types != list(task.image_types)
            or [image.type for image in package.images] != list(task.image_types)
        ):
            raise _linkage_conflict()
        _validate_package_preferences(package, resolved)
        relation_map = {
            relation.id: relation.relation for relation in package.basis.auxiliary_relations
        }
        try:
            return validate_prompt_package(
                package.model_dump(mode="json"),
                expected_image_types=task.image_types,
                expected_platform=platform,
                legal_reference_ids=[reference.ref_id for reference in expected_references],
                auxiliary_relation_by_id=relation_map,
            )
        except PromptPackageValidationError:
            raise DomainError(422, "PROMPT_PACKAGE_INVALID", "提示词包校验失败") from None

    def _confirm_original_submission(
        self,
        *,
        task_id: UUID,
        submission_id: UUID,
        request_digest: bytes,
        package_digest: bytes,
        status: str,
    ) -> ChatflowPromptPackageAccepted | None:
        """Read only. Never constructs a request/permit during recovery."""
        try:
            with self._sessions() as db:
                package = db.execute(
                    select(ChatflowPromptPackage).where(
                        ChatflowPromptPackage.task_id == task_id,
                        ChatflowPromptPackage.submission_id == submission_id,
                    )
                ).scalar_one_or_none()
                if package is None:
                    return None
                if (
                    package.request_digest != request_digest
                    or package.package_digest != package_digest
                    or package.contract_version != _CONTRACT_VERSION
                    or package.package_version != _PACKAGE_VERSION
                ):
                    raise _content_conflict()
                generation_request = db.execute(
                    select(ChatflowGenerationRequest).where(
                        ChatflowGenerationRequest.package_id == package.id,
                        ChatflowGenerationRequest.task_id == task_id,
                    )
                ).scalar_one_or_none()
                if (
                    generation_request is None
                    or generation_request.package_digest != package_digest
                    or generation_request.request_digest != request_digest
                ):
                    raise DomainError(503, "PERMIT_UNAVAILABLE", "提示词许可暂时无法确认")
                handoff_session = db.get(BusinessSession, package.business_session_id)
                permit = db.execute(
                    select(ChatflowGenerationPermit).where(
                        ChatflowGenerationPermit.generation_request_id == generation_request.id,
                        ChatflowGenerationPermit.task_id == task_id,
                    )
                ).scalar_one_or_none()
                if (
                    permit is None
                    or permit.package_digest != package_digest
                    or permit.contract_version != _CONTRACT_VERSION
                    or permit.permit_version != _PERMIT_VERSION
                    or permit.slot_mapping_digest is None
                    or permit.reference_manifest_digest is None
                ):
                    raise DomainError(503, "PERMIT_UNAVAILABLE", "提示词许可暂时无法确认")
                if handoff_session is None or (
                    handoff_session.flow_version == V34_FLOW_VERSION
                    and (
                        self._service_authority_id is None
                        or generation_request.service_authority_id != self._service_authority_id
                        or permit.service_authority_id != self._service_authority_id
                    )
                ):
                    raise DomainError(
                        503,
                        "GENERATION_AUTHORITY_UNAVAILABLE",
                        "生图服务身份与原许可不一致",
                    )
                now = _db_now(db)
                if permit.expires_at <= now:
                    raise _expired()
                token = _sign_permit(
                    self._permit_key,
                    permit_id=permit.permit_id,
                    generation_request_id=generation_request.id,
                    task_id=task_id,
                    package_digest=bytes(permit.package_digest),
                    slot_mapping_digest=bytes(permit.slot_mapping_digest),
                    reference_manifest_digest=bytes(permit.reference_manifest_digest),
                    service_authority_id=permit.service_authority_id,
                    expires_at=permit.expires_at,
                )
                token_digest = hashlib.sha256(token.encode("ascii")).digest()
                if not hmac.compare_digest(token_digest, bytes(permit.token_digest)):
                    raise DomainError(503, "PERMIT_UNAVAILABLE", "提示词许可暂时无法确认")
                slots = (
                    db.execute(
                        select(ChatflowGenerationRequestSlot)
                        .where(
                            ChatflowGenerationRequestSlot.generation_request_id
                            == generation_request.id,
                            ChatflowGenerationRequestSlot.task_id == task_id,
                        )
                        .order_by(
                            ChatflowGenerationRequestSlot.position,
                            ChatflowGenerationRequestSlot.slot_id,
                        )
                    )
                    .scalars()
                    .all()
                )
                images = (
                    db.execute(
                        select(ChatflowPromptImage)
                        .where(
                            ChatflowPromptImage.package_id == package.id,
                            ChatflowPromptImage.task_id == task_id,
                        )
                        .order_by(ChatflowPromptImage.position, ChatflowPromptImage.id)
                    )
                    .scalars()
                    .all()
                )
                if (
                    not slots
                    or len(slots) != len(images)
                    or len(slots) != len(package.image_types)
                    or any(
                        slot.position != position
                        or slot.slot_id != f"slot_{position:02d}"
                        or slot.image_type != image.image_type
                        or slot.task_item_id != image.task_item_id
                        or slot.position != image.position
                        for position, (slot, image) in enumerate(
                            zip(slots, images, strict=True), start=1
                        )
                    )
                ):
                    raise DomainError(503, "PERMIT_UNAVAILABLE", "提示词许可暂时无法确认")
                return ChatflowPromptPackageAccepted(
                    status=status,  # type: ignore[arg-type]
                    contract_version=_CONTRACT_VERSION,
                    task_id=task_id,
                    submission_id=submission_id,
                    generation_request_id=generation_request.id,
                    package_digest=package_digest.hex(),
                    slots=[
                        ChatflowPromptSlot(
                            task_item_id=slot.task_item_id,
                            attempt_id=slot.attempt_id,
                            slot_id=slot.slot_id,
                            position=slot.position,
                            image_type=slot.image_type,
                        )
                        for slot in slots
                    ],
                    permit=GenerationPermit(
                        permit_id=permit.permit_id,
                        permit_version=_PERMIT_VERSION,
                        contract_version=_PERMIT_CONTRACT_VERSION,
                        slot_mapping_digest=bytes(permit.slot_mapping_digest).hex(),
                        reference_manifest_digest=bytes(permit.reference_manifest_digest).hex(),
                        token=token,
                        digest=token_digest.hex(),
                        expires_at=permit.expires_at.astimezone(UTC).isoformat(),
                    ),
                )
        except DomainError:
            raise
        except SQLAlchemyError:
            return None

    def _upgrade_existing_permit_v2(
        self,
        db: Session,
        *,
        task_id: UUID,
        submission: ChatflowPromptPackage,
        task_items: list[TaskItem],
        expected_references: list[PromptReferenceSubmission],
        now: datetime,
        expected_service_authority_id: UUID | None,
    ) -> None:
        generation_request = db.execute(
            select(ChatflowGenerationRequest).where(
                ChatflowGenerationRequest.task_id == task_id,
                ChatflowGenerationRequest.package_id == submission.id,
            )
        ).scalar_one_or_none()
        if generation_request is None:
            raise DomainError(503, "PERMIT_UNAVAILABLE", "提示词许可暂时无法确认")
        permit = db.execute(
            select(ChatflowGenerationPermit)
            .where(
                ChatflowGenerationPermit.generation_request_id == generation_request.id,
                ChatflowGenerationPermit.task_id == task_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if (
            permit is None
            or permit.package_digest != submission.package_digest
            or permit.contract_version != _CONTRACT_VERSION
        ):
            raise DomainError(503, "PERMIT_UNAVAILABLE", "提示词许可暂时无法确认")
        if expected_service_authority_id is not None and (
            generation_request.service_authority_id != expected_service_authority_id
            or permit.service_authority_id != expected_service_authority_id
        ):
            raise DomainError(
                503,
                "GENERATION_AUTHORITY_UNAVAILABLE",
                "生图服务身份与原许可不一致",
            )
        if permit.expires_at <= now:
            raise _expired()

        slots = (
            db.execute(
                select(ChatflowGenerationRequestSlot)
                .where(
                    ChatflowGenerationRequestSlot.generation_request_id == generation_request.id,
                    ChatflowGenerationRequestSlot.task_id == task_id,
                )
                .order_by(
                    ChatflowGenerationRequestSlot.position,
                    ChatflowGenerationRequestSlot.slot_id,
                )
            )
            .scalars()
            .all()
        )
        expected_slots = _slot_mapping_for_items(generation_request.id, task_items)
        actual_slots = _slot_mapping_for_rows(slots)
        references = (
            db.execute(
                select(ChatflowPromptReference)
                .where(
                    ChatflowPromptReference.package_id == submission.id,
                    ChatflowPromptReference.task_id == task_id,
                )
                .order_by(
                    ChatflowPromptReference.position,
                    ChatflowPromptReference.ref_id,
                )
            )
            .scalars()
            .all()
        )
        expected_reference_manifest = _reference_manifest_for_expected(expected_references)
        actual_reference_manifest = _reference_manifest_for_rows(references)
        if (
            not slots
            or actual_slots != expected_slots
            or actual_reference_manifest != expected_reference_manifest
        ):
            raise _linkage_conflict()

        slot_mapping_digest = canonical_sha256(expected_slots)
        reference_manifest_digest = canonical_sha256(expected_reference_manifest)
        if permit.permit_version == 1:
            token = _sign_permit(
                self._permit_key,
                permit_id=permit.permit_id,
                generation_request_id=generation_request.id,
                task_id=task_id,
                package_digest=bytes(permit.package_digest),
                slot_mapping_digest=slot_mapping_digest,
                reference_manifest_digest=reference_manifest_digest,
                expires_at=permit.expires_at,
            )
            permit.permit_version = _PERMIT_VERSION
            permit.slot_mapping_digest = slot_mapping_digest
            permit.reference_manifest_digest = reference_manifest_digest
            permit.token_digest = hashlib.sha256(token.encode("ascii")).digest()
            db.flush()
            return
        if (
            permit.permit_version != _PERMIT_VERSION
            or permit.slot_mapping_digest != slot_mapping_digest
            or permit.reference_manifest_digest != reference_manifest_digest
        ):
            raise DomainError(503, "PERMIT_UNAVAILABLE", "提示词许可暂时无法确认")


def _validate_package_preferences(package, resolved: object) -> None:
    if not isinstance(resolved, Mapping):
        raise _linkage_conflict()
    form = package.basis.preference_form
    for key, (field_name, _) in _PREFERENCE_FIELDS.items():
        saved = resolved.get(field_name)
        submitted = form.get(key)
        if not isinstance(saved, Mapping) or submitted is None:
            raise _linkage_conflict()
        mode = saved.get("mode")
        if not isinstance(mode, str) or submitted.source != mode:
            raise _linkage_conflict()
        if mode in {"A", "B", "C", "custom"} and submitted.value != saved.get("text"):
            raise _linkage_conflict()
    platform = resolved.get("platform")
    submitted_platform = form.get("2")
    if (
        not isinstance(platform, Mapping)
        or submitted_platform is None
        or platform.get("mode") != "platform"
        or platform.get("value") != submitted_platform.value
        or submitted_platform.source != "PLATFORM"
    ):
        raise _linkage_conflict()


def _sign_permit(
    key: bytes,
    *,
    permit_id: UUID,
    generation_request_id: UUID,
    task_id: UUID,
    package_digest: bytes,
    slot_mapping_digest: bytes,
    reference_manifest_digest: bytes,
    service_authority_id: UUID | None = None,
    expires_at: datetime,
) -> str:
    payload = {
        "contract_version": _PERMIT_CONTRACT_VERSION,
        "expires_at": expires_at.astimezone(UTC).isoformat(),
        "generation_request_id": str(generation_request_id),
        "package_digest": package_digest.hex(),
        "permit_id": str(permit_id),
        "reference_manifest_digest": reference_manifest_digest.hex(),
        "slot_mapping_digest": slot_mapping_digest.hex(),
        "task_id": str(task_id),
        "token_version": _PERMIT_VERSION,
    }
    if service_authority_id is not None:
        payload["service_authority_id"] = str(service_authority_id)
    payload_bytes = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")
    encoded = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=")
    signing_input = b"chatflow-generation-permit-v2." + encoded
    signature = hmac.digest(key, signing_input, "sha256")
    signature_encoded = base64.urlsafe_b64encode(signature).rstrip(b"=")
    return f"h4p2.{encoded.decode('ascii')}.{signature_encoded.decode('ascii')}"


def _slot_mapping_for_items(
    generation_request_id: UUID, task_items: list[TaskItem]
) -> list[dict[str, object]]:
    return [
        {
            "slot_id": f"slot_{item.position:02d}",
            "position": item.position,
            "image_type": item.image_type,
            "task_item_id": str(item.id),
            "attempt_id": str(uuid5(generation_request_id, f"task-item-attempt:{item.id}")),
        }
        for item in sorted(task_items, key=lambda value: value.position)
    ]


def _slot_mapping_for_rows(
    slots: list[ChatflowGenerationRequestSlot],
) -> list[dict[str, object]]:
    return [
        {
            "slot_id": slot.slot_id,
            "position": slot.position,
            "image_type": slot.image_type,
            "task_item_id": str(slot.task_item_id),
            "attempt_id": str(slot.attempt_id),
        }
        for slot in sorted(slots, key=lambda value: (value.position, value.slot_id))
    ]


def _reference_manifest_for_expected(
    references: list[PromptReferenceSubmission],
) -> list[dict[str, object]]:
    return [
        {
            "asset_id": str(reference.asset_id),
            "position": reference.order,
            "content_sha256": reference.content_sha256,
            "ref_id": reference.ref_id,
        }
        for reference in sorted(references, key=lambda value: value.order)
    ]


def _reference_manifest_for_rows(
    references: list[ChatflowPromptReference],
) -> list[dict[str, object]]:
    return [
        {
            "asset_id": str(reference.asset_id),
            "position": reference.position,
            "content_sha256": reference.content_sha256,
            "ref_id": reference.ref_id,
        }
        for reference in sorted(references, key=lambda value: (value.position, value.ref_id))
    ]


def _db_now(db: Session) -> datetime:
    value = db.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise DomainError(503, "DATABASE_UNAVAILABLE", "流程暂时无法读取")
    return value.astimezone(UTC)


def _content_conflict() -> DomainError:
    return DomainError(409, "PROMPT_SUBMISSION_CONFLICT", "提示词提交与已保存内容冲突")


def _linkage_conflict() -> DomainError:
    return DomainError(409, "PROMPT_PACKAGE_LINKAGE_INVALID", "提示词包与当前任务关联不匹配")


def _expired() -> DomainError:
    return DomainError(410, "GENERATION_PERMIT_EXPIRED", "生图许可或其关联数据已过期")
