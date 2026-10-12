from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

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
AuxiliaryRelationKind = Literal["confirm", "supplement", "ignored_conflict"]
AuxiliaryScope = Literal[
    "global",
    "front_only",
    "back_only",
    "side_only",
    "detail_only",
]
PreferenceSource = Literal["A", "B", "C", "D", "custom", "PLATFORM"]

_NonBlank = Annotated[StrictStr, Field(min_length=1)]


class _InternalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PhysicalProfile(_InternalModel):
    silhouette_proportions: _NonBlank
    parts_count: _NonBlank
    openings_connections: _NonBlank
    material_appearance: _NonBlank
    color_zones: _NonBlank
    pattern_logo_placement: _NonBlank
    visible_texture: _NonBlank

    @field_validator(
        "silhouette_proportions",
        "parts_count",
        "openings_connections",
        "material_appearance",
        "color_zones",
        "pattern_logo_placement",
        "visible_texture",
    )
    @classmethod
    def require_non_whitespace(cls, value: str) -> str:
        if not value.strip() or len(value) > 900:
            raise ValueError("invalid physical profile text")
        return value


class AuxiliaryRelation(_InternalModel):
    id: Annotated[StrictStr, Field(min_length=1, max_length=5)]
    view: Annotated[StrictStr, Field(min_length=1, max_length=120)]
    relation: AuxiliaryRelationKind
    scope: AuxiliaryScope
    evidence: Annotated[StrictStr, Field(min_length=1, max_length=500)]

    @field_validator("view", "evidence")
    @classmethod
    def require_non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("invalid auxiliary relation text")
        return value


class AssetIds(_InternalModel):
    primary: Literal["PRIMARY"]
    auxiliary: list[Annotated[StrictStr, Field(min_length=1, max_length=5)]]


class PreferenceValue(_InternalModel):
    name: Annotated[StrictStr, Field(min_length=1, max_length=32)]
    source: PreferenceSource
    value: Annotated[StrictStr, Field(min_length=1, max_length=3500)]

    @field_validator("name", "value")
    @classmethod
    def require_non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("invalid preference text")
        return value


class PlatformCanvas(_InternalModel):
    type: ImageType
    canvas: Annotated[StrictStr, Field(min_length=1, max_length=100)]


class PlatformProfile(_InternalModel):
    platform: PlatformName
    rule: Annotated[StrictStr, Field(min_length=1, max_length=1200)]
    canvases: list[PlatformCanvas]

    @field_validator("rule")
    @classmethod
    def require_non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("invalid platform rule")
        return value


class PromptBasis(_InternalModel):
    product: Annotated[StrictStr, Field(min_length=1, max_length=1000)]
    declared_category: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    primary_view: Annotated[StrictStr, Field(min_length=1, max_length=300)]
    physical_profile: PhysicalProfile
    auxiliary_relations: list[AuxiliaryRelation]
    asset_ids: AssetIds
    identity_constraints: Annotated[list[_NonBlank], Field(min_length=3, max_length=8)]
    identity_prompt: Annotated[StrictStr, Field(min_length=1, max_length=3500)]
    unknowns: list[_NonBlank]
    preference_form: dict[Annotated[StrictStr, Field(min_length=1, max_length=1)], PreferenceValue]
    image_types: Annotated[list[ImageType], Field(min_length=1, max_length=8)]
    platform_profile: PlatformProfile

    @field_validator("product", "declared_category", "primary_view", "identity_prompt")
    @classmethod
    def require_non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("invalid prompt basis text")
        return value

    @field_validator("identity_constraints")
    @classmethod
    def validate_identity_constraints(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 450 for value in values):
            raise ValueError("invalid identity constraints")
        return values

    @field_validator("unknowns")
    @classmethod
    def validate_unknowns(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 300 for value in values):
            raise ValueError("invalid unknown facts")
        return values


class PreferenceUse(_InternalModel):
    field: Annotated[StrictStr, Field(min_length=1, max_length=1)]
    use: Annotated[StrictStr, Field(min_length=1, max_length=1200)]

    @field_validator("use")
    @classmethod
    def require_non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("invalid preference use")
        return value


class ReferencePlan(_InternalModel):
    identity_anchor_id: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    view_reference_ids: list[Annotated[StrictStr, Field(min_length=1, max_length=100)]]
    excluded_reference_ids: list[Annotated[StrictStr, Field(min_length=1, max_length=100)]]
    target_view: Annotated[StrictStr, Field(min_length=1, max_length=200)]

    @field_validator("target_view")
    @classmethod
    def require_non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("invalid target view")
        return value


class PromptImage(_InternalModel):
    type: ImageType
    canvas: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    design_prompt: Annotated[StrictStr, Field(min_length=1, max_length=16000)]
    identity_prompt: Annotated[StrictStr, Field(min_length=1, max_length=3500)]
    resources: Annotated[list[PreferenceUse], Field(min_length=1)]
    reference_plan: ReferencePlan

    @field_validator("design_prompt", "identity_prompt")
    @classmethod
    def require_non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("invalid prompt text")
        return value


class PromptPackage(_InternalModel):
    """Private v3.3 Chatflow deliver package; never serialize in a public DTO."""

    basis: PromptBasis
    images: Annotated[list[PromptImage], Field(min_length=1, max_length=8)]
