from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ReferenceImageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    image_id: UUID
    slot: Literal["PRIMARY", "AUX01", "AUX02", "AUX03"]
    mime_type: Literal["image/jpeg", "image/png", "image/webp"]
    size_bytes: int = Field(gt=0, le=10 * 1024 * 1024)
    width: int = Field(gt=0, le=25_000_000)
    height: int = Field(gt=0, le=25_000_000)


class ReferencePrimaryImageResponse(ReferenceImageResponse):
    slot: Literal["PRIMARY"]


class ReferenceAuxiliaryImageResponse(ReferenceImageResponse):
    slot: Literal["AUX01", "AUX02", "AUX03"]


class ReferenceSetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_set_id: UUID
    product_category: str = Field(min_length=1, max_length=100)
    status: Literal["ready"]
    primary_image: ReferencePrimaryImageResponse
    auxiliary_images: list[ReferenceAuxiliaryImageResponse] = Field(max_length=3)
