from __future__ import annotations

from uuid import uuid4

import pytest
from app.integrations.dify.chatflow import ChatflowOriginalRunEvent
from app.services.chatflow_actions import _v34_cancel_terminal_path
from app.services.chatflow_v34_events import _v34_generation_tracking_exists

_RUN_ID = "bound-dify-run"


def _cancelled_path(*events: ChatflowOriginalRunEvent) -> tuple[ChatflowOriginalRunEvent, ...]:
    return (
        ChatflowOriginalRunEvent(
            event="node_finished", dify_run_id=_RUN_ID, node_id="selection_cancelled_callback"
        ),
        ChatflowOriginalRunEvent(event="node_finished", dify_run_id=_RUN_ID, node_id="cancelled"),
        *events,
        ChatflowOriginalRunEvent(
            event="workflow_finished", dify_run_id=_RUN_ID, workflow_status="succeeded"
        ),
    )


def test_cancel_recovery_requires_cancel_callback_and_cancelled_node() -> None:
    events = _cancelled_path()

    assert _v34_cancel_terminal_path(events, expected_run_id=_RUN_ID) is True


@pytest.mark.parametrize("workflow_status", [None, "failed"])
def test_cancel_recovery_requires_successful_workflow_terminal(
    workflow_status: str | None,
) -> None:
    events = _cancelled_path()[:-1] + (
        ChatflowOriginalRunEvent(
            event="workflow_finished", dify_run_id=_RUN_ID, workflow_status=workflow_status
        ),
    )

    assert _v34_cancel_terminal_path(events, expected_run_id=_RUN_ID) is False


@pytest.mark.parametrize("expired_node", ["selection_expired_callback", "selection_expired"])
def test_expiry_callback_or_answer_never_proves_cancellation(expired_node: str) -> None:
    expiry_only_events = (
        ChatflowOriginalRunEvent(
            event="node_finished", dify_run_id=_RUN_ID, node_id="selection_expired_callback"
        ),
        ChatflowOriginalRunEvent(event="node_finished", dify_run_id=_RUN_ID, node_id=expired_node),
        ChatflowOriginalRunEvent(
            event="workflow_finished", dify_run_id=_RUN_ID, workflow_status="succeeded"
        ),
    )
    mixed_terminal_events = _cancelled_path(
        ChatflowOriginalRunEvent(event="node_finished", dify_run_id=_RUN_ID, node_id=expired_node),
    )

    assert _v34_cancel_terminal_path(expiry_only_events, expected_run_id=_RUN_ID) is False
    assert _v34_cancel_terminal_path(mixed_terminal_events, expected_run_id=_RUN_ID) is False


@pytest.mark.parametrize(
    "entered_node", ["design", "generation_prepare", "generation_submit", "generation_wait"]
)
def test_cancel_recovery_rejects_any_started_design_or_generation_node(entered_node: str) -> None:
    events = _cancelled_path(
        ChatflowOriginalRunEvent(event="node_started", dify_run_id=_RUN_ID, node_id=entered_node),
    )

    assert _v34_cancel_terminal_path(events, expected_run_id=_RUN_ID) is False


def test_generation_action_fallback_matches_h6_and_h7_action_prefix() -> None:
    class Result:
        def __init__(self, row):
            self.row = row

        def first(self):
            return self.row

    class Database:
        def __init__(self) -> None:
            self.calls = 0
            self.statements = []

        def execute(self, statement):
            self.calls += 1
            self.statements.append(statement)
            return Result((uuid4(),) if self.calls == 3 else None)

    db = Database()

    assert _v34_generation_tracking_exists(db, uuid4()) is True
    assert db.calls == 3
    assert "chatflow.generation.%" in db.statements[-1].compile().params.values()
