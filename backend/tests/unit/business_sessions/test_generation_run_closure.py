from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from app.integrations.dify.run_details import HttpDifyRunDetailsPort, normalize_run_detail
from app.services.chatflow_generation_closure import CLOSE_ACTION, execute_close_observation
from app.workers.action_processor import ActionInput, _mark_domain_reconciling


def _input():
    return ActionInput(
        action_id=uuid4(),
        task_id=uuid4(),
        scope_kind="generation_request",
        scope_id=uuid4(),
        action_kind=CLOSE_ACTION,
        sequence=1,
        input_digest_version=1,
        input_digest=b"x" * 32,
        payload={"run_id": str(uuid4()), "app_id": "app", "workflow_id": "workflow", "skip": False},
    )


@pytest.mark.parametrize("status", ["succeeded", "partial-succeeded", "failed", "stopped"])
def test_readonly_details_accept_all_known_terminal_states_without_sse_resume(status):
    run_id = str(uuid4())
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": run_id,
                "workflow_id": "workflow",
                "status": status,
                "error": "Aborted: Maximum execution steps exceeded: 501 > 500"
                if status == "stopped"
                else None,
            },
        )

    port = HttpDifyRunDetailsPort(
        "http://dify", "app", "private-test-token", transport=httpx.MockTransport(handler)
    )
    result = port.get_run(run_id, "app", "workflow")
    assert result["status"] == status
    assert len(result["digest"]) == 32
    assert len(requests) == 1 and requests[0].method == "GET"
    assert requests[0].url.path == f"/v1/workflows/run/{run_id}"
    if status == "stopped":
        assert result["error_code"] == "DIFY_EXECUTION_STEP_LIMIT"
    assert "private-test-token" not in repr(result)


@pytest.mark.parametrize(
    "change",
    [
        {"id": "another-run"},
        {"workflow_id": "another-workflow"},
        {"status": "invented-terminal"},
        {"error": {"unexpected": "shape"}},
    ],
)
def test_mismatched_or_malformed_run_details_are_not_completion_evidence(change):
    body = {"id": "run", "workflow_id": "workflow", "status": "succeeded", **change}
    with pytest.raises(ValueError):
        normalize_run_detail(body, "run", "workflow")


def test_app_scope_mismatch_is_rejected_without_sending_request():
    def forbidden(_request):
        raise AssertionError("cross-app request must not reach HTTP")

    port = HttpDifyRunDetailsPort(
        "http://dify", "app", "private-token", transport=httpx.MockTransport(forbidden)
    )
    with pytest.raises(ValueError):
        port.get_run(str(uuid4()), "another-app", "workflow")


def test_read_failure_is_accepted_as_observation_failure_not_generation_failure():
    class FailingPort:
        def get_run(self, *_args):
            raise httpx.ReadTimeout("temporary connection problem")

    result = execute_close_observation(FailingPort(), _input())
    assert result.outcome == "accepted"
    assert result.safe_reason_code == "DIFY_RUN_DETAIL_UNAVAILABLE"
    assert result.value == {"queried": True}


def test_lost_observation_lease_cannot_change_completed_business_results():
    task = SimpleNamespace(status="succeeded")
    session = SimpleNamespace(status="task_created")
    _mark_domain_reconciling(task, None, None, business_session=session, action_kind=CLOSE_ACTION)
    assert task.status == "succeeded" and session.status == "task_created"
