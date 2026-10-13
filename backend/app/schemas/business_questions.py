from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

_FIELD_ID = r"^[a-z][a-z0-9_]{0,63}$"


class HumanInputField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    field_id: Annotated[StrictStr, Field(pattern=_FIELD_ID)]
    label: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    kind: Literal["text", "single_choice", "multi_choice"]
    required: StrictBool = True
    options: Annotated[
        list[Annotated[StrictStr, Field(min_length=1, max_length=200)]], Field(max_length=20)
    ] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_options(self) -> HumanInputField:
        if self.kind == "text" and self.options:
            raise ValueError("text fields cannot define options")
        if self.kind != "text" and not self.options:
            raise ValueError("choice fields must define options")
        if len(self.options) != len(set(self.options)):
            raise ValueError("field options must be unique")
        return self


class HumanInputRequiredPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: UUID
    round_number: Annotated[StrictInt, Field(ge=1, le=2)]
    question_text: Annotated[StrictStr, Field(min_length=1, max_length=3000)]
    fields: Annotated[list[HumanInputField], Field(min_length=1, max_length=10)]
    expires_at: datetime
    form_token: Annotated[StrictStr, Field(min_length=1, max_length=4096)] | None = Field(
        default=None, repr=False
    )

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("question expiry must be an aware UTC timestamp")
        return value

    @model_validator(mode="after")
    def validate_fields(self) -> HumanInputRequiredPayload:
        ids = [item.field_id for item in self.fields]
        if len(ids) != len(set(ids)):
            raise ValueError("question field ids must be unique")
        if self.form_token is not None and not self.form_token.strip():
            raise ValueError("form token is invalid")
        return self


AnswerValue = Annotated[
    StrictStr
    | Annotated[
        list[Annotated[StrictStr, Field(min_length=1, max_length=200)]],
        Field(min_length=1, max_length=20),
    ],
    Field(union_mode="smart"),
]


class BusinessQuestionAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    answers: Annotated[
        dict[Annotated[StrictStr, Field(pattern=_FIELD_ID)], AnswerValue],
        Field(min_length=1, max_length=10),
    ]


class BusinessQuestionPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question_id: UUID
    question_text: str
    fields: list[HumanInputField]
    round_number: Annotated[int, Field(ge=1, le=2)]
    expires_at: datetime
    status: Literal[
        "awaiting_form_binding",
        "awaiting_answer",
        "confirming",
        "answered",
        "rejected",
        "expired",
        "reconciling",
    ]
    answer_status: Literal["confirming", "accepted", "rejected", "unknown"] | None = None
    can_answer: bool


class BusinessQuestionAnswerAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["chatflow-handoff-v1"] = "chatflow-handoff-v1"
    business_session_id: UUID
    question_id: UUID
    answer_submission_id: UUID
    status: Literal["confirming", "accepted", "rejected", "replayed"]
