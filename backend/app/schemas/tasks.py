from __future__ import annotations

import re
from typing import Annotated, Literal
from uuid import UUID

from app.schemas.recommendations import IMAGE_TYPES, ImageType, PlatformName
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

_NON_WHITESPACE = re.compile(r"\S")


class PreferenceMode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["A", "B", "C", "D"]


class CustomPreference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["custom"]
    text: Annotated[str, Field(min_length=1, max_length=3500)]

    @field_validator("text")
    @classmethod
    def require_non_whitespace(cls, value: str) -> str:
        if _NON_WHITESPACE.search(value) is None:
            raise ValueError("custom preference must contain non-whitespace text")
        return value


PreferenceSelection = Annotated[PreferenceMode | CustomPreference, Field(discriminator="mode")]


class TaskPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    language: PreferenceSelection
    platform: PlatformName
    usage_scene: PreferenceSelection
    visual_style: PreferenceSelection
    selling_points: PreferenceSelection


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2]
    reference_set_id: UUID
    recommendation_id: UUID
    recommendation_revision: Annotated[StrictInt, Field(ge=1)]
    preferences: TaskPreferences
    image_types: Annotated[list[ImageType], Field(min_length=1, max_length=8)]

    @field_validator("image_types")
    @classmethod
    def require_unique_image_types(cls, value: list[ImageType]) -> list[ImageType]:
        if len(value) != len(set(value)):
            raise ValueError("image_types must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_image_type_contract(self) -> TaskCreateRequest:
        if any(image_type not in IMAGE_TYPES for image_type in self.image_types):
            raise ValueError("image_types contains an unsupported value")
        return self


TaskStatus = Literal[
    "queued",
    "prompting",
    "needs_input",
    "generating",
    "reconciling",
    "succeeded",
    "partially_succeeded",
    "failed",
]
TaskItemStatus = Literal["queued", "running", "saving", "reconciling", "succeeded", "failed"]


class TaskAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    task_id: UUID
    status: TaskStatus
    planned_count: Annotated[int, Field(ge=1, le=8)]


class TaskCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    planned: Annotated[int, Field(ge=0)]
    succeeded: Annotated[int, Field(ge=0)]
    failed: Annotated[int, Field(ge=0)]
    pending: Annotated[int, Field(ge=0)]
    unresolved: Annotated[int, Field(ge=0)]


class TaskItemPublicError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal[
        "IMAGE_GENERATION_FAILED",
        "V34_PRE_GENERATION_FAILED",
        "V34_WORKFLOW_ENDED_WITHOUT_GENERATION",
        "GENERATION_PERMIT_EXPIRED_BEFORE_ACCEPTANCE",
    ]
    message: Literal[
        "本张图片生成失败，可单独重试。",
        "商品身份核对未通过，未发起生图。",
        "流程已结束，但未取得生图许可，本次未发起生图。",
        "生图许可已过期，本次未发起生图。",
    ]

    @model_validator(mode="after")
    def validate_error_message(self) -> TaskItemPublicError:
        expected = {
            "IMAGE_GENERATION_FAILED": "本张图片生成失败，可单独重试。",
            "V34_PRE_GENERATION_FAILED": "商品身份核对未通过，未发起生图。",
            "V34_WORKFLOW_ENDED_WITHOUT_GENERATION": (
                "流程已结束，但未取得生图许可，本次未发起生图。"
            ),
            "GENERATION_PERMIT_EXPIRED_BEFORE_ACCEPTANCE": "生图许可已过期，本次未发起生图。",
        }[self.code]
        if self.message != expected:
            raise ValueError("task item public error message does not match its code")
        return self


class TaskItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_item_id: UUID
    position: Annotated[int, Field(ge=1, le=8)]
    image_type: ImageType
    status: TaskItemStatus
    image_id: UUID | None = None
    preview_ready: bool
    width: Annotated[int, Field(gt=0)] | None = None
    height: Annotated[int, Field(gt=0)] | None = None
    error: TaskItemPublicError | None = None


class TaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    task_id: UUID
    status: TaskStatus
    counts: TaskCounts
    items: list[TaskItemResponse]
