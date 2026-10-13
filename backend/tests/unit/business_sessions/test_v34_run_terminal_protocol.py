from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace

import httpx
import pytest
from app.integrations.dify.chatflow import ChatflowOriginalRunEvent
from app.integrations.dify.chatflow_v34_http import (
    DifyV34HttpSseAdapter,
    DifyV34ObservationResult,
    DifyV34ObservedEvent,
)
from app.integrations.dify.chatflow_v34_port import DifyV34ChatflowPort

API_BASE_URL = "https://dify.test"
APP_ID = "v34-test-app"
APP_TOKEN = "secret-app-api-token"
WORKFLOW_ID = "published-v34-workflow"
USER_ID = "saved-business-session-user"
RUN_ID = "original-dify-run-001"
SSE_HEADERS = {"Content-Type": "text/event-stream; charset=utf-8"}


def _event(name: str, run_id: str = RUN_ID, **fields: object) -> dict[str, object]:
    return {"event": name, "workflow_run_id": run_id, **fields}


def _sse(*events: dict[str, object]) -> str:
    return "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events)


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> DifyV34HttpSseAdapter:
    return DifyV34HttpSseAdapter(
        API_BASE_URL,
        APP_ID,
        APP_TOKEN,
        WORKFLOW_ID,
        transport=httpx.MockTransport(handler),
    )


def _observe(
    body: str,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> DifyV34ObservationResult:
    adapter = _adapter(
        lambda _request: httpx.Response(
            status_code,
            headers=headers if headers is not None else SSE_HEADERS,
            text=body,
        )
    )
    try:
        return adapter.observe_original_run_terminal(workflow_run_id=RUN_ID, user=USER_ID)
    finally:
        adapter.close()


@pytest.mark.parametrize(
    ("event_name", "status"),
    [
        ("workflow_finished", "partial-succeeded"),
        ("workflow_finished", "succeeded"),
        ("workflow_finished", "failed"),
        ("workflow_failed", "failed"),
        ("workflow_failed", None),
    ],
)
def test_original_run_terminal_observer_accepts_only_the_exact_terminal_whitelist(
    event_name: str, status: str | None
) -> None:
    event_fields: dict[str, object] = {
        "data": {"outputs": {"private": "not exposed"}},
    }
    if status is not None:
        event_fields["data"] = {"status": status, "outputs": {"private": "not exposed"}}
    elif event_name == "workflow_failed":
        event_fields["data"] = {"status": None, "outputs": {"private": "not exposed"}}
    adapter = _adapter(
        lambda _request: httpx.Response(
            200,
            headers=SSE_HEADERS,
            text=_sse(_event(event_name, **event_fields)),
        )
    )
    try:
        result = adapter.observe_original_run_terminal(workflow_run_id=RUN_ID, user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "received"
    assert result.events == (
        DifyV34ObservedEvent(
            event=event_name,
            workflow_run_id=RUN_ID,
            workflow_status=status,
        ),
    )
    assert "not exposed" not in repr(result)
    assert not hasattr(result.events[0], "form_token")


def test_original_run_terminal_observer_accepts_workflow_failed_without_status_field() -> None:
    result = _observe(_sse(_event("workflow_failed")))

    assert result.outcome == "received"
    assert result.events == (DifyV34ObservedEvent(event="workflow_failed", workflow_run_id=RUN_ID),)


@pytest.mark.parametrize(
    ("event_name", "status"),
    [
        ("workflow_finished", "running"),
        ("workflow_finished", "partial_success"),
        ("workflow_finished", "succeeded-with-warning"),
        ("workflow_finished", None),
        ("workflow_failed", "succeeded"),
        ("workflow_failed", "partial-succeeded"),
    ],
)
def test_original_run_terminal_observer_rejects_unknown_or_missing_status(
    event_name: str, status: str | None
) -> None:
    result = _observe(_sse(_event(event_name, data={"status": status})))

    assert result.outcome == "unknown"
    assert result.safe_reason_code in {
        "DIFY_TERMINAL_STATUS_UNKNOWN",
        "DIFY_TRANSPORT_OR_WIRE_UNKNOWN",
    }


@pytest.mark.parametrize(
    "events",
    [
        (),
        (_event("node_failed", data={"node_id": "design", "status": "failed"}),),
        (_event("workflow_paused", data={"node_id": "form_0", "status": "failed"}),),
        (
            _event("workflow_finished", data={"status": "succeeded"}),
            _event("node_finished", data={"node_id": "finish"}),
        ),
        (
            _event("workflow_finished", data={"status": "succeeded"}),
            _event("workflow_failed", data={"status": "failed"}),
        ),
    ],
)
def test_original_run_terminal_observer_requires_one_final_workflow_terminal(
    events: tuple[dict[str, object], ...],
) -> None:
    result = _observe(_sse(*events))

    assert result.outcome == "unknown"


def test_original_run_terminal_observer_rejects_any_event_bound_to_another_run() -> None:
    result = _observe(
        _sse(
            _event("node_finished", run_id="other-run", data={"node_id": "design"}),
            _event("workflow_finished", data={"status": "partial-succeeded"}),
        )
    )

    assert result.outcome == "unknown"
    assert result.safe_reason_code == "DIFY_TRANSPORT_OR_WIRE_UNKNOWN"


@pytest.mark.parametrize(
    ("body", "status_code", "headers"),
    [
        ("data: {not-json}\n\n", 200, SSE_HEADERS),
        (
            "data: " + json.dumps(_event("workflow_finished", data={"status": "succeeded"})) + "\n",
            200,
            SSE_HEADERS,
        ),
        (_sse(_event("workflow_finished", data={"status": "succeeded"})), 502, SSE_HEADERS),
        (
            _sse(_event("workflow_finished", data={"status": "succeeded"})),
            200,
            {"Content-Type": "application/json"},
        ),
    ],
)
def test_original_run_terminal_observer_rejects_bad_json_truncation_non_200_and_non_sse(
    body: str, status_code: int, headers: dict[str, str]
) -> None:
    result = _observe(body, status_code=status_code, headers=headers)

    assert result.outcome == "unknown"


def test_original_run_terminal_observer_sends_one_bounded_get_with_original_identity() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers=SSE_HEADERS,
            text=_sse(
                _event(
                    "workflow_finished",
                    data={"status": "partial-succeeded", "outputs": {"prompt": "private"}},
                )
            ),
        )

    adapter = _adapter(handle)
    try:
        result = adapter.observe_original_run_terminal(workflow_run_id=RUN_ID, user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "received"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == f"/v1/workflow/{RUN_ID}/events"
    assert dict(request.url.params.multi_items()) == {
        "user": USER_ID,
        "include_state_snapshot": "true",
        "continue_on_pause": "false",
    }
    assert request.headers["Authorization"] == f"Bearer {APP_TOKEN}"
    assert request.headers["Accept"] == "text/event-stream"
    assert "private" not in repr(result)


def test_original_run_terminal_observer_maps_transport_exception_to_unknown() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection dropped", request=request)

    adapter = _adapter(handle)
    try:
        result = adapter.observe_original_run_terminal(workflow_run_id=RUN_ID, user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "unknown"
    assert result.safe_reason_code == "DIFY_TRANSPORT_OR_WIRE_UNKNOWN"


class _StrictAdapterDouble:
    def __init__(self, result: DifyV34ObservationResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def __enter__(self) -> _StrictAdapterDouble:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def observe_original_run_terminal(self, **kwargs: object) -> DifyV34ObservationResult:
        self.calls.append(kwargs)
        return self.result


def _port_with_adapter(adapter: _StrictAdapterDouble) -> DifyV34ChatflowPort:
    port = DifyV34ChatflowPort(
        api_base_url=API_BASE_URL,
        app_id=APP_ID,
        app_api_token=APP_TOKEN,
        workflow_id=WORKFLOW_ID,
    )
    port._adapter = lambda: adapter  # type: ignore[method-assign]
    return port


def test_port_returns_only_sanitized_structured_partial_success_observation() -> None:
    private_prompt = "PRIVATE-PROMPT"
    private_token = "PRIVATE-FORM-TOKEN"
    adapter = _StrictAdapterDouble(
        DifyV34ObservationResult(
            "received",
            events=(
                SimpleNamespace(
                    event="workflow_finished",
                    workflow_run_id=RUN_ID,
                    workflow_status="partial-succeeded",
                    prompt=private_prompt,
                    form_token=private_token,
                ),
            ),
        )
    )
    port = _port_with_adapter(adapter)

    result = port.observe_original_run(RUN_ID, USER_ID)

    assert result.outcome == "received"
    assert result.events == (
        ChatflowOriginalRunEvent(
            event="workflow_finished",
            dify_run_id=RUN_ID,
            workflow_status="partial-succeeded",
        ),
    )
    assert adapter.calls == [{"workflow_run_id": RUN_ID, "user": USER_ID}]
    assert private_prompt not in repr(result)
    assert private_token not in repr(result)


def test_port_defensively_rejects_an_invalid_received_terminal_from_strict_adapter() -> None:
    adapter = _StrictAdapterDouble(
        DifyV34ObservationResult(
            "received",
            events=(
                DifyV34ObservedEvent(
                    event="workflow_finished",
                    workflow_run_id=RUN_ID,
                    workflow_status="succeeded-with-warning",
                ),
            ),
        )
    )
    port = _port_with_adapter(adapter)

    result = port.observe_original_run(RUN_ID, USER_ID)

    assert result.outcome == "unknown"
    assert result.safe_reason_code == "DIFY_RUN_OBSERVATION_TERMINAL_UNKNOWN"
    assert result.events == ()


def test_port_rejects_contradictory_terminal_events_from_strict_adapter() -> None:
    adapter = _StrictAdapterDouble(
        DifyV34ObservationResult(
            "received",
            events=(
                DifyV34ObservedEvent(
                    event="workflow_finished",
                    workflow_run_id=RUN_ID,
                    workflow_status="succeeded",
                ),
                DifyV34ObservedEvent(
                    event="workflow_failed",
                    workflow_run_id=RUN_ID,
                    workflow_status="failed",
                ),
            ),
        )
    )
    port = _port_with_adapter(adapter)

    result = port.observe_original_run(RUN_ID, USER_ID)

    assert result.outcome == "unknown"
    assert result.events == ()
