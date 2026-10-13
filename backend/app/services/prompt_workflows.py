from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.models import User
from app.db.outbox_models import OutboxEvent
from app.db.prompt_models import (
    PromptSubmission,
    TaskPromptItem,
    TaskPromptPackage,
    TaskPromptWorkflow,
)
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ReferenceSet
from app.db.task_models import Task, TaskItem, TaskItemAttempt
from app.integrations.dify.contracts import (
    DifyNotConfigured,
    DifyPromptSubmissionConflict,
    DifyPromptWorkflowPort,
)
from app.schemas.prompt_packages import PromptImage, PromptPackage
from app.schemas.prompt_workflow import (
    PROMPT_CONTENT_DIGEST_VERSION,
    PROMPT_RESULT_DIGEST_VERSION,
    CallbackAcceptance,
    PromptCallback,
    PromptPhase,
    PromptRecoveryResult,
    PromptSubmissionCommand,
    PromptSubmitResult,
    canonical_json_bytes,
    canonical_sha256,
    json_safe_value,
    prompt_callback_digest_payload,
)
from app.services.outbox import enqueue_in_transaction
from app.services.prompt_validation import (
    PROMPT_PACKAGE_VALIDATOR_VERSION,
    PromptPackageValidationError,
    validate_prompt_package,
)
from app.services.trusted_analysis import (
    TrustedAnalysisSnapshot,
    TrustedAnalysisSnapshotError,
    load_trusted_analysis_snapshot,
)
from app.workers.action_processor import (
    ActionExecutionResult,
    ActionInput,
    ActionRegistration,
    schedule_action_in_transaction,
)

PROMPT_INITIAL_SUBMIT_ACTION_KIND: Final[str] = "prompt.initial.submit"
PROMPT_INITIAL_RECOVER_ACTION_KIND: Final[str] = "prompt.initial.recover"
PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND: Final[str] = "prompt.followup.submit"
PROMPT_FOLLOWUP_RECOVER_ACTION_KIND: Final[str] = "prompt.followup.recover"
PROMPT_MOCK_RESULT_QUERY_ACTION_KIND: Final[str] = "prompt.mock_result.query"
GENERATION_START_EVENT_TYPE: Final[str] = "task.generation.start.v1"
GENERATION_START_SCHEMA_VERSION: Final[int] = 1
GENERATION_START_OPERATION_VERSION: Final[int] = 1
PROMPT_PACKAGE_CONTRACT_VERSION: Final[str] = "v3.3"
PROMPT_PACKAGE_DIGEST_VERSION: Final[int] = 1
PROMPT_RECONCILE_DELAY_SECONDS: Final[int] = 30
PROMPT_MOCK_RESULT_QUERY_MAX_ATTEMPTS: Final[int] = 4
PROMPT_MOCK_RESULT_QUERY_RETRY_SECONDS: Final[int] = PROMPT_RECONCILE_DELAY_SECONDS
_MAX_CALLBACK_BYTES: Final[int] = 2 * 1024 * 1024


class PromptWorkflowError(ValueError):
    """Safe fail-closed workflow error without private prompt or callback content."""

    code = "PROMPT_WORKFLOW_UNAVAILABLE"
    message = "提示词工作流暂不可用"

    def __init__(self) -> None:
        super().__init__(self.message)


class PromptWorkflowConflict(PromptWorkflowError):
    code = "PROMPT_WORKFLOW_CONFLICT"
    message = "提示词工作流版本或摘要冲突"


class PromptWorkflowUnavailable(PromptWorkflowError):
    pass


class PromptCallbackAcceptor(Protocol):
    """Application path shared by authenticated callbacks and trusted adapters."""

    def accept(self, callback: PromptCallback, *, phase: PromptPhase) -> CallbackAcceptance: ...


def create_initial_prompt_submission_in_transaction(
    db: Session,
    *,
    task: Task,
) -> PromptSubmission:
    """Create revision 1 and its submission Action in the caller's DB-only event transaction.

    The caller must already hold User → ReferenceSet → Recommendation → Task locks.
    This helper performs no adapter, network, model, or image I/O. It is intended for
    A's `task.prompt.v1` handler.
    """
    if not isinstance(task, Task) or task.id is None:
        raise PromptWorkflowUnavailable
    existing_workflow = db.execute(
        select(TaskPromptWorkflow)
        .where(TaskPromptWorkflow.task_id == task.id, TaskPromptWorkflow.workflow_revision == 1)
        .with_for_update()
    ).scalar_one_or_none()
    if existing_workflow is not None:
        snapshot = _load_task_pinned_analysis(task)
        _assert_workflow_snapshot(existing_workflow, snapshot)
        expected_payload = _initial_submission_payload(task, snapshot)
        expected_digest = _submission_digest("initial_preferences", 0, expected_payload)
        submission = db.execute(
            select(PromptSubmission)
            .where(
                PromptSubmission.workflow_id == existing_workflow.id,
                PromptSubmission.phase == "initial_preferences",
                PromptSubmission.followup_round == 0,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if (
            submission is None
            or submission.private_payload != expected_payload
            or submission.content_digest_version != PROMPT_CONTENT_DIGEST_VERSION
            or submission.content_digest != expected_digest
        ):
            raise PromptWorkflowUnavailable
        action = db.execute(
            select(Action).where(
                Action.scope_kind == "submission",
                Action.scope_id == submission.submission_id,
                Action.sequence == 1,
            )
        ).scalar_one_or_none()
        if (
            action is None
            or action.task_id != task.id
            or action.scope_id != submission.submission_id
            or action.action_kind != PROMPT_INITIAL_SUBMIT_ACTION_KIND
            or action.input_digest_version != submission.content_digest_version
            or action.input_digest != submission.content_digest
        ):
            raise PromptWorkflowUnavailable
        return submission

    if task.status != "queued" or (task.expires_at is not None and task.expires_at <= _db_now(db)):
        raise PromptWorkflowUnavailable
    snapshot = _load_task_pinned_analysis(task)
    private_payload = _initial_submission_payload(task, snapshot)
    digest = _submission_digest("initial_preferences", 0, private_payload)
    now = _db_now(db)
    workflow = TaskPromptWorkflow(
        task_id=task.id,
        workflow_run_id=uuid4(),
        workflow_revision=1,
        trusted_analysis_snapshot_id=snapshot.snapshot_id,
        trusted_analysis_snapshot_digest=bytes.fromhex(snapshot.snapshot_digest),
        status="pending_submission",
        created_at=now,
        updated_at=now,
    )
    db.add(workflow)
    db.flush()
    submission = PromptSubmission(
        submission_id=uuid4(),
        workflow_id=workflow.id,
        task_id=task.id,
        workflow_run_id=workflow.workflow_run_id,
        workflow_revision=workflow.workflow_revision,
        phase="initial_preferences",
        followup_round=0,
        content_digest_version=PROMPT_CONTENT_DIGEST_VERSION,
        content_digest=digest,
        private_payload=private_payload,
        status="prepared",
        created_at=now,
        updated_at=now,
    )
    db.add(submission)
    db.flush()
    task.status = "prompting"
    task.updated_at = now
    schedule_action_in_transaction(
        db,
        action_id=uuid4(),
        task_id=task.id,
        scope_kind="submission",
        scope_id=submission.submission_id,
        action_kind=PROMPT_INITIAL_SUBMIT_ACTION_KIND,
        sequence=1,
        due_at=now,
        input_digest_version=PROMPT_CONTENT_DIGEST_VERSION,
        input_digest=digest,
    )
    return submission


def build_prompt_action_registrations(
    port: DifyPromptWorkflowPort,
    *,
    mock_result_callback_service: PromptCallbackAcceptor | None = None,
) -> dict[str, ActionRegistration]:
    """Build prompt actions and opt into durable mock result polling explicitly."""
    query_enabled = mock_result_callback_service is not None
    registrations = {
        PROMPT_INITIAL_SUBMIT_ACTION_KIND: ActionRegistration(
            kind=PROMPT_INITIAL_SUBMIT_ACTION_KIND,
            prepare=prepare_initial_prompt_submission,
            execute=lambda action_input: execute_initial_prompt_submission(action_input, port),
            finalize=lambda db, action, action_input, result: finalize_initial_prompt_action(
                db,
                action,
                action_input,
                result,
                schedule_mock_result_query=query_enabled,
            ),
            recover=lambda action_input: recover_initial_prompt_submission(action_input, port),
            scope_kinds=frozenset({"submission"}),
        ),
        PROMPT_INITIAL_RECOVER_ACTION_KIND: ActionRegistration(
            kind=PROMPT_INITIAL_RECOVER_ACTION_KIND,
            prepare=prepare_initial_prompt_submission,
            execute=lambda action_input: recover_initial_prompt_submission(action_input, port),
            finalize=lambda db, action, action_input, result: finalize_initial_prompt_action(
                db,
                action,
                action_input,
                result,
                schedule_mock_result_query=query_enabled,
            ),
            recover=lambda action_input: recover_initial_prompt_submission(action_input, port),
            scope_kinds=frozenset({"submission"}),
        ),
        PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND: ActionRegistration(
            kind=PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND,
            prepare=prepare_followup_prompt_submission,
            execute=lambda action_input: execute_followup_prompt_submission(action_input, port),
            finalize=lambda db, action, action_input, result: finalize_followup_prompt_action(
                db,
                action,
                action_input,
                result,
                schedule_mock_result_query=query_enabled,
            ),
            recover=lambda action_input: recover_followup_prompt_submission(action_input, port),
            scope_kinds=frozenset({"submission"}),
        ),
        PROMPT_FOLLOWUP_RECOVER_ACTION_KIND: ActionRegistration(
            kind=PROMPT_FOLLOWUP_RECOVER_ACTION_KIND,
            prepare=prepare_followup_prompt_submission,
            execute=lambda action_input: recover_followup_prompt_submission(action_input, port),
            finalize=lambda db, action, action_input, result: finalize_followup_prompt_action(
                db,
                action,
                action_input,
                result,
                schedule_mock_result_query=query_enabled,
            ),
            recover=lambda action_input: recover_followup_prompt_submission(action_input, port),
            scope_kinds=frozenset({"submission"}),
        ),
    }
    if mock_result_callback_service is not None:
        registrations[PROMPT_MOCK_RESULT_QUERY_ACTION_KIND] = ActionRegistration(
            kind=PROMPT_MOCK_RESULT_QUERY_ACTION_KIND,
            prepare=prepare_mock_prompt_result_query,
            execute=lambda action_input: execute_mock_prompt_result_query(
                action_input, port, mock_result_callback_service
            ),
            finalize=finalize_mock_prompt_result_query,
            recover=lambda action_input: execute_mock_prompt_result_query(
                action_input, port, mock_result_callback_service
            ),
            scope_kinds=frozenset({"submission"}),
        )
    return registrations


def prepare_followup_prompt_submission(db: Session, action: Action) -> ActionInput:
    if (
        action.scope_kind != "submission"
        or action.scope_id is None
        or action.action_kind
        not in {PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND, PROMPT_FOLLOWUP_RECOVER_ACTION_KIND}
        or action.sequence < 1
        or (action.action_kind == PROMPT_FOLLOWUP_RECOVER_ACTION_KIND and action.sequence < 2)
        or action.task_item_id is not None
        or action.attempt_id is not None
    ):
        raise PromptWorkflowUnavailable
    submission = db.execute(
        select(PromptSubmission).where(
            PromptSubmission.submission_id == action.scope_id,
            PromptSubmission.task_id == action.task_id,
        )
    ).scalar_one_or_none()
    if submission is None:
        raise PromptWorkflowUnavailable
    workflow = db.execute(
        select(TaskPromptWorkflow).where(TaskPromptWorkflow.id == submission.workflow_id)
    ).scalar_one_or_none()
    if (
        workflow is None
        or workflow.task_id != action.task_id
        or workflow.workflow_run_id != submission.workflow_run_id
        or workflow.workflow_revision != submission.workflow_revision
        or submission.phase != "followup_answer"
        or submission.followup_round not in {1, 2}
    ):
        raise PromptWorkflowUnavailable
    command = _command_from_submission(submission)
    return ActionInput(
        action_id=action.id,
        task_id=action.task_id,
        scope_kind=action.scope_kind,
        scope_id=action.scope_id,
        action_kind=action.action_kind,
        sequence=action.sequence,
        input_digest_version=action.input_digest_version,
        input_digest=action.input_digest,
        payload=command.model_dump(mode="python"),
    )


def execute_followup_prompt_submission(
    action_input: ActionInput,
    port: DifyPromptWorkflowPort,
) -> ActionExecutionResult:
    command = _command_from_action_input(action_input)
    if command.phase != "followup_answer":
        return ActionExecutionResult("unknown", safe_reason_code="PROMPT_PHASE_MISMATCH")
    try:
        result = port.submit_preferences(command)
    except DifyPromptSubmissionConflict:
        return ActionExecutionResult("unknown", safe_reason_code="PROMPT_SUBMISSION_ID_CONFLICT")
    except Exception:
        return ActionExecutionResult("unknown", safe_reason_code="PROMPT_SUBMIT_OUTCOME_UNKNOWN")
    if not _result_matches_command(result, command):
        return ActionExecutionResult("unknown", safe_reason_code="PROMPT_SUBMIT_BINDING_MISMATCH")
    return _action_result_for_submit(result)


def recover_followup_prompt_submission(
    action_input: ActionInput,
    port: DifyPromptWorkflowPort,
) -> ActionExecutionResult:
    command = _command_from_action_input(action_input)
    if command.phase != "followup_answer":
        return ActionExecutionResult("unknown", safe_reason_code="PROMPT_PHASE_MISMATCH")
    try:
        result = port.recover_submission(command)
    except Exception:
        return ActionExecutionResult("unknown", safe_reason_code="PROMPT_RECOVERY_OUTCOME_UNKNOWN")
    if not _recovery_matches_command(result, command):
        return ActionExecutionResult("unknown", safe_reason_code="PROMPT_RECOVERY_BINDING_MISMATCH")
    payload = {"operation": "recover", **result.model_dump(mode="python")}
    if result.outcome == "accepted":
        return ActionExecutionResult("accepted", value=payload)
    if result.outcome == "definitely_rejected":
        return ActionExecutionResult(
            "definitely_rejected", safe_reason_code=result.safe_reason_code, value=payload
        )
    if result.outcome == "definitely_not_found":
        return ActionExecutionResult("accepted", value=payload)
    return ActionExecutionResult(
        "unknown",
        safe_reason_code=result.safe_reason_code or "PROMPT_RECOVERY_UNKNOWN",
        value=payload,
    )


def prepare_initial_prompt_submission(db: Session, action: Action) -> ActionInput:
    if (
        action.scope_kind != "submission"
        or action.scope_id is None
        or action.action_kind
        not in {PROMPT_INITIAL_SUBMIT_ACTION_KIND, PROMPT_INITIAL_RECOVER_ACTION_KIND}
        or action.sequence < 1
        or (action.action_kind == PROMPT_INITIAL_RECOVER_ACTION_KIND and action.sequence < 2)
        or action.task_item_id is not None
        or action.attempt_id is not None
    ):
        raise PromptWorkflowUnavailable
    submission = db.execute(
        select(PromptSubmission).where(
            PromptSubmission.submission_id == action.scope_id,
            PromptSubmission.task_id == action.task_id,
        )
    ).scalar_one_or_none()
    if submission is None:
        raise PromptWorkflowUnavailable
    workflow = db.execute(
        select(TaskPromptWorkflow).where(TaskPromptWorkflow.id == submission.workflow_id)
    ).scalar_one_or_none()
    if (
        workflow is None
        or workflow.task_id != action.task_id
        or workflow.workflow_run_id != submission.workflow_run_id
        or workflow.workflow_revision != submission.workflow_revision
        or submission.phase != "initial_preferences"
        or submission.followup_round != 0
    ):
        raise PromptWorkflowUnavailable
    command = _command_from_submission(submission)
    return ActionInput(
        action_id=action.id,
        task_id=action.task_id,
        scope_kind=action.scope_kind,
        scope_id=action.scope_id,
        action_kind=action.action_kind,
        sequence=action.sequence,
        input_digest_version=action.input_digest_version,
        input_digest=action.input_digest,
        payload=command.model_dump(mode="python"),
    )


def prepare_mock_prompt_result_query(db: Session, action: Action) -> ActionInput:
    if (
        action.scope_kind != "submission"
        or action.scope_id is None
        or action.action_kind != PROMPT_MOCK_RESULT_QUERY_ACTION_KIND
        or action.sequence < 2
        or action.task_item_id is not None
        or action.attempt_id is not None
    ):
        raise PromptWorkflowUnavailable
    submission = db.execute(
        select(PromptSubmission).where(
            PromptSubmission.submission_id == action.scope_id,
            PromptSubmission.task_id == action.task_id,
        )
    ).scalar_one_or_none()
    if submission is None:
        raise PromptWorkflowUnavailable
    workflow = db.execute(
        select(TaskPromptWorkflow).where(
            TaskPromptWorkflow.id == submission.workflow_id,
            TaskPromptWorkflow.task_id == action.task_id,
        )
    ).scalar_one_or_none()
    if (
        workflow is None
        or workflow.workflow_run_id != submission.workflow_run_id
        or workflow.workflow_revision != submission.workflow_revision
        or submission.status not in {"accepted", "callback_received", "package_ready", "failed"}
    ):
        raise PromptWorkflowUnavailable
    query_attempt = db.scalar(
        select(func.count(Action.id)).where(
            Action.scope_kind == "submission",
            Action.scope_id == submission.submission_id,
            Action.action_kind == PROMPT_MOCK_RESULT_QUERY_ACTION_KIND,
            Action.sequence <= action.sequence,
        )
    )
    if query_attempt is None or not 1 <= query_attempt <= PROMPT_MOCK_RESULT_QUERY_MAX_ATTEMPTS:
        raise PromptWorkflowUnavailable
    command = _command_from_submission(submission)
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
            "command": command.model_dump(mode="python"),
            "query_attempt": query_attempt,
        },
    )


def execute_mock_prompt_result_query(
    action_input: ActionInput,
    port: DifyPromptWorkflowPort,
    callback_service: PromptCallbackAcceptor,
) -> ActionExecutionResult:
    command, query_attempt = _command_from_mock_result_query_input(action_input)
    try:
        callback = port.read_prompt_result(command.submission_id)
    except DifyNotConfigured:
        return _mock_result_query_unavailable(query_attempt, "PROMPT_RESULT_NOT_READY")
    except DifyPromptSubmissionConflict:
        return ActionExecutionResult(
            "definitely_rejected", safe_reason_code="PROMPT_RESULT_DIGEST_INVALID"
        )
    if not _prompt_callback_matches_command(callback, command):
        return ActionExecutionResult(
            "definitely_rejected", safe_reason_code="PROMPT_RESULT_BINDING_MISMATCH"
        )
    try:
        acceptance = callback_service.accept(callback, phase=command.phase)
    except DomainError as error:
        if error.code == "DATABASE_UNAVAILABLE":
            return _mock_result_query_unavailable(query_attempt, "PROMPT_RESULT_STORE_RETRY")
        if error.code == "PROMPT_CALLBACK_CONFLICT":
            return ActionExecutionResult(
                "definitely_rejected", safe_reason_code="PROMPT_RESULT_DIGEST_CONFLICT"
            )
        if error.code == "PROMPT_CALLBACK_STALE":
            return ActionExecutionResult(
                "definitely_rejected", safe_reason_code="PROMPT_RESULT_STALE"
            )
        return ActionExecutionResult(
            "unknown", safe_reason_code="PROMPT_RESULT_ACCEPTANCE_UNAVAILABLE"
        )
    return ActionExecutionResult("accepted", value={"callback_status": acceptance.status})


def finalize_mock_prompt_result_query(
    db: Session,
    action: Action,
    action_input: ActionInput,
    result: ActionExecutionResult,
) -> None:
    _validate_action_input_identity(action, action_input)
    command, query_attempt = _command_from_mock_result_query_input(action_input)
    submission = db.execute(
        select(PromptSubmission).where(
            PromptSubmission.submission_id == command.submission_id,
            PromptSubmission.task_id == command.task_id,
        )
    ).scalar_one_or_none()
    if submission is None or not _prompt_command_matches(
        command, _command_from_submission(submission)
    ):
        raise PromptWorkflowUnavailable
    if result.outcome == "safe_retry":
        if query_attempt >= PROMPT_MOCK_RESULT_QUERY_MAX_ATTEMPTS:
            raise PromptWorkflowConflict
        _schedule_next_mock_prompt_result_query(db, action, submission)


def _mock_result_query_unavailable(query_attempt: int, reason_code: str) -> ActionExecutionResult:
    if query_attempt >= PROMPT_MOCK_RESULT_QUERY_MAX_ATTEMPTS:
        return ActionExecutionResult("unknown", safe_reason_code="PROMPT_RESULT_QUERY_MAX_ATTEMPTS")
    return ActionExecutionResult(
        "safe_retry",
        safe_reason_code=reason_code,
        retry_after_seconds=PROMPT_MOCK_RESULT_QUERY_RETRY_SECONDS,
    )


def _command_from_mock_result_query_input(
    action_input: ActionInput,
) -> tuple[PromptSubmissionCommand, int]:
    payload = _thaw_action_value(action_input.payload)
    if not isinstance(payload, dict):
        raise PromptWorkflowUnavailable
    try:
        command = PromptSubmissionCommand.model_validate(payload.get("command"))
    except (TypeError, ValueError):
        raise PromptWorkflowUnavailable from None
    attempt = payload.get("query_attempt")
    if (
        isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or not 1 <= attempt <= PROMPT_MOCK_RESULT_QUERY_MAX_ATTEMPTS
        or action_input.scope_kind != "submission"
        or action_input.scope_id != command.submission_id
        or action_input.task_id != command.task_id
        or action_input.action_kind != PROMPT_MOCK_RESULT_QUERY_ACTION_KIND
        or action_input.input_digest_version != command.content_digest_version
        or action_input.input_digest != command.content_digest
    ):
        raise PromptWorkflowUnavailable
    return command, attempt


def _prompt_callback_matches_command(
    callback: PromptCallback, command: PromptSubmissionCommand
) -> bool:
    return (
        isinstance(callback, PromptCallback)
        and callback.task_id == command.task_id
        and callback.workflow_run_id == command.workflow_run_id
        and callback.workflow_revision == command.workflow_revision
        and callback.submission_id == command.submission_id
        and callback.submission_content_digest_version == command.content_digest_version
        and callback.submission_content_digest == command.content_digest
    )


def execute_initial_prompt_submission(
    action_input: ActionInput,
    port: DifyPromptWorkflowPort,
) -> ActionExecutionResult:
    command = _command_from_action_input(action_input)
    try:
        result = port.submit_preferences(command)
    except DifyPromptSubmissionConflict:
        return ActionExecutionResult(
            "unknown",
            safe_reason_code="PROMPT_SUBMISSION_ID_CONFLICT",
            value={"operation": "submit"},
        )
    if not _result_matches_command(result, command):
        return ActionExecutionResult(
            "unknown",
            safe_reason_code="PROMPT_SUBMIT_RESULT_BINDING_MISMATCH",
            value={"operation": "submit"},
        )
    return _action_result_for_submit(result)


def recover_initial_prompt_submission(
    action_input: ActionInput,
    port: DifyPromptWorkflowPort,
) -> ActionExecutionResult:
    command = _command_from_action_input(action_input)
    try:
        result = port.recover_submission(command)
    except DifyPromptSubmissionConflict:
        return ActionExecutionResult(
            "unknown",
            safe_reason_code="PROMPT_RECOVERY_BINDING_CONFLICT",
            value={"operation": "recover"},
        )
    if not _recovery_matches_command(result, command):
        return ActionExecutionResult(
            "unknown",
            safe_reason_code="PROMPT_RECOVERY_RESULT_BINDING_MISMATCH",
            value={"operation": "recover"},
        )
    data = result.model_dump(mode="python")
    data["operation"] = "recover"
    if result.outcome == "accepted":
        return ActionExecutionResult("accepted", value=data)
    if result.outcome in {"definitely_rejected", "definitely_not_found"}:
        return ActionExecutionResult(
            "definitely_rejected",
            safe_reason_code=(
                result.safe_reason_code
                if result.outcome == "definitely_rejected"
                else "PROMPT_SUBMISSION_CONFIRMED_ABSENT"
            ),
            value=data,
        )
    return ActionExecutionResult(
        "unknown",
        safe_reason_code=result.safe_reason_code or "PROMPT_RECOVERY_UNKNOWN",
        value=data,
    )


def finalize_initial_prompt_action(
    db: Session,
    action: Action,
    action_input: ActionInput,
    result: ActionExecutionResult,
    *,
    schedule_mock_result_query: bool = False,
) -> None:
    _finalize_prompt_submission_action(
        db,
        action,
        action_input,
        result,
        expected_phase="initial_preferences",
        submit_kind=PROMPT_INITIAL_SUBMIT_ACTION_KIND,
        recover_kind=PROMPT_INITIAL_RECOVER_ACTION_KIND,
        schedule_mock_result_query=schedule_mock_result_query,
    )


def finalize_followup_prompt_action(
    db: Session,
    action: Action,
    action_input: ActionInput,
    result: ActionExecutionResult,
    *,
    schedule_mock_result_query: bool = False,
) -> None:
    _finalize_prompt_submission_action(
        db,
        action,
        action_input,
        result,
        expected_phase="followup_answer",
        submit_kind=PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND,
        recover_kind=PROMPT_FOLLOWUP_RECOVER_ACTION_KIND,
        schedule_mock_result_query=schedule_mock_result_query,
    )


def _finalize_prompt_submission_action(
    db: Session,
    action: Action,
    action_input: ActionInput,
    result: ActionExecutionResult,
    *,
    expected_phase: PromptPhase,
    submit_kind: str,
    recover_kind: str,
    schedule_mock_result_query: bool,
) -> None:
    _validate_action_input_identity(action, action_input)
    task = db.execute(
        select(Task).where(Task.id == action.task_id).with_for_update()
    ).scalar_one_or_none()
    hint = db.execute(
        select(PromptSubmission.workflow_id).where(
            PromptSubmission.submission_id == action.scope_id,
            PromptSubmission.task_id == action.task_id,
        )
    ).scalar_one_or_none()
    if task is None or hint is None:
        raise PromptWorkflowUnavailable
    workflow = db.execute(
        select(TaskPromptWorkflow)
        .where(TaskPromptWorkflow.id == hint, TaskPromptWorkflow.task_id == task.id)
        .with_for_update()
    ).scalar_one_or_none()
    submission = db.execute(
        select(PromptSubmission)
        .where(
            PromptSubmission.submission_id == action.scope_id,
            PromptSubmission.task_id == task.id,
            PromptSubmission.workflow_id == hint,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if workflow is None or submission is None or submission.phase != expected_phase:
        raise PromptWorkflowUnavailable
    command = _command_from_submission(submission)
    if not _action_payload_matches_submission(action_input, command):
        raise PromptWorkflowConflict

    value = _safe_result_mapping(result.value)
    if submission.callback_result_digest is not None:
        # A bound asynchronous callback proves the workflow has advanced. A late,
        # conflicting submit/recovery result must never reopen submission or replace it.
        if schedule_mock_result_query and result.outcome == "accepted":
            _schedule_mock_prompt_result_query(db, action, submission)
        return
    is_recovery = value.get("operation") == "recover" or action.action_kind == recover_kind
    if is_recovery:
        recovery_outcome = value.get("outcome")
        if recovery_outcome == "definitely_not_found":
            submission.status = "prepared"
            workflow.status = "pending_submission"
            _store_submit_result(
                submission,
                PromptSubmitResult(
                    submission_id=command.submission_id,
                    task_id=command.task_id,
                    workflow_run_id=command.workflow_run_id,
                    workflow_revision=command.workflow_revision,
                    outcome="unknown",
                    safe_reason_code="PROMPT_SUBMISSION_CONFIRMED_ABSENT",
                ),
            )
            _schedule_next_submission_action(db, action, submission, submit_kind)
            task.status = "prompting"
            task.updated_at = func.clock_timestamp()
            workflow.updated_at = func.clock_timestamp()
            return
        if recovery_outcome == "accepted" and result.outcome == "accepted":
            accepted = _submit_result_from_recovery(value, command)
            _store_submit_result(submission, accepted)
            if submission.callback_result_digest is None:
                submission.status = "accepted"
                workflow.status = "submitted"
                task.status = "prompting"
                task.updated_at = func.clock_timestamp()
            if schedule_mock_result_query:
                _schedule_mock_prompt_result_query(db, action, submission)
            workflow.updated_at = func.clock_timestamp()
            return
        if recovery_outcome == "definitely_rejected" and result.outcome == "definitely_rejected":
            rejected = _submit_result_from_recovery(value, command)
            _store_submit_result(submission, rejected)
            submission.status = "rejected"
            workflow.status = "failed"
            _fail_prompt_task_items(db, task, error_code="PROMPT_WORKFLOW_FAILED")
            workflow.updated_at = func.clock_timestamp()
            return
        _mark_prompt_unknown(submission, workflow, task, result.safe_reason_code)
        if submission.callback_result_digest is None:
            _schedule_next_submission_action(db, action, submission, recover_kind)
        return

    if result.outcome == "accepted":
        try:
            # Action result values are detached into JSON-safe mappings above,
            # so their UUIDs are strings at finalize time. Parse those canonical
            # UUID strings, then bind every identity field to the saved command.
            submitted = PromptSubmitResult.model_validate(value.get("result"), strict=False)
        except (TypeError, ValueError):
            submitted = None
        if submitted is None or not _result_matches_command(submitted, command):
            _mark_prompt_unknown(
                submission,
                workflow,
                task,
                "PROMPT_SUBMIT_RESULT_BINDING_MISMATCH",
            )
            if submission.callback_result_digest is None:
                _schedule_next_submission_action(db, action, submission, recover_kind)
            return
        _store_submit_result(submission, submitted)
        if submission.callback_result_digest is None:
            submission.status = "accepted"
            workflow.status = "submitted"
        if schedule_mock_result_query:
            _schedule_mock_prompt_result_query(db, action, submission)
        workflow.updated_at = func.clock_timestamp()
        return
    if result.outcome == "definitely_rejected":
        rejected = PromptSubmitResult(
            submission_id=command.submission_id,
            task_id=command.task_id,
            workflow_run_id=command.workflow_run_id,
            workflow_revision=command.workflow_revision,
            outcome="definitely_rejected",
            safe_reason_code=result.safe_reason_code or "PROMPT_SUBMISSION_REJECTED",
        )
        _store_submit_result(submission, rejected)
        submission.status = "rejected"
        workflow.status = "failed"
        _fail_prompt_task_items(db, task, error_code="PROMPT_WORKFLOW_FAILED")
        workflow.updated_at = func.clock_timestamp()
        return

    _mark_prompt_unknown(submission, workflow, task, result.safe_reason_code)
    if submission.callback_result_digest is None:
        _schedule_next_submission_action(db, action, submission, recover_kind)


def accept_prompt_callback(
    db: Session,
    callback: PromptCallback,
    *,
    expected_phase: PromptPhase | None = None,
) -> CallbackAcceptance:
    """Persist a phase-bound callback in the caller's parent-lock transaction."""
    if not isinstance(callback, PromptCallback):
        raise PromptWorkflowUnavailable
    callback_bytes = canonical_json_bytes(prompt_callback_digest_payload(callback))
    if len(callback_bytes) > _MAX_CALLBACK_BYTES:
        raise PromptWorkflowUnavailable
    if callback.result_digest_version != PROMPT_RESULT_DIGEST_VERSION:
        raise PromptWorkflowUnavailable
    if canonical_sha256(prompt_callback_digest_payload(callback)) != callback.result_digest:
        raise PromptWorkflowUnavailable

    hint = db.execute(
        select(PromptSubmission.task_id, PromptSubmission.workflow_id).where(
            PromptSubmission.submission_id == callback.submission_id
        )
    ).one_or_none()
    if hint is None:
        raise PromptWorkflowUnavailable
    task = _lock_task_parent_chain(db, hint.task_id)
    if task is None:
        raise PromptWorkflowUnavailable
    now = _db_now(db)
    workflow = db.execute(
        select(TaskPromptWorkflow)
        .where(TaskPromptWorkflow.task_id == task.id)
        .order_by(TaskPromptWorkflow.workflow_revision.desc())
        .limit(1)
        .with_for_update()
    ).scalar_one_or_none()
    if workflow is None or workflow.id != hint.workflow_id:
        raise PromptWorkflowUnavailable
    submission = db.execute(
        select(PromptSubmission)
        .where(
            PromptSubmission.submission_id == callback.submission_id,
            PromptSubmission.task_id == task.id,
            PromptSubmission.workflow_id == workflow.id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if (
        submission is None
        or callback.task_id != submission.task_id
        or callback.task_id != workflow.task_id
        or callback.workflow_run_id != submission.workflow_run_id
        or callback.workflow_run_id != workflow.workflow_run_id
        or callback.workflow_revision != submission.workflow_revision
        or callback.workflow_revision != workflow.workflow_revision
        or callback.submission_content_digest_version != submission.content_digest_version
        or callback.submission_content_digest != submission.content_digest
        or (expected_phase is not None and submission.phase != expected_phase)
    ):
        raise PromptWorkflowUnavailable

    if submission.callback_result_digest is not None:
        if (
            submission.callback_result_digest_version == callback.result_digest_version
            and submission.callback_result_digest == callback.result_digest
        ):
            if submission.status == "package_ready":
                replay_status = "replayed"
            elif submission.status == "callback_received":
                replay_status = "needs_input"
            elif submission.status == "failed":
                replay_status = "rejected"
            else:
                raise PromptWorkflowUnavailable
            return CallbackAcceptance(
                status=replay_status,
                workflow_run_id=workflow.workflow_run_id,
                workflow_revision=workflow.workflow_revision,
                safe_reason_code=submission.callback_reason_code,
            )
        raise PromptWorkflowConflict

    if (
        task.terminal_at is not None
        or task.expires_at is not None
        or task.status not in {"prompting", "reconciling"}
        or workflow.status not in {"pending_submission", "submitted", "reconciling"}
    ):
        raise PromptWorkflowUnavailable
    if submission.status not in {"prepared", "accepted", "unknown"}:
        raise PromptWorkflowConflict

    if callback.outcome == "needs_input":
        expected_round = submission.followup_round + 1
        if callback.next_question_round != expected_round:
            raise PromptWorkflowUnavailable
        valid_fields = {
            "language",
            "platform",
            "usage_scene",
            "visual_style",
            "selling_points",
        }
        if (
            callback.allowed_fields is None
            or not callback.allowed_fields
            or callback.locked_fields is None
            or any(
                field not in valid_fields
                for field in (*callback.allowed_fields, *callback.locked_fields)
            )
            or set(callback.allowed_fields) & set(callback.locked_fields)
        ):
            raise PromptWorkflowUnavailable

    submission.callback_outcome = callback.outcome
    submission.callback_result_digest_version = callback.result_digest_version
    submission.callback_result_digest = callback.result_digest
    submission.callback_received_at = now
    submission.callback_reason_code = callback.safe_reason_code
    submission.callback_round = callback.next_question_round
    submission.callback_allowed_fields = (
        list(callback.allowed_fields) if callback.allowed_fields is not None else None
    )
    submission.callback_locked_fields = (
        list(callback.locked_fields) if callback.locked_fields is not None else None
    )
    submission.callback_valid_until = callback.valid_until

    if callback.outcome == "definitely_rejected":
        submission.status = "failed"
        workflow.status = "failed"
        _fail_prompt_task_items(db, task, error_code="PROMPT_WORKFLOW_FAILED")
        workflow.updated_at = now
        return CallbackAcceptance(
            status="rejected",
            workflow_run_id=workflow.workflow_run_id,
            workflow_revision=workflow.workflow_revision,
            safe_reason_code=callback.safe_reason_code,
        )
    if callback.outcome == "needs_input":
        if callback.next_question_round == 3:
            submission.status = "failed"
            submission.callback_reason_code = "NEEDS_INPUT_LIMIT"
            submission.callback_round = None
            submission.callback_allowed_fields = None
            submission.callback_locked_fields = None
            submission.callback_valid_until = None
            workflow.status = "failed"
            _fail_prompt_task_items(db, task, error_code="PROMPT_WORKFLOW_FAILED")
            workflow.updated_at = now
            return CallbackAcceptance(
                status="rejected",
                workflow_run_id=workflow.workflow_run_id,
                workflow_revision=workflow.workflow_revision,
                safe_reason_code="NEEDS_INPUT_LIMIT",
            )
        if callback.valid_until is None or callback.valid_until <= now:
            submission.status = "failed"
            submission.callback_reason_code = "PROMPT_QUESTION_EXPIRED"
            submission.callback_round = None
            submission.callback_allowed_fields = None
            submission.callback_locked_fields = None
            submission.callback_valid_until = None
            workflow.status = "failed"
            _fail_prompt_task_items(db, task, error_code="PROMPT_WORKFLOW_FAILED")
            workflow.updated_at = now
            return CallbackAcceptance(
                status="rejected",
                workflow_run_id=workflow.workflow_run_id,
                workflow_revision=workflow.workflow_revision,
                safe_reason_code="PROMPT_QUESTION_EXPIRED",
            )
        submission.status = "callback_received"
        workflow.status = "needs_input"
        task.status = "needs_input"
        task.updated_at = now
        workflow.updated_at = now
        return CallbackAcceptance(
            status="needs_input",
            workflow_run_id=workflow.workflow_run_id,
            workflow_revision=workflow.workflow_revision,
        )

    try:
        _persist_validated_prompt_package(
            db, task=task, workflow=workflow, submission=submission, raw=callback.package
        )
    except (PromptPackageValidationError, PromptWorkflowError, TrustedAnalysisSnapshotError):
        submission.status = "failed"
        submission.callback_reason_code = "PROMPT_PACKAGE_INVALID"
        workflow.status = "failed"
        _fail_prompt_task_items(db, task, error_code="PROMPT_WORKFLOW_FAILED")
        workflow.updated_at = now
        return CallbackAcceptance(
            status="rejected",
            workflow_run_id=workflow.workflow_run_id,
            workflow_revision=workflow.workflow_revision,
            safe_reason_code="PROMPT_PACKAGE_INVALID",
        )
    submission.status = "package_ready"
    workflow.status = "package_ready"
    task.status = "generating"
    task.updated_at = now
    workflow.updated_at = now
    return CallbackAcceptance(
        status="accepted",
        workflow_run_id=workflow.workflow_run_id,
        workflow_revision=workflow.workflow_revision,
    )


def _persist_validated_prompt_package(
    db: Session,
    *,
    task: Task,
    workflow: TaskPromptWorkflow,
    submission: PromptSubmission,
    raw: object,
) -> TaskPromptPackage:
    snapshot = _load_task_pinned_analysis(task)
    _assert_workflow_snapshot(workflow, snapshot)
    if (
        workflow.task_id != task.id
        or submission.task_id != task.id
        or submission.workflow_id != workflow.id
        or submission.workflow_run_id != workflow.workflow_run_id
        or submission.workflow_revision != workflow.workflow_revision
    ):
        raise PromptWorkflowUnavailable

    expected_types = tuple(task.image_types)
    platform = _task_platform(task)
    relation_by_alias = {item.alias: item.relation for item in snapshot.auxiliary_relations}
    legal_aliases = tuple(reference.alias for reference in snapshot.references)
    package = validate_prompt_package(
        raw,
        expected_image_types=expected_types,
        expected_platform=platform,
        legal_reference_ids=legal_aliases,
        auxiliary_relation_by_id=relation_by_alias,
    )
    _validate_relation_details(package, snapshot)

    package_value = package.model_dump(mode="json")
    package_digest = canonical_sha256(package_value)
    existing = db.execute(
        select(TaskPromptPackage)
        .where(TaskPromptPackage.workflow_id == workflow.id)
        .with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.submission_id == submission.submission_id
            and existing.package_digest == package_digest
            and existing.trusted_analysis_snapshot_id == snapshot.snapshot_id
            and existing.trusted_analysis_snapshot_digest == bytes.fromhex(snapshot.snapshot_digest)
        ):
            return existing
        raise PromptWorkflowConflict

    task_items = (
        db.execute(
            select(TaskItem)
            .where(TaskItem.task_id == task.id)
            .order_by(TaskItem.position)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    if (
        len(task_items) != len(package.images)
        or tuple(item.image_type for item in task_items) != expected_types
    ):
        raise PromptWorkflowUnavailable
    references_by_alias = {reference.alias: reference for reference in snapshot.references}
    primary = references_by_alias.get("PRIMARY")
    if primary is None:
        raise PromptWorkflowUnavailable

    item_inputs: list[tuple[TaskItem, PromptImage, list[str], list[str], list[str]]] = []
    for task_item, image in zip(task_items, package.images, strict=True):
        used_aliases = list(image.reference_plan.view_reference_ids)
        excluded_aliases = list(image.reference_plan.excluded_reference_ids)
        if any(
            alias not in references_by_alias
            for alias in ["PRIMARY", *used_aliases, *excluded_aliases]
        ):
            raise PromptWorkflowUnavailable
        if used_aliases != list(relation_by_alias) or excluded_aliases:
            raise PromptPackageValidationError
        allowed_asset_ids = [str(primary.model_asset_id)] + [
            str(references_by_alias[alias].model_asset_id) for alias in used_aliases
        ]
        item_inputs.append((task_item, image, used_aliases, excluded_aliases, allowed_asset_ids))

    stored = TaskPromptPackage(
        workflow_id=workflow.id,
        submission_id=submission.submission_id,
        task_id=task.id,
        workflow_run_id=workflow.workflow_run_id,
        workflow_revision=workflow.workflow_revision,
        trusted_analysis_snapshot_id=snapshot.snapshot_id,
        trusted_analysis_snapshot_digest=bytes.fromhex(snapshot.snapshot_digest),
        chatflow_contract_version=PROMPT_PACKAGE_CONTRACT_VERSION,
        validator_version=PROMPT_PACKAGE_VALIDATOR_VERSION,
        package_digest_version=PROMPT_PACKAGE_DIGEST_VERSION,
        package_digest=package_digest,
        package=package_value,
    )
    db.add(stored)
    db.flush()

    for task_item, image, used_aliases, excluded_aliases, allowed_asset_ids in item_inputs:
        stored_item = TaskPromptItem(
            package_id=stored.id,
            task_id=task.id,
            task_item_id=task_item.id,
            position=task_item.position,
            image_type=image.type,
            design_prompt=image.design_prompt,
            identity_prompt=image.identity_prompt,
            identity_anchor_asset_id=primary.model_asset_id,
            allowed_reference_aliases=["PRIMARY", *used_aliases],
            excluded_reference_aliases=excluded_aliases,
            allowed_reference_asset_ids=allowed_asset_ids,
        )
        db.add(stored_item)
        db.flush()
        enqueue_in_transaction(
            db,
            event_type=GENERATION_START_EVENT_TYPE,
            aggregate_id=task_item.id,
            operation_version=GENERATION_START_OPERATION_VERSION,
            schema_version=GENERATION_START_SCHEMA_VERSION,
            payload={"task_item_id": task_item.id},
        )
    return stored


def _load_task_pinned_analysis(task: Task) -> TrustedAnalysisSnapshot:
    recommendation_snapshot = task.recommendation_snapshot
    if not isinstance(recommendation_snapshot, dict):
        raise PromptWorkflowUnavailable
    raw = recommendation_snapshot.get("trusted_analysis_snapshot")
    snapshot_id = recommendation_snapshot.get("trusted_analysis_snapshot_id")
    snapshot_digest = recommendation_snapshot.get("trusted_analysis_snapshot_digest")
    if (
        not isinstance(raw, dict)
        or not isinstance(snapshot_id, str)
        or not isinstance(snapshot_digest, str)
        or raw.get("snapshot_id") != snapshot_id
        or raw.get("snapshot_digest") != snapshot_digest
        or recommendation_snapshot.get("recommendation_id") != str(task.recommendation_id)
        or recommendation_snapshot.get("revision") != task.recommendation_revision
    ):
        raise PromptWorkflowUnavailable
    try:
        return load_trusted_analysis_snapshot(
            raw,
            reference_set_id=task.reference_set_id,
            recommendation_id=task.recommendation_id,
            recommendation_revision=task.recommendation_revision,
            category=task.category_snapshot,
            expected_references=task.reference_assets_snapshot,
        )
    except TrustedAnalysisSnapshotError:
        raise PromptWorkflowUnavailable from None


def _lock_task_parent_chain(db: Session, task_id: UUID) -> Task | None:
    """Lock the S6 parent chain before callback workflow children.

    The initial task lookup is only a hint; persisted parent IDs are rechecked after
    acquiring User → ReferenceSet → Recommendation → Task locks in contract order.
    """
    hint = db.execute(
        select(Task.owner_user_id, Task.reference_set_id, Task.recommendation_id).where(
            Task.id == task_id
        )
    ).one_or_none()
    if hint is None:
        return None
    owner_id, reference_set_id, recommendation_id = hint
    owner = db.execute(
        select(User).where(User.id == owner_id).with_for_update()
    ).scalar_one_or_none()
    reference_set = db.execute(
        select(ReferenceSet)
        .where(
            ReferenceSet.id == reference_set_id,
            ReferenceSet.owner_user_id == owner_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    recommendation = db.execute(
        select(Recommendation)
        .where(
            Recommendation.id == recommendation_id,
            Recommendation.owner_user_id == owner_id,
            Recommendation.reference_set_id == reference_set_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    task = db.execute(select(Task).where(Task.id == task_id).with_for_update()).scalar_one_or_none()
    if (
        owner is None
        or reference_set is None
        or recommendation is None
        or task is None
        or task.owner_user_id != owner.id
        or task.reference_set_id != reference_set.id
        or task.recommendation_id != recommendation.id
    ):
        return None
    return task


def _initial_submission_payload(task: Task, snapshot: TrustedAnalysisSnapshot) -> dict[str, object]:
    if not isinstance(task.resolved_preferences, dict) or not isinstance(task.image_types, list):
        raise PromptWorkflowUnavailable
    return {
        "category_snapshot": task.category_snapshot,
        "image_types": list(task.image_types),
        "resolved_preferences": json_safe_value(task.resolved_preferences),
        "trusted_analysis_snapshot": snapshot.model_dump(mode="json"),
    }


def _command_from_submission(submission: PromptSubmission) -> PromptSubmissionCommand:
    try:
        return PromptSubmissionCommand.model_validate(
            {
                "task_id": submission.task_id,
                "workflow_run_id": submission.workflow_run_id,
                "workflow_revision": submission.workflow_revision,
                "submission_id": submission.submission_id,
                "phase": submission.phase,
                "followup_round": submission.followup_round,
                "content_digest_version": submission.content_digest_version,
                "content_digest": submission.content_digest,
                "private_payload": submission.private_payload,
            }
        )
    except (TypeError, ValueError):
        raise PromptWorkflowUnavailable from None


def _command_from_action_input(action_input: ActionInput) -> PromptSubmissionCommand:
    payload = _thaw_action_value(action_input.payload)
    if not isinstance(payload, dict):
        raise PromptWorkflowUnavailable
    try:
        command = PromptSubmissionCommand.model_validate(payload)
    except (TypeError, ValueError):
        raise PromptWorkflowUnavailable from None
    if (
        action_input.scope_kind != "submission"
        or action_input.scope_id != command.submission_id
        or action_input.task_id != command.task_id
        or action_input.input_digest_version != command.content_digest_version
        or action_input.input_digest != command.content_digest
    ):
        raise PromptWorkflowUnavailable
    return command


def _thaw_action_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_action_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_action_value(item) for item in value]
    return value


def _action_result_for_submit(result: PromptSubmitResult) -> ActionExecutionResult:
    payload = {"operation": "submit", "result": result.model_dump(mode="python")}
    if result.outcome == "accepted":
        return ActionExecutionResult("accepted", value=payload)
    if result.outcome == "definitely_rejected":
        return ActionExecutionResult(
            "definitely_rejected",
            safe_reason_code=result.safe_reason_code,
            value=payload,
        )
    return ActionExecutionResult(
        "unknown",
        safe_reason_code=result.safe_reason_code or "PROMPT_SUBMIT_UNKNOWN",
        value=payload,
    )


def _result_matches_command(result: PromptSubmitResult, command: PromptSubmissionCommand) -> bool:
    return (
        isinstance(result, PromptSubmitResult)
        and result.task_id == command.task_id
        and result.workflow_run_id == command.workflow_run_id
        and result.workflow_revision == command.workflow_revision
        and result.submission_id == command.submission_id
    )


def _recovery_matches_command(
    result: PromptRecoveryResult, command: PromptSubmissionCommand
) -> bool:
    return (
        isinstance(result, PromptRecoveryResult)
        and result.task_id == command.task_id
        and result.workflow_run_id == command.workflow_run_id
        and result.workflow_revision == command.workflow_revision
        and result.submission_id == command.submission_id
    )


def _submit_result_from_recovery(
    value: Mapping[str, object], command: PromptSubmissionCommand
) -> PromptSubmitResult:
    outcome = value.get("outcome")
    if outcome == "accepted":
        return PromptSubmitResult(
            submission_id=command.submission_id,
            task_id=command.task_id,
            workflow_run_id=command.workflow_run_id,
            workflow_revision=command.workflow_revision,
            outcome="accepted",
            provider_submission_ref=_optional_private_ref(value.get("provider_submission_ref")),
            provider_run_ref=_optional_private_ref(value.get("provider_run_ref")),
        )
    return PromptSubmitResult(
        submission_id=command.submission_id,
        task_id=command.task_id,
        workflow_run_id=command.workflow_run_id,
        workflow_revision=command.workflow_revision,
        outcome="definitely_rejected",
        safe_reason_code="PROMPT_SUBMISSION_REJECTED",
    )


def _optional_private_ref(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _store_submit_result(submission: PromptSubmission, result: PromptSubmitResult) -> None:
    if (
        result.submission_id != submission.submission_id
        or result.task_id != submission.task_id
        or result.workflow_run_id != submission.workflow_run_id
        or result.workflow_revision != submission.workflow_revision
    ):
        raise PromptWorkflowUnavailable
    digest_value = {
        "submission_id": str(result.submission_id),
        "task_id": str(result.task_id),
        "workflow_run_id": str(result.workflow_run_id),
        "workflow_revision": result.workflow_revision,
        "outcome": result.outcome,
        "provider_submission_ref": result.provider_submission_ref,
        "provider_run_ref": result.provider_run_ref,
        "safe_reason_code": result.safe_reason_code,
    }
    submission.submit_outcome = result.outcome
    submission.submit_result_digest_version = PROMPT_RESULT_DIGEST_VERSION
    submission.submit_result_digest = canonical_sha256(digest_value)
    submission.provider_submission_ref = result.provider_submission_ref
    submission.provider_run_ref = result.provider_run_ref
    submission.submit_reason_code = result.safe_reason_code
    submission.updated_at = func.clock_timestamp()


def _mark_prompt_unknown(
    submission: PromptSubmission,
    workflow: TaskPromptWorkflow,
    task: Task,
    reason_code: str | None,
) -> None:
    submission.submit_outcome = "unknown"
    submission.submit_reason_code = reason_code or "PROMPT_SUBMIT_UNKNOWN"
    submission.updated_at = func.clock_timestamp()
    if submission.callback_result_digest is None:
        submission.status = "unknown"
        workflow.status = "reconciling"
        task.status = "reconciling"
        task.updated_at = func.clock_timestamp()
    workflow.updated_at = func.clock_timestamp()


def _fail_prompt_task_items(
    db: Session,
    task: Task,
    *,
    error_code: str,
) -> None:
    """Terminally reject an entire pre-generation task without partial successes."""
    items = (
        db.execute(
            select(TaskItem)
            .where(TaskItem.task_id == task.id)
            .order_by(TaskItem.position)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    if (
        len(items) != task.planned_count
        or tuple(item.position for item in items) != tuple(range(1, task.planned_count + 1))
        or any(item.status != "queued" for item in items)
    ):
        raise PromptWorkflowUnavailable
    attempts = db.scalar(
        select(func.count(TaskItemAttempt.id)).where(
            TaskItemAttempt.task_item_id.in_([item.id for item in items])
        )
    )
    if attempts:
        raise PromptWorkflowUnavailable
    generation_intents = db.scalar(
        select(func.count(OutboxEvent.id)).where(
            OutboxEvent.event_type == GENERATION_START_EVENT_TYPE,
            OutboxEvent.aggregate_id.in_([item.id for item in items]),
        )
    )
    if generation_intents:
        raise PromptWorkflowUnavailable

    now = _db_now(db)
    for item in items:
        item.status = "failed"
        item.error_code = error_code
        item.updated_at = now
    task.status = "failed"
    task.terminal_at = now
    task.expires_at = now + timedelta(days=task.retention_days)
    task.updated_at = now


def _schedule_next_submission_action(
    db: Session,
    current_action: Action,
    submission: PromptSubmission,
    action_kind: str,
) -> Action:
    sequence = current_action.sequence + 1
    existing = db.execute(
        select(Action).where(
            Action.scope_kind == "submission",
            Action.scope_id == submission.submission_id,
            Action.sequence == sequence,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.task_id != submission.task_id
            or existing.action_kind != action_kind
            or existing.input_digest != submission.content_digest
        ):
            raise PromptWorkflowConflict
        return existing
    due_at = _db_now(db) + timedelta(seconds=PROMPT_RECONCILE_DELAY_SECONDS)
    return schedule_action_in_transaction(
        db,
        action_id=uuid4(),
        task_id=submission.task_id,
        scope_kind="submission",
        scope_id=submission.submission_id,
        action_kind=action_kind,
        sequence=sequence,
        due_at=due_at,
        input_digest_version=submission.content_digest_version,
        input_digest=submission.content_digest,
    )


def _schedule_mock_prompt_result_query(
    db: Session,
    current_action: Action,
    submission: PromptSubmission,
    *,
    delay_seconds: int = 0,
) -> Action:
    sequence = current_action.sequence + 1
    existing = db.execute(
        select(Action).where(
            Action.scope_kind == "submission",
            Action.scope_id == submission.submission_id,
            Action.sequence == sequence,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.task_id != submission.task_id
            or existing.action_kind != PROMPT_MOCK_RESULT_QUERY_ACTION_KIND
            or existing.input_digest_version != submission.content_digest_version
            or existing.input_digest != submission.content_digest
        ):
            raise PromptWorkflowConflict
        return existing
    return schedule_action_in_transaction(
        db,
        action_id=uuid4(),
        task_id=submission.task_id,
        scope_kind="submission",
        scope_id=submission.submission_id,
        action_kind=PROMPT_MOCK_RESULT_QUERY_ACTION_KIND,
        sequence=sequence,
        due_at=_db_now(db) + timedelta(seconds=delay_seconds),
        input_digest_version=submission.content_digest_version,
        input_digest=submission.content_digest,
    )


def _schedule_next_mock_prompt_result_query(
    db: Session,
    current_action: Action,
    submission: PromptSubmission,
) -> Action:
    return _schedule_mock_prompt_result_query(
        db,
        current_action,
        submission,
        # Keep the action due slightly beyond ActionProcessor's retry-after fence.
        delay_seconds=PROMPT_MOCK_RESULT_QUERY_RETRY_SECONDS + 1,
    )


def _action_payload_matches_submission(
    action_input: ActionInput,
    command: PromptSubmissionCommand,
) -> bool:
    try:
        saved = _command_from_action_input(action_input)
    except PromptWorkflowError:
        return False
    return (
        saved.submission_id == command.submission_id
        and saved.workflow_run_id == command.workflow_run_id
        and saved.workflow_revision == command.workflow_revision
        and saved.content_digest == command.content_digest
    )


def _prompt_command_matches(
    left: PromptSubmissionCommand,
    right: PromptSubmissionCommand,
) -> bool:
    return (
        left.submission_id == right.submission_id
        and left.task_id == right.task_id
        and left.workflow_run_id == right.workflow_run_id
        and left.workflow_revision == right.workflow_revision
        and left.phase == right.phase
        and left.followup_round == right.followup_round
        and left.content_digest_version == right.content_digest_version
        and left.content_digest == right.content_digest
    )


def _validate_action_input_identity(action: Action, action_input: ActionInput) -> None:
    if (
        action.id != action_input.action_id
        or action.task_id != action_input.task_id
        or action.scope_kind != "submission"
        or action.scope_id != action_input.scope_id
        or action.action_kind != action_input.action_kind
        or action.sequence != action_input.sequence
        or action.input_digest_version != action_input.input_digest_version
        or action.input_digest != action_input.input_digest
    ):
        raise PromptWorkflowUnavailable


def _safe_result_mapping(value: object) -> dict[str, object]:
    try:
        copied = json_safe_value(value)
    except ValueError:
        return {}
    return copied if isinstance(copied, dict) else {}


def _task_platform(task: Task) -> str:
    preferences = task.resolved_preferences
    platform = preferences.get("platform") if isinstance(preferences, dict) else None
    value = platform.get("value") if isinstance(platform, dict) else None
    if not isinstance(value, str):
        raise PromptWorkflowUnavailable
    return value


def _validate_relation_details(
    package: PromptPackage,
    snapshot: TrustedAnalysisSnapshot,
) -> None:
    expected = {
        relation.alias: (
            relation.relation,
            relation.scope,
            relation.view,
            relation.evidence,
        )
        for relation in snapshot.auxiliary_relations
    }
    actual = {
        relation.id: (relation.relation, relation.scope, relation.view, relation.evidence)
        for relation in package.basis.auxiliary_relations
    }
    if actual != expected or len(package.basis.auxiliary_relations) != len(expected):
        raise PromptPackageValidationError
    for image in package.images:
        if (
            tuple(image.reference_plan.view_reference_ids) != tuple(expected)
            or image.reference_plan.excluded_reference_ids
        ):
            raise PromptPackageValidationError


def _assert_workflow_snapshot(
    workflow: TaskPromptWorkflow,
    snapshot: TrustedAnalysisSnapshot,
) -> None:
    if (
        workflow.trusted_analysis_snapshot_id != snapshot.snapshot_id
        or workflow.trusted_analysis_snapshot_digest != bytes.fromhex(snapshot.snapshot_digest)
    ):
        raise PromptWorkflowConflict


def _submission_digest(phase: str, followup_round: int, payload: Mapping[str, object]) -> bytes:
    try:
        return canonical_sha256(
            {
                "phase": phase,
                "followup_round": followup_round,
                "private_payload": json_safe_value(payload),
            }
        )
    except ValueError:
        raise PromptWorkflowUnavailable from None


def _db_now(db: Session) -> datetime:
    now = db.scalar(select(func.clock_timestamp()))
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise PromptWorkflowUnavailable
    return now.astimezone(UTC)
