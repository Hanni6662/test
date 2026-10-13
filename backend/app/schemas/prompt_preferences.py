from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from app.schemas.prompt_workflow import PromptCallback, canonical_sha256, json_safe_value
from app.schemas.recommendations import IMAGE_TYPES, PlatformName
from app.schemas.tasks import PreferenceSelection
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

_SAFE_CODE = Annotated[StrictStr, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
_SHA256_HEX = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
_KEY = re.compile(r"^[!-~]{1,200}$")


class _R3Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


FollowupAnswer = PreferenceSelection | PlatformName


class PromptPreferenceRequest(_R3Model):
    """One bounded answer to the currently open preference question."""

    # FastAPI parses HTTP JSON into Python values before model validation; UUID
    # strings therefore need normal UUID parsing here, while StrictInt/StrictStr
    # still enforce the scalar contract of workflow fields and answer keys.
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)

    workflow_run_id: UUID
    workflow_revision: Annotated[StrictInt, Field(gt=0)]
    question_round: Annotated[StrictInt, Field(ge=1, le=2)]
    answers: Annotated[dict[StrictStr, FollowupAnswer], Field(min_length=1, max_length=5)]

    @model_validator(mode="after")
    def validate_answer_fields(self) -> PromptPreferenceRequest:
        if any(
            field not in {"language", "platform", "usage_scene", "visual_style", "selling_points"}
            for field in self.answers
        ):
            raise ValueError("answers contain an unsupported field")
        return self


class PromptPreferenceAccepted(_R3Model):
    status: Literal["accepted", "replayed"]
    task_id: UUID
    submission_id: UUID
    workflow_run_id: UUID
    workflow_revision: Annotated[StrictInt, Field(gt=0)]
    question_round: Annotated[StrictInt, Field(ge=1, le=2)]


class PromptCallbackRequest(_R3Model):
    """Wire DTO with hexadecimal digests and an explicit submission phase."""

    task_id: UUID
    workflow_run_id: UUID
    workflow_revision: Annotated[StrictInt, Field(gt=0)]
    submission_id: UUID
    phase: Literal["initial_preferences", "followup_answer"]
    submission_content_digest_version: Annotated[StrictInt, Field(gt=0)]
    submission_content_digest: _SHA256_HEX
    result_digest_version: Annotated[StrictInt, Field(gt=0)]
    result_digest: _SHA256_HEX
    outcome: Literal["completed", "needs_input", "definitely_rejected"]
    package: object | None = None
    next_question_round: Annotated[StrictInt, Field(ge=1, le=3)] | None = None
    allowed_fields: tuple[StrictStr, ...] | None = None
    locked_fields: tuple[StrictStr, ...] | None = None
    valid_until: datetime | None = None
    safe_reason_code: _SAFE_CODE | None = None

    @model_validator(mode="after")
    def validate_wire_callback(self) -> PromptCallbackRequest:
        try:
            self.to_domain()
        except (TypeError, ValueError):
            raise ValueError("callback fields are invalid") from None
        return self

    def to_domain(self) -> PromptCallback:
        return PromptCallback.model_validate(
            {
                "task_id": self.task_id,
                "workflow_run_id": self.workflow_run_id,
                "workflow_revision": self.workflow_revision,
                "submission_id": self.submission_id,
                "submission_content_digest_version": self.submission_content_digest_version,
                "submission_content_digest": bytes.fromhex(self.submission_content_digest),
                "result_digest_version": self.result_digest_version,
                "result_digest": bytes.fromhex(self.result_digest),
                "outcome": self.outcome,
                "package": self.package,
                "next_question_round": self.next_question_round,
                "allowed_fields": tuple(self.allowed_fields)
                if self.allowed_fields is not None
                else None,
                "locked_fields": tuple(self.locked_fields)
                if self.locked_fields is not None
                else None,
                "valid_until": self.valid_until,
                "safe_reason_code": self.safe_reason_code,
            },
            strict=True,
        )


class RecommendationResultCallback(_R3Model):
    """Synthetic-only result envelope for the current S5 recommendation run."""

    recommendation_id: UUID
    reference_set_id: UUID
    recommendation_revision: Annotated[StrictInt, Field(gt=0)]
    run_ref: Annotated[StrictStr, Field(min_length=1, max_length=320)]
    status: Literal["ready", "failed"]
    original_recommendation: dict[str, object] | None = None
    public_options: dict[str, object] | None = None
    image_types: tuple[StrictStr, ...] = ()
    audit: dict[str, object] | None = None
    state: dict[str, object] = Field(default_factory=dict)
    form_ref: Annotated[StrictStr, Field(min_length=1, max_length=320)] | None = None
    valid_until: datetime | None = None
    failure_code: _SAFE_CODE | None = None
    result_digest_version: Annotated[StrictInt, Field(gt=0)]
    result_digest: _SHA256_HEX

    @model_validator(mode="after")
    def validate_result_shape(self) -> RecommendationResultCallback:
        if self.result_digest_version != 1:
            raise ValueError("unsupported recommendation result digest version")
        if self.status == "ready":
            if (
                self.original_recommendation is None
                or self.public_options is None
                or self.audit is None
                or self.form_ref is None
                or self.valid_until is None
                or self.failure_code is not None
                or tuple(self.image_types) != IMAGE_TYPES
                or self.valid_until.tzinfo is None
                or self.valid_until.utcoffset() != timedelta(0)
            ):
                raise ValueError("ready recommendation result is incomplete")
        elif (
            self.failure_code is None
            or self.original_recommendation is not None
            or self.public_options is not None
            or self.audit is not None
            or self.form_ref is not None
            or self.valid_until is not None
            or self.image_types
        ):
            raise ValueError("failed recommendation result has invalid fields")
        return self


class InternalCallbackAccepted(_R3Model):
    status: Literal["accepted", "replayed", "needs_input", "rejected"]
    business_id: UUID
    workflow_run_id: UUID | None = None
    workflow_revision: Annotated[StrictInt, Field(gt=0)] | None = None
    safe_reason_code: _SAFE_CODE | None = None


def valid_idempotency_key(value: str) -> bool:
    return bool(_KEY.fullmatch(value))


def recommendation_callback_digest_payload(
    callback: RecommendationResultCallback,
) -> dict[str, object]:
    """Canonical callback content excluding its self-referential digest."""
    return {
        "recommendation_id": str(callback.recommendation_id),
        "reference_set_id": str(callback.reference_set_id),
        "recommendation_revision": callback.recommendation_revision,
        "run_ref": callback.run_ref,
        "status": callback.status,
        "original_recommendation": json_safe_value(callback.original_recommendation),
        "public_options": json_safe_value(callback.public_options),
        "image_types": list(callback.image_types),
        "audit": json_safe_value(callback.audit),
        "state": json_safe_value(callback.state),
        "form_ref": callback.form_ref,
        "valid_until": callback.valid_until.isoformat()
        if callback.valid_until is not None
        else None,
        "failure_code": callback.failure_code,
    }


def recommendation_callback_digest(callback: RecommendationResultCallback) -> str:
    return canonical_sha256(recommendation_callback_digest_payload(callback)).hex()
