from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
import pytest
from app.integrations.dify.chatflow import ChatflowFirstFormField
from app.integrations.dify.chatflow_v34_http import DifyV34HttpSseAdapter

API_BASE_URL = "https://dify.test"
APP_ID = "v34-test-app"
APP_TOKEN = "secret-app-api-token"
WORKFLOW_ID = "release-v34-test"
USER_ID = "business-session-test"


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> DifyV34HttpSseAdapter:
    return DifyV34HttpSseAdapter(
        API_BASE_URL,
        APP_ID,
        APP_TOKEN,
        WORKFLOW_ID,
        transport=httpx.MockTransport(handler),
    )


def _sse(*events: dict[str, object]) -> str:
    return "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events)


def _form_payload() -> dict[str, object]:
    return {
        "form_content": "PRIVATE-FORM-CONTENT",
        "resolved_default_values": {"style": "PRIVATE-DEFAULT"},
        "inputs": [
            {
                "output_variable_name": "visual_style",
                "type": "select",
                "option_source": {"value": ["fixture-option-1", "fixture-option-2"]},
            },
            {"output_variable_name": "custom_notes", "type": "paragraph"},
        ],
        "user_actions": [
            {"id": "submit-selection", "title": "Continue"},
            {"id": "cancel-selection", "title": "Cancel"},
        ],
        "expiration_time": 1_900_000_000,
    }


_CANCEL_FIELD_LAYOUT = (
    ("choice_1", "select"),
    ("custom_1", "paragraph"),
    ("choice_2", "select"),
    ("choice_3", "select"),
    ("custom_3", "paragraph"),
    ("choice_4", "select"),
    ("custom_4", "paragraph"),
    ("choice_5", "select"),
    ("custom_5", "paragraph"),
    ("image_types", "paragraph"),
)
_CANCEL_SELECT_OPTIONS = {
    "choice_1": (
        "A|||简体中文商品说明",
        "B|||Concise English copy",
        "C|||无文字纯视觉",
        "D|||由视觉审核 Agent 自由设计",
        "custom|||自定义",
    ),
    "choice_2": ("PLATFORM|||TEMU", "PLATFORM|||Amazon"),
    "choice_3": (
        "A|||桌面办公收纳",
        "B|||居家衣柜整理",
        "C|||户外随身收纳",
        "D|||由视觉审核 Agent 自由设计",
        "custom|||自定义",
    ),
    "choice_4": (
        "A|||自然光生活方式",
        "B|||简洁电商棚拍",
        "C|||精致杂志质感",
        "D|||由视觉审核 Agent 自由设计",
        "custom|||自定义",
    ),
    "choice_5": (
        "A|||展示接口与按键",
        "B|||突出轻巧便携",
        "C|||呈现包装与配件",
        "D|||由视觉审核 Agent 自由设计",
        "custom|||自定义",
    ),
}
_CANCEL_FORM_EXPIRATION = 1_900_000_000


def _cancel_expected_fields() -> tuple[ChatflowFirstFormField, ...]:
    return tuple(
        ChatflowFirstFormField(
            output_variable_name=name,
            input_type=input_type,
            option_values=_CANCEL_SELECT_OPTIONS.get(name, ()),
        )
        for name, input_type in _CANCEL_FIELD_LAYOUT
    )


def _cancel_form_definition_digest(fields: tuple[ChatflowFirstFormField, ...]) -> bytes:
    canonical = json.dumps(
        [
            {
                "name": field.output_variable_name,
                "type": field.input_type,
                "options": list(field.option_values),
            }
            for field in fields
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def _cancel_form_payload(
    fields: tuple[ChatflowFirstFormField, ...] | None = None,
    *,
    action_ids: tuple[str, ...] = ("submit", "cancel"),
    expiration_time: int = _CANCEL_FORM_EXPIRATION,
) -> dict[str, object]:
    selected_fields = fields or _cancel_expected_fields()
    raw_inputs: list[dict[str, object]] = []
    for field in selected_fields:
        item: dict[str, object] = {
            "output_variable_name": field.output_variable_name,
            "type": field.input_type,
        }
        if field.input_type == "select":
            item["option_source"] = {"value": list(field.option_values)}
        raw_inputs.append(item)
    return {
        "form_content": "PRIVATE-CANCEL-FORM-CONTENT",
        "resolved_default_values": {"private": "PRIVATE-CANCEL-DEFAULT"},
        "inputs": raw_inputs,
        "user_actions": [{"id": action_id, "title": action_id} for action_id in action_ids],
        "expiration_time": expiration_time,
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://dify.test/api",
        "https://user:password@dify.test",
        "https://dify.test?tenant=other",
        "https://dify.test/#fragment",
    ],
)
def test_adapter_accepts_only_a_fixed_http_origin(url: str) -> None:
    with pytest.raises(ValueError):
        DifyV34HttpSseAdapter(url, APP_ID, APP_TOKEN, WORKFLOW_ID)


def test_client_disables_environment_proxies_and_redirects() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={}))
    try:
        # These httpx settings are part of the transport security boundary.
        assert adapter._client._trust_env is False
        assert adapter._client.follow_redirects is False
    finally:
        adapter.close()


def test_start_sends_fixed_workflow_and_ordered_local_file_ids_without_exposing_sse_text() -> None:
    captured: list[httpx.Request] = []
    private_prompt = "PRIVATE-PROMPT-NEVER-RETURN"
    private_form_token = "PRIVATE-START-FORM-TOKEN"
    form_id = "8b0d5f34-65e5-4d52-82c2-60a17015a30d"

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            text=_sse(
                {
                    "event": "workflow_started",
                    "workflow_run_id": "run-v34-001",
                    "data": {"query": private_prompt},
                },
                {
                    "event": "human_input_required",
                    "workflow_run_id": "run-v34-001",
                    "data": {
                        "node_id": "form_0",
                        "form_id": form_id,
                        "form_token": private_form_token,
                        "form_content": private_prompt,
                    },
                },
            ),
        )

    adapter = _adapter(handle)
    try:
        result = adapter.start_chatflow(
            inputs={"language": "en", "private_prompt": private_prompt},
            query="Generate a product image",
            files=[
                {
                    "type": "image",
                    "transfer_method": "local_file",
                    "upload_file_id": "upload-first",
                },
                {
                    "type": "image",
                    "transfer_method": "local_file",
                    "upload_file_id": "upload-second",
                },
            ],
            user=USER_ID,
        )
    finally:
        adapter.close()

    assert result.outcome == "started"
    assert result.workflow_run_id == "run-v34-001"
    assert private_prompt not in repr(result)
    assert result.form_node_id == "form_0"
    assert result.form_id == form_id
    assert result.form_token == private_form_token
    assert private_form_token not in repr(result)
    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == f"{API_BASE_URL}/v1/chat-messages"
    assert request.headers["Authorization"] == f"Bearer {APP_TOKEN}"
    assert request.headers["Accept"] == "text/event-stream"
    payload = json.loads(request.content)
    assert payload == {
        "inputs": {"language": "en", "private_prompt": private_prompt},
        "query": "Generate a product image",
        "files": [
            {
                "type": "image",
                "transfer_method": "local_file",
                "upload_file_id": "upload-first",
            },
            {
                "type": "image",
                "transfer_method": "local_file",
                "upload_file_id": "upload-second",
            },
        ],
        "response_mode": "streaming",
        "user": USER_ID,
        "auto_generate_name": False,
        "workflow_id": WORKFLOW_ID,
    }


def test_start_without_a_pause_is_unknown_and_keeps_only_the_observed_run_id() -> None:
    adapter = _adapter(
        lambda request: httpx.Response(
            200,
            text=_sse(
                {"event": "workflow_started", "workflow_run_id": "run-no-pause"},
                {
                    "event": "node_started",
                    "workflow_run_id": "run-no-pause",
                    "data": {"node_id": "prepare"},
                },
            ),
        )
    )
    try:
        result = adapter.start_chatflow(inputs={}, query="", files=[], user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "unknown"
    assert result.workflow_run_id == "run-no-pause"
    assert result.form_node_id is None
    assert result.form_id is None
    assert result.form_token is None


def test_start_pause_event_requires_its_own_matching_run_id() -> None:
    adapter = _adapter(
        lambda request: httpx.Response(
            200,
            text=_sse(
                {"event": "workflow_started", "workflow_run_id": "run-original"},
                {
                    "event": "human_input_required",
                    "data": {
                        "node_id": "form_0",
                        "form_id": "8b0d5f34-65e5-4d52-82c2-60a17015a30d",
                        "form_token": "private-token",
                    },
                },
            ),
        )
    )
    try:
        result = adapter.start_chatflow(inputs={}, query="", files=[], user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "unknown"
    assert result.workflow_run_id == "run-original"
    assert result.form_token is None


def test_form_definition_maps_dynamic_fields_and_omits_content_and_defaults() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_form_payload())

    adapter = _adapter(handle)
    try:
        result = adapter.get_form_definition(form_token="private-form-token", user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "available"
    assert [(item.output_variable_name, item.input_type) for item in result.inputs] == [
        ("visual_style", "select"),
        ("custom_notes", "paragraph"),
    ]
    assert result.inputs[0].option_values == ("fixture-option-1", "fixture-option-2")
    assert result.inputs[1].option_values == ()
    assert result.action_ids == ("submit-selection", "cancel-selection")
    assert result.expiration_time == 1_900_000_000
    assert not hasattr(result, "form_content")
    assert not hasattr(result, "resolved_default_values")
    assert "PRIVATE-FORM-CONTENT" not in repr(result)
    assert "PRIVATE-DEFAULT" not in repr(result)
    assert "fixture-option-1" not in repr(result)
    assert "private-form-token" not in repr(result)
    assert len(captured) == 1
    request = captured[0]
    assert request.method == "GET"
    assert request.url.path == "/v1/form/human_input/private-form-token"
    assert dict(request.url.params) == {"user": USER_ID}
    assert request.headers["Authorization"] == f"Bearer {APP_TOKEN}"


def test_form_token_is_not_written_to_http_logs(caplog: pytest.LogCaptureFixture) -> None:
    private_form_token = "PRIVATE-FORM-TOKEN-FOR-LOG-CHECK"
    caplog.set_level(logging.DEBUG, logger="httpx")
    adapter = _adapter(
        lambda request: httpx.Response(
            200,
            json=_form_payload() if request.method == "GET" else {},
        )
    )
    try:
        adapter.get_form_definition(form_token=private_form_token, user=USER_ID)
        adapter.submit_form(
            form_node_id="form_0",
            form_token=private_form_token,
            user=USER_ID,
            action="submit-selection",
            inputs={},
        )
    finally:
        adapter.close()

    assert private_form_token not in caplog.text


def test_httpcore_trace_records_are_suppressed_only_during_adapter_io(
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_form_token = "PRIVATE-FORM-TOKEN-FOR-TRACE-CHECK"
    start_form_id = "8b0d5f34-65e5-4d52-82c2-60a17015a30d"
    trace_loggers = ("httpcore.connection", "httpcore.http11", "httpcore.http2")
    for logger_name in trace_loggers:
        caplog.set_level(logging.DEBUG, logger=logger_name)

    def handle(request: httpx.Request) -> httpx.Response:
        for logger_name in trace_loggers:
            logging.getLogger(logger_name).debug(
                "adapter-request-active url=%s form_token=%s authorization=%s",
                request.url,
                private_form_token,
                APP_TOKEN,
            )
        if request.url.path == "/v1/chat-messages":
            return httpx.Response(
                200,
                text=_sse(
                    {
                        "event": "human_input_required",
                        "workflow_run_id": "run-httpcore-check",
                        "data": {
                            "node_id": "form_0",
                            "form_id": start_form_id,
                            "form_token": private_form_token,
                        },
                    }
                ),
            )
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                text=_sse(
                    {
                        "event": "workflow_finished",
                        "workflow_run_id": "run-httpcore-check",
                    }
                ),
            )
        if request.method == "GET":
            return httpx.Response(200, json=_form_payload())
        return httpx.Response(200, json={})

    adapter = _adapter(handle)
    outside_logger = logging.getLogger("httpcore.http11")
    try:
        outside_logger.debug("outside-adapter-before")
        form_result = adapter.get_form_definition(form_token=private_form_token, user=USER_ID)
        submit_result = adapter.submit_form(
            form_node_id="form_0",
            form_token=private_form_token,
            user=USER_ID,
            action="submit-selection",
            inputs={},
        )
        start_result = adapter.start_chatflow(inputs={}, query="", files=[], user=USER_ID)
        recovery_result = adapter.recover_events(workflow_run_id="run-httpcore-check", user=USER_ID)
        outside_logger.debug("outside-adapter-after")
    finally:
        adapter.close()

    assert form_result.outcome == "available"
    assert submit_result.outcome == "accepted"
    assert start_result.outcome == "started"
    assert recovery_result.outcome == "received"
    assert "outside-adapter-before" in caplog.text
    assert "outside-adapter-after" in caplog.text
    assert "adapter-request-active" not in caplog.text
    assert private_form_token not in caplog.text
    assert APP_TOKEN not in caplog.text


def test_submit_accepts_only_an_empty_json_object_and_sends_one_post() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={})

    adapter = _adapter(handle)
    try:
        result = adapter.submit_form(
            form_node_id="form_0",
            form_token="private-form-token",
            user=USER_ID,
            action="submit-selection",
            inputs={"visual_style": "warm", "custom_notes": "private note"},
        )
    finally:
        adapter.close()

    assert result.outcome == "accepted"
    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/form/human_input/private-form-token"
    assert json.loads(request.content) == {
        "user": USER_ID,
        "action": "submit-selection",
        "inputs": {"visual_style": "warm", "custom_notes": "private note"},
    }


def test_only_form_0_can_be_submitted() -> None:
    requests: list[httpx.Request] = []
    adapter = _adapter(lambda request: requests.append(request) or httpx.Response(200, json={}))
    try:
        with pytest.raises(ValueError, match="only form_0"):
            adapter.submit_form(
                form_node_id="form_1",
                form_token="private-form-token-1",
                user=USER_ID,
                action="submit-selection",
                inputs={},
            )
    finally:
        adapter.close()

    assert requests == []


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (200, {"accepted": True}),
        (200, []),
        (201, {}),
        (302, {}),
        (412, {"error": "form changed"}),
        (500, {"error": "private server detail"}),
        (503, {}),
    ],
)
def test_submit_non_contract_responses_are_unknown_without_retry(
    status_code: int, body: object
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json=body)

    adapter = _adapter(handle)
    try:
        result = adapter.submit_form(
            form_node_id="form_0",
            form_token="private-form-token",
            user=USER_ID,
            action="submit-selection",
            inputs={"visual_style": "warm"},
        )
    finally:
        adapter.close()

    assert result.outcome == "unknown"
    assert len(requests) == 1
    assert "private server detail" not in repr(result)


@pytest.mark.parametrize("status_code", [302, 412, 500, 503])
def test_start_non_200_responses_are_unknown_without_retry(status_code: int) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, text="private error body")

    adapter = _adapter(handle)
    try:
        result = adapter.start_chatflow(inputs={}, query="", files=[], user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "unknown"
    assert len(requests) == 1
    assert "private error body" not in repr(result)


def test_post_network_failure_is_unknown_without_retry_or_exception_text() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("private network diagnostic", request=request)

    adapter = _adapter(handle)
    try:
        result = adapter.submit_form(
            form_node_id="form_0",
            form_token="private-form-token",
            user=USER_ID,
            action="submit-selection",
            inputs={"visual_style": "warm"},
        )
    finally:
        adapter.close()

    assert result.outcome == "unknown"
    assert len(requests) == 1
    assert "private network diagnostic" not in repr(result)


def test_recovery_uses_original_run_get_params_and_only_safe_event_identity_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requested: list[httpx.Request] = []
    private_prompt = "PRIVATE-PROMPT-AND-OUTPUT"
    private_form_token = "PRIVATE-FORM-TOKEN-0"
    hidden_form_token = "PRIVATE-FORM-TOKEN-1"
    form_0_id = "8b0d5f34-65e5-4d52-82c2-60a17015a30d"
    form_1_id = "518e606d-f012-411e-8c49-9d750c6d94a8"

    def handle(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(
            200,
            text=_sse(
                {"event": "ping"},
                {
                    "event": "workflow_started",
                    "workflow_run_id": "run-original-001",
                    "task_id": "task-001",
                    "data": {"query": private_prompt},
                },
                {
                    "event": "node_started",
                    "workflow_run_id": "run-original-001",
                    "data": {"node_id": "prepare", "query": private_prompt},
                },
                {
                    "event": "human_input_required",
                    "workflow_run_id": "run-original-001",
                    "data": {
                        "node_id": "form_0",
                        "form_id": form_0_id,
                        "form_token": private_form_token,
                        "form_content": private_prompt,
                    },
                },
                {
                    "event": "workflow_paused",
                    "workflow_run_id": "run-original-001",
                    "data": {"node_id": "form_0"},
                },
                {
                    "event": "message",
                    "workflow_run_id": "run-original-001",
                    "answer": private_prompt,
                },
                {
                    "event": "human_input_form_filled",
                    "workflow_run_id": "run-original-001",
                    "data": {"node_id": "form_0", "action": "submit"},
                },
                {
                    "event": "human_input_required",
                    "workflow_run_id": "run-original-001",
                    "data": {
                        "node_id": "form_1",
                        "form_id": form_1_id,
                        "form_token": hidden_form_token,
                    },
                },
                {
                    "event": "workflow_paused",
                    "workflow_run_id": "run-original-001",
                    "data": {"node_id": "form_1"},
                },
                {
                    "event": "workflow_finished",
                    "workflow_run_id": "run-original-001",
                    "data": {"outputs": {"prompt": private_prompt}},
                },
            ),
        )

    caplog.set_level(logging.DEBUG)
    adapter = _adapter(handle)
    try:
        result = adapter.recover_events(workflow_run_id="run-original-001", user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "received"
    assert [event.event for event in result.events] == [
        "workflow_started",
        "node_started",
        "human_input_required",
        "workflow_paused",
        "message",
        "human_input_form_filled",
        "human_input_required",
        "workflow_paused",
        "workflow_finished",
    ]
    assert all(event.workflow_run_id == "run-original-001" for event in result.events)
    assert result.events[0].task_id == "task-001"
    assert result.events[1].node_id == "prepare"
    assert result.events[2].node_id == "form_0"
    assert result.events[2].form_id == form_0_id
    assert result.events[2].form_token == private_form_token
    assert result.events[6].node_id == "form_1"
    assert result.events[6].form_id == form_1_id
    assert result.events[6].form_token is None
    representation = repr(result)
    assert private_form_token not in representation
    assert hidden_form_token not in representation
    assert private_prompt not in representation
    assert private_prompt not in caplog.text
    assert len(requested) == 1
    request = requested[0]
    assert request.method == "GET"
    assert request.url.path == "/v1/workflow/run-original-001/events"
    assert parse_qs(request.url.query.decode("ascii")) == {
        "user": [USER_ID],
        "include_state_snapshot": ["true"],
        "continue_on_pause": ["false"],
    }


def test_recovery_rejects_unbounded_continue_on_pause_before_request() -> None:
    requests: list[httpx.Request] = []
    adapter = _adapter(lambda request: requests.append(request) or httpx.Response(200, text=""))
    try:
        with pytest.raises(ValueError, match="finite recovery"):
            adapter.recover_events(
                workflow_run_id="run-original-001",
                user=USER_ID,
                continue_on_pause=True,
            )
    finally:
        adapter.close()

    assert requests == []


def test_recovery_allows_form_filled_before_the_next_human_input_pause() -> None:
    next_form_id = "518e606d-f012-411e-8c49-9d750c6d94a8"
    adapter = _adapter(
        lambda request: httpx.Response(
            200,
            text=_sse(
                {
                    "event": "human_input_form_filled",
                    "workflow_run_id": "run-original-001",
                    "data": {"node_id": "form_0", "action": "submit"},
                },
                {
                    "event": "human_input_required",
                    "workflow_run_id": "run-original-001",
                    "data": {
                        "node_id": "form_1",
                        "form_id": next_form_id,
                        "form_token": "private-form-token-1",
                    },
                },
                {
                    "event": "workflow_paused",
                    "workflow_run_id": "run-original-001",
                    "data": {"node_id": "form_1"},
                },
            ),
        )
    )
    try:
        result = adapter.recover_events(workflow_run_id="run-original-001", user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "received"
    assert [event.event for event in result.events] == [
        "human_input_form_filled",
        "human_input_required",
        "workflow_paused",
    ]
    assert result.events[1].node_id == "form_1"
    assert result.events[1].form_id == next_form_id
    assert result.events[1].form_token is None
    assert result.events[2].event == "workflow_paused"


def test_recovery_old_pause_followed_by_form_filled_and_form_1_eof_is_unknown() -> None:
    form_0_id = "8b0d5f34-65e5-4d52-82c2-60a17015a30d"
    form_1_id = "518e606d-f012-411e-8c49-9d750c6d94a8"
    adapter = _adapter(
        lambda request: httpx.Response(
            200,
            text=_sse(
                {
                    "event": "human_input_required",
                    "workflow_run_id": "run-original-001",
                    "data": {
                        "node_id": "form_0",
                        "form_id": form_0_id,
                        "form_token": "private-form-token-0",
                    },
                },
                {
                    "event": "workflow_paused",
                    "workflow_run_id": "run-original-001",
                    "data": {"node_id": "form_0"},
                },
                {
                    "event": "human_input_form_filled",
                    "workflow_run_id": "run-original-001",
                    "data": {"node_id": "form_0", "action": "submit"},
                },
                {
                    "event": "human_input_required",
                    "workflow_run_id": "run-original-001",
                    "data": {
                        "node_id": "form_1",
                        "form_id": form_1_id,
                        "form_token": "private-form-token-1",
                    },
                },
            ),
        )
    )
    try:
        result = adapter.recover_events(workflow_run_id="run-original-001", user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "unknown"
    assert [event.event for event in result.events] == [
        "human_input_required",
        "workflow_paused",
        "human_input_form_filled",
        "human_input_required",
    ]
    assert result.events[0].node_id == "form_0"
    assert result.events[0].form_id == form_0_id
    assert result.events[0].form_token == "private-form-token-0"
    assert result.events[-1].node_id == "form_1"
    assert result.events[-1].form_id == form_1_id
    assert result.events[-1].form_token is None
    assert "private-form-token" not in repr(result)


def test_recovery_rejects_events_from_a_different_run() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                {
                    "event": "workflow_started",
                    "workflow_run_id": "run-unrelated-999",
                }
            ),
        )

    adapter = _adapter(handle)
    try:
        result = adapter.recover_events(workflow_run_id="run-original-001", user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "unknown"
    assert result.events == ()
    assert "run-unrelated-999" not in repr(result)


@pytest.mark.parametrize("status_code", [302, 412, 500, 503])
def test_recovery_http_errors_are_unknown_without_retry(status_code: int) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, text="PRIVATE-RECOVERY-ERROR")

    adapter = _adapter(handle)
    try:
        result = adapter.recover_events(workflow_run_id="run-original-001", user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "unknown"
    assert len(requests) == 1
    assert "PRIVATE-RECOVERY-ERROR" not in repr(result)


def test_recovery_network_failure_is_unknown_without_exception_text() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("PRIVATE-RECOVERY-NETWORK-ERROR", request=request)

    adapter = _adapter(handle)
    try:
        result = adapter.recover_events(workflow_run_id="run-original-001", user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "unknown"
    assert len(requests) == 1
    assert "PRIVATE-RECOVERY-NETWORK-ERROR" not in repr(result)


def test_recovery_without_terminal_boundary_is_unknown_but_keeps_safe_partial_events() -> None:
    adapter = _adapter(
        lambda request: httpx.Response(
            200,
            text=_sse(
                {
                    "event": "workflow_started",
                    "workflow_run_id": "run-original-001",
                    "task_id": "task-001",
                },
                {
                    "event": "node_started",
                    "workflow_run_id": "run-original-001",
                    "data": {"node_id": "prepare", "prompt": "PRIVATE-PROMPT"},
                },
            ),
        )
    )
    try:
        result = adapter.recover_events(workflow_run_id="run-original-001", user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "unknown"
    assert [event.event for event in result.events] == ["workflow_started", "node_started"]
    assert "PRIVATE-PROMPT" not in repr(result)


@pytest.mark.parametrize("terminal_event", ["workflow_paused", "workflow_failed"])
def test_recovery_accepts_each_terminal_boundary(terminal_event: str) -> None:
    event: dict[str, object] = {
        "event": terminal_event,
        "workflow_run_id": "run-original-001",
    }
    if terminal_event == "workflow_paused":
        event["data"] = {"node_id": "form_0"}
    adapter = _adapter(lambda request: httpx.Response(200, text=_sse(event)))
    try:
        result = adapter.recover_events(workflow_run_id="run-original-001", user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "received"
    assert [item.event for item in result.events] == [terminal_event]


@pytest.mark.parametrize(
    "body",
    [
        "data: not-json\n\n",
        'data: ["not", "an", "object"]\n\n',
        'data: {"workflow_run_id":"run-original-001"}\n\n',
        'data: {"event":"workflow_started"}\n\n',
        ('data: {"event":"node_started","workflow_run_id":"run-original-001","data":{}}\n\n'),
        (
            'data: {"event":"human_input_required","workflow_run_id":"run-original-001",'
            '"data":{"node_id":"form_0","form_id":"form_0"}}\n\n'
        ),
    ],
)
def test_recovery_malformed_sse_fails_closed(body: str) -> None:
    adapter = _adapter(lambda request: httpx.Response(200, text=body))
    try:
        result = adapter.recover_events(workflow_run_id="run-original-001", user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "unknown"
    assert result.events == ()


def test_form_malformed_dynamic_select_fails_closed() -> None:
    payload = _form_payload()
    inputs = payload["inputs"]
    assert isinstance(inputs, list)
    inputs[0]["option_source"] = {"options": ["fixture-option"]}
    adapter = _adapter(lambda request: httpx.Response(200, json=payload))
    try:
        result = adapter.get_form_definition(form_token="private-form-token", user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "unknown"
    assert result.inputs == ()
    assert "fixture-option" not in repr(result)


def test_recovery_unknown_event_fails_closed_without_exposing_its_text() -> None:
    private_text = "PRIVATE-UNKNOWN-EVENT-PAYLOAD"
    adapter = _adapter(
        lambda request: httpx.Response(
            200,
            text=_sse(
                {
                    "event": "future_sensitive_event",
                    "workflow_run_id": "run-original-001",
                    "answer": private_text,
                }
            ),
        )
    )
    try:
        result = adapter.recover_events(workflow_run_id="run-original-001", user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "unknown"
    assert result.events == ()
    assert private_text not in repr(result)


def test_known_message_event_does_not_expose_answer_text() -> None:
    private_text = "PRIVATE-ANSWER-TEXT"
    adapter = _adapter(
        lambda request: httpx.Response(
            200,
            text=_sse(
                {
                    "event": "message",
                    "workflow_run_id": "run-original-001",
                    "answer": private_text,
                },
                {"event": "workflow_finished", "workflow_run_id": "run-original-001"},
            ),
        )
    )
    try:
        result = adapter.recover_events(workflow_run_id="run-original-001", user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "received"
    assert [event.event for event in result.events] == ["message", "workflow_finished"]
    assert private_text not in repr(result)


def test_cancel_fresh_get_posts_exact_ten_empty_fields_once() -> None:
    expected_fields = _cancel_expected_fields()
    expected_digest = _cancel_form_definition_digest(expected_fields)
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.method == "GET":
            assert request.url.path == "/v1/form/human_input/private-v34-cancel-token"
            return httpx.Response(200, json=_cancel_form_payload())
        assert request.method == "POST"
        return httpx.Response(200, json={})

    adapter = _adapter(handle)
    try:
        result = adapter.cancel_before_selection(
            form_node_id="form_0",
            form_token="private-v34-cancel-token",
            user=USER_ID,
            expected_fields=expected_fields,
            expected_form_definition_digest=expected_digest,
            expected_expiration_time=_CANCEL_FORM_EXPIRATION,
        )
    finally:
        adapter.close()

    assert result.outcome == "accepted"
    assert len(captured) == 2
    get_request, post_request = captured
    assert get_request.method == "GET"
    assert get_request.url.params["user"] == USER_ID
    assert post_request.method == "POST"
    assert post_request.url.path == get_request.url.path
    assert json.loads(post_request.content) == {
        "user": USER_ID,
        "action": "cancel",
        "inputs": {name: "" for name, _input_type in _CANCEL_FIELD_LAYOUT},
    }
    post_inputs = json.loads(post_request.content)["inputs"]
    assert list(post_inputs) == [name for name, _input_type in _CANCEL_FIELD_LAYOUT]
    assert len(post_inputs) == 10
    assert all(value == "" for value in post_inputs.values())
    assert "PRIVATE-CANCEL-FORM-CONTENT" not in repr(result)
    assert "PRIVATE-CANCEL-DEFAULT" not in repr(result)


@pytest.mark.parametrize(
    "mismatch",
    [
        "missing_field",
        "extra_field",
        "field_order",
        "field_name",
        "field_type",
        "select_options",
        "cancel_action",
        "definition_digest",
        "expiration_time",
    ],
)
def test_cancel_form_mismatch_is_rejected_before_post(mismatch: str) -> None:
    expected_fields = _cancel_expected_fields()
    expected_digest = _cancel_form_definition_digest(expected_fields)
    form_payload = _cancel_form_payload()
    raw_fields = form_payload["inputs"]
    assert isinstance(raw_fields, list)
    expected_expiration = _CANCEL_FORM_EXPIRATION

    if mismatch == "missing_field":
        raw_fields.pop()
    elif mismatch == "extra_field":
        raw_fields.append({"output_variable_name": "unexpected", "type": "paragraph"})
    elif mismatch == "field_order":
        raw_fields[0], raw_fields[1] = raw_fields[1], raw_fields[0]
    elif mismatch == "field_name":
        raw_fields[0]["output_variable_name"] = "renamed_choice"
    elif mismatch == "field_type":
        raw_fields[0]["type"] = "paragraph"
    elif mismatch == "select_options":
        raw_fields[0]["option_source"]["value"].append("A|||unbound option")
    elif mismatch == "cancel_action":
        form_payload["user_actions"] = [{"id": "submit", "title": "Continue"}]
    elif mismatch == "definition_digest":
        expected_digest = bytes([expected_digest[0] ^ 1]) + expected_digest[1:]
    elif mismatch == "expiration_time":
        expected_expiration += 1

    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=form_payload)

    adapter = _adapter(handle)
    try:
        result = adapter.cancel_before_selection(
            form_node_id="form_0",
            form_token="private-v34-cancel-token",
            user=USER_ID,
            expected_fields=expected_fields,
            expected_form_definition_digest=expected_digest,
            expected_expiration_time=expected_expiration,
        )
    finally:
        adapter.close()

    assert result.outcome == "definitely_rejected"
    expected_methods = [] if mismatch == "definition_digest" else ["GET"]
    assert [request.method for request in requests] == expected_methods


@pytest.mark.parametrize("status_code", [302, 412, 500, 503])
def test_cancel_post_non_200_is_unknown_without_post_replay(status_code: int) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_cancel_form_payload())
        return httpx.Response(status_code, text="PRIVATE-CANCEL-ERROR-BODY")

    adapter = _adapter(handle)
    try:
        result = adapter.cancel_before_selection(
            form_node_id="form_0",
            form_token="private-v34-cancel-token",
            user=USER_ID,
            expected_fields=_cancel_expected_fields(),
            expected_form_definition_digest=_cancel_form_definition_digest(
                _cancel_expected_fields()
            ),
            expected_expiration_time=_CANCEL_FORM_EXPIRATION,
        )
    finally:
        adapter.close()

    assert result.outcome == "unknown"
    assert [request.method for request in requests] == ["GET", "POST"]
    assert "PRIVATE-CANCEL-ERROR-BODY" not in repr(result)


def test_cancel_post_timeout_is_unknown_and_never_replayed() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_cancel_form_payload())
        raise httpx.ReadTimeout("PRIVATE-CANCEL-RESPONSE-LOSS")

    adapter = _adapter(handle)
    try:
        fields = _cancel_expected_fields()
        result = adapter.cancel_before_selection(
            form_node_id="form_0",
            form_token="private-v34-cancel-token",
            user=USER_ID,
            expected_fields=fields,
            expected_form_definition_digest=_cancel_form_definition_digest(fields),
            expected_expiration_time=_CANCEL_FORM_EXPIRATION,
        )
    finally:
        adapter.close()

    assert result.outcome == "unknown"
    assert [request.method for request in requests] == ["GET", "POST"]
    assert "PRIVATE-CANCEL-RESPONSE-LOSS" not in repr(result)


def test_cancel_recovery_is_get_only_on_exact_original_run() -> None:
    requests: list[httpx.Request] = []
    run_id = "run-original-cancel-001"

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text=_sse(
                {
                    "event": "human_input_required",
                    "workflow_run_id": run_id,
                    "data": {
                        "node_id": "form_0",
                        "form_id": "8b0d5f34-65e5-4d52-82c2-60a17015a30d",
                        "form_token": "PRIVATE-RECOVERED-FORM-TOKEN",
                    },
                },
                {"event": "workflow_finished", "workflow_run_id": run_id},
            ),
        )

    adapter = _adapter(handle)
    try:
        result = adapter.recover_cancel_before_selection(workflow_run_id=run_id, user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "received"
    assert [request.method for request in requests] == ["GET"]
    request = requests[0]
    assert request.url.path == f"/v1/workflow/{run_id}/events"
    assert request.url.params["user"] == USER_ID
    assert request.url.params["include_state_snapshot"] == "true"
    assert request.url.params["continue_on_pause"] == "false"
    assert [event.workflow_run_id for event in result.events] == [run_id, run_id]
    assert [event.event for event in result.events] == [
        "human_input_required",
        "workflow_finished",
    ]
    assert all(not hasattr(event, "form_token") for event in result.events)
    assert "PRIVATE-RECOVERED-FORM-TOKEN" not in repr(result)


def test_original_run_observation_accepts_workflow_cancelled_as_terminal_boundary() -> None:
    run_id = "run-original-cancel-terminal-001"
    adapter = _adapter(
        lambda _request: httpx.Response(
            200,
            text=_sse(
                {
                    "event": "workflow_cancelled",
                    "workflow_run_id": run_id,
                }
            ),
        )
    )
    try:
        result = adapter.observe_original_run(workflow_run_id=run_id, user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "received"
    assert [(event.event, event.workflow_run_id) for event in result.events] == [
        ("workflow_cancelled", run_id)
    ]
    assert not hasattr(result.events[0], "form_token")


@pytest.mark.parametrize(
    ("event_name", "status"),
    [
        ("workflow_finished", "failed"),
        ("workflow_finished", "succeeded"),
        ("workflow_failed", "failed"),
    ],
)
def test_original_run_preserves_terminal_status_from_get(event_name: str, status: str) -> None:
    run_id = "run-original-terminal-status-001"
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text=_sse(
                {
                    "event": event_name,
                    "workflow_run_id": run_id,
                    "data": {"status": status, "outputs": {"private": "not exposed"}},
                }
            ),
        )

    adapter = _adapter(handle)
    try:
        raw_result = adapter.recover_events(workflow_run_id=run_id, user=USER_ID)
        observed_result = adapter.observe_original_run(workflow_run_id=run_id, user=USER_ID)
    finally:
        adapter.close()

    assert [request.method for request in requests] == ["GET", "GET"]
    assert raw_result.outcome == observed_result.outcome == "received"
    assert raw_result.events[0].workflow_status == status
    assert observed_result.events[0].workflow_status == status
    assert not hasattr(observed_result.events[0], "form_token")
    assert "not exposed" not in repr(observed_result)


def test_workflow_failed_without_status_retains_terminal_identity() -> None:
    run_id = "run-original-terminal-status-missing"
    adapter = _adapter(
        lambda _request: httpx.Response(
            200,
            text=_sse(
                {
                    "event": "workflow_failed",
                    "workflow_run_id": run_id,
                    "data": {},
                }
            ),
        )
    )
    try:
        result = adapter.observe_original_run(workflow_run_id=run_id, user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "received"
    assert result.events[0].event == "workflow_failed"
    assert result.events[0].workflow_status is None


@pytest.mark.parametrize("event_name", ["workflow_finished", "workflow_failed"])
@pytest.mark.parametrize("invalid_status", [None, 42, False, {"value": "failed"}])
def test_terminal_status_must_be_a_string_when_present(
    event_name: str, invalid_status: object
) -> None:
    run_id = "run-original-terminal-status-invalid"
    adapter = _adapter(
        lambda _request: httpx.Response(
            200,
            text=_sse(
                {
                    "event": event_name,
                    "workflow_run_id": run_id,
                    "data": {"status": invalid_status},
                }
            ),
        )
    )
    try:
        result = adapter.observe_original_run(workflow_run_id=run_id, user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "unknown"
    assert result.events == ()


def test_node_failure_and_pause_do_not_gain_workflow_failed_status() -> None:
    run_id = "run-original-node-failure-pause-001"
    adapter = _adapter(
        lambda _request: httpx.Response(
            200,
            text=_sse(
                {
                    "event": "node_failed",
                    "workflow_run_id": run_id,
                    "data": {"node_id": "design", "status": "failed"},
                },
                {
                    "event": "workflow_paused",
                    "workflow_run_id": run_id,
                    "data": {"node_id": "form_0", "status": "failed"},
                },
            ),
        )
    )
    try:
        result = adapter.observe_original_run(workflow_run_id=run_id, user=USER_ID)
    finally:
        adapter.close()

    assert result.outcome == "received"
    assert [event.event for event in result.events] == ["node_failed", "workflow_paused"]
    assert [event.workflow_status for event in result.events] == [None, None]
