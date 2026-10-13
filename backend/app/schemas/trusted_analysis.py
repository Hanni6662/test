from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationInfo,
    field_validator,
)

ReferenceAlias = Literal["PRIMARY", "AUX01", "AUX02", "AUX03"]
AuxiliaryAlias = Literal["AUX01", "AUX02", "AUX03"]
AuxiliaryRelationKind = Literal["confirm", "supplement", "ignored_conflict"]
AuxiliaryScope = Literal["global", "front_only", "back_only", "side_only", "detail_only"]
ImageMimeType = Literal["image/jpeg", "image/png", "image/webp"]

_Sha256 = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
_NonBlank = Annotated[StrictStr, Field(min_length=1)]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TrustedAnalysisReference(_FrozenModel):
    alias: ReferenceAlias
    position: Annotated[StrictInt, Field(ge=0, le=3)]
    reference_set_image_id: UUID
    model_asset_id: UUID
    source_asset_id: UUID
    model_sha256: _Sha256
    source_sha256: _Sha256
    model_mime_type: ImageMimeType
    source_mime_type: ImageMimeType
    model_size_bytes: Annotated[StrictInt, Field(gt=0)]
    source_size_bytes: Annotated[StrictInt, Field(gt=0)]
    model_width: Annotated[StrictInt, Field(gt=0)]
    model_height: Annotated[StrictInt, Field(gt=0)]
    source_width: Annotated[StrictInt, Field(gt=0)]
    source_height: Annotated[StrictInt, Field(gt=0)]


class TrustedRelationProvenance(_FrozenModel):
    synthetic: Literal[True]
    rule_version: Annotated[StrictStr, Field(min_length=1, max_length=120)]


class TrustedAuxiliaryRelation(_FrozenModel):
    alias: AuxiliaryAlias
    relation: AuxiliaryRelationKind
    scope: AuxiliaryScope
    view: Annotated[StrictStr, Field(min_length=1, max_length=120)]
    evidence: Annotated[StrictStr, Field(min_length=1, max_length=500)]
    provenance: TrustedRelationProvenance

    @field_validator("view", "evidence")
    @classmethod
    def require_non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("trusted relation text must not be blank")
        return value


class TrustedAnalysisSnapshot(_FrozenModel):
    """Private, versioned analysis context; never returned by a public API."""

    snapshot_id: UUID
    reference_set_id: UUID
    recommendation_id: UUID
    recommendation_revision: Annotated[StrictInt, Field(gt=0)]
    source_kind: Literal["synthetic_mock"]
    source_version: Annotated[StrictStr, Field(min_length=1, max_length=120)]
    analysis_run_id: UUID
    analysis_revision: Annotated[StrictInt, Field(gt=0)]
    snapshot_schema_version: Annotated[StrictInt, Field(gt=0)]
    snapshot_digest_version: Literal[1]
    snapshot_digest: _Sha256
    input_digest_version: Annotated[StrictInt, Field(gt=0)]
    input_digest: _Sha256
    references: Annotated[tuple[TrustedAnalysisReference, ...], Field(min_length=1, max_length=4)]
    auxiliary_relations: Annotated[tuple[TrustedAuxiliaryRelation, ...], Field(max_length=3)]
    created_at: datetime

    @field_validator("source_version")
    @classmethod
    def require_source_version(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source version must not be blank")
        return value

    @field_validator("created_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("trusted analysis timestamps must be UTC")
        return value

    @field_validator("references")
    @classmethod
    def require_canonical_alias_order(
        cls, values: tuple[TrustedAnalysisReference, ...]
    ) -> tuple[TrustedAnalysisReference, ...]:
        aliases = [reference.alias for reference in values]
        expected = ["PRIMARY", *(f"AUX{index:02d}" for index in range(1, len(values)))]
        positions = list(range(len(values)))
        if aliases != expected or [reference.position for reference in values] != positions:
            raise ValueError("trusted reference aliases must be complete and ordered")
        asset_ids = [
            asset_id
            for reference in values
            for asset_id in (
                reference.reference_set_image_id,
                reference.model_asset_id,
                reference.source_asset_id,
            )
        ]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("trusted reference asset identifiers must be unique")
        return values

    @field_validator("auxiliary_relations")
    @classmethod
    def require_relation_alias_order(
        cls,
        values: tuple[TrustedAuxiliaryRelation, ...],
        info: ValidationInfo,
    ) -> tuple[TrustedAuxiliaryRelation, ...]:
        references = info.data.get("references", [])
        expected = [reference.alias for reference in references[1:]]
        if [relation.alias for relation in values] != expected:
            raise ValueError("trusted relation aliases must match the saved AUX slots")
        return tuple(values)
