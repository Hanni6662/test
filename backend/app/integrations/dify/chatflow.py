from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ChatflowStartCommand:
    business_session_id: UUID
    recommendation_id: UUID
    recommendation_revision: int
    start_operation_id: UUID
    dify_user_id: str
    reference_set_id: UUID
    reference_asset_digest: str
    reference_assets: tuple[dict[str, object], ...]
    category: str
    reference_files: tuple[dict[str, object], ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class DifyReferenceUploadCommand:
    business_session_id: UUID
    dify_app_id: str
    dify_user_id: str
    reference_set_image_id: UUID
    model_asset_id: UUID
    slot: str
    position: int
    content_sha256: str
    mime_type: str
    size_bytes: int
    content: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class DifyReferenceUploadResult:
    outcome: Literal["uploaded", "definitely_rejected", "unknown", "not_sent"]
    dify_file_id: str | None = None
    safe_reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class ChatflowFirstFormField:
    output_variable_name: str
    input_type: str
    option_values: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class ChatflowFirstFormBinding:
    form_node_id: str
    form_id: str
    form_token: str = field(repr=False)
    fields: tuple[ChatflowFirstFormField, ...] = field(default=(), repr=False)
    action_ids: tuple[str, ...] = ()
    expiration_time: int | None = None


@dataclass(frozen=True, slots=True)
class ChatflowFirstFormDefinitionResult:
    outcome: Literal["available", "unknown"]
    fields: tuple[ChatflowFirstFormField, ...] = field(default=(), repr=False)
    action_ids: tuple[str, ...] = ()
    expiration_time: int | None = None
    safe_reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class ChatflowFirstFormDefinitionCommand:
    business_session_id: UUID
    recommendation_id: UUID
    recommendation_revision: int
    dify_run_id: str
    dify_user_id: str
    form_node_id: str
    form_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ChatflowStartResult:
    outcome: Literal["started", "definitely_rejected", "unknown"]
    dify_run_id: str | None = None
    safe_reason_code: str | None = None
    first_form: ChatflowFirstFormBinding | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ChatflowContinuationCommand:
    business_session_id: UUID
    recommendation_id: UUID
    recommendation_revision: int
    dify_run_id: str
    dify_user_id: str
    task_id: UUID
    selection_snapshot: dict[str, object]
    selection_submission_id: UUID | None = None
    form_node_id: str | None = None
    form_token: str | None = field(default=None, repr=False)
    form_action: str | None = None
    form_inputs: dict[str, str] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ChatflowContinuationResult:
    outcome: Literal["accepted", "definitely_rejected", "unknown"]
    safe_reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class ChatflowOriginalRunEvent:
    event: str
    dify_run_id: str
    node_id: str | None = None
    form_id: str | None = None
    workflow_status: str | None = None


@dataclass(frozen=True, slots=True)
class ChatflowCancelCommand:
    business_session_id: UUID
    recommendation_id: UUID
    recommendation_revision: int
    dify_run_id: str
    dify_user_id: str
    form_node_id: str
    form_token: str = field(repr=False)
    expected_form_definition_digest: bytes = field(repr=False)
    expected_fields: tuple[ChatflowFirstFormField, ...] = field(repr=False)
    expected_expiration_time: int


@dataclass(frozen=True, slots=True)
class ChatflowCancelResult:
    outcome: Literal["accepted", "definitely_rejected", "unknown"]
    safe_reason_code: str | None = None
    events: tuple[ChatflowOriginalRunEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class ChatflowOriginalRunObservationResult:
    outcome: Literal["received", "unknown"]
    events: tuple[ChatflowOriginalRunEvent, ...] = ()
    safe_reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class ChatflowHumanInputCommand:
    business_session_id: UUID
    recommendation_id: UUID
    recommendation_revision: int
    dify_run_id: str
    dify_user_id: str
    task_id: UUID
    question_id: UUID
    answer_submission_id: UUID
    round_number: int
    form_token: str = field(repr=False)
    answers: dict[str, object] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ChatflowHumanInputResult:
    outcome: Literal["accepted", "definitely_rejected", "unknown"]
    safe_reason_code: str | None = None


class DifyChatflowPort(Protocol):
    """One-shot Chatflow port; implementations never receive a DB Session."""

    def start_recommendation(self, command: ChatflowStartCommand) -> ChatflowStartResult: ...

    def get_first_form_definition(
        self, command: ChatflowFirstFormDefinitionCommand
    ) -> ChatflowFirstFormDefinitionResult: ...

    def upload_reference(
        self, command: DifyReferenceUploadCommand
    ) -> DifyReferenceUploadResult: ...

    def continue_after_selection(
        self, command: ChatflowContinuationCommand
    ) -> ChatflowContinuationResult: ...

    def recover_after_selection(
        self, command: ChatflowContinuationCommand
    ) -> ChatflowContinuationResult: ...

    def cancel_before_selection(self, command: ChatflowCancelCommand) -> ChatflowCancelResult: ...

    def recover_cancel_before_selection(
        self, command: ChatflowCancelCommand
    ) -> ChatflowCancelResult: ...

    def observe_original_run(
        self, dify_run_id: str, dify_user_id: str
    ) -> ChatflowOriginalRunObservationResult: ...

    def submit_human_input(
        self, command: ChatflowHumanInputCommand
    ) -> ChatflowHumanInputResult: ...

    def recover_human_input(
        self, command: ChatflowHumanInputCommand
    ) -> ChatflowHumanInputResult: ...


class DifyChatflowNotConfigured(RuntimeError):
    """Raised when a non-stub Chatflow mode is requested before a safe adapter exists."""


class StubDifyChatflowAdapter:
    """Offline test adapter that records calls and never opens a socket."""

    mode = "mock"

    def __init__(
        self,
        *,
        lose_answer_response_once: bool = False,
        lose_reference_upload_response_once: bool = False,
        lose_reference_upload_response_at: int | None = None,
    ) -> None:
        self.start_calls: list[ChatflowStartCommand] = []
        self.reference_upload_calls: list[DifyReferenceUploadCommand] = []
        self.continuation_calls: list[ChatflowContinuationCommand] = []
        self.cancel_calls: list[ChatflowCancelCommand] = []
        self.cancel_recovery_calls: list[ChatflowCancelCommand] = []
        self.original_run_observation_calls: list[tuple[str, str]] = []
        self.human_input_calls: list[ChatflowHumanInputCommand] = []
        self.human_input_recovery_calls: list[ChatflowHumanInputCommand] = []
        self._accepted_answers: dict[UUID, ChatflowHumanInputCommand] = {}
        self._lose_answer_response_once = lose_answer_response_once
        self._lose_reference_upload_response_once = lose_reference_upload_response_once
        self._lose_reference_upload_response_at = lose_reference_upload_response_at

    def start_recommendation(self, command: ChatflowStartCommand) -> ChatflowStartResult:
        self.start_calls.append(command)
        return ChatflowStartResult(
            "started", dify_run_id=f"stub-run-{command.business_session_id.hex}"
        )

    def get_first_form_definition(
        self, _command: ChatflowFirstFormDefinitionCommand
    ) -> ChatflowFirstFormDefinitionResult:
        return ChatflowFirstFormDefinitionResult(
            "unknown", safe_reason_code="DIFY_FIRST_FORM_NOT_AVAILABLE_IN_STUB"
        )

    def upload_reference(self, command: DifyReferenceUploadCommand) -> DifyReferenceUploadResult:
        self.reference_upload_calls.append(command)
        file_id = f"stub-file-{command.business_session_id.hex}-{command.position}"
        if self._lose_reference_upload_response_once or (
            self._lose_reference_upload_response_at == command.position
        ):
            self._lose_reference_upload_response_once = False
            return DifyReferenceUploadResult(
                "unknown", safe_reason_code="DIFY_UPLOAD_OUTCOME_UNKNOWN"
            )
        return DifyReferenceUploadResult("uploaded", dify_file_id=file_id)

    def continue_after_selection(
        self, command: ChatflowContinuationCommand
    ) -> ChatflowContinuationResult:
        self.continuation_calls.append(command)
        return ChatflowContinuationResult("accepted")

    def recover_after_selection(
        self, command: ChatflowContinuationCommand
    ) -> ChatflowContinuationResult:
        if any(item == command for item in self.continuation_calls):
            return ChatflowContinuationResult("accepted")
        return ChatflowContinuationResult("unknown", "CHATFLOW_RESUME_OUTCOME_UNKNOWN")

    def cancel_before_selection(self, command: ChatflowCancelCommand) -> ChatflowCancelResult:
        self.cancel_calls.append(command)
        return ChatflowCancelResult(
            "definitely_rejected", safe_reason_code="V34_CANCEL_UNAVAILABLE_IN_STUB"
        )

    def recover_cancel_before_selection(
        self, command: ChatflowCancelCommand
    ) -> ChatflowCancelResult:
        self.cancel_recovery_calls.append(command)
        return ChatflowCancelResult(
            "unknown", safe_reason_code="V34_CANCEL_RECOVERY_UNAVAILABLE_IN_STUB"
        )

    def observe_original_run(
        self, dify_run_id: str, dify_user_id: str
    ) -> ChatflowOriginalRunObservationResult:
        self.original_run_observation_calls.append((dify_run_id, dify_user_id))
        return ChatflowOriginalRunObservationResult(
            "unknown", safe_reason_code="V34_RUN_OBSERVATION_UNAVAILABLE_IN_STUB"
        )

    def submit_human_input(self, command: ChatflowHumanInputCommand) -> ChatflowHumanInputResult:
        self.human_input_calls.append(command)
        self._accepted_answers[command.answer_submission_id] = command
        if self._lose_answer_response_once:
            self._lose_answer_response_once = False
            return ChatflowHumanInputResult("unknown", "CHATFLOW_ANSWER_OUTCOME_UNKNOWN")
        return ChatflowHumanInputResult("accepted")

    def recover_human_input(self, command: ChatflowHumanInputCommand) -> ChatflowHumanInputResult:
        self.human_input_recovery_calls.append(command)
        if self._accepted_answers.get(command.answer_submission_id) == command:
            return ChatflowHumanInputResult("accepted")
        return ChatflowHumanInputResult("unknown", "CHATFLOW_ANSWER_RECOVERY_UNKNOWN")
