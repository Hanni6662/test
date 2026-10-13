from __future__ import annotations

from uuid import uuid4

import pytest
from app.core.domain_errors import DomainError
from app.integrations.dify.contracts import DifyNotConfigured
from app.schemas.prompt_workflow import (
    PROMPT_CONTENT_DIGEST_VERSION,
    PROMPT_RESULT_DIGEST_VERSION,
    CallbackAcceptance,
    PromptCallback,
    PromptSubmissionCommand,
    canonical_sha256,
    prompt_callback_digest_payload,
)
from app.services.prompt_workflows import (
    PROMPT_MOCK_RESULT_QUERY_ACTION_KIND,
    PROMPT_MOCK_RESULT_QUERY_MAX_ATTEMPTS,
    PROMPT_MOCK_RESULT_QUERY_RETRY_SECONDS,
    build_prompt_action_registrations,
    execute_mock_prompt_result_query,
)
from app.workers.action_processor import ActionInput


def _command() -> PromptSubmissionCommand:
    private_payload = {"synthetic_fixture": True}
    return PromptSubmissionCommand(
        task_id=uuid4(),
        workflow_run_id=uuid4(),
        workflow_revision=1,
        submission_id=uuid4(),
        phase="initial_preferences",
        followup_round=0,
        content_digest_version=PROMPT_CONTENT_DIGEST_VERSION,
        content_digest=canonical_sha256(
            {
                "phase": "initial_preferences",
                "followup_round": 0,
                "private_payload": private_payload,
            }
        ),
        private_payload=private_payload,
    )


def _callback(command: PromptSubmissionCommand) -> PromptCallback:
    callback = PromptCallback(
        task_id=command.task_id,
        workflow_run_id=command.workflow_run_id,
        workflow_revision=command.workflow_revision,
        submission_id=command.submission_id,
        submission_content_digest_version=command.content_digest_version,
        submission_content_digest=command.content_digest,
        result_digest_version=PROMPT_RESULT_DIGEST_VERSION,
        result_digest=bytes(32),
        outcome="completed",
        package={"synthetic_fixture": True},
    )
    return callback.model_copy(
        update={"result_digest": canonical_sha256(prompt_callback_digest_payload(callback))}
    )


def _query_input(command: PromptSubmissionCommand, attempt: int) -> ActionInput:
    return ActionInput(
        action_id=uuid4(),
        task_id=command.task_id,
        scope_kind="submission",
        scope_id=command.submission_id,
        action_kind=PROMPT_MOCK_RESULT_QUERY_ACTION_KIND,
        sequence=attempt + 1,
        input_digest_version=command.content_digest_version,
        input_digest=command.content_digest,
        payload={"command": command.model_dump(mode="python"), "query_attempt": attempt},
    )


class _AvailableResultPort:
    def __init__(self, callback: PromptCallback) -> None:
        self.callback = callback

    def read_prompt_result(self, submission_id):
        assert submission_id == self.callback.submission_id
        return self.callback


class _CallbackAcceptor:
    def __init__(self, error: DomainError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[PromptCallback, str]] = []

    def accept(self, callback: PromptCallback, *, phase: str) -> CallbackAcceptance:
        self.calls.append((callback, phase))
        if self.error is not None:
            raise self.error
        return CallbackAcceptance(
            status="accepted",
            workflow_run_id=callback.workflow_run_id,
            workflow_revision=callback.workflow_revision,
        )


def test_result_query_uses_shared_callback_acceptance_application_path() -> None:
    command = _command()
    callback = _callback(command)
    acceptor = _CallbackAcceptor()

    result = execute_mock_prompt_result_query(
        _query_input(command, 1), _AvailableResultPort(callback), acceptor
    )

    assert result.outcome == "accepted"
    assert acceptor.calls == [(callback, "initial_preferences")]


class _UnavailableResultPort:
    def read_prompt_result(self, _submission_id):
        raise DifyNotConfigured("synthetic result is not ready")


@pytest.mark.parametrize("attempt", range(1, PROMPT_MOCK_RESULT_QUERY_MAX_ATTEMPTS))
def test_result_query_uses_bounded_durable_retry_result(attempt: int) -> None:
    command = _command()
    result = execute_mock_prompt_result_query(
        _query_input(command, attempt), _UnavailableResultPort(), _CallbackAcceptor()
    )

    assert result.outcome == "safe_retry"
    assert result.retry_after_seconds == PROMPT_MOCK_RESULT_QUERY_RETRY_SECONDS
    assert result.safe_reason_code == "PROMPT_RESULT_NOT_READY"


def test_result_query_stops_after_maximum_attempts() -> None:
    command = _command()
    result = execute_mock_prompt_result_query(
        _query_input(command, PROMPT_MOCK_RESULT_QUERY_MAX_ATTEMPTS),
        _UnavailableResultPort(),
        _CallbackAcceptor(),
    )

    assert result.outcome == "unknown"
    assert result.retry_after_seconds is None
    assert result.safe_reason_code == "PROMPT_RESULT_QUERY_MAX_ATTEMPTS"


def test_conflicting_callback_digest_is_rejected_without_replacing_existing_state() -> None:
    command = _command()
    callback = _callback(command)
    acceptor = _CallbackAcceptor(
        DomainError(409, "PROMPT_CALLBACK_CONFLICT", "safe synthetic conflict")
    )

    result = execute_mock_prompt_result_query(
        _query_input(command, 1), _AvailableResultPort(callback), acceptor
    )

    assert result.outcome == "definitely_rejected"
    assert result.safe_reason_code == "PROMPT_RESULT_DIGEST_CONFLICT"


def test_mock_result_query_action_is_only_registered_when_explicitly_enabled() -> None:
    class _Port:
        pass

    assert PROMPT_MOCK_RESULT_QUERY_ACTION_KIND not in build_prompt_action_registrations(_Port())
    enabled = build_prompt_action_registrations(
        _Port(), mock_result_callback_service=_CallbackAcceptor()
    )
    assert PROMPT_MOCK_RESULT_QUERY_ACTION_KIND in enabled
