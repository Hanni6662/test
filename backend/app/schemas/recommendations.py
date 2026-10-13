from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

PlatformName = Literal[
    "亚马逊主图",
    "亚马逊高级A+",
    "TEMU",
    "TEMU服饰类",
    "TIKTOK",
    "淘宝",
    "京东",
    "拼多多",
    "1688",
]
ImageType = Literal[
    "品牌导购主视觉",
    "核心卖点信息图",
    "生活方式海报",
    "多场景展示图",
    "场景化卖点图",
    "使用体验图",
    "SKU展示图",
    "细节展示图",
]
RecommendationStatus = Literal["analyzing", "ready", "failed", "expired"]
PreferenceCode = Literal["A", "B", "C"]

PLATFORMS: tuple[PlatformName, ...] = (
    "亚马逊主图",
    "亚马逊高级A+",
    "TEMU",
    "TEMU服饰类",
    "TIKTOK",
    "淘宝",
    "京东",
    "拼多多",
    "1688",
)
IMAGE_TYPES: tuple[ImageType, ...] = (
    "品牌导购主视觉",
    "核心卖点信息图",
    "生活方式海报",
    "多场景展示图",
    "场景化卖点图",
    "使用体验图",
    "SKU展示图",
    "细节展示图",
)


class RecommendationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_set_id: UUID


class RecommendationAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recommendation_id: UUID
    status: Literal["analyzing"] = "analyzing"


class PreferenceChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: PreferenceCode
    text: Annotated[str, Field(min_length=1, max_length=3500)]


class PreferenceFieldOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    choices: Annotated[list[PreferenceChoice], Field(min_length=3, max_length=3)]
    allow_agent_design: bool = True
    allow_custom: bool = True

    @field_validator("choices")
    @classmethod
    def require_ordered_choices(cls, value: list[PreferenceChoice]) -> list[PreferenceChoice]:
        if [choice.code for choice in value] != ["A", "B", "C"]:
            raise ValueError("preference choices must be ordered A, B, C")
        return value


class RecommendationPublicOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    language: PreferenceFieldOptions
    platform: Annotated[list[PlatformName], Field(min_length=9, max_length=9)]
    usage_scene: PreferenceFieldOptions
    visual_style: PreferenceFieldOptions
    selling_points: PreferenceFieldOptions

    @field_validator("platform")
    @classmethod
    def require_schema_platforms(cls, value: list[PlatformName]) -> list[PlatformName]:
        if value != list(PLATFORMS):
            raise ValueError("platform choices must match task-create-v2 schema order")
        return value


class RecommendationPublicError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal["RECOMMENDATION_FAILED"]
    message: Literal["推荐分析暂时不可用，请稍后重试"]


class RecommendationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recommendation_id: UUID
    reference_set_id: UUID
    status: RecommendationStatus
    revision: Annotated[int, Field(ge=1)]
    options: RecommendationPublicOptions | None = None
    image_types: Annotated[list[ImageType], Field(min_length=8, max_length=8)]
    valid_until: datetime | None = None
    error: RecommendationPublicError | None = None

    @field_validator("image_types")
    @classmethod
    def require_schema_image_types(cls, value: list[ImageType]) -> list[ImageType]:
        if value != list(IMAGE_TYPES):
            raise ValueError("image types must match task-create-v2 schema order")
        return value
