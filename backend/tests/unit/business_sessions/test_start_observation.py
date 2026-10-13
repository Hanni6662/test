from uuid import uuid4

import pytest
from app.integrations.dify.chatflow import (
    ChatflowOriginalRunEvent,
    ChatflowOriginalRunObservationResult,
)
from app.services.chatflow_actions import (
    CHATFLOW_START_OBSERVATION_ACTION,
    execute_chatflow_start_observation,
)
from app.workers.action_processor import ActionInput


def action(sequence=25, **payload):
    session = uuid4()
    return ActionInput(
        action_id=uuid4(),
        task_id=None,
        scope_kind="business_session",
        scope_id=session,
        business_session_id=session,
        action_kind=CHATFLOW_START_OBSERVATION_ACTION,
        sequence=sequence,
        input_digest_version=1,
        input_digest=b"x" * 32,
        payload={"dify_run_id": "original-run", "dify_user_id": "owner", **payload},
    )


class Port:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def observe_original_run(self, run, user):
        self.calls.append((run, user))
        return self.result


def test_observer_accepts_only_matching_original_run_failure():
    port = Port(
        ChatflowOriginalRunObservationResult(
            "received",
            events=(
                ChatflowOriginalRunEvent(
                    event="workflow_finished", dify_run_id="original-run", workflow_status="failed"
                ),
            ),
        )
    )
    result = execute_chatflow_start_observation(port, action())
    assert result.value == {"status": "failed"}
    assert port.calls == [("original-run", "owner")]


@pytest.mark.parametrize(
    "observed",
    [
        None,
        ChatflowOriginalRunObservationResult("unknown"),
        ChatflowOriginalRunObservationResult(
            "received",
            events=(
                ChatflowOriginalRunEvent(event="workflow_failed", dify_run_id="different-run"),
            ),
        ),
        ChatflowOriginalRunObservationResult(
            "received",
            events=(ChatflowOriginalRunEvent(event="node_failed", dify_run_id="original-run"),),
        ),
    ],
)
def test_unknown_observation_is_bounded_and_never_resubmits(observed):
    port = Port(observed)
    assert execute_chatflow_start_observation(port, action()).outcome == "safe_retry"
    last = execute_chatflow_start_observation(port, action(27))
    assert last.outcome == "accepted"
    assert last.safe_reason_code == "V34_START_OBSERVATION_EXHAUSTED"
    assert last.value is None


def test_finished_or_submitted_sessions_do_not_observe():
    port = Port(None)
    assert (
        execute_chatflow_start_observation(port, action(skip_observation=True)).outcome
        == "accepted"
    )
    assert port.calls == []
