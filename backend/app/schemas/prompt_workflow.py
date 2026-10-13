from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    model_validator,
)

PromptPhase = Literal["initial_preferences", "followup_answer"]
PromptSubmitOutcome = Literal["accepted", "definitely_rejected", "unknown"]
PromptCallbackOutcome = Literal["completed", "needs_input", "definitely_rejected"]
PromptRecoveryOutcome = Literal[
    "accepted", "definitely_rejected", "definitely_not_found", "unknown"
]
_Digest = Annotated[bytes, Field(min_length=32, max_length=32)]
_SafeCode = Annotated[StrictStr, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
_PrivateReference = Annotated[StrictStr, Field(min_length=1, max_length=512)]


class _WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


PROMPT_CONTENT_DIGEST_VERSION = 1
PROMPT_RESULT_DIGEST_VERSION = 1


class PromptSubmissionCommand(_WorkflowModel):
    """Private, immutable input to exactly one preferences-submit call."""

    task_id: UUID
    workflow_run_id: UUID
    workflow_revision: Annotated[StrictInt, Field(gt=0)]
    submission_id: UUID
    phase: PromptPhase
    followup_round: Annotated[StrictInt, Field(ge=0, le=2)]
    content_digest_version: Annotated[StrictInt, Field(gt=0)]
    content_digest: _Digest
    private_payload: dict[str, object]

    @model_validator(mode="after")
    def validate_phase_round(self) -> PromptSubmissionCommand:
        if self.phase == "initial_preferences" and self.followup_round != 0:
            raise ValueError("initial prompt submission must use followup round zero")
        if self.phase == "followup_answer" and self.followup_round not in {1, 2}:
            raise ValueError("followup answer must use round one or two")
        if self.content_digest_version != PROMPT_CONTENT_DIGEST_VERSION:
            raise ValueError("unsupported prompt content digest version")
        expected_digest = canonical_sha256(
            {
                "phase": self.phase,
                "followup_round": self.followup_round,
                "private_payload": json_safe_value(self.private_payload),
            }
        )
        if self.content_digest != expected_digest:
            raise ValueError("prompt content digest does not match its immutable content")
        return self


class PromptSubmitResult(_WorkflowModel):
    """Immediate submit result; provider references are private workflow state."""

    submission_id: UUID
    task_id: UUID
    workflow_run_id: UUID
    workflow_revision: Annotated[StrictInt, Field(gt=0)]
    outcome: PromptSubmitOutcome
    provider_submission_ref: _PrivateReference | None = None
    provider_run_ref: _PrivateReference | None = None
    safe_reason_code: _SafeCode | None = None

    @model_validator(mode="after")
    def validate_outcome_fields(self) -> PromptSubmitResult:
        if self.outcome == "definitely_rejected" and self.safe_reason_code is None:
            raise ValueError("definite rejection requires a safe reason code")
        if self.outcome == "accepted" and self.safe_reason_code is not None:
            raise ValueError("accepted submission cannot carry a failure reason")
        return self


class PromptRecoveryResult(_WorkflowModel):
    """Result of a read-only idempotency/reconciliation check."""

    submission_id: UUID
    task_id: UUID
    workflow_run_id: UUID
    workflow_revision: Annotated[StrictInt, Field(gt=0)]
    outcome: PromptRecoveryOutcome
    provider_submission_ref: _PrivateReference | None = None
    provider_run_ref: _PrivateReference | None = None
    safe_reason_code: _SafeCode | None = None

    @model_validator(mode="after")
    def validate_outcome_fields(self) -> PromptRecoveryResult:
        if self.outcome in {"definitely_rejected", "unknown"} and self.safe_reason_code is None:
            raise ValueError("non-accepted recovery requires a safe reason code")
        return self


class PromptCallback(_WorkflowModel):
    """Private complete callback; `package` is the whole deliver.package value."""

    task_id: UUID
    workflow_run_id: UUID
    workflow_revision: Annotated[StrictInt, Field(gt=0)]
    submission_id: UUID
    submission_content_digest_version: Annotated[StrictInt, Field(gt=0)]
    submission_content_digest: _Digest
    result_digest_version: Annotated[StrictInt, Field(gt=0)]
    result_digest: _Digest
    outcome: PromptCallbackOutcome
    package: object | None = None
    next_question_round: Annotated[StrictInt, Field(ge=1, le=3)] | None = None
    allowed_fields: tuple[StrictStr, ...] | None = None
    locked_fields: tuple[StrictStr, ...] | None = None
    valid_until: datetime | None = None
    safe_reason_code: _SafeCode | None = None

    @model_validator(mode="after")
    def validate_callback_shape(self) -> PromptCallback:
        if self.outcome == "completed" and self.package is None:
            raise ValueError("completed callback requires a full prompt package")
        if self.outcome != "completed" and self.package is not None:
            raise ValueError("non-completed callback cannot include a prompt package")
        if self.outcome == "needs_input":
            if self.next_question_round is None or self.valid_until is None:
                raise ValueError("needs_input callback requires a round and expiration")
            if self.allowed_fields is None or self.locked_fields is None:
                raise ValueError("needs_input callback requires field lists")
        elif any(
            value is not None
            for value in (
                self.next_question_round,
                self.allowed_fields,
                self.locked_fields,
                self.valid_until,
            )
        ):
            raise ValueError("only needs_input callback can include follow-up fields")
        if self.outcome == "definitely_rejected" and self.safe_reason_code is None:
            raise ValueError("definite callback rejection requires a safe reason code")
        if self.valid_until is not None and (
            self.valid_until.tzinfo is None or self.valid_until.utcoffset() != timedelta(0)
        ):
            raise ValueError("callback valid_until must be UTC")
        if self.allowed_fields is not None and (
            len(self.allowed_fields) != len(set(self.allowed_fields))
        ):
            raise ValueError("callback allowed fields must be unique")
        if self.locked_fields is not None and (
            len(self.locked_fields) != len(set(self.locked_fields))
        ):
            raise ValueError("callback locked fields must be unique")
        return self


class CallbackAcceptance(_WorkflowModel):
    status: Literal["accepted", "replayed", "needs_input", "rejected"]
    workflow_run_id: UUID
    workflow_revision: Annotated[StrictInt, Field(gt=0)]
    safe_reason_code: _SafeCode | None = None


def canonical_json_bytes(value: object) -> bytes:
    """Canonical UTF-8 JSON used for stable workflow content/result digests."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise ValueError("workflow content is not canonical JSON") from None


def canonical_sha256(value: object) -> bytes:
    return hashlib.sha256(canonical_json_bytes(value)).digest()


def json_safe_value(value: object) -> object:
    """Detach JSON-shaped mappings/sequences from frozen ActionInput containers."""
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_safe_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise ValueError("value is not JSON-safe")


def validate_result_digest(value: object, digest: bytes) -> bool:
    try:
        return canonical_sha256(value) == digest
    except ValueError:
        return False


def prompt_callback_digest_payload(callback: PromptCallback) -> dict[str, object]:
    """Return callback business content without its self-referential digest."""
    return {
        "task_id": str(callback.task_id),
        "workflow_run_id": str(callback.workflow_run_id),
        "workflow_revision": callback.workflow_revision,
        "submission_id": str(callback.submission_id),
        "submission_content_digest_version": callback.submission_content_digest_version,
        "submission_content_digest": callback.submission_content_digest.hex(),
        "outcome": callback.outcome,
        "package": json_safe_value(callback.package),
        "next_question_round": callback.next_question_round,
        "allowed_fields": list(callback.allowed_fields)
        if callback.allowed_fields is not None
        else None,
        "locked_fields": list(callback.locked_fields)
        if callback.locked_fields is not None
        else None,
        "valid_until": callback.valid_until.isoformat()
        if callback.valid_until is not None
        else None,
        "safe_reason_code": callback.safe_reason_code,
    }
