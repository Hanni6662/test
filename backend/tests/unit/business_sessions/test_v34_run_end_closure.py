from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.integrations.dify.chatflow import (
    ChatflowOriginalRunEvent,
    ChatflowOriginalRunObservationResult,
)
from app.schemas.recommendations import IMAGE_TYPES
from app.services.chatflow_actions import _v34_observed_workflow_terminal
from app.services.tasks import TaskService

_RUN_ID = "run-end-unit-run"
_PUBLIC_CODE = "V34_WORKFLOW_ENDED_WITHOUT_GENERATION"
_PUBLIC_MESSAGE = "流程已结束，但未取得生图许可，本次未发起生图。"


@pytest.mark.parametrize(
    ("event_name", "workflow_status", "expected"),
    [
        ("workflow_finished", "partial-succeeded", ("workflow_finished", "partial-succeeded")),
        ("workflow_finished", "succeeded", ("workflow_finished", "succeeded")),
        ("workflow_finished", "failed", ("workflow_finished", "failed")),
        ("workflow_failed", None, ("workflow_failed", "failed")),
        ("workflow_failed", "failed", ("workflow_failed", "failed")),
        ("workflow_finished", "running", None),
        ("workflow_finished", "paused", None),
        ("workflow_finished", "partial-failed", None),
        ("workflow_finished", None, None),
        ("workflow_paused", "paused", None),
        ("node_failed", "failed", None),
    ],
)
def test_run_terminal_classifier_uses_the_exact_workflow_terminal_allowlist(
    event_name: str,
    workflow_status: str | None,
    expected: tuple[str, str] | None,
) -> None:
    result = ChatflowOriginalRunObservationResult(
        "received",
        events=(
            ChatflowOriginalRunEvent(
                event=event_name,
                dify_run_id=_RUN_ID,
                workflow_status=workflow_status,
            ),
        ),
    )

    assert _v34_observed_workflow_terminal(result, expected_run_id=_RUN_ID) == expected


@pytest.mark.parametrize(
    "result",
    [
        ChatflowOriginalRunObservationResult(
            "unknown",
            events=(
                ChatflowOriginalRunEvent(
                    event="workflow_finished",
                    dify_run_id=_RUN_ID,
                    workflow_status="partial-succeeded",
                ),
            ),
        ),
        ChatflowOriginalRunObservationResult(
            "received",
            events=(
                ChatflowOriginalRunEvent(
                    event="workflow_finished",
                    dify_run_id=f"other-{_RUN_ID}",
                    workflow_status="succeeded",
                ),
            ),
        ),
        ChatflowOriginalRunObservationResult(
            "received",
            events=(
                ChatflowOriginalRunEvent(
                    event="workflow_finished",
                    dify_run_id=_RUN_ID,
                    workflow_status="partial-succeeded",
                ),
                ChatflowOriginalRunEvent(
                    event="node_finished",
                    dify_run_id=_RUN_ID,
                    node_id="end",
                ),
            ),
        ),
    ],
)
def test_run_terminal_classifier_rejects_unknown_wrong_run_or_nonfinal_terminal(
    result: ChatflowOriginalRunObservationResult,
) -> None:
    assert _v34_observed_workflow_terminal(result, expected_run_id=_RUN_ID) is None


def test_task_public_projection_uses_the_fixed_no_generation_error_and_hides_preview() -> None:
    task_id = uuid4()
    task = SimpleNamespace(id=task_id, status="failed", planned_count=2)
    items = [
        SimpleNamespace(
            id=uuid4(),
            task_id=task_id,
            position=position,
            image_type=IMAGE_TYPES[position - 1],
            status="failed",
            image_id=None,
            preview_ready=False,
            width=None,
            height=None,
            error_code=_PUBLIC_CODE,
        )
        for position in (1, 2)
    ]

    response = TaskService._to_public_response(task, items)
    body = response.model_dump(mode="json")

    assert body["status"] == "failed"
    assert body["counts"] == {
        "planned": 2,
        "succeeded": 0,
        "failed": 2,
        "pending": 0,
        "unresolved": 0,
    }
    assert all(
        item["error"] == {"code": _PUBLIC_CODE, "message": _PUBLIC_MESSAGE}
        and item["image_id"] is None
        and item["preview_ready"] is False
        and "preview_url" not in item
        for item in body["items"]
    )
