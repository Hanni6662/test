from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.schemas.prompt_workflow import (
    PROMPT_CONTENT_DIGEST_VERSION,
    PromptRecoveryResult,
    PromptSubmissionCommand,
    PromptSubmitResult,
    canonical_sha256,
)
from app.services import prompt_workflows
from app.services.prompt_workflows import (
    PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND,
    PROMPT_INITIAL_RECOVER_ACTION_KIND,
    PROMPT_INITIAL_SUBMIT_ACTION_KIND,
    execute_initial_prompt_submission,
    finalize_followup_prompt_action,
    finalize_initial_prompt_action,
    prepare_initial_prompt_submission,
    recover_initial_prompt_submission,
)
from app.workers.action_processor import ActionExecutionResult, ActionInput


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _FakeSession:
    def __init__(self, *values: object) -> None:
        self._values = iter(values)

    def execute(self, _statement: object) -> _ScalarResult:
        return _ScalarResult(next(self._values))


class _ScriptedPort:
    def __init__(self) -> None:
        self.submit_calls = 0
        self.recovery_calls = 0

    def submit_preferences(self, command: PromptSubmissionCommand) -> PromptSubmitResult:
        self.submit_calls += 1
        if self.submit_calls == 1:
            outcome = "unknown"
            reason = "SYNTHETIC_SUBMIT_UNKNOWN"
            submission_ref = None
        else:
            outcome = "accepted"
            reason = None
            submission_ref = f"mock:{command.submission_id}"
        return PromptSubmitResult(
            submission_id=command.submission_id,
            task_id=command.task_id,
            workflow_run_id=command.workflow_run_id,
            workflow_revision=command.workflow_revision,
            outcome=outcome,
            provider_submission_ref=submission_ref,
            safe_reason_code=reason,
        )

    def recover_submission(self, command: PromptSubmissionCommand) -> PromptRecoveryResult:
        self.recovery_calls += 1
        return PromptRecoveryResult(
            submission_id=command.submission_id,
            task_id=command.task_id,
            workflow_run_id=command.workflow_run_id,
            workflow_revision=command.workflow_revision,
            outcome="definitely_not_found",
            safe_reason_code="SYNTHETIC_SUBMISSION_ABSENT",
        )


def _command() -> PromptSubmissionCommand:
    private_payload: dict[str, object] = {"synthetic_fixture": True}
    phase_payload = {
        "phase": "initial_preferences",
        "followup_round": 0,
        "private_payload": private_payload,
    }
    return PromptSubmissionCommand(
        task_id=uuid4(),
        workflow_run_id=uuid4(),
        workflow_revision=1,
        submission_id=uuid4(),
        phase="initial_preferences",
        followup_round=0,
        content_digest_version=PROMPT_CONTENT_DIGEST_VERSION,
        content_digest=canonical_sha256(phase_payload),
        private_payload=private_payload,
    )


def _submission(command: PromptSubmissionCommand, workflow_id) -> SimpleNamespace:
    return SimpleNamespace(
        submission_id=command.submission_id,
        workflow_id=workflow_id,
        task_id=command.task_id,
        workflow_run_id=command.workflow_run_id,
        workflow_revision=command.workflow_revision,
        phase=command.phase,
        followup_round=command.followup_round,
        content_digest_version=command.content_digest_version,
        content_digest=command.content_digest,
        private_payload=command.private_payload,
        callback_result_digest=None,
        status="prepared",
    )


def _workflow(command: PromptSubmissionCommand, workflow_id) -> SimpleNamespace:
    return SimpleNamespace(
        id=workflow_id,
        task_id=command.task_id,
        workflow_run_id=command.workflow_run_id,
        workflow_revision=command.workflow_revision,
        status="pending_submission",
    )


def _action(
    command: PromptSubmissionCommand,
    *,
    action_kind: str,
    sequence: int,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        task_id=command.task_id,
        scope_kind="submission",
        scope_id=command.submission_id,
        action_kind=action_kind,
        sequence=sequence,
        input_digest_version=command.content_digest_version,
        input_digest=command.content_digest,
        task_item_id=None,
        attempt_id=None,
    )


@pytest.mark.parametrize("sequence_after_absence", [2, 3])
def test_confirmed_absence_allows_later_submit_sequences_without_blind_resubmit(
    monkeypatch: pytest.MonkeyPatch,
    sequence_after_absence: int,
) -> None:
    command = _command()
    workflow_id = uuid4()
    workflow = _workflow(command, workflow_id)
    submission = _submission(command, workflow_id)
    task = SimpleNamespace(id=command.task_id, status="prompting")
    port = _ScriptedPort()
    scheduled: list[tuple[int, str]] = []

    def capture_schedule(_db, action, _submission, action_kind: str) -> None:
        scheduled.append((action.sequence + 1, action_kind))

    monkeypatch.setattr(prompt_workflows, "_schedule_next_submission_action", capture_schedule)

    first_action = _action(
        command,
        action_kind=PROMPT_INITIAL_SUBMIT_ACTION_KIND,
        sequence=1,
    )
    first_input = prepare_initial_prompt_submission(
        _FakeSession(submission, workflow), first_action
    )
    unknown = execute_initial_prompt_submission(first_input, port)
    assert unknown.outcome == "unknown"
    assert port.submit_calls == 1

    if sequence_after_absence == 3:
        finalize_initial_prompt_action(
            _FakeSession(task, workflow_id, workflow, submission),
            first_action,
            first_input,
            unknown,
        )
        assert scheduled == [(2, PROMPT_INITIAL_RECOVER_ACTION_KIND)]
        recovery_action = _action(
            command,
            action_kind=PROMPT_INITIAL_RECOVER_ACTION_KIND,
            sequence=2,
        )
    else:
        recovery_action = first_action

    recovery_input = (
        first_input
        if recovery_action is first_action
        else prepare_initial_prompt_submission(_FakeSession(submission, workflow), recovery_action)
    )
    recovered_absence = recover_initial_prompt_submission(recovery_input, port)
    assert recovered_absence.outcome == "definitely_rejected"
    assert port.submit_calls == 1
    finalize_initial_prompt_action(
        _FakeSession(task, workflow_id, workflow, submission),
        recovery_action,
        recovery_input,
        recovered_absence,
    )
    assert scheduled[-1] == (
        sequence_after_absence,
        PROMPT_INITIAL_SUBMIT_ACTION_KIND,
    )
    assert port.submit_calls == 1

    next_submit = _action(
        command,
        action_kind=PROMPT_INITIAL_SUBMIT_ACTION_KIND,
        sequence=sequence_after_absence,
    )
    prepared_next_submit = prepare_initial_prompt_submission(
        _FakeSession(submission, workflow), next_submit
    )
    assert execute_initial_prompt_submission(prepared_next_submit, port).outcome == "accepted"
    assert port.submit_calls == 2
    assert port.recovery_calls == 1


@pytest.mark.parametrize("sequence", [2, 3])
def test_prepare_accepts_submit_at_any_later_positive_sequence(sequence: int) -> None:
    command = _command()
    workflow_id = uuid4()
    action = _action(
        command,
        action_kind=PROMPT_INITIAL_SUBMIT_ACTION_KIND,
        sequence=sequence,
    )

    prepared = prepare_initial_prompt_submission(
        _FakeSession(_submission(command, workflow_id), _workflow(command, workflow_id)),
        action,
    )

    assert prepared.action_id == action.id
    assert prepared.sequence == sequence


def test_accepted_submit_finalize_parses_detached_uuid_strings_and_marks_submitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _command()
    workflow_id = uuid4()
    workflow = _workflow(command, workflow_id)
    submission = _submission(command, workflow_id)
    task = SimpleNamespace(id=command.task_id, status="prompting")
    action = _action(
        command,
        action_kind=PROMPT_INITIAL_SUBMIT_ACTION_KIND,
        sequence=1,
    )

    class _AcceptedPort:
        def submit_preferences(self, command: PromptSubmissionCommand) -> PromptSubmitResult:
            return PromptSubmitResult(
                submission_id=command.submission_id,
                task_id=command.task_id,
                workflow_run_id=command.workflow_run_id,
                workflow_revision=command.workflow_revision,
                outcome="accepted",
                provider_submission_ref="mock:accepted",
            )

    scheduled = []
    mock_result_queries = []
    monkeypatch.setattr(
        prompt_workflows,
        "_schedule_next_submission_action",
        lambda *args: scheduled.append(args),
    )
    monkeypatch.setattr(
        prompt_workflows,
        "_schedule_mock_prompt_result_query",
        lambda _db, action, _submission: mock_result_queries.append(action.sequence + 1),
    )
    action_input = prepare_initial_prompt_submission(_FakeSession(submission, workflow), action)
    result = execute_initial_prompt_submission(action_input, _AcceptedPort())
    assert result.outcome == "accepted"

    finalize_initial_prompt_action(
        _FakeSession(task, workflow_id, workflow, submission),
        action,
        action_input,
        result,
        schedule_mock_result_query=True,
    )

    assert submission.status == "accepted"
    assert submission.submit_outcome == "accepted"
    assert workflow.status == "submitted"
    assert not scheduled
    assert mock_result_queries == [2]


def test_accepted_followup_submission_schedules_mock_result_query_in_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, workflow_id, submission_id = uuid4(), uuid4(), uuid4()
    private_payload = {"followup_answers": {"language": {"mode": "B"}}}
    digest = canonical_sha256(
        {
            "phase": "followup_answer",
            "followup_round": 1,
            "private_payload": private_payload,
        }
    )
    command = PromptSubmissionCommand(
        task_id=task_id,
        workflow_run_id=uuid4(),
        workflow_revision=1,
        submission_id=submission_id,
        phase="followup_answer",
        followup_round=1,
        content_digest_version=PROMPT_CONTENT_DIGEST_VERSION,
        content_digest=digest,
        private_payload=private_payload,
    )
    workflow = SimpleNamespace(
        id=workflow_id,
        task_id=task_id,
        workflow_run_id=command.workflow_run_id,
        workflow_revision=command.workflow_revision,
        status="pending_submission",
    )
    submission = SimpleNamespace(
        submission_id=submission_id,
        workflow_id=workflow_id,
        task_id=task_id,
        workflow_run_id=command.workflow_run_id,
        workflow_revision=1,
        phase="followup_answer",
        followup_round=1,
        content_digest_version=command.content_digest_version,
        content_digest=digest,
        private_payload=private_payload,
        callback_result_digest=None,
        status="prepared",
    )
    task = SimpleNamespace(id=task_id, status="prompting")
    action = SimpleNamespace(
        id=uuid4(),
        task_id=task_id,
        scope_kind="submission",
        scope_id=submission_id,
        action_kind=PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND,
        sequence=1,
        input_digest_version=command.content_digest_version,
        input_digest=command.content_digest,
    )
    action_input = ActionInput(
        action_id=action.id,
        task_id=task_id,
        scope_kind="submission",
        scope_id=submission_id,
        action_kind=action.action_kind,
        sequence=1,
        input_digest_version=command.content_digest_version,
        input_digest=command.content_digest,
        payload=command.model_dump(mode="python"),
    )
    accepted = PromptSubmitResult(
        submission_id=submission_id,
        task_id=task_id,
        workflow_run_id=command.workflow_run_id,
        workflow_revision=1,
        outcome="accepted",
        provider_submission_ref="mock:followup",
    )
    scheduled: list[int] = []
    monkeypatch.setattr(
        prompt_workflows,
        "_schedule_mock_prompt_result_query",
        lambda _db, current, _submission: scheduled.append(current.sequence + 1),
    )

    finalize_followup_prompt_action(
        _FakeSession(task, workflow_id, workflow, submission),
        action,
        action_input,
        ActionExecutionResult(
            "accepted",
            value={"operation": "submit", "result": accepted.model_dump(mode="python")},
        ),
        schedule_mock_result_query=True,
    )

    assert submission.status == "accepted"
    assert submission.submit_outcome == "accepted"
    assert workflow.status == "submitted"
    assert scheduled == [2]
