from __future__ import annotations

import json

from app.integrations.dify.chatflow import (
    ChatflowCancelCommand,
    ChatflowCancelResult,
    ChatflowContinuationCommand,
    ChatflowContinuationResult,
    ChatflowFirstFormBinding,
    ChatflowFirstFormDefinitionCommand,
    ChatflowFirstFormDefinitionResult,
    ChatflowFirstFormField,
    ChatflowHumanInputCommand,
    ChatflowHumanInputResult,
    ChatflowOriginalRunEvent,
    ChatflowOriginalRunObservationResult,
    ChatflowStartCommand,
    ChatflowStartResult,
    DifyReferenceUploadCommand,
    DifyReferenceUploadResult,
)
from app.integrations.dify.chatflow_v34_http import DifyV34HttpSseAdapter


class DifyV34ChatflowPort:
    """V3.4 Dify Service API port; methods own bounded HTTP exchanges."""

    def __init__(
        self,
        *,
        api_base_url: str,
        app_id: str,
        app_api_token: str,
        workflow_id: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._api_base_url = api_base_url
        self._app_id = app_id
        self._app_api_token = app_api_token
        self._workflow_id = workflow_id
        self._timeout_seconds = timeout_seconds

    def start_recommendation(self, command: ChatflowStartCommand) -> ChatflowStartResult:
        references = self._ordered_reference_files(command.reference_files)
        primary = references[0]
        supplementary = references[1:]
        inputs = {
            "primary_product_image": [primary],
            "supplementary_product_images": supplementary,
            "product_category": command.category,
            "backend_context": json.dumps(
                {
                    "contract_version": "visual-v34-stage-v1",
                    "flow_version": "visual-v3.4-single-selection",
                    "business_session_id": str(command.business_session_id),
                    "recommendation_id": str(command.recommendation_id),
                    "recommendation_revision": command.recommendation_revision,
                    "start_operation_id": str(command.start_operation_id),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ),
        }
        with self._adapter() as adapter:
            started = adapter.start_chatflow(
                inputs=inputs,
                query="开始设计商品图",
                files=(),
                user=command.dify_user_id,
            )
            if started.outcome in {"unknown", "definitely_rejected"}:
                return ChatflowStartResult(
                    started.outcome,
                    dify_run_id=started.workflow_run_id,
                    safe_reason_code=started.safe_reason_code,
                )
            if started.workflow_run_id is None:
                return ChatflowStartResult("unknown", safe_reason_code="DIFY_START_RUN_ID_MISSING")
            if started.form_node_id != "form_0":
                return ChatflowStartResult(
                    "started",
                    dify_run_id=started.workflow_run_id,
                    safe_reason_code="V34_FIRST_FORM_IDENTITY_MISMATCH",
                    first_form=ChatflowFirstFormBinding(
                        form_node_id=started.form_node_id or "",
                        form_id=started.form_id or "",
                        form_token="",
                    ),
                )
            if started.form_token is None:
                return ChatflowStartResult(
                    "unknown",
                    dify_run_id=started.workflow_run_id,
                    safe_reason_code="DIFY_FORM_TOKEN_MISSING",
                )
            return ChatflowStartResult(
                "started",
                dify_run_id=started.workflow_run_id,
                first_form=ChatflowFirstFormBinding(
                    form_node_id=started.form_node_id,
                    form_id=started.form_id or "",
                    form_token=started.form_token,
                ),
            )

    def get_first_form_definition(
        self, command: ChatflowFirstFormDefinitionCommand
    ) -> ChatflowFirstFormDefinitionResult:
        if command.form_node_id != "form_0" or not command.form_token:
            return ChatflowFirstFormDefinitionResult(
                "unknown", safe_reason_code="V34_FIRST_FORM_IDENTITY_MISMATCH"
            )
        with self._adapter() as adapter:
            form = adapter.get_form_definition(
                form_token=command.form_token,
                user=command.dify_user_id,
            )
            if form.outcome != "available" or form.expiration_time is None:
                return ChatflowFirstFormDefinitionResult(
                    "unknown",
                    safe_reason_code=form.safe_reason_code or "DIFY_FORM_DEFINITION_UNKNOWN",
                )
            return ChatflowFirstFormDefinitionResult(
                "available",
                fields=tuple(
                    ChatflowFirstFormField(
                        output_variable_name=item.output_variable_name,
                        input_type=item.input_type,
                        option_values=item.option_values,
                    )
                    for item in form.inputs
                ),
                action_ids=form.action_ids,
                expiration_time=form.expiration_time,
            )

    def upload_reference(self, _command: DifyReferenceUploadCommand) -> DifyReferenceUploadResult:
        return DifyReferenceUploadResult(
            "definitely_rejected", safe_reason_code="DIFY_REFERENCE_UPLOAD_PORT_REQUIRED"
        )

    def continue_after_selection(
        self, command: ChatflowContinuationCommand
    ) -> ChatflowContinuationResult:
        if (
            command.form_node_id != "form_0"
            or not command.form_token
            or command.form_action != "submit"
            or command.selection_submission_id is None
            or command.form_inputs is None
        ):
            return ChatflowContinuationResult(
                "definitely_rejected", safe_reason_code="V34_SELECTION_BINDING_INVALID"
            )
        try:
            with self._adapter() as adapter:
                result = adapter.submit_form(
                    form_node_id=command.form_node_id,
                    form_token=command.form_token,
                    user=command.dify_user_id,
                    action=command.form_action,
                    inputs=command.form_inputs,
                )
        except Exception:
            return ChatflowContinuationResult(
                "unknown", safe_reason_code="DIFY_SELECTION_SUBMIT_OUTCOME_UNKNOWN"
            )
        if result.outcome == "accepted":
            return ChatflowContinuationResult("accepted")
        return ChatflowContinuationResult(
            "unknown",
            safe_reason_code=result.safe_reason_code or "DIFY_SELECTION_SUBMIT_OUTCOME_UNKNOWN",
        )

    def recover_after_selection(
        self, command: ChatflowContinuationCommand
    ) -> ChatflowContinuationResult:
        if not command.dify_run_id or not command.dify_user_id:
            return ChatflowContinuationResult(
                "unknown", safe_reason_code="DIFY_SELECTION_RECOVERY_IDENTITY_INVALID"
            )
        try:
            with self._adapter() as adapter:
                result = adapter.recover_events(
                    workflow_run_id=command.dify_run_id,
                    user=command.dify_user_id,
                    include_state_snapshot=True,
                    continue_on_pause=False,
                )
        except Exception:
            return ChatflowContinuationResult(
                "unknown", safe_reason_code="DIFY_SELECTION_RECOVERY_UNKNOWN"
            )
        # A finite snapshot may replay the old form_0 pause after a successful
        # POST; only the bound selection.applied receipt proves acceptance.
        del result
        return ChatflowContinuationResult(
            "unknown", safe_reason_code="DIFY_SELECTION_ACCEPTANCE_UNVERIFIED"
        )

    def cancel_before_selection(self, command: ChatflowCancelCommand) -> ChatflowCancelResult:
        if (
            not command.dify_run_id
            or not command.dify_user_id
            or command.form_node_id != "form_0"
            or not command.form_token
        ):
            return ChatflowCancelResult(
                "definitely_rejected", safe_reason_code="V34_CANCEL_BINDING_INVALID"
            )
        try:
            with self._adapter() as adapter:
                result = adapter.cancel_before_selection(
                    form_node_id=command.form_node_id,
                    form_token=command.form_token,
                    user=command.dify_user_id,
                    expected_fields=command.expected_fields,
                    expected_form_definition_digest=command.expected_form_definition_digest,
                    expected_expiration_time=command.expected_expiration_time,
                )
        except Exception:
            return ChatflowCancelResult(
                "unknown", safe_reason_code="DIFY_CANCEL_SUBMIT_OUTCOME_UNKNOWN"
            )
        return ChatflowCancelResult(
            result.outcome,
            safe_reason_code=result.safe_reason_code,
        )

    def recover_cancel_before_selection(
        self, command: ChatflowCancelCommand
    ) -> ChatflowCancelResult:
        if not command.dify_run_id or not command.dify_user_id:
            return ChatflowCancelResult(
                "unknown", safe_reason_code="DIFY_CANCEL_RECOVERY_IDENTITY_INVALID"
            )
        try:
            with self._adapter() as adapter:
                result = adapter.recover_cancel_before_selection(
                    workflow_run_id=command.dify_run_id,
                    user=command.dify_user_id,
                )
        except Exception:
            return ChatflowCancelResult("unknown", safe_reason_code="DIFY_CANCEL_RECOVERY_UNKNOWN")
        try:
            events = self._sanitized_run_events(result.events, command.dify_run_id)
        except ValueError:
            return ChatflowCancelResult(
                "unknown", safe_reason_code="DIFY_CANCEL_RECOVERY_WIRE_UNKNOWN"
            )
        reason = (
            "DIFY_CANCEL_RECOVERY_REQUIRES_ROOT_EVIDENCE"
            if result.outcome == "received"
            else result.safe_reason_code or "DIFY_CANCEL_RECOVERY_UNKNOWN"
        )
        # The transport cannot establish the durable receipt or terminal business projection.
        return ChatflowCancelResult("unknown", safe_reason_code=reason, events=events)

    def observe_original_run(
        self, dify_run_id: str, dify_user_id: str
    ) -> ChatflowOriginalRunObservationResult:
        if not dify_run_id or not dify_user_id:
            return ChatflowOriginalRunObservationResult(
                "unknown", safe_reason_code="DIFY_RUN_OBSERVATION_IDENTITY_INVALID"
            )
        try:
            with self._adapter() as adapter:
                terminal_observer = getattr(adapter, "observe_original_run_terminal", None)
                strict_terminal_protocol = callable(terminal_observer)
                if strict_terminal_protocol:
                    result = terminal_observer(
                        workflow_run_id=dify_run_id,
                        user=dify_user_id,
                    )
                else:
                    # Keep compatibility with older injected adapter doubles.
                    result = adapter.observe_original_run(
                        workflow_run_id=dify_run_id,
                        user=dify_user_id,
                    )
        except Exception:
            return ChatflowOriginalRunObservationResult(
                "unknown", safe_reason_code="DIFY_RUN_OBSERVATION_UNKNOWN"
            )
        try:
            events = self._sanitized_run_events(result.events, dify_run_id)
        except ValueError:
            return ChatflowOriginalRunObservationResult(
                "unknown", safe_reason_code="DIFY_RUN_OBSERVATION_WIRE_UNKNOWN"
            )
        if (
            strict_terminal_protocol
            and result.outcome == "received"
            and not self._is_unique_terminal_observation(events)
        ):
            return ChatflowOriginalRunObservationResult(
                "unknown", safe_reason_code="DIFY_RUN_OBSERVATION_TERMINAL_UNKNOWN"
            )
        return ChatflowOriginalRunObservationResult(
            result.outcome,
            events=events,
            safe_reason_code=result.safe_reason_code,
        )

    @staticmethod
    def _is_unique_terminal_observation(events: tuple[ChatflowOriginalRunEvent, ...]) -> bool:
        if not events:
            return False
        terminal_indexes = [
            index
            for index, event in enumerate(events)
            if event.event in {"workflow_finished", "workflow_failed", "workflow_cancelled"}
        ]
        if terminal_indexes != [len(events) - 1]:
            return False
        terminal = events[-1]
        if terminal.event == "workflow_finished":
            return terminal.workflow_status in {
                "partial-succeeded",
                "succeeded",
                "failed",
            }
        if terminal.event == "workflow_failed":
            return terminal.workflow_status in {None, "failed"}
        return False

    @staticmethod
    def _sanitized_run_events(
        events: tuple[object, ...], expected_run_id: str
    ) -> tuple[ChatflowOriginalRunEvent, ...]:
        sanitized: list[ChatflowOriginalRunEvent] = []
        for item in events:
            event_name = getattr(item, "event", None)
            run_id = getattr(item, "workflow_run_id", None)
            node_id = getattr(item, "node_id", None)
            form_id = getattr(item, "form_id", None)
            if not isinstance(event_name, str) or not event_name:
                raise ValueError("original-run event name is malformed")
            raw_workflow_status = getattr(item, "workflow_status", None)
            workflow_status: str | None = None
            if event_name in {"workflow_finished", "workflow_failed"}:
                if raw_workflow_status is not None and (
                    not isinstance(raw_workflow_status, str)
                    or not raw_workflow_status
                    or raw_workflow_status.strip() != raw_workflow_status
                ):
                    raise ValueError("workflow terminal status is malformed")
                workflow_status = raw_workflow_status
            if (
                run_id != expected_run_id
                or (node_id is not None and not isinstance(node_id, str))
                or (form_id is not None and not isinstance(form_id, str))
            ):
                raise ValueError("original-run event identity is malformed")
            sanitized.append(
                ChatflowOriginalRunEvent(
                    event=event_name,
                    dify_run_id=run_id,
                    node_id=node_id,
                    form_id=form_id,
                    workflow_status=workflow_status,
                )
            )
        return tuple(sanitized)

    def submit_human_input(self, _command: ChatflowHumanInputCommand) -> ChatflowHumanInputResult:
        return ChatflowHumanInputResult(
            "definitely_rejected", safe_reason_code="V34_H3_HUMAN_INPUT_DISABLED"
        )

    def recover_human_input(self, _command: ChatflowHumanInputCommand) -> ChatflowHumanInputResult:
        return ChatflowHumanInputResult("unknown", safe_reason_code="V34_H3_HUMAN_INPUT_DISABLED")

    def _adapter(self) -> DifyV34HttpSseAdapter:
        return DifyV34HttpSseAdapter(
            self._api_base_url,
            self._app_id,
            self._app_api_token,
            self._workflow_id,
            timeout_seconds=self._timeout_seconds,
        )

    @staticmethod
    def _ordered_reference_files(
        reference_files: tuple[dict[str, object], ...],
    ) -> list[dict[str, str]]:
        if not 1 <= len(reference_files) <= 4:
            raise ValueError("v3.4 requires one primary and up to three auxiliary images")
        result: list[dict[str, str]] = []
        for position, item in enumerate(reference_files):
            expected_slot = "PRIMARY" if position == 0 else f"AUX{position:02d}"
            file_id = item.get("dify_file_id")
            if (
                item.get("position") != position
                or item.get("slot") != expected_slot
                or not isinstance(file_id, str)
                or not file_id
            ):
                raise ValueError("v3.4 reference file order or slot is invalid")
            result.append(
                {
                    "type": "image",
                    "transfer_method": "local_file",
                    "upload_file_id": file_id,
                }
            )
        return result


__all__ = ["DifyV34ChatflowPort"]
