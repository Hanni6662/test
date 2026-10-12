from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Literal
from uuid import uuid4

import pytest
from app.integrations.dify.chatflow import (
    ChatflowCancelCommand,
    ChatflowContinuationCommand,
    ChatflowFirstFormDefinitionCommand,
    ChatflowFirstFormField,
    ChatflowOriginalRunEvent,
    ChatflowStartCommand,
)
from app.integrations.dify.chatflow_v34_http import (
    DifyV34CancelResult,
    DifyV34EventsResult,
    DifyV34FormInput,
    DifyV34FormResult,
    DifyV34ObservationResult,
    DifyV34ObservedEvent,
    DifyV34StartResult,
    DifyV34SubmitResult,
)
from app.integrations.dify.chatflow_v34_port import DifyV34ChatflowPort
from app.schemas.recommendations import PLATFORMS
from app.services.chatflow_v34_conversion import recommendation_from_prepare_audit
from app.services.chatflow_v34_events import validate_v34_form_definition

_FORM_TOKEN = "private-v34-form-token"
_FORM_ID = "8b0d5f34-65e5-4d52-82c2-60a17015a30d"
_RUN_ID = "workflow-run-v34-001"
_DESIGN_OPTION = "D|||由视觉审核 Agent 自由设计"
_CUSTOM_OPTION = "custom|||自定义"


class _AdapterDouble:
    def __init__(
        self,
        *,
        start_result: DifyV34StartResult,
        form_result: DifyV34FormResult | None = None,
        submit_result: DifyV34SubmitResult | None = None,
        cancel_result: DifyV34CancelResult | None = None,
        events_result: DifyV34EventsResult | None = None,
        observation_result: DifyV34ObservationResult | None = None,
    ) -> None:
        self.start_result = start_result
        self.form_result = form_result
        self.submit_result = submit_result
        self.cancel_result = cancel_result
        self.events_result = events_result
        self.observation_result = observation_result
        self.start_calls: list[dict[str, object]] = []
        self.form_calls: list[dict[str, str]] = []
        self.submit_calls: list[dict[str, object]] = []
        self.cancel_calls: list[dict[str, object]] = []
        self.recovery_calls: list[dict[str, object]] = []
        self.cancel_recovery_calls: list[dict[str, object]] = []
        self.observation_calls: list[dict[str, object]] = []
        self.close_calls = 0

    def __enter__(self) -> _AdapterDouble:
        return self

    def __exit__(self, *_: object) -> None:
        self.close_calls += 1

    def start_chatflow(self, **kwargs: object) -> DifyV34StartResult:
        self.start_calls.append(kwargs)
        return self.start_result

    def get_form_definition(self, *, form_token: str, user: str) -> DifyV34FormResult:
        self.form_calls.append({"form_token": form_token, "user": user})
        if self.form_result is None:
            raise AssertionError("unexpected form request")
        return self.form_result

    def submit_form(self, **kwargs: object) -> DifyV34SubmitResult:
        self.submit_calls.append(kwargs)
        if self.submit_result is None:
            raise AssertionError("unexpected form submit")
        return self.submit_result

    def recover_events(self, **kwargs: object) -> DifyV34EventsResult:
        self.recovery_calls.append(kwargs)
        if self.events_result is None:
            raise AssertionError("unexpected workflow event recovery")
        return self.events_result

    def cancel_before_selection(self, **kwargs: object) -> DifyV34CancelResult:
        self.cancel_calls.append(kwargs)
        if self.cancel_result is None:
            raise AssertionError("unexpected cancel form submit")
        return self.cancel_result

    def recover_cancel_before_selection(self, **kwargs: object) -> DifyV34ObservationResult:
        self.cancel_recovery_calls.append(kwargs)
        if self.observation_result is None:
            raise AssertionError("unexpected cancel recovery")
        return self.observation_result

    def observe_original_run(self, **kwargs: object) -> DifyV34ObservationResult:
        self.observation_calls.append(kwargs)
        if self.observation_result is None:
            raise AssertionError("unexpected original-run observation")
        return self.observation_result


def _command() -> ChatflowStartCommand:
    business_session_id = uuid4()
    return ChatflowStartCommand(
        business_session_id=business_session_id,
        recommendation_id=uuid4(),
        recommendation_revision=7,
        start_operation_id=uuid4(),
        dify_user_id=f"bs-{business_session_id.hex}",
        reference_set_id=uuid4(),
        reference_asset_digest="a" * 64,
        reference_assets=(),
        category="家居收纳用品",
        reference_files=(
            {
                "dify_file_id": "uploaded-primary",
                "slot": "PRIMARY",
                "position": 0,
                "content_sha256": "1" * 64,
            },
            {
                "dify_file_id": "uploaded-aux-1",
                "slot": "AUX01",
                "position": 1,
                "content_sha256": "2" * 64,
            },
            {
                "dify_file_id": "uploaded-aux-2",
                "slot": "AUX02",
                "position": 2,
                "content_sha256": "3" * 64,
            },
            {
                "dify_file_id": "uploaded-aux-3",
                "slot": "AUX03",
                "position": 3,
                "content_sha256": "4" * 64,
            },
        ),
    )


def _original_recommendation():
    audit = {
        "declared_category": "家居收纳用品",
        "fields": [
            {"id": "1", "options": ["简体中文商品说明", "Concise English copy", "无文字纯视觉"]},
            {"id": "2", "options": list(PLATFORMS)},
            {"id": "3", "options": ["桌面办公收纳", "居家衣柜整理", "户外随身收纳"]},
            {"id": "4", "options": ["自然光生活方式", "简洁电商棚拍", "精致杂志质感"]},
            {"id": "5", "options": ["展示接口与按键", "突出轻巧便携", "呈现包装与配件"]},
        ],
    }
    return recommendation_from_prepare_audit(
        json.dumps(audit, ensure_ascii=False)
    ).original_recommendation


def _expected_form_inputs() -> tuple[DifyV34FormInput, ...]:
    original = _original_recommendation()
    select_options = {
        "choice_1": original.field_texts["language"],
        "choice_3": original.field_texts["usage_scene"],
        "choice_4": original.field_texts["visual_style"],
        "choice_5": original.field_texts["selling_points"],
    }
    select_options["choice_2"] = [f"PLATFORM|||{platform}" for platform in PLATFORMS]
    fields: list[DifyV34FormInput] = []
    for name, input_type in (
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
    ):
        options = (
            tuple(
                f"{letter}|||{value}"
                for letter, value in zip("ABC", select_options[name], strict=True)
            )
            + (_DESIGN_OPTION, _CUSTOM_OPTION)
            if name in select_options and name != "choice_2"
            else tuple(select_options[name])
            if name == "choice_2"
            else ()
        )
        fields.append(
            DifyV34FormInput(
                output_variable_name=name,
                input_type=input_type,
                option_values=options,
            )
        )
    return tuple(fields)


def _start_result(
    *,
    outcome: Literal["started", "unknown"] = "started",
    workflow_run_id: str | None = _RUN_ID,
    form_node_id: str | None = "form_0",
    form_id: str | None = _FORM_ID,
    form_token: str | None = _FORM_TOKEN,
    safe_reason_code: str | None = None,
) -> DifyV34StartResult:
    return DifyV34StartResult(
        outcome=outcome,
        workflow_run_id=workflow_run_id,
        form_node_id=form_node_id,
        form_id=form_id,
        form_token=form_token,
        safe_reason_code=safe_reason_code,
    )


def _form_definition_command(
    command: ChatflowStartCommand,
    *,
    form_node_id: str = "form_0",
    form_token: str = _FORM_TOKEN,
    workflow_run_id: str = _RUN_ID,
) -> ChatflowFirstFormDefinitionCommand:
    return ChatflowFirstFormDefinitionCommand(
        business_session_id=command.business_session_id,
        recommendation_id=command.recommendation_id,
        recommendation_revision=command.recommendation_revision,
        dify_run_id=workflow_run_id,
        dify_user_id=command.dify_user_id,
        form_node_id=form_node_id,
        form_token=form_token,
    )


def _continuation_command(
    *,
    form_node_id: str = "form_0",
) -> ChatflowContinuationCommand:
    start = _command()
    return ChatflowContinuationCommand(
        business_session_id=start.business_session_id,
        recommendation_id=start.recommendation_id,
        recommendation_revision=start.recommendation_revision,
        dify_run_id=_RUN_ID,
        dify_user_id=start.dify_user_id,
        task_id=uuid4(),
        selection_snapshot={"selection": {"image_types": ["品牌导购主视觉"]}},
        selection_submission_id=uuid4(),
        form_node_id=form_node_id,
        form_token=_FORM_TOKEN,
        form_action="submit",
        form_inputs={
            "choice_1": "A|||简体中文商品说明",
            "custom_1": "",
            "choice_2": "PLATFORM|||TEMU",
            "choice_3": "B|||居家衣柜整理",
            "custom_3": "",
            "choice_4": "C|||精致杂志质感",
            "custom_4": "",
            "choice_5": "custom|||自定义",
            "custom_5": "仅采用可核验的商品信息",
            "image_types": '["品牌导购主视觉"]',
        },
    )


def _cancel_command(
    *,
    dify_run_id: str = _RUN_ID,
    dify_user_id: str | None = None,
    form_node_id: str = "form_0",
    form_token: str = _FORM_TOKEN,
) -> ChatflowCancelCommand:
    start = _command()
    fields = tuple(
        ChatflowFirstFormField(
            output_variable_name=item.output_variable_name,
            input_type=item.input_type,
            option_values=item.option_values,
        )
        for item in _expected_form_inputs()
    )
    return ChatflowCancelCommand(
        business_session_id=start.business_session_id,
        recommendation_id=start.recommendation_id,
        recommendation_revision=start.recommendation_revision,
        dify_run_id=dify_run_id,
        dify_user_id=dify_user_id or start.dify_user_id,
        form_node_id=form_node_id,
        form_token=form_token,
        expected_form_definition_digest=b"d" * 32,
        expected_fields=fields,
        expected_expiration_time=1_900_000_000,
    )


def _port_with_adapter(monkeypatch: pytest.MonkeyPatch, adapter: _AdapterDouble):
    port = DifyV34ChatflowPort(
        api_base_url="https://dify.test",
        app_id="v34-test-app",
        app_api_token="private-app-token",
        workflow_id="published-v34-workflow",
        timeout_seconds=12.5,
    )
    monkeypatch.setattr(port, "_adapter", lambda: adapter)
    return port


def test_start_only_creates_run_binding_and_forwards_context_and_all_ordered_uploads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    form_inputs = _expected_form_inputs()
    adapter = _AdapterDouble(
        start_result=_start_result(),
        form_result=DifyV34FormResult(
            "available",
            inputs=form_inputs,
            action_ids=("submit", "cancel"),
            expiration_time=1_900_000_000,
        ),
    )
    port = _port_with_adapter(monkeypatch, adapter)
    command = _command()

    result = port.start_recommendation(command)

    assert result.outcome == "started"
    assert result.dify_run_id == _RUN_ID
    assert result.safe_reason_code is None
    assert result.first_form is not None
    assert result.first_form.form_node_id == "form_0"
    assert result.first_form.form_id == _FORM_ID
    assert result.first_form.form_token == _FORM_TOKEN
    assert result.first_form.fields == ()
    assert result.first_form.action_ids == ()
    assert result.first_form.expiration_time is None
    assert _FORM_TOKEN not in repr(result)
    assert adapter.close_calls == 1
    assert len(adapter.start_calls) == 1
    assert adapter.form_calls == []

    start_call = adapter.start_calls[0]
    assert start_call["query"] == "开始设计商品图"
    assert start_call["files"] == ()
    assert start_call["user"] == command.dify_user_id
    start_inputs = start_call["inputs"]
    assert isinstance(start_inputs, dict)
    assert start_inputs["product_category"] == command.category
    assert start_inputs["primary_product_image"] == [
        {
            "type": "image",
            "transfer_method": "local_file",
            "upload_file_id": "uploaded-primary",
        }
    ]
    assert start_inputs["supplementary_product_images"] == [
        {
            "type": "image",
            "transfer_method": "local_file",
            "upload_file_id": file_id,
        }
        for file_id in ("uploaded-aux-1", "uploaded-aux-2", "uploaded-aux-3")
    ]
    backend_context = json.loads(start_inputs["backend_context"])
    assert backend_context == {
        "contract_version": "visual-v34-stage-v1",
        "flow_version": "visual-v3.4-single-selection",
        "business_session_id": str(command.business_session_id),
        "recommendation_id": str(command.recommendation_id),
        "recommendation_revision": command.recommendation_revision,
        "start_operation_id": str(command.start_operation_id),
    }

    # The form lookup is a separate durable action. Its identity is derived
    # from the exact run and private token returned by the start stream.
    form_command = _form_definition_command(
        command,
        form_node_id=result.first_form.form_node_id,
        form_token=result.first_form.form_token,
        workflow_run_id=result.dify_run_id,
    )
    assert form_command.dify_run_id == result.dify_run_id == _RUN_ID
    definition = port.get_first_form_definition(form_command)
    assert definition.outcome == "available"
    assert [
        (field.output_variable_name, field.input_type, field.option_values)
        for field in definition.fields
    ] == [
        (field.output_variable_name, field.input_type, field.option_values) for field in form_inputs
    ]
    assert definition.action_ids == ("submit", "cancel")
    assert definition.expiration_time == 1_900_000_000
    assert not hasattr(definition, "dify_run_id")
    assert adapter.form_calls == [{"form_token": _FORM_TOKEN, "user": command.dify_user_id}]

    recommendation = SimpleNamespace(
        status="ready",
        original_recommendation=_original_recommendation().model_dump(mode="python"),
    )
    bound_fields = tuple(
        DifyV34FormInput(
            output_variable_name=field.output_variable_name,
            input_type=field.input_type,
            option_values=field.option_values,
        )
        for field in definition.fields
    )
    assert validate_v34_form_definition(
        bound_fields,
        definition.action_ids,
        recommendation=recommendation,
    )


def test_start_unknown_is_returned_without_form_lookup_or_second_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _AdapterDouble(
        start_result=_start_result(
            outcome="unknown",
            safe_reason_code="DIFY_TRANSPORT_OR_WIRE_UNKNOWN",
        )
    )
    port = _port_with_adapter(monkeypatch, adapter)

    result = port.start_recommendation(_command())

    assert result.outcome == "unknown"
    assert result.dify_run_id == _RUN_ID
    assert result.safe_reason_code == "DIFY_TRANSPORT_OR_WIRE_UNKNOWN"
    assert result.first_form is None
    assert len(adapter.start_calls) == 1
    assert adapter.form_calls == []


@pytest.mark.parametrize(
    ("form_result", "expected_reason"),
    [
        (DifyV34FormResult("unknown", safe_reason_code="DIFY_HTTP_NON_200"), "DIFY_HTTP_NON_200"),
        (
            DifyV34FormResult("available", inputs=(), action_ids=("submit",)),
            "DIFY_FORM_DEFINITION_UNKNOWN",
        ),
    ],
)
def test_unavailable_or_incomplete_form_definition_does_not_create_a_binding(
    monkeypatch: pytest.MonkeyPatch,
    form_result: DifyV34FormResult,
    expected_reason: str,
) -> None:
    adapter = _AdapterDouble(start_result=_start_result(), form_result=form_result)
    port = _port_with_adapter(monkeypatch, adapter)
    command = _command()

    started = port.start_recommendation(command)

    assert started.outcome == "started"
    assert started.dify_run_id == _RUN_ID
    assert started.first_form is not None
    assert adapter.form_calls == []

    definition = port.get_first_form_definition(
        _form_definition_command(
            command,
            form_node_id=started.first_form.form_node_id,
            form_token=started.first_form.form_token,
            workflow_run_id=started.dify_run_id,
        )
    )
    # An unavailable definition or one without expiry cannot be used to mark
    # the session ready; start remains a single, already completed operation.
    assert definition.outcome == "unknown"
    assert definition.safe_reason_code == expected_reason
    assert len(adapter.start_calls) == 1
    assert len(adapter.form_calls) == 1


def test_wrong_first_form_identity_and_missing_run_id_fail_closed_without_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _AdapterDouble(
        start_result=_start_result(form_node_id="form_1", form_token="private-form-token-1"),
        form_result=DifyV34FormResult("available", inputs=_expected_form_inputs()),
    )
    port = _port_with_adapter(monkeypatch, adapter)

    wrong_form = port.start_recommendation(_command())

    assert wrong_form.outcome == "started"
    assert wrong_form.dify_run_id == _RUN_ID
    assert wrong_form.first_form is not None
    assert wrong_form.first_form.form_node_id == "form_1"
    assert wrong_form.first_form.form_token == ""
    assert wrong_form.safe_reason_code == "V34_FIRST_FORM_IDENTITY_MISMATCH"
    assert adapter.form_calls == []

    wrong_definition = port.get_first_form_definition(
        _form_definition_command(
            _command(), form_node_id="form_1", form_token="private-form-token-1"
        )
    )
    assert wrong_definition.outcome == "unknown"
    assert wrong_definition.safe_reason_code == "V34_FIRST_FORM_IDENTITY_MISMATCH"
    assert adapter.form_calls == []

    missing_run_adapter = _AdapterDouble(start_result=_start_result(workflow_run_id=None))
    missing_run_port = _port_with_adapter(monkeypatch, missing_run_adapter)
    missing_run = missing_run_port.start_recommendation(_command())
    assert missing_run.outcome == "unknown"
    assert missing_run.dify_run_id is None
    assert missing_run.first_form is None
    assert missing_run.safe_reason_code == "DIFY_START_RUN_ID_MISSING"
    assert missing_run_adapter.form_calls == []


def test_form_contract_rejects_missing_fields_and_unexpected_dynamic_options() -> None:
    recommendation = SimpleNamespace(
        status="ready",
        original_recommendation=_original_recommendation().model_dump(mode="python"),
    )
    valid_fields = _expected_form_inputs()
    assert validate_v34_form_definition(
        valid_fields,
        ("submit", "cancel"),
        recommendation=recommendation,
    )

    assert not validate_v34_form_definition(
        valid_fields[:-1],
        ("submit", "cancel"),
        recommendation=recommendation,
    )
    altered = list(valid_fields)
    altered[0] = DifyV34FormInput(
        output_variable_name="choice_1",
        input_type="select",
        option_values=(*valid_fields[0].option_values, "D|||未配置选项"),
    )
    assert not validate_v34_form_definition(
        tuple(altered),
        ("submit", "cancel"),
        recommendation=recommendation,
    )
    assert not validate_v34_form_definition(
        valid_fields,
        ("cancel",),
        recommendation=recommendation,
    )


def test_invalid_reference_order_is_rejected_before_adapter_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _AdapterDouble(start_result=_start_result())
    port = _port_with_adapter(monkeypatch, adapter)
    command = _command()
    bad_order = replace(command, reference_files=tuple(reversed(command.reference_files)))

    with pytest.raises(ValueError, match="order or slot"):
        port.start_recommendation(bad_order)
    assert adapter.start_calls == []
    assert adapter.form_calls == []


def test_selection_submit_requires_form_0_and_uses_one_bound_form_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _AdapterDouble(
        start_result=_start_result(),
        submit_result=DifyV34SubmitResult("accepted"),
    )
    port = _port_with_adapter(monkeypatch, adapter)
    command = _continuation_command()

    result = port.continue_after_selection(command)

    assert result.outcome == "accepted"
    assert adapter.submit_calls == [
        {
            "form_node_id": "form_0",
            "form_token": _FORM_TOKEN,
            "user": command.dify_user_id,
            "action": "submit",
            "inputs": command.form_inputs,
        }
    ]
    assert adapter.start_calls == []
    assert adapter.form_calls == []
    assert adapter.recovery_calls == []
    assert adapter.close_calls == 1

    invalid = replace(command, form_node_id="form_1")
    rejected = port.continue_after_selection(invalid)
    assert rejected.outcome == "definitely_rejected"
    assert rejected.safe_reason_code == "V34_SELECTION_BINDING_INVALID"
    assert len(adapter.submit_calls) == 1


def test_selection_submit_http_unknown_is_preserved_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _AdapterDouble(
        start_result=_start_result(),
        submit_result=DifyV34SubmitResult("unknown", safe_reason_code="DIFY_HTTP_NON_200"),
    )
    port = _port_with_adapter(monkeypatch, adapter)

    result = port.continue_after_selection(_continuation_command())

    assert result.outcome == "unknown"
    assert result.safe_reason_code == "DIFY_HTTP_NON_200"
    assert len(adapter.submit_calls) == 1
    assert adapter.start_calls == []


def test_selection_recovery_queries_only_the_original_run_without_resubmitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _AdapterDouble(
        start_result=_start_result(),
        events_result=DifyV34EventsResult("received"),
    )
    port = _port_with_adapter(monkeypatch, adapter)
    command = _continuation_command()

    result = port.recover_after_selection(command)

    assert result.outcome == "unknown"
    assert result.safe_reason_code == "DIFY_SELECTION_ACCEPTANCE_UNVERIFIED"
    assert adapter.recovery_calls == [
        {
            "workflow_run_id": command.dify_run_id,
            "user": command.dify_user_id,
            "include_state_snapshot": True,
            "continue_on_pause": False,
        }
    ]
    assert adapter.submit_calls == []
    assert adapter.start_calls == []
    assert adapter.close_calls == 1


def test_cancel_posts_one_bound_form_with_stored_definition_and_preserves_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _AdapterDouble(
        start_result=_start_result(), cancel_result=DifyV34CancelResult("accepted")
    )
    port = _port_with_adapter(monkeypatch, adapter)
    command = _cancel_command()

    result = port.cancel_before_selection(command)

    assert result.outcome == "accepted"
    assert result.safe_reason_code is None
    assert adapter.cancel_calls == [
        {
            "form_node_id": command.form_node_id,
            "form_token": command.form_token,
            "user": command.dify_user_id,
            "expected_fields": command.expected_fields,
            "expected_form_definition_digest": command.expected_form_definition_digest,
            "expected_expiration_time": command.expected_expiration_time,
        }
    ]
    assert _FORM_TOKEN not in repr(command)
    assert adapter.start_calls == []
    assert adapter.form_calls == []
    assert adapter.submit_calls == []
    assert adapter.close_calls == 1


def test_cancel_transport_unknown_is_not_retried_or_restarted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _AdapterDouble(
        start_result=_start_result(),
        cancel_result=DifyV34CancelResult(
            "unknown", safe_reason_code="DIFY_TRANSPORT_OR_WIRE_UNKNOWN"
        ),
    )
    port = _port_with_adapter(monkeypatch, adapter)

    result = port.cancel_before_selection(_cancel_command())

    assert result.outcome == "unknown"
    assert result.safe_reason_code == "DIFY_TRANSPORT_OR_WIRE_UNKNOWN"
    assert len(adapter.cancel_calls) == 1
    assert adapter.cancel_recovery_calls == []
    assert adapter.start_calls == []
    assert adapter.close_calls == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"dify_run_id": ""},
        {"dify_user_id": ""},
        {"form_node_id": "form_1"},
        {"form_token": ""},
    ],
)
def test_cancel_invalid_bound_identity_is_rejected_before_adapter_io(
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, str],
) -> None:
    adapter = _AdapterDouble(start_result=_start_result())
    port = _port_with_adapter(monkeypatch, adapter)

    result = port.cancel_before_selection(replace(_cancel_command(), **changes))

    assert result.outcome == "definitely_rejected"
    assert result.safe_reason_code == "V34_CANCEL_BINDING_INVALID"
    assert adapter.cancel_calls == []
    assert adapter.start_calls == []


def test_cancel_recovery_is_get_only_exact_run_and_never_establishes_business_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_pause = DifyV34ObservedEvent(
        event="human_input_required",
        workflow_run_id=_RUN_ID,
        node_id="form_0",
        form_id=_FORM_ID,
    )
    later_terminal = DifyV34ObservedEvent(
        event="workflow_cancelled",
        workflow_run_id=_RUN_ID,
    )
    adapter = _AdapterDouble(
        start_result=_start_result(),
        observation_result=DifyV34ObservationResult("received", events=(old_pause, later_terminal)),
    )
    port = _port_with_adapter(monkeypatch, adapter)
    command = _cancel_command()

    result = port.recover_cancel_before_selection(command)

    assert result.outcome == "unknown"
    assert result.safe_reason_code == "DIFY_CANCEL_RECOVERY_REQUIRES_ROOT_EVIDENCE"
    assert result.events == (
        ChatflowOriginalRunEvent(
            event="human_input_required",
            dify_run_id=_RUN_ID,
            node_id="form_0",
            form_id=_FORM_ID,
        ),
        ChatflowOriginalRunEvent(event="workflow_cancelled", dify_run_id=_RUN_ID),
    )
    assert all(not hasattr(event, "form_token") for event in result.events)
    assert adapter.cancel_recovery_calls == [
        {"workflow_run_id": _RUN_ID, "user": command.dify_user_id}
    ]
    assert adapter.cancel_calls == []
    assert adapter.submit_calls == []
    assert adapter.start_calls == []


def test_cancel_recovery_rejects_events_from_a_different_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _cancel_command()
    adapter = _AdapterDouble(
        start_result=_start_result(),
        observation_result=DifyV34ObservationResult(
            "received",
            events=(DifyV34ObservedEvent(event="workflow_cancelled", workflow_run_id="other-run"),),
        ),
    )
    port = _port_with_adapter(monkeypatch, adapter)

    result = port.recover_cancel_before_selection(command)

    assert result.outcome == "unknown"
    assert result.safe_reason_code == "DIFY_CANCEL_RECOVERY_WIRE_UNKNOWN"
    assert result.events == ()
    assert adapter.cancel_recovery_calls == [
        {"workflow_run_id": _RUN_ID, "user": command.dify_user_id}
    ]
    assert adapter.cancel_calls == []
    assert adapter.start_calls == []


def test_original_run_observer_sanitizes_and_preserves_only_event_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_token = "PRIVATE-OBSERVATION-TOKEN"
    adapter = _AdapterDouble(
        start_result=_start_result(),
        observation_result=DifyV34ObservationResult(
            "received",
            events=(
                SimpleNamespace(
                    event="workflow_cancelled",
                    workflow_run_id=_RUN_ID,
                    node_id="form_0",
                    form_id=_FORM_ID,
                    form_token=private_token,
                    data="PRIVATE-OBSERVATION-TEXT",
                ),
            ),
        ),
    )
    port = _port_with_adapter(monkeypatch, adapter)
    command = _cancel_command()

    result = port.observe_original_run(command.dify_run_id, command.dify_user_id)

    assert result.outcome == "received"
    assert result.events == (
        ChatflowOriginalRunEvent(
            event="workflow_cancelled",
            dify_run_id=_RUN_ID,
            node_id="form_0",
            form_id=_FORM_ID,
        ),
    )
    assert private_token not in repr(result)
    assert "PRIVATE-OBSERVATION-TEXT" not in repr(result)
    assert adapter.observation_calls == [{"workflow_run_id": _RUN_ID, "user": command.dify_user_id}]
    assert adapter.cancel_calls == []
    assert adapter.submit_calls == []
    assert adapter.start_calls == []


@pytest.mark.parametrize("status", ["failed", "succeeded"])
def test_original_run_port_preserves_workflow_finished_status(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    adapter = _AdapterDouble(
        start_result=_start_result(),
        observation_result=DifyV34ObservationResult(
            "received",
            events=(
                DifyV34ObservedEvent(
                    event="workflow_finished",
                    workflow_run_id=_RUN_ID,
                    workflow_status=status,
                ),
            ),
        ),
    )
    port = _port_with_adapter(monkeypatch, adapter)
    command = _cancel_command()

    result = port.observe_original_run(command.dify_run_id, command.dify_user_id)

    assert result.outcome == "received"
    assert result.events == (
        ChatflowOriginalRunEvent(
            event="workflow_finished",
            dify_run_id=_RUN_ID,
            workflow_status=status,
        ),
    )


def test_original_run_port_preserves_workflow_failed_without_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _AdapterDouble(
        start_result=_start_result(),
        observation_result=DifyV34ObservationResult(
            "received",
            events=(DifyV34ObservedEvent(event="workflow_failed", workflow_run_id=_RUN_ID),),
        ),
    )
    port = _port_with_adapter(monkeypatch, adapter)
    command = _cancel_command()

    result = port.observe_original_run(command.dify_run_id, command.dify_user_id)

    assert result.outcome == "received"
    assert result.events == (
        ChatflowOriginalRunEvent(event="workflow_failed", dify_run_id=_RUN_ID),
    )


def test_original_run_port_does_not_promote_node_failure_or_pause_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _AdapterDouble(
        start_result=_start_result(),
        observation_result=DifyV34ObservationResult(
            "received",
            events=(
                DifyV34ObservedEvent(
                    event="node_failed",
                    workflow_run_id=_RUN_ID,
                    node_id="design",
                    workflow_status="failed",
                ),
                DifyV34ObservedEvent(
                    event="workflow_paused",
                    workflow_run_id=_RUN_ID,
                    node_id="form_0",
                    workflow_status="failed",
                ),
            ),
        ),
    )
    port = _port_with_adapter(monkeypatch, adapter)
    command = _cancel_command()

    result = port.observe_original_run(command.dify_run_id, command.dify_user_id)

    assert result.outcome == "received"
    assert [event.event for event in result.events] == ["node_failed", "workflow_paused"]
    assert [event.workflow_status for event in result.events] == [None, None]


def test_original_run_port_rejects_malformed_terminal_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _AdapterDouble(
        start_result=_start_result(),
        observation_result=DifyV34ObservationResult(
            "received",
            events=(
                SimpleNamespace(
                    event="workflow_finished",
                    workflow_run_id=_RUN_ID,
                    workflow_status=42,
                ),
            ),
        ),
    )
    port = _port_with_adapter(monkeypatch, adapter)
    command = _cancel_command()

    result = port.observe_original_run(command.dify_run_id, command.dify_user_id)

    assert result.outcome == "unknown"
    assert result.safe_reason_code == "DIFY_RUN_OBSERVATION_WIRE_UNKNOWN"
    assert result.events == ()
