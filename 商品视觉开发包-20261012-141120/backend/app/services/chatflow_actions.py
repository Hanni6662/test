from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import timedelta
from typing import Protocol
from uuid import UUID, uuid5

from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.upload_limits import IMAGE_MAX_BYTES
from app.db.action_models import Action
from app.db.business_question_models import BusinessAnswerSubmission, BusinessQuestion
from app.db.business_session_models import BusinessSession
from app.db.chatflow_reference_files import ChatflowDifyReferenceFile
from app.db.chatflow_v34_models import (
    ChatflowV34SessionState,
    ChatflowV34StageEventReceipt,
)
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ImageAsset, ReferenceSet, ReferenceSetImage
from app.db.task_models import Task, TaskItem
from app.integrations.dify.chatflow import (
    ChatflowCancelCommand,
    ChatflowCancelResult,
    ChatflowContinuationCommand,
    ChatflowContinuationResult,
    ChatflowFirstFormDefinitionCommand,
    ChatflowFirstFormDefinitionResult,
    ChatflowFirstFormField,
    ChatflowHumanInputCommand,
    ChatflowHumanInputResult,
    ChatflowOriginalRunEvent,
    ChatflowOriginalRunObservationResult,
    ChatflowStartCommand,
    ChatflowStartResult,
    DifyChatflowPort,
    DifyReferenceUploadCommand,
    DifyReferenceUploadResult,
)
from app.schemas.business_sessions import DifyOriginalRecommendation
from app.services.tasks import validate_dify_chatflow_task_snapshot
from app.workers.action_processor import (
    ActionExecutionResult,
    ActionInput,
    ActionRegistration,
    schedule_action_in_transaction,
)

CHATFLOW_START_ACTION = "dify.chatflow.start.v1"
CHATFLOW_CONTINUE_ACTION = "dify.chatflow.continue.v1"
CHATFLOW_ANSWER_ACTION = "dify.chatflow.answer.v1"
CHATFLOW_ANSWER_RECOVERY_ACTION = "dify.chatflow.answer.recover.v1"
CHATFLOW_REFERENCE_UPLOAD_ACTION = "dify.chatflow.reference.upload.v1"
CHATFLOW_FORM_BIND_ACTION = "dify.chatflow.form.bind.v1"
CHATFLOW_CANCEL_ACTION = "dify.chatflow.cancel.v1"
CHATFLOW_CANCEL_RECOVERY_ACTION = "dify.chatflow.cancel.recover.v1"
CHATFLOW_RUN_OBSERVATION_ACTION = "dify.chatflow.run.observe.v1"
CHATFLOW_START_SEQUENCE = 1
CHATFLOW_CONTINUE_SEQUENCE = 2
CHATFLOW_ANSWER_FIRST_SEQUENCE = 3
CHATFLOW_ANSWER_MAX_RECOVERIES = 3
CHATFLOW_REFERENCE_UPLOAD_SEQUENCE_BASE = 10
CHATFLOW_FORM_BIND_SEQUENCE = 14
CHATFLOW_CANCEL_SEQUENCE = 15
CHATFLOW_CANCEL_RECOVERY_SEQUENCE_BASE = 16
CHATFLOW_CANCEL_RECOVERY_MAX_SEQUENCE = 18
CHATFLOW_RUN_OBSERVATION_SEQUENCE_BASE = 19
CHATFLOW_RUN_OBSERVATION_MAX_SEQUENCE = 24
_CHATFLOW_RUN_OBSERVATION_DELAYS = (0, 5, 10, 20, 40, 80)
CHATFLOW_ACTION_NAMESPACE = UUID("85ed724b-09da-4462-8b6a-374526d28e4d")
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9._:-]{1,320}$")
_SAFE_ERROR = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")


def chatflow_action_id(*, business_session_id: UUID, action_kind: str) -> UUID:
    return uuid5(CHATFLOW_ACTION_NAMESPACE, f"{action_kind}:{business_session_id}")


def chatflow_reference_upload_action_id(*, business_session_id: UUID, position: int) -> UUID:
    return uuid5(
        CHATFLOW_ACTION_NAMESPACE,
        f"{CHATFLOW_REFERENCE_UPLOAD_ACTION}:{business_session_id}:{position}",
    )


def chatflow_reference_upload_sequence(position: int) -> int:
    if not 0 <= position <= 3:
        raise ValueError("reference position is outside the supported range")
    return CHATFLOW_REFERENCE_UPLOAD_SEQUENCE_BASE + position


def chatflow_form_binding_action_digest(
    *,
    session: BusinessSession,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
) -> bytes:
    run_id = session.dify_run_id
    if (
        run_id is None
        or state.form_node_id != "form_0"
        or state.form_token is None
        or state.form_token_digest is None
        or hashlib.sha256(state.form_token.encode("utf-8")).digest() != state.form_token_digest
    ):
        raise ValueError("v3.4 first form binding identity is incomplete")
    immutable_digest = hashlib.sha256(
        session.id.bytes
        + recommendation.id.bytes
        + recommendation.revision.to_bytes(4, "big", signed=False)
        + run_id.encode("utf-8")
        + bytes(state.form_token_digest)
    ).digest()
    return chatflow_action_digest(
        action_kind=CHATFLOW_FORM_BIND_ACTION,
        business_session_id=session.id,
        task_id=None,
        request_digest=immutable_digest,
    )


def schedule_chatflow_form_binding_in_transaction(
    db: Session,
    *,
    session: BusinessSession,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
    due_at,
) -> Action:
    return schedule_action_in_transaction(
        db,
        action_id=chatflow_action_id(
            business_session_id=session.id,
            action_kind=CHATFLOW_FORM_BIND_ACTION,
        ),
        task_id=None,
        business_session_id=session.id,
        scope_kind="business_session",
        scope_id=session.id,
        action_kind=CHATFLOW_FORM_BIND_ACTION,
        sequence=CHATFLOW_FORM_BIND_SEQUENCE,
        due_at=due_at,
        input_digest_version=1,
        input_digest=chatflow_form_binding_action_digest(
            session=session,
            recommendation=recommendation,
            state=state,
        ),
    )


def chatflow_v34_cancel_action_digest(
    *,
    session: BusinessSession,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
) -> bytes:
    from app.services.chatflow_v34_events import recommendation_form_digest

    if (
        session.flow_version != "visual-v3.4-single-selection"
        or session.dify_run_id is None
        or recommendation.business_session_id != session.id
        or recommendation.dify_run_id != session.dify_run_id
        or state.business_session_id != session.id
        or state.flow_version != session.flow_version
        or state.form_status not in {"cancelling", "cancelled"}
        or state.form_node_id != "form_0"
        or state.form_token is None
        or state.form_token_digest is None
        or hashlib.sha256(state.form_token.encode("utf-8")).digest() != state.form_token_digest
        or state.form_definition_digest is None
        or state.form_expires_at is None
        or recommendation.status != "ready"
    ):
        raise ValueError("v3.4 cancel Action binding is incomplete")
    current_digest = recommendation_form_digest(recommendation)
    if current_digest is None or current_digest != state.form_definition_digest:
        raise ValueError("v3.4 cancel form definition changed")
    immutable_digest = hashlib.sha256(
        session.id.bytes
        + recommendation.id.bytes
        + recommendation.revision.to_bytes(4, "big", signed=False)
        + session.dify_run_id.encode("utf-8")
        + state.form_token_digest
        + state.form_definition_digest
        + int(state.form_expires_at.timestamp()).to_bytes(8, "big", signed=False)
    ).digest()
    return chatflow_action_digest(
        action_kind=CHATFLOW_CANCEL_ACTION,
        business_session_id=session.id,
        task_id=None,
        request_digest=immutable_digest,
    )


def schedule_chatflow_v34_cancel_in_transaction(
    db: Session,
    *,
    session: BusinessSession,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
    due_at,
) -> Action:
    return schedule_action_in_transaction(
        db,
        action_id=chatflow_action_id(
            business_session_id=session.id, action_kind=CHATFLOW_CANCEL_ACTION
        ),
        task_id=None,
        business_session_id=session.id,
        scope_kind="business_session",
        scope_id=session.id,
        action_kind=CHATFLOW_CANCEL_ACTION,
        sequence=CHATFLOW_CANCEL_SEQUENCE,
        due_at=due_at,
        input_digest_version=1,
        input_digest=chatflow_v34_cancel_action_digest(
            session=session, recommendation=recommendation, state=state
        ),
    )


def schedule_chatflow_v34_cancel_recovery_in_transaction(
    db: Session,
    *,
    session: BusinessSession,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
    sequence: int,
    due_at,
) -> Action:
    if not (
        CHATFLOW_CANCEL_RECOVERY_SEQUENCE_BASE <= sequence <= CHATFLOW_CANCEL_RECOVERY_MAX_SEQUENCE
    ):
        raise ValueError("v3.4 cancel recovery sequence is outside the bounded window")
    return schedule_action_in_transaction(
        db,
        action_id=uuid5(
            CHATFLOW_ACTION_NAMESPACE,
            f"{CHATFLOW_CANCEL_RECOVERY_ACTION}:{session.id}:{sequence}",
        ),
        task_id=None,
        business_session_id=session.id,
        scope_kind="business_session",
        scope_id=session.id,
        action_kind=CHATFLOW_CANCEL_RECOVERY_ACTION,
        sequence=sequence,
        due_at=due_at,
        input_digest_version=1,
        input_digest=chatflow_v34_cancel_recovery_action_digest(
            session=session,
            recommendation=recommendation,
            state=state,
            sequence=sequence,
        ),
    )


def chatflow_v34_cancel_recovery_action_digest(
    *,
    session: BusinessSession,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
    sequence: int,
) -> bytes:
    if not (
        CHATFLOW_CANCEL_RECOVERY_SEQUENCE_BASE <= sequence <= CHATFLOW_CANCEL_RECOVERY_MAX_SEQUENCE
    ):
        raise ValueError("v3.4 cancel recovery sequence is outside the bounded window")
    cancel_digest = chatflow_v34_cancel_action_digest(
        session=session, recommendation=recommendation, state=state
    )
    immutable_digest = hashlib.sha256(
        session.id.bytes
        + recommendation.id.bytes
        + recommendation.revision.to_bytes(4, "big", signed=False)
        + cancel_digest
        + sequence.to_bytes(4, "big", signed=False)
    ).digest()
    return chatflow_action_digest(
        action_kind=CHATFLOW_CANCEL_RECOVERY_ACTION,
        business_session_id=session.id,
        task_id=None,
        request_digest=immutable_digest,
    )


def chatflow_v34_run_observation_action_id(*, business_session_id: UUID, sequence: int) -> UUID:
    if (
        not CHATFLOW_RUN_OBSERVATION_SEQUENCE_BASE
        <= sequence
        <= CHATFLOW_RUN_OBSERVATION_MAX_SEQUENCE
    ):
        raise ValueError("v3.4 run observation sequence is outside the bounded window")
    return uuid5(
        CHATFLOW_ACTION_NAMESPACE,
        f"{CHATFLOW_RUN_OBSERVATION_ACTION}:{business_session_id}:{sequence}",
    )


def chatflow_v34_run_observation_action_digest(
    *,
    session: BusinessSession,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
    task: Task,
    sequence: int,
) -> bytes:
    if (
        not CHATFLOW_RUN_OBSERVATION_SEQUENCE_BASE
        <= sequence
        <= CHATFLOW_RUN_OBSERVATION_MAX_SEQUENCE
    ):
        raise ValueError("v3.4 run observation sequence is outside the bounded window")
    if (
        session.flow_version != "visual-v3.4-single-selection"
        or session.dify_run_id is None
        or session.dify_user_id != f"bs-{session.id.hex}"
        or recommendation.business_session_id != session.id
        or recommendation.dify_run_id != session.dify_run_id
        or recommendation.status not in {"ready", "failed"}
        or recommendation.revision != task.recommendation_revision
        or task.business_session_id != session.id
        or task.owner_user_id != session.owner_user_id
        or task.recommendation_id != recommendation.id
        or task.provenance_kind != "dify_chatflow"
        or task.request_digest is None
        or state.business_session_id != session.id
        or state.flow_version != session.flow_version
        or state.selection_status != "accepted"
        or state.selection_evidence_kind not in {"http_200", "selection_applied"}
        or state.selection_submission_id is None
        or state.selection_input_digest is None
    ):
        raise ValueError("v3.4 run observation binding is incomplete")
    immutable_digest = hashlib.sha256(
        session.id.bytes
        + recommendation.id.bytes
        + recommendation.revision.to_bytes(4, "big", signed=False)
        + task.id.bytes
        + task.request_digest
        + state.selection_submission_id.bytes
        + state.selection_input_digest
        + session.dify_run_id.encode("utf-8")
        + sequence.to_bytes(4, "big", signed=False)
    ).digest()
    return chatflow_action_digest(
        action_kind=CHATFLOW_RUN_OBSERVATION_ACTION,
        business_session_id=session.id,
        task_id=task.id,
        request_digest=immutable_digest,
    )


def schedule_chatflow_v34_run_observation_in_transaction(
    db: Session,
    *,
    session: BusinessSession,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
    task: Task,
    sequence: int,
    due_at,
) -> Action:
    action_id = chatflow_v34_run_observation_action_id(
        business_session_id=session.id, sequence=sequence
    )
    input_digest = chatflow_v34_run_observation_action_digest(
        session=session,
        recommendation=recommendation,
        state=state,
        task=task,
        sequence=sequence,
    )
    existing = db.execute(
        select(Action).where(Action.id == action_id).with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.action_kind != CHATFLOW_RUN_OBSERVATION_ACTION
            or existing.scope_kind != "business_session"
            or existing.scope_id != session.id
            or existing.business_session_id != session.id
            or existing.task_id != task.id
            or existing.sequence != sequence
            or existing.input_digest_version != 1
            or existing.input_digest != input_digest
        ):
            raise ValueError("v3.4 run observation Action identity conflicts")
        return existing
    return schedule_action_in_transaction(
        db,
        action_id=action_id,
        task_id=task.id,
        business_session_id=session.id,
        scope_kind="business_session",
        scope_id=session.id,
        action_kind=CHATFLOW_RUN_OBSERVATION_ACTION,
        sequence=sequence,
        due_at=due_at,
        input_digest_version=1,
        input_digest=input_digest,
    )


def chatflow_v34_selection_action_digest(
    *,
    session: BusinessSession,
    recommendation: Recommendation,
    state: ChatflowV34SessionState,
    task: Task,
) -> bytes:
    from app.services.chatflow_v34_conversion import order_form_0_submission

    expected_action_id = chatflow_action_id(
        business_session_id=session.id,
        action_kind=CHATFLOW_CONTINUE_ACTION,
    )
    if (
        session.flow_version != "visual-v3.4-single-selection"
        or session.dify_run_id is None
        or recommendation.dify_run_id != session.dify_run_id
        or recommendation.business_session_id != session.id
        or task.business_session_id != session.id
        or task.recommendation_id != recommendation.id
        or state.business_session_id != session.id
        or state.form_status != "consumed"
        or state.form_node_id != "form_0"
        or state.form_token is None
        or state.form_token_digest is None
        or hashlib.sha256(state.form_token.encode("utf-8")).digest() != state.form_token_digest
        or state.form_definition_digest is None
        or state.selection_submission_id != expected_action_id
        or state.selection_input_digest is None
        or state.selection_inputs is None
        or task.request_digest is None
    ):
        raise ValueError("v3.4 selection Action binding is incomplete")
    form_inputs = order_form_0_submission(state.selection_inputs)
    canonical_inputs = json.dumps(
        {
            "contract_version": "visual-v34-stage-v1",
            "business_session_id": str(session.id),
            "recommendation_id": str(recommendation.id),
            "recommendation_revision": recommendation.revision,
            "dify_run_id": session.dify_run_id,
            "task_id": str(task.id),
            "selection_submission_id": str(state.selection_submission_id),
            "form_token_digest": state.form_token_digest.hex(),
            "form_definition_digest": state.form_definition_digest.hex(),
            "form_inputs": [[name, value] for name, value in form_inputs.items()],
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    expected_input_digest = hashlib.sha256(canonical_inputs.encode("utf-8")).digest()
    if state.selection_input_digest != expected_input_digest:
        raise ValueError("v3.4 selection form input digest changed")
    immutable_digest = hashlib.sha256(
        session.id.bytes
        + recommendation.id.bytes
        + recommendation.revision.to_bytes(4, "big", signed=False)
        + session.dify_run_id.encode("utf-8")
        + task.id.bytes
        + task.request_digest
        + state.selection_submission_id.bytes
        + state.selection_input_digest
        + state.form_token_digest
        + state.form_definition_digest
    ).digest()
    return chatflow_action_digest(
        action_kind=CHATFLOW_CONTINUE_ACTION,
        business_session_id=session.id,
        task_id=task.id,
        request_digest=immutable_digest,
    )


def set_chatflow_v34_selection_input_digest(
    *,
    session: BusinessSession,
    recommendation: Recommendation,
    task: Task,
    state: ChatflowV34SessionState,
    form_inputs: dict[str, str],
) -> bytes:
    from app.services.chatflow_v34_conversion import order_form_0_submission

    ordered_inputs = order_form_0_submission(form_inputs)
    canonical_inputs = json.dumps(
        {
            "contract_version": "visual-v34-stage-v1",
            "business_session_id": str(session.id),
            "recommendation_id": str(recommendation.id),
            "recommendation_revision": recommendation.revision,
            "dify_run_id": session.dify_run_id,
            "task_id": str(task.id),
            "selection_submission_id": str(state.selection_submission_id),
            "form_token_digest": state.form_token_digest.hex(),
            "form_definition_digest": state.form_definition_digest.hex(),
            "form_inputs": [[name, value] for name, value in ordered_inputs.items()],
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical_inputs.encode("utf-8")).digest()


def chatflow_reference_upload_digest(row: ChatflowDifyReferenceFile) -> bytes:
    canonical = json.dumps(
        {
            "action_kind": CHATFLOW_REFERENCE_UPLOAD_ACTION,
            "business_session_id": str(row.business_session_id),
            "owner_user_id": str(row.owner_user_id),
            "reference_set_id": str(row.reference_set_id),
            "reference_set_image_id": str(row.reference_set_image_id),
            "model_asset_id": str(row.model_asset_id),
            "slot": row.slot,
            "position": row.position,
            "content_sha256": row.content_sha256,
            "dify_app_id": row.dify_app_id,
            "dify_user_id": row.dify_user_id,
            "upload_action_id": str(row.upload_action_id),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def schedule_chatflow_reference_upload_in_transaction(
    db: Session, row: ChatflowDifyReferenceFile, *, due_at
) -> Action:
    return schedule_action_in_transaction(
        db,
        action_id=row.upload_action_id,
        task_id=None,
        business_session_id=row.business_session_id,
        scope_kind="business_session",
        scope_id=row.business_session_id,
        action_kind=CHATFLOW_REFERENCE_UPLOAD_ACTION,
        sequence=chatflow_reference_upload_sequence(row.position),
        due_at=due_at,
        input_digest_version=1,
        input_digest=chatflow_reference_upload_digest(row),
    )


class _ReferenceContentStorage(Protocol):
    def open_reader(self, key: str): ...


class _ReferenceUploadPort(Protocol):
    def upload_reference(
        self, command: DifyReferenceUploadCommand
    ) -> DifyReferenceUploadResult: ...


def prepare_chatflow_reference_upload(db: Session, action: Action) -> ActionInput:
    if (
        action.action_kind != CHATFLOW_REFERENCE_UPLOAD_ACTION
        or action.scope_kind != "business_session"
        or action.task_id is not None
        or action.business_session_id != action.scope_id
        or action.sequence < CHATFLOW_REFERENCE_UPLOAD_SEQUENCE_BASE
    ):
        raise ValueError("Chatflow reference upload Action identity is invalid")
    row = db.execute(
        select(ChatflowDifyReferenceFile).where(
            ChatflowDifyReferenceFile.upload_action_id == action.id,
            ChatflowDifyReferenceFile.business_session_id == action.business_session_id,
        )
    ).scalar_one_or_none()
    session = db.get(BusinessSession, action.business_session_id)
    if (
        row is None
        or session is None
        or row.status != "pending"
        or row.owner_user_id != session.owner_user_id
        or row.reference_set_id != session.reference_set_id
        or row.dify_user_id != session.dify_user_id
        or action.sequence != chatflow_reference_upload_sequence(row.position)
        or action.input_digest != chatflow_reference_upload_digest(row)
        or session.status != "starting"
        or session.start_outcome != "pending"
    ):
        raise ValueError("Chatflow reference upload state is invalid")
    if db.execute(
        select(ChatflowDifyReferenceFile.id).where(
            ChatflowDifyReferenceFile.business_session_id == session.id,
            ChatflowDifyReferenceFile.position < row.position,
            ChatflowDifyReferenceFile.status != "uploaded",
        )
    ).first():
        raise ValueError("Chatflow reference uploads must preserve image order")
    asset = db.execute(
        select(ImageAsset)
        .join(
            ReferenceSetImage,
            ReferenceSetImage.model_asset_id == ImageAsset.id,
        )
        .where(
            ReferenceSetImage.id == row.reference_set_image_id,
            ReferenceSetImage.reference_set_id == session.reference_set_id,
            ReferenceSetImage.owner_user_id == session.owner_user_id,
            ReferenceSetImage.model_asset_id == row.model_asset_id,
            ReferenceSetImage.position == row.position,
            ReferenceSetImage.slot == row.slot,
            ReferenceSetImage.content_sha256 == row.content_sha256,
            ImageAsset.owner_user_id == session.owner_user_id,
            ImageAsset.asset_kind == "reference_model_input",
            ImageAsset.content_sha256 == row.content_sha256,
        )
    ).scalar_one_or_none()
    if asset is None or asset.size_bytes > IMAGE_MAX_BYTES:
        raise ValueError("Chatflow reference asset binding is invalid")
    payload = {
        "business_session_id": str(session.id),
        "owner_user_id": str(session.owner_user_id),
        "reference_set_id": str(session.reference_set_id),
        "reference_set_image_id": str(row.reference_set_image_id),
        "model_asset_id": str(row.model_asset_id),
        "dify_app_id": row.dify_app_id,
        "dify_user_id": row.dify_user_id,
        "slot": row.slot,
        "position": row.position,
        "content_sha256": row.content_sha256,
        "mime_type": asset.mime_type,
        "size_bytes": asset.size_bytes,
        "object_key": asset.object_key,
    }
    return _action_input(action, payload)


def execute_chatflow_reference_upload(
    port: _ReferenceUploadPort,
    action_input: ActionInput,
    *,
    reference_storage: _ReferenceContentStorage | None,
) -> ActionExecutionResult:
    payload = action_input.payload
    if not isinstance(payload, Mapping):
        return ActionExecutionResult(
            "definitely_rejected", safe_reason_code="DIFY_REFERENCE_UPLOAD_INPUT_INVALID"
        )
    if reference_storage is None:
        return ActionExecutionResult(
            "definitely_rejected", safe_reason_code="DIFY_REFERENCE_STORAGE_NOT_CONFIGURED"
        )
    object_key = payload.get("object_key")
    expected_digest = payload.get("content_sha256")
    expected_size = payload.get("size_bytes")
    if (
        not isinstance(object_key, str)
        or not object_key
        or not isinstance(expected_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or not 0 < expected_size <= IMAGE_MAX_BYTES
    ):
        return ActionExecutionResult(
            "definitely_rejected", safe_reason_code="DIFY_REFERENCE_UPLOAD_INPUT_INVALID"
        )
    try:
        with reference_storage.open_reader(object_key) as reader:
            content = reader.read(IMAGE_MAX_BYTES + 1)
    except Exception:
        return ActionExecutionResult(
            "definitely_rejected", safe_reason_code="DIFY_REFERENCE_BYTES_UNAVAILABLE"
        )
    if len(content) != expected_size or hashlib.sha256(content).hexdigest() != expected_digest:
        return ActionExecutionResult(
            "definitely_rejected", safe_reason_code="DIFY_REFERENCE_BYTES_INVALID"
        )
    try:
        result = port.upload_reference(
            DifyReferenceUploadCommand(
                business_session_id=UUID(str(payload["business_session_id"])),
                dify_app_id=str(payload["dify_app_id"]),
                dify_user_id=str(payload["dify_user_id"]),
                reference_set_image_id=UUID(str(payload["reference_set_image_id"])),
                model_asset_id=UUID(str(payload["model_asset_id"])),
                slot=str(payload["slot"]),
                position=int(payload["position"]),
                content_sha256=expected_digest,
                mime_type=str(payload["mime_type"]),
                size_bytes=expected_size,
                content=content,
            )
        )
    except Exception:
        return ActionExecutionResult(
            "unknown", safe_reason_code="DIFY_REFERENCE_UPLOAD_OUTCOME_UNKNOWN"
        )
    if not isinstance(result, DifyReferenceUploadResult):
        return ActionExecutionResult(
            "unknown", safe_reason_code="DIFY_REFERENCE_UPLOAD_RESULT_INVALID"
        )
    if result.outcome == "uploaded":
        file_id = result.dify_file_id
        if not isinstance(file_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,255}", file_id) is None:
            return ActionExecutionResult(
                "unknown", safe_reason_code="DIFY_REFERENCE_UPLOAD_RESULT_INVALID"
            )
        return ActionExecutionResult("accepted", value={"dify_file_id": file_id})
    if result.outcome == "definitely_rejected":
        return ActionExecutionResult(
            "definitely_rejected",
            safe_reason_code=_safe_reason(
                result.safe_reason_code, "DIFY_REFERENCE_UPLOAD_REJECTED"
            ),
        )
    return ActionExecutionResult(
        "unknown",
        safe_reason_code=_safe_reason(
            result.safe_reason_code, "DIFY_REFERENCE_UPLOAD_OUTCOME_UNKNOWN"
        ),
    )


def recover_chatflow_reference_upload(_action_input: ActionInput) -> ActionExecutionResult:
    # Dify has no documented upload lookup/idempotency key; never repeat an ambiguous POST.
    return ActionExecutionResult(
        "unknown", safe_reason_code="DIFY_REFERENCE_UPLOAD_OUTCOME_UNKNOWN"
    )


def finalize_chatflow_reference_upload(
    db: Session,
    action: Action,
    action_input: ActionInput,
    result: ActionExecutionResult,
) -> None:
    session, recommendation = _load_action_session_and_recommendation(db, action, action_input)
    row = db.execute(
        select(ChatflowDifyReferenceFile).where(
            ChatflowDifyReferenceFile.upload_action_id == action.id,
            ChatflowDifyReferenceFile.business_session_id == session.id,
            ChatflowDifyReferenceFile.owner_user_id == session.owner_user_id,
        )
    ).scalar_one_or_none()
    if row is None or action.input_digest != chatflow_reference_upload_digest(row):
        raise ValueError("Chatflow reference mapping identity changed")
    if result.outcome == "accepted":
        value = result.value
        file_id = value.get("dify_file_id") if isinstance(value, Mapping) else None
        if not isinstance(file_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,255}", file_id) is None:
            raise ValueError("Chatflow reference upload has no valid Dify file ID")
        if row.status == "uploaded":
            if row.dify_file_id != file_id:
                raise ValueError("Dify reference file mapping changed")
            return
        if row.status != "pending":
            raise ValueError("Chatflow reference upload mapping is no longer pending")
        now = db.scalar(select(func.clock_timestamp()))
        if now is None:
            raise ValueError("database time is unavailable")
        row.status = "uploaded"
        row.dify_file_id = file_id
        row.uploaded_at = now
        row.updated_at = now
        rows = (
            db.execute(
                select(ChatflowDifyReferenceFile)
                .where(ChatflowDifyReferenceFile.business_session_id == session.id)
                .order_by(ChatflowDifyReferenceFile.position)
            )
            .scalars()
            .all()
        )
        if not rows or any(item.status not in {"pending", "uploaded"} for item in rows):
            raise ValueError("Chatflow reference mapping contains a terminal failure")
        next_row = next((item for item in rows if item.status == "pending"), None)
        if next_row is not None:
            schedule_chatflow_reference_upload_in_transaction(db, next_row, due_at=now)
            return
        if not all(item.status == "uploaded" and item.dify_file_id for item in rows):
            raise ValueError("Chatflow run cannot start before all file IDs are durable")
        bound_digest = hashlib.sha256(
            session.request_digest + session.reference_asset_digest
        ).digest()
        schedule_action_in_transaction(
            db,
            action_id=chatflow_action_id(
                business_session_id=session.id, action_kind=CHATFLOW_START_ACTION
            ),
            task_id=None,
            business_session_id=session.id,
            scope_kind="business_session",
            scope_id=session.id,
            action_kind=CHATFLOW_START_ACTION,
            sequence=CHATFLOW_START_SEQUENCE,
            due_at=now,
            input_digest_version=1,
            input_digest=chatflow_action_digest(
                action_kind=CHATFLOW_START_ACTION,
                business_session_id=session.id,
                task_id=None,
                request_digest=bound_digest,
            ),
        )
        return
    if result.outcome == "unknown":
        row.status = "unknown"
        row.dify_file_id = None
        row.uploaded_at = None
        session.status = "reconciling"
        session.start_outcome = "unknown"
        session.updated_at = func.clock_timestamp()
        state = dict(recommendation.state) if isinstance(recommendation.state, dict) else {}
        state.update({"stage": "reference_upload_unknown"})
        recommendation.state = state
        recommendation.updated_at = func.clock_timestamp()
        return
    row.status = "rejected"
    row.dify_file_id = None
    row.uploaded_at = None
    session.status = "failed"
    session.start_outcome = "rejected"
    session.updated_at = func.clock_timestamp()
    recommendation.status = "failed"
    recommendation.failure_code = result.safe_reason_code or "DIFY_REFERENCE_UPLOAD_REJECTED"
    recommendation.state = {"stage": "failed", "failure_code": recommendation.failure_code}
    recommendation.updated_at = func.clock_timestamp()


def chatflow_action_digest(
    *,
    action_kind: str,
    business_session_id: UUID,
    task_id: UUID | None,
    request_digest: bytes,
) -> bytes:
    canonical = json.dumps(
        {
            "action_kind": action_kind,
            "business_session_id": str(business_session_id),
            "task_id": str(task_id) if task_id is not None else None,
            "request_digest": request_digest.hex(),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def build_chatflow_action_registrations(
    port: DifyChatflowPort,
    *,
    reference_storage: _ReferenceContentStorage | None = None,
    reference_upload_port: _ReferenceUploadPort | None = None,
) -> dict[str, ActionRegistration]:
    upload_port = reference_upload_port or port
    return {
        CHATFLOW_REFERENCE_UPLOAD_ACTION: ActionRegistration(
            kind=CHATFLOW_REFERENCE_UPLOAD_ACTION,
            prepare=prepare_chatflow_reference_upload,
            execute=lambda action_input: execute_chatflow_reference_upload(
                upload_port, action_input, reference_storage=reference_storage
            ),
            recover=recover_chatflow_reference_upload,
            finalize=finalize_chatflow_reference_upload,
            scope_kinds=frozenset({"business_session"}),
            complete_unknown_step=True,
        ),
        CHATFLOW_START_ACTION: ActionRegistration(
            kind=CHATFLOW_START_ACTION,
            prepare=prepare_chatflow_start,
            execute=lambda action_input: execute_chatflow_start(port, action_input),
            recover=recover_chatflow_start,
            finalize=finalize_chatflow_start,
            scope_kinds=frozenset({"business_session"}),
        ),
        CHATFLOW_FORM_BIND_ACTION: ActionRegistration(
            kind=CHATFLOW_FORM_BIND_ACTION,
            prepare=prepare_chatflow_form_binding,
            execute=lambda action_input: execute_chatflow_form_binding(port, action_input),
            recover=lambda action_input: recover_chatflow_form_binding(port, action_input),
            finalize=finalize_chatflow_form_binding,
            scope_kinds=frozenset({"business_session"}),
        ),
        CHATFLOW_CANCEL_ACTION: ActionRegistration(
            kind=CHATFLOW_CANCEL_ACTION,
            prepare=prepare_chatflow_v34_cancel,
            execute=lambda action_input: execute_chatflow_v34_cancel(port, action_input),
            recover=lambda action_input: recover_chatflow_v34_cancel(port, action_input),
            finalize=finalize_chatflow_v34_cancel,
            scope_kinds=frozenset({"business_session"}),
            complete_unknown_step=True,
        ),
        CHATFLOW_CANCEL_RECOVERY_ACTION: ActionRegistration(
            kind=CHATFLOW_CANCEL_RECOVERY_ACTION,
            prepare=prepare_chatflow_v34_cancel,
            execute=lambda action_input: recover_chatflow_v34_cancel(port, action_input),
            recover=lambda action_input: recover_chatflow_v34_cancel(port, action_input),
            finalize=finalize_chatflow_v34_cancel,
            scope_kinds=frozenset({"business_session"}),
            complete_unknown_step=True,
        ),
        CHATFLOW_CONTINUE_ACTION: ActionRegistration(
            kind=CHATFLOW_CONTINUE_ACTION,
            prepare=prepare_chatflow_continuation,
            execute=lambda action_input: execute_chatflow_continuation(port, action_input),
            recover=lambda action_input: recover_chatflow_continuation(port, action_input),
            finalize=finalize_chatflow_continuation,
            scope_kinds=frozenset({"business_session"}),
        ),
        CHATFLOW_RUN_OBSERVATION_ACTION: ActionRegistration(
            kind=CHATFLOW_RUN_OBSERVATION_ACTION,
            prepare=prepare_chatflow_v34_run_observation,
            execute=lambda action_input: execute_chatflow_v34_run_observation(port, action_input),
            recover=lambda action_input: execute_chatflow_v34_run_observation(port, action_input),
            finalize=finalize_chatflow_v34_run_observation,
            scope_kinds=frozenset({"business_session"}),
            complete_unknown_step=True,
        ),
        CHATFLOW_ANSWER_ACTION: ActionRegistration(
            kind=CHATFLOW_ANSWER_ACTION,
            prepare=prepare_chatflow_answer,
            execute=lambda action_input: execute_chatflow_answer(port, action_input),
            recover=lambda action_input: recover_chatflow_answer(port, action_input),
            finalize=finalize_chatflow_answer,
            scope_kinds=frozenset({"business_session"}),
            complete_unknown_step=True,
        ),
        CHATFLOW_ANSWER_RECOVERY_ACTION: ActionRegistration(
            kind=CHATFLOW_ANSWER_RECOVERY_ACTION,
            prepare=prepare_chatflow_answer,
            execute=lambda action_input: recover_chatflow_answer(port, action_input),
            recover=lambda action_input: recover_chatflow_answer(port, action_input),
            finalize=finalize_chatflow_answer,
            scope_kinds=frozenset({"business_session"}),
            complete_unknown_step=True,
        ),
    }


def prepare_chatflow_v34_cancel(db: Session, action: Action) -> ActionInput:
    if (
        action.action_kind not in {CHATFLOW_CANCEL_ACTION, CHATFLOW_CANCEL_RECOVERY_ACTION}
        or action.scope_kind != "business_session"
        or action.task_id is not None
        or action.business_session_id != action.scope_id
        or (
            action.action_kind == CHATFLOW_CANCEL_ACTION
            and action.sequence != CHATFLOW_CANCEL_SEQUENCE
        )
        or (
            action.action_kind == CHATFLOW_CANCEL_RECOVERY_ACTION
            and not (
                CHATFLOW_CANCEL_RECOVERY_SEQUENCE_BASE
                <= action.sequence
                <= CHATFLOW_CANCEL_RECOVERY_MAX_SEQUENCE
            )
        )
    ):
        raise ValueError("v3.4 cancel Action identity is invalid")
    session = db.get(BusinessSession, action.business_session_id)
    recommendation = db.execute(
        select(Recommendation).where(
            Recommendation.business_session_id == action.business_session_id,
            Recommendation.source_kind == "dify_chatflow",
        )
    ).scalar_one_or_none()
    state = db.execute(
        select(ChatflowV34SessionState)
        .where(ChatflowV34SessionState.business_session_id == action.business_session_id)
        .with_for_update()
    ).scalar_one_or_none()
    task_exists = db.execute(
        select(Task.id).where(Task.business_session_id == action.business_session_id).limit(1)
    ).first()
    expected_digest = None
    if session is not None and recommendation is not None and state is not None:
        if action.action_kind == CHATFLOW_CANCEL_ACTION:
            expected_digest = chatflow_v34_cancel_action_digest(
                session=session, recommendation=recommendation, state=state
            )
            expected_action_id = chatflow_action_id(
                business_session_id=session.id, action_kind=CHATFLOW_CANCEL_ACTION
            )
        else:
            expected_digest = chatflow_v34_cancel_recovery_action_digest(
                session=session,
                recommendation=recommendation,
                state=state,
                sequence=action.sequence,
            )
            expected_action_id = uuid5(
                CHATFLOW_ACTION_NAMESPACE,
                f"{CHATFLOW_CANCEL_RECOVERY_ACTION}:{session.id}:{action.sequence}",
            )
    else:
        expected_action_id = None
    if (
        session is None
        or recommendation is None
        or state is None
        or session.flow_version != "visual-v3.4-single-selection"
        or session.status != "reconciling"
        or session.dify_run_id is None
        or recommendation.dify_run_id != session.dify_run_id
        or recommendation.status != "ready"
        or state.form_status != "cancelling"
        or state.selection_status != "not_started"
        or state.form_node_id != "form_0"
        or state.form_token is None
        or state.form_token_digest is None
        or action.id != expected_action_id
        or task_exists is not None
        or action.input_digest != expected_digest
    ):
        raise ValueError("v3.4 cancel state is invalid")
    from app.services.chatflow_v34_conversion import (
        form_0_definition_for_recommendation,
    )

    original = DifyOriginalRecommendation.model_validate(
        recommendation.original_recommendation, strict=True
    )
    expected_fields = tuple(
        ChatflowFirstFormField(
            output_variable_name=item.name,
            input_type=item.kind,
            option_values=item.allowed_values if item.kind == "select" else (),
        )
        for item in form_0_definition_for_recommendation(original)
    )
    expiration_timestamp = int(state.form_expires_at.timestamp())
    payload = {
        "business_session_id": str(session.id),
        "recommendation_id": str(recommendation.id),
        "recommendation_revision": recommendation.revision,
        "dify_run_id": session.dify_run_id,
        "dify_user_id": session.dify_user_id,
        "form_node_id": "form_0",
        "form_token": SecretStr(state.form_token),
        "expected_form_definition_digest": bytes(state.form_definition_digest),
        "expected_fields": tuple(
            {
                "output_variable_name": item.output_variable_name,
                "input_type": item.input_type,
                "option_values": item.option_values,
            }
            for item in expected_fields
        ),
        "expected_expiration_time": expiration_timestamp,
    }
    return _action_input(action, payload)


def execute_chatflow_v34_cancel(
    port: DifyChatflowPort, action_input: ActionInput
) -> ActionExecutionResult:
    try:
        command = _v34_cancel_command(action_input.payload)
        result = port.cancel_before_selection(command)
    except Exception:
        return ActionExecutionResult("unknown", safe_reason_code="DIFY_CANCEL_OUTCOME_UNKNOWN")
    return _v34_cancel_result(result)


def recover_chatflow_v34_cancel(
    port: DifyChatflowPort, action_input: ActionInput
) -> ActionExecutionResult:
    try:
        command = _v34_cancel_command(action_input.payload)
        result = port.recover_cancel_before_selection(command)
    except Exception:
        return ActionExecutionResult("unknown", safe_reason_code="DIFY_CANCEL_RECOVERY_UNKNOWN")
    if not isinstance(result, ChatflowCancelResult):
        return ActionExecutionResult("unknown", safe_reason_code="DIFY_CANCEL_RECOVERY_INVALID")
    return ActionExecutionResult(
        "unknown",
        safe_reason_code=_safe_reason(
            result.safe_reason_code, "DIFY_CANCEL_RECOVERY_REQUIRES_ROOT_EVIDENCE"
        ),
        value=result,
    )


def finalize_chatflow_v34_cancel(
    db: Session,
    action: Action,
    action_input: ActionInput,
    result: ActionExecutionResult,
) -> None:
    session, recommendation = _load_action_session_and_recommendation(db, action, action_input)
    state = db.execute(
        select(ChatflowV34SessionState)
        .where(ChatflowV34SessionState.business_session_id == session.id)
        .with_for_update()
    ).scalar_one_or_none()
    if state is None or action.task_id is not None or state.selection_status != "not_started":
        raise ValueError("v3.4 cancel finalization binding changed")
    if state.form_status in {"cancelled", "expired", "rejected"} or session.status in {
        "cancelled",
        "expired",
        "failed",
    }:
        return
    expected_digest = (
        chatflow_v34_cancel_action_digest(
            session=session, recommendation=recommendation, state=state
        )
        if action.action_kind == CHATFLOW_CANCEL_ACTION
        else chatflow_v34_cancel_recovery_action_digest(
            session=session,
            recommendation=recommendation,
            state=state,
            sequence=action.sequence,
        )
    )
    if action.input_digest != expected_digest:
        raise ValueError("v3.4 cancel finalization binding changed")
    if state.form_status != "cancelling" or session.status != "reconciling":
        raise ValueError("v3.4 cancel state is no longer pending")

    if (
        isinstance(result.value, ChatflowCancelResult)
        and _v34_cancel_terminal_path(
            result.value.events, expected_run_id=session.dify_run_id or ""
        )
        and _v34_cancel_receipt_is_accepted(
            db,
            session=session,
            recommendation=recommendation,
        )
    ):
        _mark_v34_cancelled(session, state)
        return
    if result.outcome == "definitely_rejected":
        state.form_status = "rejected"
        state.safe_reason_code = _safe_reason(
            result.safe_reason_code, "V34_CANCEL_DEFINITELY_REJECTED"
        )
        session.status = "failed"
        session.updated_at = func.clock_timestamp()
        state.updated_at = func.clock_timestamp()
        return

    now = db.scalar(select(func.clock_timestamp()))
    if now is None:
        raise ValueError("database time is unavailable")
    next_sequence = (
        CHATFLOW_CANCEL_RECOVERY_SEQUENCE_BASE
        if action.action_kind == CHATFLOW_CANCEL_ACTION
        else action.sequence + 1
    )
    if next_sequence <= CHATFLOW_CANCEL_RECOVERY_MAX_SEQUENCE:
        schedule_chatflow_v34_cancel_recovery_in_transaction(
            db,
            session=session,
            recommendation=recommendation,
            state=state,
            sequence=next_sequence,
            due_at=now,
        )
    state.safe_reason_code = result.safe_reason_code or "DIFY_CANCEL_OUTCOME_PENDING"
    session.status = "reconciling"
    session.updated_at = now
    state.updated_at = now


def _v34_cancel_command(payload: object) -> ChatflowCancelCommand:
    if not isinstance(payload, Mapping):
        raise ValueError("v3.4 cancel payload is invalid")
    token = payload.get("form_token")
    fields_raw = payload.get("expected_fields")
    digest = payload.get("expected_form_definition_digest")
    if not isinstance(token, SecretStr) or not isinstance(fields_raw, tuple):
        raise ValueError("v3.4 cancel payload is incomplete")
    fields: list[ChatflowFirstFormField] = []
    for item in fields_raw:
        if not isinstance(item, Mapping):
            raise ValueError("v3.4 cancel expected fields are invalid")
        name = item.get("output_variable_name")
        input_type = item.get("input_type")
        options = item.get("option_values")
        if (
            not isinstance(name, str)
            or not isinstance(input_type, str)
            or not isinstance(options, tuple)
            or any(not isinstance(option, str) for option in options)
        ):
            raise ValueError("v3.4 cancel expected field is invalid")
        fields.append(
            ChatflowFirstFormField(
                output_variable_name=name,
                input_type=input_type,
                option_values=options,
            )
        )
    if not isinstance(digest, bytes) or len(digest) != 32:
        raise ValueError("v3.4 cancel definition digest is invalid")
    return ChatflowCancelCommand(
        business_session_id=UUID(str(payload["business_session_id"])),
        recommendation_id=UUID(str(payload["recommendation_id"])),
        recommendation_revision=int(payload["recommendation_revision"]),
        dify_run_id=str(payload["dify_run_id"]),
        dify_user_id=str(payload["dify_user_id"]),
        form_node_id=str(payload["form_node_id"]),
        form_token=token.get_secret_value(),
        expected_form_definition_digest=digest,
        expected_fields=tuple(fields),
        expected_expiration_time=int(payload["expected_expiration_time"]),
    )


def _v34_cancel_result(result: ChatflowCancelResult) -> ActionExecutionResult:
    if not isinstance(result, ChatflowCancelResult):
        return ActionExecutionResult("unknown", safe_reason_code="DIFY_CANCEL_RESULT_INVALID")
    if result.outcome == "accepted":
        return ActionExecutionResult("accepted", value=result)
    if result.outcome == "definitely_rejected":
        return ActionExecutionResult(
            "definitely_rejected",
            safe_reason_code=_safe_reason(result.safe_reason_code, "V34_CANCEL_REJECTED"),
            value=result,
        )
    return ActionExecutionResult(
        "unknown",
        safe_reason_code=_safe_reason(result.safe_reason_code, "DIFY_CANCEL_OUTCOME_UNKNOWN"),
        value=result,
    )


def _v34_cancel_terminal_path(
    events: tuple[ChatflowOriginalRunEvent, ...], *, expected_run_id: str
) -> bool:
    if not events or not expected_run_id:
        return False
    if any(event.dify_run_id != expected_run_id for event in events):
        return False
    finished_node_ids = {
        event.node_id for event in events if event.event == "node_finished" and event.node_id
    }
    observed_node_ids = {event.node_id for event in events if event.node_id}
    forbidden = {
        "parse_0",
        "resolve_0",
        "design",
        "generation_prepare",
        "generation_submit",
        "generation_wait",
    }
    return (
        events[-1].event == "workflow_finished"
        and events[-1].workflow_status == "succeeded"
        and {"selection_cancelled_callback", "cancelled"} <= finished_node_ids
        and not {"selection_expired_callback", "selection_expired"}.intersection(finished_node_ids)
        and not forbidden.intersection(observed_node_ids)
    )


def _v34_cancel_receipt_is_accepted(
    db: Session,
    *,
    session: BusinessSession,
    recommendation: Recommendation,
) -> bool:
    return (
        db.execute(
            select(ChatflowV34StageEventReceipt.id)
            .where(
                ChatflowV34StageEventReceipt.business_session_id == session.id,
                ChatflowV34StageEventReceipt.flow_version == session.flow_version,
                ChatflowV34StageEventReceipt.recommendation_id == recommendation.id,
                ChatflowV34StageEventReceipt.recommendation_revision == recommendation.revision,
                ChatflowV34StageEventReceipt.dify_run_id == session.dify_run_id,
                ChatflowV34StageEventReceipt.node_id == "selection_cancelled_callback",
                ChatflowV34StageEventReceipt.event_type == "selection.cancelled",
                ChatflowV34StageEventReceipt.payload == {"reason_code": "FORM_CANCELLED"},
                ChatflowV34StageEventReceipt.disposition == "accepted",
                ChatflowV34StageEventReceipt.processed_at.is_not(None),
            )
            .with_for_update()
        ).first()
        is not None
    )


def _mark_v34_cancelled(session: BusinessSession, state: ChatflowV34SessionState) -> None:
    state.form_status = "cancelled"
    state.safe_reason_code = None
    session.status = "cancelled"
    session.updated_at = func.clock_timestamp()
    state.updated_at = func.clock_timestamp()


def prepare_chatflow_start(db: Session, action: Action) -> ActionInput:
    if (
        action.action_kind != CHATFLOW_START_ACTION
        or action.scope_kind != "business_session"
        or action.task_id is not None
        or action.business_session_id != action.scope_id
        or action.sequence != CHATFLOW_START_SEQUENCE
    ):
        raise ValueError("Chatflow start Action identity is invalid")
    session = db.get(BusinessSession, action.business_session_id)
    recommendation = db.execute(
        select(Recommendation).where(
            Recommendation.business_session_id == action.business_session_id,
            Recommendation.source_kind == "dify_chatflow",
        )
    ).scalar_one_or_none()
    if (
        session is None
        or recommendation is None
        or session.status != "starting"
        or session.start_outcome != "pending"
        or session.dify_run_id is not None
        or recommendation.status != "analyzing"
        or recommendation.run_ref is not None
    ):
        raise ValueError("Chatflow start state is invalid")
    bound_digest = hashlib.sha256(session.request_digest + session.reference_asset_digest).digest()
    if action.input_digest != chatflow_action_digest(
        action_kind=CHATFLOW_START_ACTION,
        business_session_id=session.id,
        task_id=None,
        request_digest=bound_digest,
    ):
        raise ValueError("Chatflow start immutable input changed")
    reference_files = (
        db.execute(
            select(ChatflowDifyReferenceFile)
            .where(ChatflowDifyReferenceFile.business_session_id == session.id)
            .order_by(ChatflowDifyReferenceFile.position)
        )
        .scalars()
        .all()
    )
    expected = session.reference_assets_snapshot
    if (
        len(reference_files) != len(expected)
        or not reference_files
        or any(
            row.status != "uploaded"
            or not row.dify_file_id
            or row.business_session_id != session.id
            or row.owner_user_id != session.owner_user_id
            or row.reference_set_id != session.reference_set_id
            or row.dify_user_id != session.dify_user_id
            or row.position != position
            or row.reference_set_image_id != UUID(str(expected[position]["reference_set_image_id"]))
            or row.model_asset_id != UUID(str(expected[position]["model_asset_id"]))
            or row.slot != expected[position]["slot"]
            or row.content_sha256 != expected[position]["content_sha256"]
            for position, row in enumerate(reference_files)
        )
        or len({row.dify_app_id for row in reference_files}) != 1
    ):
        raise ValueError("Chatflow start is blocked until all bound reference files are uploaded")
    reference_set = db.get(ReferenceSet, session.reference_set_id)
    if reference_set is None:
        raise ValueError("Chatflow start reference set is missing")
    payload = {
        "business_session_id": str(session.id),
        "recommendation_id": str(recommendation.id),
        "recommendation_revision": recommendation.revision,
        "dify_user_id": session.dify_user_id,
        "reference_set_id": str(session.reference_set_id),
        "reference_asset_digest": session.reference_asset_digest.hex(),
        "reference_assets": session.reference_assets_snapshot,
        "reference_files": [
            {
                "dify_file_id": row.dify_file_id,
                "slot": row.slot,
                "position": row.position,
                "content_sha256": row.content_sha256,
            }
            for row in reference_files
        ],
        "category": reference_set.product_category,
    }
    return _action_input(action, payload)


def execute_chatflow_start(
    port: DifyChatflowPort, action_input: ActionInput
) -> ActionExecutionResult:
    try:
        result = port.start_recommendation(
            _start_command(action_input.payload, action_input.action_id)
        )
    except Exception:
        return ActionExecutionResult("unknown", safe_reason_code="CHATFLOW_START_OUTCOME_UNKNOWN")
    if not isinstance(result, ChatflowStartResult):
        return ActionExecutionResult("unknown", safe_reason_code="CHATFLOW_START_RESULT_INVALID")
    if result.outcome == "unknown":
        value = {"dify_run_id": result.dify_run_id} if result.dify_run_id else None
        return ActionExecutionResult(
            "unknown",
            safe_reason_code=_safe_reason(
                result.safe_reason_code, "CHATFLOW_START_OUTCOME_UNKNOWN"
            ),
            value=value,
        )
    if result.outcome == "definitely_rejected":
        return ActionExecutionResult(
            "definitely_rejected",
            safe_reason_code=_safe_reason(result.safe_reason_code, "CHATFLOW_START_REJECTED"),
        )
    if result.dify_run_id is None or _SAFE_RUN_ID.fullmatch(result.dify_run_id) is None:
        return ActionExecutionResult("unknown", safe_reason_code="CHATFLOW_START_RUN_ID_INVALID")
    return ActionExecutionResult(
        "accepted",
        safe_reason_code=(
            _safe_reason(result.safe_reason_code, "CHATFLOW_FIRST_FORM_PENDING")
            if result.safe_reason_code
            else None
        ),
        value={
            "dify_run_id": result.dify_run_id,
            "first_form": result.first_form,
        },
    )


def recover_chatflow_start(_action_input: ActionInput) -> ActionExecutionResult:
    # Dify start has no verified idempotency key or lookup by our session id.
    # A lease recovery must never submit a second Chatflow.
    return ActionExecutionResult("unknown", safe_reason_code="CHATFLOW_START_OUTCOME_UNKNOWN")


def _fail_v34_recommendation(recommendation: Recommendation, reason_code: str) -> None:
    recommendation.status = "failed"
    recommendation.failure_code = reason_code
    state = dict(recommendation.state) if isinstance(recommendation.state, dict) else {}
    state.update({"stage": "failed", "safe_reason_code": reason_code})
    recommendation.state = state


def finalize_chatflow_start(
    db: Session,
    action: Action,
    action_input: ActionInput,
    result: ActionExecutionResult,
) -> None:
    session, recommendation = _load_action_session_and_recommendation(db, action, action_input)
    reference_files = (
        db.execute(
            select(ChatflowDifyReferenceFile)
            .where(ChatflowDifyReferenceFile.business_session_id == session.id)
            .order_by(ChatflowDifyReferenceFile.position)
        )
        .scalars()
        .all()
    )
    if (
        not reference_files
        or len(reference_files) != len(session.reference_assets_snapshot)
        or any(row.status != "uploaded" or not row.dify_file_id for row in reference_files)
    ):
        # A stale pre-bridge start may have reached Dify while deployment was
        # being quiesced. Never report it accepted or continue that run.
        session.status = "reconciling"
        session.start_outcome = "unknown"
        session.updated_at = func.clock_timestamp()
        state = dict(recommendation.state) if isinstance(recommendation.state, dict) else {}
        state.update({"stage": "reference_file_bridge_required"})
        recommendation.state = state
        recommendation.updated_at = func.clock_timestamp()
        return
    if result.outcome == "accepted":
        value = result.value
        run_id = value.get("dify_run_id") if isinstance(value, Mapping) else None
        if not isinstance(run_id, str) or _SAFE_RUN_ID.fullmatch(run_id) is None:
            raise ValueError("Chatflow start result has no safe run id")
        if session.dify_run_id not in {None, run_id}:
            raise ValueError("Chatflow run identity changed")
        session.dify_run_id = run_id
        session.start_outcome = "accepted"
        session.status = "awaiting_recommendation"
        recommendation.dify_run_id = run_id
        if session.flow_version == "visual-v3.4-single-selection":
            state = db.execute(
                select(ChatflowV34SessionState)
                .where(ChatflowV34SessionState.business_session_id == session.id)
                .with_for_update()
            ).scalar_one_or_none()
            first_form = value.get("first_form") if isinstance(value, Mapping) else None
            safe_reason = result.safe_reason_code
            if state is None:
                raise ValueError("v3.4 session state is missing")
            if first_form is None:
                if safe_reason == "V34_FIRST_FORM_IDENTITY_MISMATCH":
                    state.form_status = "rejected"
                    state.safe_reason_code = safe_reason
                    session.status = "failed"
                    _fail_v34_recommendation(recommendation, safe_reason)
                else:
                    state.form_status = "unknown"
                    state.safe_reason_code = safe_reason or "DIFY_FIRST_FORM_NOT_VERIFIED"
                    session.status = "reconciling"
            else:
                from app.integrations.dify.chatflow import ChatflowFirstFormBinding

                if not isinstance(first_form, ChatflowFirstFormBinding):
                    raise ValueError("v3.4 first form binding is invalid")
                if (
                    first_form.form_node_id != "form_0"
                    or not first_form.form_token
                    or not first_form.form_id
                ):
                    state.form_status = "rejected"
                    state.safe_reason_code = "V34_FIRST_FORM_IDENTITY_MISMATCH"
                    session.status = "failed"
                    _fail_v34_recommendation(recommendation, state.safe_reason_code)
                else:
                    import hashlib
                    from uuid import UUID

                    from app.services.chatflow_v34_events import (
                        process_pending_v34_stage_events,
                    )

                    try:
                        UUID(first_form.form_id)
                    except ValueError:
                        state.form_status = "rejected"
                        state.safe_reason_code = "V34_FIRST_FORM_IDENTITY_MISMATCH"
                        session.status = "failed"
                        _fail_v34_recommendation(recommendation, state.safe_reason_code)
                        form_valid = False
                    else:
                        form_valid = True
                    if not form_valid:
                        pass
                    elif state.form_token is not None and (
                        state.form_token != first_form.form_token
                        or state.form_node_id != first_form.form_node_id
                    ):
                        raise ValueError("v3.4 first form identity changed")
                    else:
                        state.form_node_id = first_form.form_node_id
                        state.form_token = first_form.form_token
                        state.form_token_digest = hashlib.sha256(
                            first_form.form_token.encode("utf-8")
                        ).digest()
                        state.form_definition_digest = None
                        state.form_expires_at = None
                        state.form_status = "binding"
                        state.safe_reason_code = None
                        reference_set = db.get(ReferenceSet, session.reference_set_id)
                        if reference_set is None:
                            raise ValueError("v3.4 reference set is missing")
                        process_pending_v34_stage_events(
                            db,
                            session=session,
                            reference_set=reference_set,
                            recommendation=recommendation,
                            state=state,
                        )
                        if session.status == "failed" or recommendation.status == "failed":
                            state.form_node_id = None
                            state.form_token = None
                            state.form_token_digest = None
                            state.form_status = "rejected"
                            state.safe_reason_code = (
                                state.safe_reason_code or "V34_RECOMMENDATION_INVALID"
                            )
                        else:
                            now = db.scalar(select(func.clock_timestamp()))
                            if now is None:
                                raise ValueError("database time is unavailable")
                            schedule_chatflow_form_binding_in_transaction(
                                db,
                                session=session,
                                recommendation=recommendation,
                                state=state,
                                due_at=now,
                            )
            if state.form_status == "rejected" and recommendation.status == "failed":
                from app.services.chatflow_v34_events import process_pending_v34_stage_events

                reference_set = db.get(ReferenceSet, session.reference_set_id)
                if reference_set is None:
                    raise ValueError("v3.4 reference set is missing")
                process_pending_v34_stage_events(
                    db,
                    session=session,
                    reference_set=reference_set,
                    recommendation=recommendation,
                    state=state,
                )
    elif result.outcome == "unknown":
        value = result.value
        run_id = value.get("dify_run_id") if isinstance(value, Mapping) else None
        if isinstance(run_id, str) and _SAFE_RUN_ID.fullmatch(run_id):
            if session.dify_run_id not in {None, run_id}:
                raise ValueError("Chatflow run identity changed")
            session.dify_run_id = run_id
            recommendation.dify_run_id = run_id
        session.start_outcome = "unknown"
        session.status = "reconciling"
        if session.flow_version == "visual-v3.4-single-selection":
            state = db.execute(
                select(ChatflowV34SessionState)
                .where(ChatflowV34SessionState.business_session_id == session.id)
                .with_for_update()
            ).scalar_one_or_none()
            if state is None:
                raise ValueError("v3.4 session state is missing")
            state.form_status = "unknown"
            state.safe_reason_code = result.safe_reason_code or "DIFY_START_OUTCOME_UNKNOWN"
            if session.dify_run_id is not None:
                from app.services.chatflow_v34_events import process_pending_v34_stage_events

                reference_set = db.get(ReferenceSet, session.reference_set_id)
                if reference_set is None:
                    raise ValueError("v3.4 reference set is missing")
                process_pending_v34_stage_events(
                    db,
                    session=session,
                    reference_set=reference_set,
                    recommendation=recommendation,
                    state=state,
                )
    elif result.outcome == "definitely_rejected":
        session.start_outcome = "rejected"
        session.status = "failed"
        recommendation.status = "failed"
        recommendation.failure_code = result.safe_reason_code or "CHATFLOW_START_REJECTED"
        state = dict(recommendation.state) if isinstance(recommendation.state, dict) else {}
        state.update({"stage": "failed", "failure_code": recommendation.failure_code})
        recommendation.state = state
    session.updated_at = func.clock_timestamp()
    recommendation.updated_at = func.clock_timestamp()


def prepare_chatflow_form_binding(db: Session, action: Action) -> ActionInput:
    if (
        action.action_kind != CHATFLOW_FORM_BIND_ACTION
        or action.scope_kind != "business_session"
        or action.task_id is not None
        or action.business_session_id != action.scope_id
        or action.sequence != CHATFLOW_FORM_BIND_SEQUENCE
    ):
        raise ValueError("v3.4 form binding Action identity is invalid")
    session = db.get(BusinessSession, action.business_session_id)
    recommendation = db.execute(
        select(Recommendation).where(
            Recommendation.business_session_id == action.business_session_id,
            Recommendation.source_kind == "dify_chatflow",
        )
    ).scalar_one_or_none()
    state = db.execute(
        select(ChatflowV34SessionState)
        .where(ChatflowV34SessionState.business_session_id == action.business_session_id)
        .with_for_update()
    ).scalar_one_or_none()
    if (
        session is None
        or recommendation is None
        or state is None
        or session.flow_version != "visual-v3.4-single-selection"
        or session.start_outcome != "accepted"
        or session.dify_run_id is None
        or recommendation.dify_run_id != session.dify_run_id
        or state.flow_version != session.flow_version
        or state.form_status != "binding"
        or state.form_node_id != "form_0"
        or state.form_token is None
        or state.form_token_digest is None
        or recommendation.status not in {"analyzing", "ready"}
        or session.status not in {"awaiting_recommendation", "recommendation_ready"}
        or action.input_digest
        != chatflow_form_binding_action_digest(
            session=session,
            recommendation=recommendation,
            state=state,
        )
    ):
        raise ValueError("v3.4 form binding state is invalid")
    payload = {
        "business_session_id": str(session.id),
        "recommendation_id": str(recommendation.id),
        "recommendation_revision": recommendation.revision,
        "dify_run_id": session.dify_run_id,
        "dify_user_id": session.dify_user_id,
        "form_node_id": state.form_node_id,
        "form_token": state.form_token,
    }
    return _action_input(action, payload)


def execute_chatflow_form_binding(
    port: DifyChatflowPort,
    action_input: ActionInput,
) -> ActionExecutionResult:
    try:
        result = port.get_first_form_definition(
            _first_form_definition_command(action_input.payload)
        )
    except Exception:
        return ActionExecutionResult("unknown", safe_reason_code="DIFY_FORM_DEFINITION_UNKNOWN")
    if not isinstance(result, ChatflowFirstFormDefinitionResult):
        return ActionExecutionResult(
            "unknown", safe_reason_code="DIFY_FORM_DEFINITION_RESULT_INVALID"
        )
    if result.outcome == "unknown":
        return ActionExecutionResult(
            "unknown",
            safe_reason_code=_safe_reason(result.safe_reason_code, "DIFY_FORM_DEFINITION_UNKNOWN"),
        )
    return ActionExecutionResult("accepted", value=result)


def recover_chatflow_form_binding(
    port: DifyChatflowPort,
    action_input: ActionInput,
) -> ActionExecutionResult:
    # This is a GET by the already-persisted form token; repeating it cannot create a run.
    return execute_chatflow_form_binding(port, action_input)


def finalize_chatflow_form_binding(
    db: Session,
    action: Action,
    action_input: ActionInput,
    result: ActionExecutionResult,
) -> None:
    session, recommendation = _load_action_session_and_recommendation(db, action, action_input)
    state = db.execute(
        select(ChatflowV34SessionState)
        .where(ChatflowV34SessionState.business_session_id == session.id)
        .with_for_update()
    ).scalar_one_or_none()
    if (
        state is None
        or state.form_status != "binding"
        or state.form_node_id != "form_0"
        or state.form_token is None
        or state.form_token_digest is None
        or action.input_digest
        != chatflow_form_binding_action_digest(
            session=session,
            recommendation=recommendation,
            state=state,
        )
    ):
        raise ValueError("v3.4 persisted form binding changed")

    if result.outcome == "accepted":
        definition = result.value
        if not isinstance(definition, ChatflowFirstFormDefinitionResult):
            raise ValueError("v3.4 form definition result is invalid")
        if (
            definition.outcome != "available"
            or isinstance(definition.expiration_time, bool)
            or not isinstance(definition.expiration_time, int)
        ):
            raise ValueError("v3.4 form definition result is incomplete")
        from datetime import UTC, datetime

        from app.integrations.dify.chatflow_v34_http import DifyV34FormInput
        from app.services.chatflow_v34_events import (
            process_pending_v34_stage_events,
            v34_form_definition_digest,
            validate_v34_form_definition,
        )

        form_inputs = tuple(
            DifyV34FormInput(
                output_variable_name=item.output_variable_name,
                input_type=item.input_type,
                option_values=item.option_values,
            )
            for item in definition.fields
        )
        state.form_definition_digest = v34_form_definition_digest(
            form_inputs,
            definition.action_ids,
            definition.expiration_time,
        )
        state.form_expires_at = datetime.fromtimestamp(definition.expiration_time, tz=UTC)
        state.form_status = "awaiting_event"
        state.safe_reason_code = "V34_RECOMMENDATION_PENDING"
        reference_set = db.get(ReferenceSet, session.reference_set_id)
        if reference_set is None:
            raise ValueError("v3.4 form binding reference set is missing")
        process_pending_v34_stage_events(
            db,
            session=session,
            reference_set=reference_set,
            recommendation=recommendation,
            state=state,
        )
        now = db.scalar(select(func.clock_timestamp()))
        if now is None:
            raise ValueError("database time is unavailable")
        expected_recommendation = recommendation if recommendation.status == "ready" else None
        if not validate_v34_form_definition(
            form_inputs,
            definition.action_ids,
            recommendation=expected_recommendation,
        ):
            state.form_status = "rejected"
            state.safe_reason_code = "V34_FORM_DEFINITION_MISMATCH"
            session.status = "failed"
        elif recommendation.status == "failed" or session.status == "failed":
            state.form_status = "rejected"
            state.safe_reason_code = state.safe_reason_code or "V34_RECOMMENDATION_INVALID"
            session.status = "failed"
        elif state.form_expires_at <= now:
            state.form_status = "expired"
            state.safe_reason_code = "V34_FORM_EXPIRED"
            session.status = "expired"
        elif recommendation.status == "ready":
            state.form_status = "ready"
            state.safe_reason_code = None
            session.status = "recommendation_ready"
        else:
            state.form_status = "awaiting_event"
            state.safe_reason_code = "V34_RECOMMENDATION_PENDING"
            session.status = "awaiting_recommendation"
    elif result.outcome == "unknown":
        state.form_status = "unknown"
        state.safe_reason_code = result.safe_reason_code or "DIFY_FORM_DEFINITION_UNKNOWN"
        session.status = "reconciling"
    else:
        state.form_status = "rejected"
        state.safe_reason_code = result.safe_reason_code or "DIFY_FORM_DEFINITION_REJECTED"
        state.form_node_id = None
        state.form_token = None
        state.form_token_digest = None
        session.status = "failed"
        recommendation.status = "failed"
        recommendation.failure_code = state.safe_reason_code
        recommendation.state = {
            "stage": "failed",
            "safe_reason_code": state.safe_reason_code,
        }
        recommendation.updated_at = func.clock_timestamp()
    session.updated_at = func.clock_timestamp()
    state.updated_at = func.clock_timestamp()


def prepare_chatflow_continuation(db: Session, action: Action) -> ActionInput:
    if (
        action.action_kind != CHATFLOW_CONTINUE_ACTION
        or action.scope_kind != "business_session"
        or action.task_id is None
        or action.business_session_id != action.scope_id
        or action.sequence != CHATFLOW_CONTINUE_SEQUENCE
    ):
        raise ValueError("Chatflow continuation Action identity is invalid")
    session = db.get(BusinessSession, action.business_session_id)
    recommendation = db.execute(
        select(Recommendation).where(
            Recommendation.business_session_id == action.business_session_id,
            Recommendation.source_kind == "dify_chatflow",
        )
    ).scalar_one_or_none()
    task = db.execute(
        select(Task).where(
            Task.id == action.task_id,
            Task.business_session_id == action.business_session_id,
            Task.provenance_kind == "dify_chatflow",
        )
    ).scalar_one_or_none()
    if (
        session is None
        or recommendation is None
        or task is None
        or session.dify_run_id is None
        or recommendation.status != "ready"
        or recommendation.dify_run_id != session.dify_run_id
        or recommendation.revision != task.recommendation_revision
        or task.request_snapshot.get("business_session_id") != str(session.id)
        or task.request_snapshot.get("reference_asset_digest")
        != session.reference_asset_digest.hex()
        or not isinstance(task.request_snapshot.get("selection"), dict)
    ):
        raise ValueError("Chatflow continuation state is invalid")
    validate_dify_chatflow_task_snapshot(task, session)
    v34_form_inputs = None
    v34_submission_id = None
    v34_form_token = None
    if session.flow_version == "visual-v3.4-single-selection":
        state = db.execute(
            select(ChatflowV34SessionState)
            .where(ChatflowV34SessionState.business_session_id == session.id)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            state is None
            or state.selection_status not in {"pending", "unknown", "accepted"}
            or state.selection_submission_id != action.id
        ):
            raise ValueError("v3.4 continuation selection state is invalid")
        from app.services.chatflow_v34_conversion import (
            form_0_definition_for_recommendation,
            order_form_0_submission,
            validate_form_0_submission,
        )

        original = DifyOriginalRecommendation.model_validate(
            recommendation.original_recommendation, strict=True
        )
        v34_form_inputs = order_form_0_submission(state.selection_inputs or {})
        validate_form_0_submission(
            v34_form_inputs,
            form_0_definition_for_recommendation(original),
        )
        if action.input_digest != chatflow_v34_selection_action_digest(
            session=session,
            recommendation=recommendation,
            state=state,
            task=task,
        ):
            raise ValueError("v3.4 continuation immutable input changed")
        v34_submission_id = state.selection_submission_id
        v34_form_token = SecretStr(state.form_token or "")
    elif action.input_digest != chatflow_action_digest(
        action_kind=CHATFLOW_CONTINUE_ACTION,
        business_session_id=session.id,
        task_id=task.id,
        request_digest=task.request_digest,
    ):
        raise ValueError("Chatflow continuation immutable input changed")
    payload = {
        "business_session_id": str(session.id),
        "recommendation_id": str(recommendation.id),
        "recommendation_revision": recommendation.revision,
        "dify_run_id": session.dify_run_id,
        "dify_user_id": session.dify_user_id,
        "task_id": str(task.id),
        "selection_snapshot": task.request_snapshot,
    }
    if session.flow_version == "visual-v3.4-single-selection":
        payload.update(
            {
                "selection_submission_id": str(v34_submission_id),
                "form_node_id": "form_0",
                "form_token": v34_form_token,
                "form_action": "submit",
                "form_inputs": v34_form_inputs,
            }
        )
    return _action_input(action, payload)


def execute_chatflow_continuation(
    port: DifyChatflowPort, action_input: ActionInput
) -> ActionExecutionResult:
    try:
        result = port.continue_after_selection(_continuation_command(action_input.payload))
    except Exception:
        return ActionExecutionResult("unknown", safe_reason_code="CHATFLOW_RESUME_OUTCOME_UNKNOWN")
    return _continuation_result(result)


def recover_chatflow_continuation(
    port: DifyChatflowPort, action_input: ActionInput
) -> ActionExecutionResult:
    try:
        result = port.recover_after_selection(_continuation_command(action_input.payload))
    except Exception:
        return ActionExecutionResult("unknown", safe_reason_code="CHATFLOW_RESUME_RECOVERY_UNKNOWN")
    return _continuation_result(result)


def finalize_chatflow_continuation(
    db: Session,
    action: Action,
    action_input: ActionInput,
    result: ActionExecutionResult,
) -> None:
    session, recommendation = _load_action_session_and_recommendation(db, action, action_input)
    if action.task_id is None:
        raise ValueError("Chatflow continuation has no Task")
    task = db.execute(
        select(Task).where(
            Task.id == action.task_id,
            Task.business_session_id == session.id,
            Task.provenance_kind == "dify_chatflow",
        )
    ).scalar_one_or_none()
    if task is None:
        raise ValueError("Chatflow continuation Task is missing")
    validate_dify_chatflow_task_snapshot(task, session)
    if session.flow_version == "visual-v3.4-single-selection":
        state = db.execute(
            select(ChatflowV34SessionState)
            .where(ChatflowV34SessionState.business_session_id == session.id)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            state is None
            or state.selection_submission_id != action.id
            or action.input_digest
            != chatflow_v34_selection_action_digest(
                session=session,
                recommendation=recommendation,
                state=state,
                task=task,
            )
        ):
            raise ValueError("v3.4 selection finalization binding changed")
        if result.outcome == "accepted":
            # A late submit response must not reopen a terminal pre-generation
            # failure written by a stage callback in the same Dify run.
            if task.status == "failed" and session.status in {"failed", "expired", "cancelled"}:
                return
            if state.selection_status != "accepted":
                now = db.scalar(select(func.clock_timestamp()))
                if now is None:
                    raise ValueError("database time is unavailable")
                state.selection_status = "accepted"
                state.selection_evidence_kind = "http_200"
                state.selection_accepted_at = now
            state.safe_reason_code = None
            session.status = "task_created"
        elif result.outcome == "unknown":
            if state.selection_status != "accepted":
                state.selection_status = "unknown"
                state.safe_reason_code = result.safe_reason_code or "DIFY_SELECTION_OUTCOME_UNKNOWN"
                session.status = "reconciling"
        elif state.selection_status != "accepted":
            state.selection_status = "rejected"
            state.safe_reason_code = result.safe_reason_code or "CHATFLOW_CONTINUATION_REJECTED"
            session.status = "failed"
            task.status = "failed"
            task.terminal_at = func.clock_timestamp()
            task.updated_at = func.clock_timestamp()
            items = db.execute(
                select(TaskItem)
                .where(TaskItem.task_id == task.id)
                .order_by(TaskItem.position, TaskItem.id)
                .with_for_update()
            ).scalars()
            for item in items:
                if item.status not in {"succeeded", "failed"}:
                    item.status = "failed"
                    item.error_code = "CHATFLOW_CONTINUATION_REJECTED"
                    item.updated_at = func.clock_timestamp()
        if state.selection_status == "accepted" and session.status not in {
            "failed",
            "expired",
            "cancelled",
        }:
            from app.services.chatflow_v34_events import _v34_generation_tracking_exists

            if not _v34_generation_tracking_exists(db, session.id):
                now = db.scalar(select(func.clock_timestamp()))
                if now is None:
                    raise ValueError("database time is unavailable")
                schedule_chatflow_v34_run_observation_in_transaction(
                    db,
                    session=session,
                    recommendation=recommendation,
                    state=state,
                    task=task,
                    sequence=CHATFLOW_RUN_OBSERVATION_SEQUENCE_BASE,
                    due_at=now,
                )
        session.updated_at = func.clock_timestamp()
        state.updated_at = func.clock_timestamp()
        return
    if result.outcome == "accepted":
        # H3 will advance the same session when a supported Human Input question arrives.
        session.status = "task_created"
    elif result.outcome == "definitely_rejected":
        session.status = "failed"
        task.status = "failed"
        task.terminal_at = func.clock_timestamp()
        task.updated_at = func.clock_timestamp()
        items = db.execute(
            select(TaskItem).where(TaskItem.task_id == task.id).with_for_update()
        ).scalars()
        for item in items:
            if item.status not in {"succeeded", "failed"}:
                item.status = "failed"
                item.error_code = "CHATFLOW_CONTINUATION_REJECTED"
                item.updated_at = func.clock_timestamp()
    session.updated_at = func.clock_timestamp()


def prepare_chatflow_v34_run_observation(db: Session, action: Action) -> ActionInput:
    if (
        action.action_kind != CHATFLOW_RUN_OBSERVATION_ACTION
        or action.scope_kind != "business_session"
        or action.task_id is None
        or action.business_session_id != action.scope_id
        or not (
            CHATFLOW_RUN_OBSERVATION_SEQUENCE_BASE
            <= action.sequence
            <= CHATFLOW_RUN_OBSERVATION_MAX_SEQUENCE
        )
    ):
        raise ValueError("v3.4 run observation Action identity is invalid")
    base_input = _action_input(action, {})
    session, recommendation = _load_action_session_and_recommendation(db, action, base_input)
    task = db.execute(
        select(Task)
        .where(
            Task.id == action.task_id,
            Task.business_session_id == session.id,
            Task.provenance_kind == "dify_chatflow",
        )
        .with_for_update()
    ).scalar_one_or_none()
    state = db.execute(
        select(ChatflowV34SessionState)
        .where(ChatflowV34SessionState.business_session_id == session.id)
        .with_for_update()
    ).scalar_one_or_none()
    if task is None or state is None or session.dify_run_id is None:
        raise ValueError("v3.4 run observation parents are missing")
    expected_digest = chatflow_v34_run_observation_action_digest(
        session=session,
        recommendation=recommendation,
        state=state,
        task=task,
        sequence=action.sequence,
    )
    selection_action = (
        db.execute(
            select(Action).where(Action.id == state.selection_submission_id).with_for_update()
        ).scalar_one_or_none()
        if state.selection_submission_id is not None
        else None
    )
    if (
        action.input_digest_version != 1
        or action.input_digest != expected_digest
        or state.selection_status != "accepted"
        or selection_action is None
        or selection_action.task_id != task.id
        or selection_action.business_session_id != session.id
        or selection_action.action_kind != CHATFLOW_CONTINUE_ACTION
        or selection_action.sequence != CHATFLOW_CONTINUE_SEQUENCE
        or selection_action.status not in {"claimed", "unknown", "completed"}
        or selection_action.input_digest
        != chatflow_v34_selection_action_digest(
            session=session,
            recommendation=recommendation,
            state=state,
            task=task,
        )
    ):
        raise ValueError("v3.4 run observation immutable selection binding changed")
    from app.services.chatflow_v34_events import _v34_generation_tracking_exists

    skip_observation = (
        _v34_generation_tracking_exists(db, session.id)
        or session.status in {"failed", "expired", "cancelled"}
        or task.status in {"succeeded", "failed", "cancelled", "expired"}
    )
    payload = {
        "business_session_id": str(session.id),
        "recommendation_id": str(recommendation.id),
        "recommendation_revision": recommendation.revision,
        "dify_run_id": session.dify_run_id,
        "dify_user_id": session.dify_user_id,
        "task_id": str(task.id),
        "selection_submission_id": str(state.selection_submission_id),
        "skip_observation": skip_observation,
    }
    return _action_input(action, payload)


def _v34_observed_workflow_terminal(
    result: ChatflowOriginalRunObservationResult, *, expected_run_id: str
) -> tuple[str, str | None] | None:
    if (
        not isinstance(result, ChatflowOriginalRunObservationResult)
        or result.outcome != "received"
        or not result.events
        or not expected_run_id
        or any(event.dify_run_id != expected_run_id for event in result.events)
    ):
        return None
    last = result.events[-1]
    if last.event == "workflow_finished" and last.workflow_status in {
        "failed",
        "partial-succeeded",
        "succeeded",
    }:
        return last.event, last.workflow_status
    if last.event == "workflow_failed" and last.workflow_status in {None, "failed"}:
        return last.event, "failed"
    return None


def execute_chatflow_v34_run_observation(
    port: DifyChatflowPort, action_input: ActionInput
) -> ActionExecutionResult:
    payload = action_input.payload
    if not isinstance(payload, Mapping):
        return ActionExecutionResult(
            "definitely_rejected", safe_reason_code="V34_RUN_OBSERVATION_INPUT_INVALID"
        )
    if payload.get("skip_observation") is True:
        return ActionExecutionResult("accepted", value=None)
    run_id = payload.get("dify_run_id")
    user_id = payload.get("dify_user_id")
    if not isinstance(run_id, str) or not isinstance(user_id, str):
        return ActionExecutionResult(
            "definitely_rejected", safe_reason_code="V34_RUN_OBSERVATION_INPUT_INVALID"
        )
    try:
        observation = port.observe_original_run(run_id, user_id)
    except Exception:
        observation = None
    terminal = (
        _v34_observed_workflow_terminal(observation, expected_run_id=run_id)
        if isinstance(observation, ChatflowOriginalRunObservationResult)
        else None
    )
    if terminal is not None:
        return ActionExecutionResult("accepted", value=observation)
    if action_input.sequence < CHATFLOW_RUN_OBSERVATION_MAX_SEQUENCE:
        next_sequence = action_input.sequence + 1
        retry_after = _CHATFLOW_RUN_OBSERVATION_DELAYS[
            next_sequence - CHATFLOW_RUN_OBSERVATION_SEQUENCE_BASE
        ]
        reason_code = (
            observation.safe_reason_code
            if isinstance(observation, ChatflowOriginalRunObservationResult)
            else "V34_RUN_TERMINAL_NOT_OBSERVED"
        )
        return ActionExecutionResult(
            "safe_retry",
            safe_reason_code=_safe_reason(reason_code, "V34_RUN_TERMINAL_NOT_OBSERVED"),
            value=observation,
            retry_after_seconds=retry_after,
        )
    reason_code = (
        observation.safe_reason_code
        if isinstance(observation, ChatflowOriginalRunObservationResult)
        else "V34_RUN_TERMINAL_NOT_OBSERVED"
    )
    return ActionExecutionResult(
        "accepted",
        safe_reason_code=_safe_reason(reason_code, "V34_RUN_TERMINAL_NOT_OBSERVED"),
        value=observation,
    )


def finalize_chatflow_v34_run_observation(
    db: Session,
    action: Action,
    action_input: ActionInput,
    result: ActionExecutionResult,
) -> None:
    session, recommendation = _load_action_session_and_recommendation(db, action, action_input)
    if action.task_id is None:
        raise ValueError("v3.4 run observation has no Task")
    task = db.execute(
        select(Task)
        .where(
            Task.id == action.task_id,
            Task.business_session_id == session.id,
            Task.provenance_kind == "dify_chatflow",
        )
        .with_for_update()
    ).scalar_one_or_none()
    state = db.execute(
        select(ChatflowV34SessionState)
        .where(ChatflowV34SessionState.business_session_id == session.id)
        .with_for_update()
    ).scalar_one_or_none()
    if task is None or state is None:
        raise ValueError("v3.4 run observation parents are missing")
    if (
        action.action_kind != CHATFLOW_RUN_OBSERVATION_ACTION
        or not (
            CHATFLOW_RUN_OBSERVATION_SEQUENCE_BASE
            <= action.sequence
            <= CHATFLOW_RUN_OBSERVATION_MAX_SEQUENCE
        )
        or action.input_digest
        != chatflow_v34_run_observation_action_digest(
            session=session,
            recommendation=recommendation,
            state=state,
            task=task,
            sequence=action.sequence,
        )
    ):
        raise ValueError("v3.4 run observation finalization binding changed")
    payload = action_input.payload
    if not isinstance(payload, Mapping) or payload.get("skip_observation") is True:
        return
    terminal = (
        _v34_observed_workflow_terminal(result.value, expected_run_id=session.dify_run_id or "")
        if isinstance(result.value, ChatflowOriginalRunObservationResult)
        else None
    )
    if terminal is not None:
        terminal_event, workflow_status = terminal
        if workflow_status == "failed":
            from app.services.chatflow_v34_events import record_v34_observed_workflow_failure

            now = db.scalar(select(func.clock_timestamp()))
            if now is None:
                raise ValueError("database time is unavailable")
            record_v34_observed_workflow_failure(
                db,
                task=task,
                session=session,
                recommendation=recommendation,
                state=state,
                dify_run_id=session.dify_run_id or "",
                terminal_event=terminal_event,
                workflow_status=workflow_status,
                now=now,
            )
        elif workflow_status in {"partial-succeeded", "succeeded"}:
            from app.services.chatflow_v34_events import (
                record_v34_observed_workflow_end_without_generation,
            )

            now = db.scalar(select(func.clock_timestamp()))
            if now is None:
                raise ValueError("database time is unavailable")
            record_v34_observed_workflow_end_without_generation(
                db,
                task=task,
                session=session,
                recommendation=recommendation,
                state=state,
                dify_run_id=session.dify_run_id or "",
                terminal_event=terminal_event,
                workflow_status=workflow_status,
                now=now,
            )
        return
    if result.outcome == "safe_retry":
        if action.sequence >= CHATFLOW_RUN_OBSERVATION_MAX_SEQUENCE:
            raise ValueError("v3.4 run observation retry exceeded its bounded window")
        next_sequence = action.sequence + 1
        now = db.scalar(select(func.clock_timestamp()))
        if now is None:
            raise ValueError("database time is unavailable")
        delay = _CHATFLOW_RUN_OBSERVATION_DELAYS[
            next_sequence - CHATFLOW_RUN_OBSERVATION_SEQUENCE_BASE
        ]
        schedule_chatflow_v34_run_observation_in_transaction(
            db,
            session=session,
            recommendation=recommendation,
            state=state,
            task=task,
            sequence=next_sequence,
            due_at=now + timedelta(seconds=delay),
        )
        if session.status not in {"failed", "expired", "cancelled"} and task.status not in {
            "succeeded",
            "failed",
            "cancelled",
            "expired",
        }:
            session.status = "reconciling"
            if task.status in {"queued", "prompting", "needs_input"}:
                task.status = "reconciling"
            state.safe_reason_code = "V34_RUN_TERMINAL_NOT_OBSERVED"
            state.updated_at = now
            session.updated_at = now
            task.updated_at = now
        return
    if action.sequence == CHATFLOW_RUN_OBSERVATION_MAX_SEQUENCE:
        from app.services.chatflow_v34_events import _v34_generation_tracking_exists

        if (
            not _v34_generation_tracking_exists(db, session.id)
            and session.status not in {"failed", "expired", "cancelled"}
            and task.status not in {"succeeded", "failed", "cancelled", "expired"}
        ):
            now = db.scalar(select(func.clock_timestamp()))
            if now is None:
                raise ValueError("database time is unavailable")
            session.status = "reconciling"
            if task.status in {"queued", "prompting", "needs_input"}:
                task.status = "reconciling"
            state.safe_reason_code = "V34_RUN_TERMINAL_NOT_OBSERVED"
            state.updated_at = now
            session.updated_at = now
            task.updated_at = now


def chatflow_answer_action_id(*, answer_submission_id: UUID, sequence: int, recovery: bool) -> UUID:
    kind = CHATFLOW_ANSWER_RECOVERY_ACTION if recovery else CHATFLOW_ANSWER_ACTION
    return uuid5(CHATFLOW_ACTION_NAMESPACE, f"{kind}:{answer_submission_id}:{sequence}")


def chatflow_answer_action_digest(
    *,
    action_kind: str,
    question: BusinessQuestion,
    answer: BusinessAnswerSubmission,
) -> bytes:
    immutable_digest = hashlib.sha256(
        question.id.bytes
        + answer.id.bytes
        + (question.form_token_digest or b"")
        + answer.request_digest
    ).digest()
    return chatflow_action_digest(
        action_kind=action_kind,
        business_session_id=question.business_session_id,
        task_id=question.task_id,
        request_digest=immutable_digest,
    )


def prepare_chatflow_answer(db: Session, action: Action) -> ActionInput:
    if (
        action.action_kind not in {CHATFLOW_ANSWER_ACTION, CHATFLOW_ANSWER_RECOVERY_ACTION}
        or action.scope_kind != "business_session"
        or action.task_id is None
        or action.business_session_id != action.scope_id
        or action.business_question_id is None
        or action.business_answer_submission_id is None
        or action.sequence < CHATFLOW_ANSWER_FIRST_SEQUENCE
    ):
        raise ValueError("Chatflow answer Action identity is invalid")
    session, recommendation = _load_action_session_and_recommendation(
        db, action, _action_input(action, {})
    )
    question = db.get(BusinessQuestion, action.business_question_id)
    answer = db.get(BusinessAnswerSubmission, action.business_answer_submission_id)
    task = db.execute(
        select(Task).where(
            Task.id == action.task_id,
            Task.business_session_id == session.id,
            Task.provenance_kind == "dify_chatflow",
        )
    ).scalar_one_or_none()
    if (
        question is None
        or answer is None
        or task is None
        or question.business_session_id != session.id
        or question.task_id != task.id
        or question.recommendation_id != recommendation.id
        or question.recommendation_revision != recommendation.revision
        or question.dify_run_id != session.dify_run_id
        or recommendation.dify_run_id != session.dify_run_id
        or question.form_token is None
        or question.form_token_digest is None
        or hashlib.sha256(question.form_token.encode("utf-8")).digest()
        != question.form_token_digest
        or question.status != "confirming"
        or answer.status != "confirming"
        or answer.business_question_id != question.id
        or answer.business_session_id != session.id
        or answer.task_id != task.id
        or answer.request_digest is None
        or session.status not in {"task_created", "reconciling"}
        or task.status not in {"needs_input", "reconciling"}
    ):
        raise ValueError("Chatflow answer parents are invalid")
    validate_dify_chatflow_task_snapshot(task, session)
    if action.input_digest != chatflow_answer_action_digest(
        action_kind=action.action_kind,
        question=question,
        answer=answer,
    ):
        raise ValueError("Chatflow answer immutable input changed")
    payload = {
        "business_session_id": str(session.id),
        "recommendation_id": str(recommendation.id),
        "recommendation_revision": recommendation.revision,
        "dify_run_id": session.dify_run_id,
        "dify_user_id": session.dify_user_id,
        "task_id": str(task.id),
        "question_id": str(question.id),
        "answer_submission_id": str(answer.id),
        "round_number": question.round_number,
        "form_token": question.form_token,
        "answers": answer.answers,
    }
    return _action_input(action, payload)


def execute_chatflow_answer(
    port: DifyChatflowPort, action_input: ActionInput
) -> ActionExecutionResult:
    try:
        result = port.submit_human_input(_human_input_command(action_input.payload))
    except Exception:
        return ActionExecutionResult("unknown", safe_reason_code="CHATFLOW_ANSWER_OUTCOME_UNKNOWN")
    return _human_input_result(result)


def recover_chatflow_answer(
    port: DifyChatflowPort, action_input: ActionInput
) -> ActionExecutionResult:
    try:
        result = port.recover_human_input(_human_input_command(action_input.payload))
    except Exception:
        return ActionExecutionResult("unknown", safe_reason_code="CHATFLOW_ANSWER_RECOVERY_UNKNOWN")
    return _human_input_result(result)


def finalize_chatflow_answer(
    db: Session,
    action: Action,
    action_input: ActionInput,
    result: ActionExecutionResult,
) -> None:
    session, recommendation = _load_action_session_and_recommendation(db, action, action_input)
    question = db.execute(
        select(BusinessQuestion)
        .where(
            BusinessQuestion.id == action.business_question_id,
            BusinessQuestion.business_session_id == session.id,
            BusinessQuestion.task_id == action.task_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    answer = db.execute(
        select(BusinessAnswerSubmission)
        .where(
            BusinessAnswerSubmission.id == action.business_answer_submission_id,
            BusinessAnswerSubmission.business_question_id == action.business_question_id,
            BusinessAnswerSubmission.business_session_id == session.id,
            BusinessAnswerSubmission.task_id == action.task_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    task = db.execute(
        select(Task)
        .where(
            Task.id == action.task_id,
            Task.business_session_id == session.id,
            Task.recommendation_id == recommendation.id,
            Task.provenance_kind == "dify_chatflow",
        )
        .with_for_update()
    ).scalar_one_or_none()
    if question is None or answer is None or task is None:
        raise ValueError("Chatflow answer parents are missing")
    if (
        action_input.business_question_id != question.id
        or action_input.business_answer_submission_id != answer.id
        or action.task_id != task.id
        or question.dify_run_id != session.dify_run_id
        or question.recommendation_revision != recommendation.revision
        or answer.status != "confirming"
    ):
        raise ValueError("Chatflow answer identity changed")
    if result.outcome == "accepted":
        answer.status = "accepted"
        answer.external_reason_code = None
        question.status = "answered"
        task.status = "prompting"
        session.status = "task_created"
    elif result.outcome == "definitely_rejected":
        answer.status = "rejected"
        answer.external_reason_code = result.safe_reason_code or "CHATFLOW_ANSWER_REJECTED"
        question.status = "rejected"
        task.status = "failed"
        task.terminal_at = func.clock_timestamp()
        task.expires_at = func.clock_timestamp() + timedelta(days=task.retention_days)
        items = db.execute(
            select(TaskItem)
            .where(TaskItem.task_id == task.id)
            .order_by(TaskItem.position, TaskItem.id)
        ).scalars()
        for item in items:
            if item.status not in {"succeeded", "failed"}:
                item.status = "failed"
                item.error_code = "CHATFLOW_ANSWER_REJECTED"
                item.updated_at = func.clock_timestamp()
        session.status = "failed"
    else:
        answer.status = "confirming"
        answer.external_reason_code = result.safe_reason_code or "CHATFLOW_ANSWER_OUTCOME_UNKNOWN"
        question.status = "confirming"
        now = db.scalar(select(func.clock_timestamp()))
        if now is None:
            raise ValueError("database time is unavailable")
        if not _schedule_chatflow_answer_recovery(db, action, question, answer, now=now):
            answer.status = "unknown"
            answer.external_reason_code = "CHATFLOW_ANSWER_RECOVERY_LIMIT"
            question.status = "reconciling"
    answer.updated_at = func.clock_timestamp()
    question.updated_at = func.clock_timestamp()
    task.updated_at = func.clock_timestamp()
    session.updated_at = func.clock_timestamp()


def _schedule_chatflow_answer_recovery(
    db: Session,
    action: Action,
    question: BusinessQuestion,
    answer: BusinessAnswerSubmission,
    *,
    now,
) -> bool:
    if (
        answer.recovery_attempts >= CHATFLOW_ANSWER_MAX_RECOVERIES
        or answer.recovery_deadline <= now
    ):
        answer.external_reason_code = "CHATFLOW_ANSWER_RECOVERY_LIMIT"
        return False
    recovery_no = answer.recovery_attempts + 1
    last_sequence = int(
        db.scalar(
            select(func.coalesce(func.max(Action.sequence), 0)).where(
                Action.scope_kind == "business_session",
                Action.scope_id == question.business_session_id,
            )
        )
        or 0
    )
    sequence = last_sequence + 1
    recovery_digest = chatflow_answer_action_digest(
        action_kind=CHATFLOW_ANSWER_RECOVERY_ACTION,
        question=question,
        answer=answer,
    )
    schedule_action_in_transaction(
        db,
        action_id=chatflow_answer_action_id(
            answer_submission_id=answer.id,
            sequence=recovery_no,
            recovery=True,
        ),
        task_id=action.task_id,
        business_session_id=question.business_session_id,
        business_question_id=question.id,
        business_answer_submission_id=answer.id,
        scope_kind="business_session",
        scope_id=question.business_session_id,
        action_kind=CHATFLOW_ANSWER_RECOVERY_ACTION,
        sequence=sequence,
        due_at=now + timedelta(seconds=min(5 * (2 ** (recovery_no - 1)), 60)),
        input_digest_version=1,
        input_digest=recovery_digest,
    )
    answer.recovery_attempts = recovery_no
    return True


def _human_input_command(payload: object) -> ChatflowHumanInputCommand:
    if not isinstance(payload, Mapping):
        raise ValueError("Chatflow answer payload is invalid")
    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        raise ValueError("Chatflow answer values are invalid")
    token = payload.get("form_token")
    if not isinstance(token, str) or not token:
        raise ValueError("Chatflow form binding is missing")
    return ChatflowHumanInputCommand(
        business_session_id=UUID(str(payload["business_session_id"])),
        recommendation_id=UUID(str(payload["recommendation_id"])),
        recommendation_revision=int(payload["recommendation_revision"]),
        dify_run_id=str(payload["dify_run_id"]),
        dify_user_id=str(payload["dify_user_id"]),
        task_id=UUID(str(payload["task_id"])),
        question_id=UUID(str(payload["question_id"])),
        answer_submission_id=UUID(str(payload["answer_submission_id"])),
        round_number=int(payload["round_number"]),
        form_token=token,
        answers=dict(answers),
    )


def _human_input_result(result: ChatflowHumanInputResult) -> ActionExecutionResult:
    if not isinstance(result, ChatflowHumanInputResult):
        return ActionExecutionResult("unknown", safe_reason_code="CHATFLOW_ANSWER_RESULT_INVALID")
    if result.outcome == "accepted":
        return ActionExecutionResult("accepted")
    if result.outcome == "definitely_rejected":
        return ActionExecutionResult(
            "definitely_rejected",
            safe_reason_code=_safe_reason(result.safe_reason_code, "CHATFLOW_ANSWER_REJECTED"),
        )
    return ActionExecutionResult(
        "unknown",
        safe_reason_code=_safe_reason(result.safe_reason_code, "CHATFLOW_ANSWER_OUTCOME_UNKNOWN"),
    )


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
        task_item_id=action.task_item_id,
        attempt_id=action.attempt_id,
        business_session_id=action.business_session_id,
        business_question_id=action.business_question_id,
        business_answer_submission_id=action.business_answer_submission_id,
    )


def _load_action_session_and_recommendation(
    db: Session, action: Action, action_input: ActionInput
) -> tuple[BusinessSession, Recommendation]:
    if (
        action.scope_kind != "business_session"
        or action.business_session_id is None
        or action_input.business_session_id != action.business_session_id
        or action_input.scope_id != action.business_session_id
        or action.scope_id != action.business_session_id
    ):
        raise ValueError("Chatflow action session identity is invalid")
    session = db.get(BusinessSession, action.business_session_id)
    recommendation = db.execute(
        select(Recommendation).where(
            Recommendation.business_session_id == action.business_session_id,
            Recommendation.source_kind == "dify_chatflow",
        )
    ).scalar_one_or_none()
    if session is None or recommendation is None:
        raise ValueError("Chatflow action parents are missing")
    return session, recommendation


def _start_command(payload: object, start_operation_id: UUID) -> ChatflowStartCommand:
    if not isinstance(payload, Mapping):
        raise ValueError("Chatflow start payload is invalid")
    references_raw = payload.get("reference_assets")
    if not isinstance(references_raw, (list, tuple)):
        raise ValueError("Chatflow start reference snapshot is invalid")
    references = tuple(dict(item) for item in references_raw if isinstance(item, Mapping))
    if len(references) != len(references_raw):
        raise ValueError("Chatflow start reference snapshot is invalid")
    category = payload.get("category")
    if not isinstance(category, str) or not category:
        raise ValueError("Chatflow start category is invalid")
    files_raw = payload.get("reference_files")
    if not isinstance(files_raw, (list, tuple)) or not files_raw:
        raise ValueError("Chatflow start reference file mapping is invalid")
    reference_files: list[dict[str, object]] = []
    for position, item in enumerate(files_raw):
        if not isinstance(item, Mapping):
            raise ValueError("Chatflow start reference file mapping is invalid")
        file_id = item.get("dify_file_id")
        if (
            item.get("position") != position
            or item.get("slot") != ("PRIMARY" if position == 0 else f"AUX{position:02d}")
            or not isinstance(file_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,255}", file_id) is None
            or not isinstance(item.get("content_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(item["content_sha256"])) is None
        ):
            raise ValueError("Chatflow start reference file mapping is invalid")
        reference_files.append(dict(item))
    return ChatflowStartCommand(
        business_session_id=UUID(str(payload["business_session_id"])),
        recommendation_id=UUID(str(payload["recommendation_id"])),
        recommendation_revision=int(payload["recommendation_revision"]),
        start_operation_id=start_operation_id,
        dify_user_id=str(payload["dify_user_id"]),
        reference_set_id=UUID(str(payload["reference_set_id"])),
        reference_asset_digest=str(payload["reference_asset_digest"]),
        reference_assets=references,
        category=category,
        reference_files=tuple(reference_files),
    )


def _first_form_definition_command(payload: object) -> ChatflowFirstFormDefinitionCommand:
    if not isinstance(payload, Mapping):
        raise ValueError("v3.4 form binding payload is invalid")
    run_id = payload.get("dify_run_id")
    user = payload.get("dify_user_id")
    form_node_id = payload.get("form_node_id")
    form_token = payload.get("form_token")
    if (
        not isinstance(run_id, str)
        or _SAFE_RUN_ID.fullmatch(run_id) is None
        or not isinstance(user, str)
        or not user
        or form_node_id != "form_0"
        or not isinstance(form_token, str)
        or not form_token
    ):
        raise ValueError("v3.4 form binding identity is invalid")
    return ChatflowFirstFormDefinitionCommand(
        business_session_id=UUID(str(payload["business_session_id"])),
        recommendation_id=UUID(str(payload["recommendation_id"])),
        recommendation_revision=int(payload["recommendation_revision"]),
        dify_run_id=run_id,
        dify_user_id=user,
        form_node_id=form_node_id,
        form_token=form_token,
    )


def _continuation_command(payload: object) -> ChatflowContinuationCommand:
    if not isinstance(payload, Mapping):
        raise ValueError("Chatflow continuation payload is invalid")
    snapshot = payload.get("selection_snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("Chatflow selection snapshot is invalid")
    submission_raw = payload.get("selection_submission_id")
    token_raw = payload.get("form_token")
    form_inputs_raw = payload.get("form_inputs")
    if submission_raw is None:
        submission_id = None
    else:
        submission_id = UUID(str(submission_raw))
    if token_raw is None:
        form_token = None
    elif isinstance(token_raw, SecretStr):
        form_token = token_raw.get_secret_value()
    else:
        raise ValueError("Chatflow form token is not stored as a secret value")
    if form_inputs_raw is None:
        form_inputs = None
    elif isinstance(form_inputs_raw, Mapping) and all(
        isinstance(name, str) and isinstance(value, str) for name, value in form_inputs_raw.items()
    ):
        form_inputs = dict(form_inputs_raw)
    else:
        raise ValueError("Chatflow form inputs are invalid")
    return ChatflowContinuationCommand(
        business_session_id=UUID(str(payload["business_session_id"])),
        recommendation_id=UUID(str(payload["recommendation_id"])),
        recommendation_revision=int(payload["recommendation_revision"]),
        dify_run_id=str(payload["dify_run_id"]),
        dify_user_id=str(payload["dify_user_id"]),
        task_id=UUID(str(payload["task_id"])),
        selection_snapshot=dict(snapshot),
        selection_submission_id=submission_id,
        form_node_id=(str(payload["form_node_id"]) if payload.get("form_node_id") else None),
        form_token=form_token,
        form_action=(str(payload["form_action"]) if payload.get("form_action") else None),
        form_inputs=form_inputs,
    )


def _continuation_result(result: ChatflowContinuationResult) -> ActionExecutionResult:
    if not isinstance(result, ChatflowContinuationResult):
        return ActionExecutionResult("unknown", safe_reason_code="CHATFLOW_RESUME_RESULT_INVALID")
    if result.outcome == "accepted":
        return ActionExecutionResult("accepted")
    if result.outcome == "definitely_rejected":
        return ActionExecutionResult(
            "definitely_rejected",
            safe_reason_code=_safe_reason(result.safe_reason_code, "CHATFLOW_RESUME_REJECTED"),
        )
    return ActionExecutionResult(
        "unknown",
        safe_reason_code=_safe_reason(result.safe_reason_code, "CHATFLOW_RESUME_OUTCOME_UNKNOWN"),
    )


def _safe_reason(value: str | None, fallback: str) -> str:
    return value if isinstance(value, str) and _SAFE_ERROR.fullmatch(value) else fallback
