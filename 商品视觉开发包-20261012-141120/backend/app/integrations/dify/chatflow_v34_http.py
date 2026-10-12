from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from app.integrations.dify.chatflow import ChatflowFirstFormField

_MAX_SSE_LINE_BYTES = 1_000_000
_MAX_SSE_RESPONSE_BYTES = 16_000_000
_MAX_SSE_EVENT_COUNT = 10_000
_MAX_HTTP_JSON_BODY_BYTES = 2_000_000
_FORM_TOKEN_PATH_PATTERN = re.compile(r"(/v1/form/human_input/)([^/?#\s\"<>]+)")
_HTTPCORE_ADAPTER_IO_ACTIVE: ContextVar[bool] = ContextVar(
    "dify_v34_httpcore_adapter_io_active", default=False
)
_HTTPCORE_LOGGER_NAMES = (
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)
_SAFE_IDENTITY_EVENTS = frozenset(
    {
        "error",
        "human_input_form_filled",
        "human_input_required",
        "human_input_form_timeout",
        "iteration_started",
        "iteration_next",
        "iteration_completed",
        "iteration_failed",
        "loop_started",
        "loop_next",
        "loop_completed",
        "loop_failed",
        "message",
        "message_end",
        "message_file",
        "message_replace",
        "node_failed",
        "node_finished",
        "node_retry",
        "node_started",
        "parallel_branch_started",
        "parallel_branch_finished",
        "reasoning_chunk",
        "text_chunk",
        "text_replace",
        "tts_message",
        "tts_message_end",
        "workflow_started",
        "workflow_finished",
        "workflow_failed",
        "workflow_cancelled",
        "workflow_paused",
        "datasource_processing",
        "datasource_completed",
        "datasource_error",
    }
)
_CANCEL_FORM_FIELDS = (
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


class _HttpxFormTokenRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "httpx":
            return True
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            if "HTTP Request:" in str(record.msg) and "/v1/form/human_input/" in str(record.msg):
                record.msg = "HTTP Request: <redacted Dify form request>"
                record.args = ()
            return True
        if "HTTP Request:" not in message:
            return True
        safe_message = _FORM_TOKEN_PATH_PATTERN.sub(r"\1<redacted>", message)
        if safe_message != message:
            record.msg = safe_message
            record.args = ()
        elif "/v1/form/human_input/" in message:
            record.msg = "HTTP Request: <redacted Dify form request>"
            record.args = ()
        return True


class _HttpcoreAdapterTraceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not (_HTTPCORE_ADAPTER_IO_ACTIVE.get() and record.levelno <= logging.DEBUG)


_HTTPX_FORM_TOKEN_REDACTION_FILTER = _HttpxFormTokenRedactionFilter()
_HTTPCORE_ADAPTER_TRACE_FILTER = _HttpcoreAdapterTraceFilter()
_LOG_FILTER_INSTALL_LOCK = threading.Lock()


@contextmanager
def _httpcore_adapter_io_context() -> Iterator[None]:
    context_token = _HTTPCORE_ADAPTER_IO_ACTIVE.set(True)
    try:
        yield
    finally:
        _HTTPCORE_ADAPTER_IO_ACTIVE.reset(context_token)


def _install_transport_log_filters() -> None:
    with _LOG_FILTER_INSTALL_LOCK:
        logger = logging.getLogger("httpx")
        if _HTTPX_FORM_TOKEN_REDACTION_FILTER not in logger.filters:
            logger.addFilter(_HTTPX_FORM_TOKEN_REDACTION_FILTER)
        for logger_name in _HTTPCORE_LOGGER_NAMES:
            logger = logging.getLogger(logger_name)
            if _HTTPCORE_ADAPTER_TRACE_FILTER not in logger.filters:
                logger.addFilter(_HTTPCORE_ADAPTER_TRACE_FILTER)


@dataclass(frozen=True, slots=True)
class DifyV34StartResult:
    outcome: Literal["started", "unknown"]
    workflow_run_id: str | None = None
    form_node_id: str | None = None
    form_id: str | None = None
    form_token: str | None = field(default=None, repr=False)
    safe_reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class DifyV34FormInput:
    output_variable_name: str
    input_type: str
    option_values: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class DifyV34FormResult:
    outcome: Literal["available", "unknown"]
    inputs: tuple[DifyV34FormInput, ...] = ()
    action_ids: tuple[str, ...] = ()
    expiration_time: int | None = None
    safe_reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class DifyV34SubmitResult:
    outcome: Literal["accepted", "unknown"]
    safe_reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class DifyV34CancelResult:
    outcome: Literal["accepted", "definitely_rejected", "unknown"]
    safe_reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class DifyV34Event:
    event: str
    workflow_run_id: str
    task_id: str | None = None
    node_id: str | None = None
    form_id: str | None = None
    form_token: str | None = field(default=None, repr=False)
    workflow_status: str | None = None


@dataclass(frozen=True, slots=True)
class DifyV34EventsResult:
    outcome: Literal["received", "unknown"]
    events: tuple[DifyV34Event, ...] = ()
    safe_reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class DifyV34ObservedEvent:
    event: str
    workflow_run_id: str
    node_id: str | None = None
    form_id: str | None = None
    workflow_status: str | None = None


@dataclass(frozen=True, slots=True)
class DifyV34ObservationResult:
    outcome: Literal["received", "unknown"]
    events: tuple[DifyV34ObservedEvent, ...] = ()
    safe_reason_code: str | None = None


class _InvalidWireShape(ValueError):
    pass


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise _InvalidWireShape(f"invalid {field_name}")
    return value


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field_name)


def _sse_json_objects(response: httpx.Response) -> Iterator[dict[str, object]]:
    total_bytes = 0
    event_count = 0
    data_lines: list[str] = []
    for line in response.iter_lines():
        line_size = len(line.encode("utf-8"))
        total_bytes += line_size + 1
        if line_size > _MAX_SSE_LINE_BYTES or total_bytes > _MAX_SSE_RESPONSE_BYTES:
            raise _InvalidWireShape("SSE response exceeded the adapter limit")
        if line == "":
            if not data_lines:
                continue
            raw_data = "\n".join(data_lines)
            data_lines.clear()
            if not raw_data:
                raise _InvalidWireShape("empty SSE data")
            try:
                value = json.loads(raw_data)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise _InvalidWireShape("invalid SSE JSON") from exc
            if not isinstance(value, dict):
                raise _InvalidWireShape("SSE JSON must be an object")
            event_name = value.get("event")
            if not isinstance(event_name, str) or not event_name:
                raise _InvalidWireShape("SSE event name is missing")
            event_count += 1
            if event_count > _MAX_SSE_EVENT_COUNT:
                raise _InvalidWireShape("SSE event count exceeded the adapter limit")
            yield value
            continue
        if line.startswith("data:"):
            raw_data = line[5:]
            if raw_data.startswith(" "):
                raw_data = raw_data[1:]
            data_lines.append(raw_data)
    if data_lines:
        raise _InvalidWireShape("SSE stream ended inside an event")


def _read_limited_response(response: httpx.Response) -> bytes:
    body = bytearray()
    for chunk in response.iter_bytes():
        if len(body) + len(chunk) > _MAX_HTTP_JSON_BODY_BYTES:
            raise _InvalidWireShape("HTTP JSON body exceeded the adapter limit")
        body.extend(chunk)
    return bytes(body)


def _form_definition_digest(fields: Sequence[ChatflowFirstFormField]) -> bytes:
    canonical = json.dumps(
        [
            {
                "name": item.output_variable_name,
                "type": item.input_type,
                "options": list(item.option_values),
            }
            for item in fields
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def _cancel_expected_fields_are_valid(
    fields: object, expected_digest: object, expected_expiration_time: object
) -> bool:
    if not isinstance(fields, tuple) or len(fields) != len(_CANCEL_FORM_FIELDS):
        return False
    if not isinstance(expected_digest, bytes) or len(expected_digest) != 32:
        return False
    if (
        isinstance(expected_expiration_time, bool)
        or not isinstance(expected_expiration_time, int)
        or expected_expiration_time <= 0
    ):
        return False
    for item, (expected_name, expected_type) in zip(fields, _CANCEL_FORM_FIELDS, strict=True):
        if not isinstance(item, ChatflowFirstFormField):
            return False
        if item.output_variable_name != expected_name or item.input_type != expected_type:
            return False
        if not isinstance(item.option_values, tuple):
            return False
        if any(
            not isinstance(option, str) or not option or option.strip() != option
            for option in item.option_values
        ):
            return False
        if len(item.option_values) != len(set(item.option_values)):
            return False
        if expected_type == "select" and not item.option_values:
            return False
        if expected_type == "paragraph" and item.option_values:
            return False
    return _form_definition_digest(fields) == expected_digest


def _form_option_values(input_item: Mapping[str, object], input_type: str) -> tuple[str, ...]:
    if input_type == "paragraph":
        return ()
    if input_type != "select":
        raise _InvalidWireShape("unsupported form input type")
    option_source = input_item.get("option_source")
    if not isinstance(option_source, dict):
        raise _InvalidWireShape("select input option source is missing")
    raw_options = option_source.get("value")
    if not isinstance(raw_options, list):
        raise _InvalidWireShape("select input options are malformed")
    if any(
        not isinstance(option, str) or not option or option.strip() != option
        for option in raw_options
    ):
        raise _InvalidWireShape("select input options are malformed")
    if len(set(raw_options)) != len(raw_options):
        raise _InvalidWireShape("select input options contain duplicates")
    return tuple(raw_options)


def _parse_form_payload(
    payload: object,
) -> tuple[tuple[DifyV34FormInput, ...], tuple[str, ...], int]:
    if not isinstance(payload, dict):
        raise _InvalidWireShape("form response is not an object")
    if not isinstance(payload.get("form_content"), str):
        raise _InvalidWireShape("form content field is malformed")
    if not isinstance(payload.get("resolved_default_values"), dict):
        raise _InvalidWireShape("form defaults field is malformed")
    raw_inputs = payload.get("inputs")
    raw_actions = payload.get("user_actions")
    expires_at = payload.get("expiration_time")
    if not isinstance(raw_inputs, list) or not isinstance(raw_actions, list):
        raise _InvalidWireShape("form inputs or actions are malformed")
    if isinstance(expires_at, bool) or not isinstance(expires_at, int):
        raise _InvalidWireShape("form expiry is malformed")

    inputs: list[DifyV34FormInput] = []
    seen_names: set[str] = set()
    for item in raw_inputs:
        if not isinstance(item, dict):
            raise _InvalidWireShape("form input is malformed")
        output_name = _required_string(item.get("output_variable_name"), "field name")
        input_type = _required_string(item.get("type"), "field type")
        if input_type not in {"select", "paragraph"}:
            raise _InvalidWireShape("unsupported form input type")
        if output_name in seen_names:
            raise _InvalidWireShape("duplicate form field name")
        seen_names.add(output_name)
        inputs.append(
            DifyV34FormInput(
                output_variable_name=output_name,
                input_type=input_type,
                option_values=_form_option_values(item, input_type),
            )
        )

    action_ids: list[str] = []
    seen_actions: set[str] = set()
    for action in raw_actions:
        if not isinstance(action, dict):
            raise _InvalidWireShape("form action is malformed")
        action_id = _required_string(action.get("id"), "action id")
        if action_id in seen_actions:
            raise _InvalidWireShape("duplicate form action id")
        seen_actions.add(action_id)
        action_ids.append(action_id)
    return tuple(inputs), tuple(action_ids), expires_at


def _parse_identity_event(
    payload: Mapping[str, object],
    expected_run_id: str,
    *,
    allow_null_workflow_failed_status: bool = False,
) -> DifyV34Event:
    event_name = _required_string(payload.get("event"), "event name")
    run_id = _required_string(payload.get("workflow_run_id"), "workflow run id")
    if run_id != expected_run_id:
        raise _InvalidWireShape("event run id does not match requested run")
    task_id = _optional_string(payload.get("task_id"), "task id")
    node_id: str | None = None
    form_id: str | None = None
    form_token: str | None = None
    workflow_status: str | None = None
    if event_name in {"workflow_finished", "workflow_failed"} and "data" in payload:
        terminal_data = payload.get("data")
        if not isinstance(terminal_data, dict):
            raise _InvalidWireShape("workflow terminal event data is malformed")
        if "status" in terminal_data:
            status_value = terminal_data["status"]
            if (
                status_value is None
                and event_name == "workflow_failed"
                and allow_null_workflow_failed_status
            ):
                workflow_status = None
            else:
                workflow_status = _required_string(status_value, "workflow status")
    if event_name in {
        "human_input_form_filled",
        "human_input_form_timeout",
        "datasource_processing",
        "datasource_completed",
        "datasource_error",
        "node_started",
        "node_finished",
        "node_failed",
        "node_retry",
        "workflow_paused",
    }:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise _InvalidWireShape("event data is malformed")
        node_id = _optional_string(data.get("node_id"), "node id")
        if (
            event_name
            in {
                "node_started",
                "node_finished",
                "node_failed",
                "node_retry",
                "human_input_form_filled",
            }
            and node_id is None
        ):
            raise _InvalidWireShape("node event has no node id")
    elif event_name == "human_input_required":
        data = payload.get("data")
        if not isinstance(data, dict):
            raise _InvalidWireShape("human input event data is malformed")
        node_id = _required_string(data.get("node_id"), "form node id")
        form_id = _required_string(data.get("form_id"), "form id")
        try:
            UUID(form_id)
        except ValueError as exc:
            raise _InvalidWireShape("Dify form id is malformed") from exc
        if node_id == "form_0":
            form_token = _required_string(data.get("form_token"), "form token")
    return DifyV34Event(
        event=event_name,
        workflow_run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        form_id=form_id,
        form_token=form_token,
        workflow_status=workflow_status,
    )


class DifyV34HttpSseAdapter:
    """Synchronous, one-shot Dify 1.17.1 transport for the v3.4 Chatflow."""

    def __init__(
        self,
        api_base_url: str,
        app_id: str,
        app_api_token: str,
        workflow_id: str,
        timeout_seconds: float = 30.0,
        max_stream_seconds: float = 300.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parsed_url = urlsplit(api_base_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.path not in {"", "/"}
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("api_base_url must be a fixed HTTP(S) origin")
        if not app_id or app_id.strip() != app_id:
            raise ValueError("app_id must be a non-empty identifier")
        if not app_api_token or app_api_token.strip() != app_api_token:
            raise ValueError("app_api_token must be non-empty")
        if not workflow_id or workflow_id.strip() != workflow_id:
            raise ValueError("workflow_id must be a non-empty identifier")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        if not math.isfinite(max_stream_seconds) or max_stream_seconds <= 0:
            raise ValueError("max_stream_seconds must be positive and finite")

        _install_transport_log_filters()
        self._api_base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
        self._app_id = app_id
        self._workflow_id = workflow_id
        self._max_stream_seconds = max_stream_seconds
        self._client = httpx.Client(
            base_url=self._api_base_url,
            headers={
                "Authorization": f"Bearer {app_api_token}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DifyV34HttpSseAdapter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start_chatflow(
        self,
        *,
        inputs: Mapping[str, object],
        query: str,
        files: Sequence[Mapping[str, object]],
        user: str,
    ) -> DifyV34StartResult:
        if not isinstance(inputs, Mapping):
            raise ValueError("inputs must be a mapping")
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        _required_string(user, "user")
        wire_files = self._validated_files(files)
        payload = {
            "inputs": dict(inputs),
            "query": query,
            "files": wire_files,
            "response_mode": "streaming",
            "user": user,
            "auto_generate_name": False,
            "workflow_id": self._workflow_id,
        }
        observed_run_id: str | None = None
        stream_started_at = time.monotonic()
        try:
            json.dumps(payload, allow_nan=False)
            with (
                _httpcore_adapter_io_context(),
                self._client.stream(
                    "POST",
                    "/v1/chat-messages",
                    json=payload,
                    headers={"Accept": "text/event-stream"},
                ) as response,
            ):
                if response.status_code != 200:
                    return DifyV34StartResult("unknown", safe_reason_code="DIFY_HTTP_NON_200")
                for item in _sse_json_objects(response):
                    if time.monotonic() - stream_started_at > self._max_stream_seconds:
                        return DifyV34StartResult(
                            "unknown",
                            workflow_run_id=observed_run_id,
                            safe_reason_code="DIFY_STREAM_DURATION_LIMIT",
                        )
                    event_name = _required_string(item.get("event"), "event name")
                    if event_name == "ping":
                        continue
                    if event_name not in _SAFE_IDENTITY_EVENTS:
                        raise _InvalidWireShape("unrecognized SSE event")
                    current_run_id = _optional_string(
                        item.get("workflow_run_id"), "workflow run id"
                    )
                    if current_run_id is not None:
                        if observed_run_id is not None and current_run_id != observed_run_id:
                            raise _InvalidWireShape("SSE run id changed during start")
                        observed_run_id = current_run_id
                    if event_name == "human_input_required":
                        if current_run_id is None:
                            raise _InvalidWireShape("human input event has no run id")
                        pause = _parse_identity_event(item, current_run_id)
                        return DifyV34StartResult(
                            "started",
                            workflow_run_id=pause.workflow_run_id,
                            form_node_id=pause.node_id,
                            form_id=pause.form_id,
                            form_token=pause.form_token,
                        )
            return DifyV34StartResult(
                "unknown",
                workflow_run_id=observed_run_id,
                safe_reason_code="DIFY_HUMAN_INPUT_NOT_OBSERVED",
            )
        except (httpx.HTTPError, _InvalidWireShape, TypeError, ValueError):
            return DifyV34StartResult(
                "unknown",
                workflow_run_id=observed_run_id,
                safe_reason_code="DIFY_TRANSPORT_OR_WIRE_UNKNOWN",
            )

    def get_form_definition(self, *, form_token: str, user: str) -> DifyV34FormResult:
        _required_string(form_token, "form token")
        _required_string(user, "user")
        path = f"/v1/form/human_input/{quote(form_token, safe='')}"
        try:
            with (
                _httpcore_adapter_io_context(),
                self._client.stream("GET", path, params={"user": user}) as response,
            ):
                if response.status_code != 200:
                    return DifyV34FormResult("unknown", safe_reason_code="DIFY_HTTP_NON_200")
                inputs, action_ids, expiration_time = _parse_form_payload(
                    json.loads(_read_limited_response(response))
                )
                return DifyV34FormResult(
                    "available",
                    inputs=inputs,
                    action_ids=action_ids,
                    expiration_time=expiration_time,
                )
        except (httpx.HTTPError, _InvalidWireShape, TypeError, ValueError):
            return DifyV34FormResult("unknown", safe_reason_code="DIFY_TRANSPORT_OR_WIRE_UNKNOWN")

    def cancel_before_selection(
        self,
        *,
        form_node_id: str,
        form_token: str,
        user: str,
        expected_fields: tuple[ChatflowFirstFormField, ...],
        expected_form_definition_digest: bytes,
        expected_expiration_time: int,
    ) -> DifyV34CancelResult:
        if form_node_id != "form_0" or not _cancel_expected_fields_are_valid(
            expected_fields,
            expected_form_definition_digest,
            expected_expiration_time,
        ):
            return DifyV34CancelResult(
                "definitely_rejected", safe_reason_code="V34_CANCEL_EXPECTED_DEFINITION_INVALID"
            )
        try:
            _required_string(form_token, "form token")
            _required_string(user, "user")
        except _InvalidWireShape:
            return DifyV34CancelResult(
                "definitely_rejected", safe_reason_code="V34_CANCEL_IDENTITY_INVALID"
            )

        fresh_form = self.get_form_definition(form_token=form_token, user=user)
        if fresh_form.outcome != "available" or fresh_form.expiration_time is None:
            return DifyV34CancelResult(
                "unknown",
                safe_reason_code=fresh_form.safe_reason_code or "DIFY_CANCEL_FRESH_FORM_UNKNOWN",
            )
        fresh_fields = tuple(
            ChatflowFirstFormField(
                output_variable_name=item.output_variable_name,
                input_type=item.input_type,
                option_values=item.option_values,
            )
            for item in fresh_form.inputs
        )
        if (
            fresh_fields != expected_fields
            or _form_definition_digest(fresh_fields) != expected_form_definition_digest
            or "cancel" not in fresh_form.action_ids
            or fresh_form.expiration_time != expected_expiration_time
            or fresh_form.expiration_time <= int(time.time())
        ):
            return DifyV34CancelResult(
                "definitely_rejected", safe_reason_code="V34_CANCEL_FORM_DEFINITION_MISMATCH"
            )

        cancel_inputs = {name: "" for name, _input_type in _CANCEL_FORM_FIELDS}
        try:
            submitted = self.submit_form(
                form_node_id="form_0",
                form_token=form_token,
                user=user,
                action="cancel",
                inputs=cancel_inputs,
            )
        except (httpx.HTTPError, TypeError, ValueError):
            return DifyV34CancelResult(
                "unknown", safe_reason_code="DIFY_CANCEL_SUBMIT_OUTCOME_UNKNOWN"
            )
        if submitted.outcome == "accepted":
            return DifyV34CancelResult("accepted")
        return DifyV34CancelResult(
            "unknown",
            safe_reason_code=submitted.safe_reason_code or "DIFY_CANCEL_SUBMIT_OUTCOME_UNKNOWN",
        )

    def submit_form(
        self,
        *,
        form_node_id: str,
        form_token: str,
        user: str,
        action: str,
        inputs: Mapping[str, object],
    ) -> DifyV34SubmitResult:
        if form_node_id != "form_0":
            raise ValueError("only form_0 may be submitted by this adapter")
        _required_string(form_token, "form token")
        _required_string(user, "user")
        _required_string(action, "action")
        if not isinstance(inputs, Mapping):
            raise ValueError("inputs must be a mapping")
        payload = {"user": user, "action": action, "inputs": dict(inputs)}
        try:
            json.dumps(payload, allow_nan=False)
            with (
                _httpcore_adapter_io_context(),
                self._client.stream(
                    "POST",
                    f"/v1/form/human_input/{quote(form_token, safe='')}",
                    json=payload,
                ) as response,
            ):
                if response.status_code != 200:
                    return DifyV34SubmitResult("unknown", safe_reason_code="DIFY_HTTP_NON_200")
                if json.loads(_read_limited_response(response)) != {}:
                    return DifyV34SubmitResult(
                        "unknown", safe_reason_code="DIFY_RESPONSE_SHAPE_UNKNOWN"
                    )
                return DifyV34SubmitResult("accepted")
        except (httpx.HTTPError, _InvalidWireShape, TypeError, ValueError):
            return DifyV34SubmitResult("unknown", safe_reason_code="DIFY_TRANSPORT_OR_WIRE_UNKNOWN")

    def recover_events(
        self,
        *,
        workflow_run_id: str,
        user: str,
        include_state_snapshot: bool = True,
        continue_on_pause: bool = False,
    ) -> DifyV34EventsResult:
        return self._recover_events(
            workflow_run_id=workflow_run_id,
            user=user,
            include_state_snapshot=include_state_snapshot,
            continue_on_pause=continue_on_pause,
            strict_terminal_observation=False,
        )

    def _recover_events(
        self,
        *,
        workflow_run_id: str,
        user: str,
        include_state_snapshot: bool,
        continue_on_pause: bool,
        strict_terminal_observation: bool,
    ) -> DifyV34EventsResult:
        _required_string(workflow_run_id, "workflow run id")
        _required_string(user, "user")
        if not isinstance(include_state_snapshot, bool) or not isinstance(continue_on_pause, bool):
            raise ValueError("event recovery flags must be booleans")
        if continue_on_pause:
            raise ValueError("continue_on_pause is unsupported by the finite recovery adapter")
        path = f"/v1/workflow/{quote(workflow_run_id, safe='')}/events"
        params = {
            "user": user,
            "include_state_snapshot": str(include_state_snapshot).lower(),
            "continue_on_pause": str(continue_on_pause).lower(),
        }
        events: list[DifyV34Event] = []
        stream_started_at = time.monotonic()
        try:
            with (
                _httpcore_adapter_io_context(),
                self._client.stream(
                    "GET",
                    path,
                    params=params,
                    headers={"Accept": "text/event-stream"},
                ) as response,
            ):
                if response.status_code != 200:
                    return DifyV34EventsResult("unknown", safe_reason_code="DIFY_HTTP_NON_200")
                if strict_terminal_observation:
                    content_type = response.headers.get("content-type", "")
                    media_type = content_type.split(";", 1)[0].strip().lower()
                    if media_type != "text/event-stream":
                        return DifyV34EventsResult(
                            "unknown", safe_reason_code="DIFY_RESPONSE_CONTENT_TYPE_UNKNOWN"
                        )
                last_event_was_terminal = False
                terminal_event_indexes: list[int] = []
                for item in _sse_json_objects(response):
                    if time.monotonic() - stream_started_at > self._max_stream_seconds:
                        return DifyV34EventsResult(
                            "unknown",
                            events=tuple(events),
                            safe_reason_code="DIFY_STREAM_DURATION_LIMIT",
                        )
                    event_name = _required_string(item.get("event"), "event name")
                    if event_name == "ping":
                        ping_run_id = item.get("workflow_run_id")
                        if strict_terminal_observation and ping_run_id is not None:
                            if ping_run_id != workflow_run_id:
                                raise _InvalidWireShape("ping run id does not match requested run")
                        continue
                    if event_name not in _SAFE_IDENTITY_EVENTS:
                        raise _InvalidWireShape("unrecognized SSE event")
                    event = _parse_identity_event(
                        item,
                        workflow_run_id,
                        allow_null_workflow_failed_status=strict_terminal_observation,
                    )
                    events.append(event)
                    if event_name in {
                        "workflow_finished",
                        "workflow_failed",
                        "workflow_cancelled",
                    }:
                        terminal_event_indexes.append(len(events) - 1)
                    last_event_was_terminal = event_name in {
                        "workflow_paused",
                        "workflow_finished",
                        "workflow_failed",
                        "workflow_cancelled",
                    }
                if not events:
                    return DifyV34EventsResult(
                        "unknown", safe_reason_code="DIFY_NO_EVENTS_OBSERVED"
                    )
                if strict_terminal_observation:
                    if (
                        len(terminal_event_indexes) != 1
                        or terminal_event_indexes[0] != len(events) - 1
                    ):
                        return DifyV34EventsResult(
                            "unknown",
                            events=tuple(events),
                            safe_reason_code="DIFY_TERMINAL_EVENT_NOT_UNIQUE_TAIL",
                        )
                    terminal = events[-1]
                    if terminal.event == "workflow_finished":
                        if terminal.workflow_status not in {
                            "partial-succeeded",
                            "succeeded",
                            "failed",
                        }:
                            return DifyV34EventsResult(
                                "unknown",
                                events=tuple(events),
                                safe_reason_code="DIFY_TERMINAL_STATUS_UNKNOWN",
                            )
                    elif terminal.event == "workflow_failed":
                        if terminal.workflow_status not in {None, "failed"}:
                            return DifyV34EventsResult(
                                "unknown",
                                events=tuple(events),
                                safe_reason_code="DIFY_TERMINAL_STATUS_UNKNOWN",
                            )
                    else:
                        return DifyV34EventsResult(
                            "unknown",
                            events=tuple(events),
                            safe_reason_code="DIFY_TERMINAL_EVENT_UNKNOWN",
                        )
                    return DifyV34EventsResult("received", events=tuple(events))
                if not last_event_was_terminal:
                    return DifyV34EventsResult(
                        "unknown",
                        events=tuple(events),
                        safe_reason_code="DIFY_TERMINAL_EVENT_NOT_OBSERVED",
                    )
                return DifyV34EventsResult("received", events=tuple(events))
        except _InvalidWireShape:
            return DifyV34EventsResult("unknown", safe_reason_code="DIFY_TRANSPORT_OR_WIRE_UNKNOWN")
        except (httpx.HTTPError, TypeError, ValueError):
            return DifyV34EventsResult(
                "unknown",
                events=tuple(events),
                safe_reason_code="DIFY_TRANSPORT_OR_WIRE_UNKNOWN",
            )

    def observe_original_run(self, *, workflow_run_id: str, user: str) -> DifyV34ObservationResult:
        result = self.recover_events(
            workflow_run_id=workflow_run_id,
            user=user,
            include_state_snapshot=True,
            continue_on_pause=False,
        )
        return DifyV34ObservationResult(
            result.outcome,
            events=tuple(
                DifyV34ObservedEvent(
                    event=item.event,
                    workflow_run_id=item.workflow_run_id,
                    node_id=item.node_id,
                    form_id=item.form_id,
                    workflow_status=item.workflow_status,
                )
                for item in result.events
            ),
            safe_reason_code=result.safe_reason_code,
        )

    def observe_original_run_terminal(
        self, *, workflow_run_id: str, user: str
    ) -> DifyV34ObservationResult:
        """Observe one fully framed terminal event from the exact original run."""
        result = self._recover_events(
            workflow_run_id=workflow_run_id,
            user=user,
            include_state_snapshot=True,
            continue_on_pause=False,
            strict_terminal_observation=True,
        )
        return DifyV34ObservationResult(
            result.outcome,
            events=tuple(
                DifyV34ObservedEvent(
                    event=item.event,
                    workflow_run_id=item.workflow_run_id,
                    node_id=item.node_id,
                    form_id=item.form_id,
                    workflow_status=item.workflow_status,
                )
                for item in result.events
            ),
            safe_reason_code=result.safe_reason_code,
        )

    def recover_cancel_before_selection(
        self, *, workflow_run_id: str, user: str
    ) -> DifyV34ObservationResult:
        return self.observe_original_run(workflow_run_id=workflow_run_id, user=user)

    @staticmethod
    def _validated_files(files: Sequence[Mapping[str, object]]) -> list[dict[str, str]]:
        if isinstance(files, (str, bytes)) or not isinstance(files, Sequence):
            raise ValueError("files must be a sequence of local Dify file references")
        validated: list[dict[str, str]] = []
        for item in files:
            if not isinstance(item, Mapping) or set(item) != {
                "type",
                "transfer_method",
                "upload_file_id",
            }:
                raise ValueError("files must use the Dify local_file upload_file_id format")
            file_type = _required_string(item.get("type"), "file type")
            transfer_method = item.get("transfer_method")
            file_id = _required_string(item.get("upload_file_id"), "upload file id")
            if transfer_method != "local_file" or file_type != "image":
                raise ValueError("files must be image local_file references")
            validated.append(
                {
                    "type": file_type,
                    "transfer_method": "local_file",
                    "upload_file_id": file_id,
                }
            )
        return validated


__all__ = [
    "DifyV34CancelResult",
    "DifyV34Event",
    "DifyV34EventsResult",
    "DifyV34FormInput",
    "DifyV34FormResult",
    "DifyV34HttpSseAdapter",
    "DifyV34ObservationResult",
    "DifyV34ObservedEvent",
    "DifyV34StartResult",
    "DifyV34SubmitResult",
]
