from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from app.schemas.prompt_packages import ImageType, PromptPackage
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

_SHA256 = r"^[0-9a-f]{64}$"
_REFERENCE_IDS = ("PRIMARY", "AUX01", "AUX02", "AUX03")


class _InternalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ChatflowPromptReference(_InternalModel):
    ref_id: Literal["PRIMARY", "AUX01", "AUX02", "AUX03"]
    asset_id: UUID
    order: Annotated[StrictInt, Field(ge=1, le=4)]
    content_sha256: Annotated[StrictStr, Field(pattern=_SHA256)]


class ChatflowPromptPackageSubmission(_InternalModel):
    """Private Dify handoff. Never include this schema in web response DTOs."""

    contract_version: Literal["chatflow-handoff-v1"]
    business_session_id: UUID
    recommendation_id: UUID
    recommendation_revision: Annotated[StrictInt, Field(ge=1)]
    dify_run_id: Annotated[StrictStr, Field(min_length=1, max_length=320)]
    task_id: UUID
    submission_id: UUID
    package_version: Literal["chatflow-prompt-package-v1"]
    package_digest_version: Literal[1]
    package_digest: Annotated[StrictStr, Field(pattern=_SHA256)]
    image_types: Annotated[list[ImageType], Field(min_length=1, max_length=8)]
    references: Annotated[list[ChatflowPromptReference], Field(min_length=1, max_length=4)]
    prompt_package: PromptPackage = Field(repr=False)

    @model_validator(mode="after")
    def validate_ordered_manifest(self) -> ChatflowPromptPackageSubmission:
        expected_ids = _REFERENCE_IDS[: len(self.references)]
        if (
            [reference.ref_id for reference in self.references] != list(expected_ids)
            or [reference.order for reference in self.references]
            != list(range(1, len(self.references) + 1))
            or len({reference.asset_id for reference in self.references}) != len(self.references)
            or len(set(self.image_types)) != len(self.image_types)
        ):
            raise ValueError("prompt package manifest is not in the required order")
        return self


class ChatflowPromptSlot(_InternalModel):
    task_item_id: UUID
    attempt_id: UUID
    slot_id: Annotated[StrictStr, Field(pattern=r"^slot_[0-9]{2}$")]
    position: Annotated[StrictInt, Field(ge=1, le=8)]
    image_type: ImageType


class GenerationPermit(_InternalModel):
    permit_id: UUID
    permit_version: Literal[2]
    contract_version: Literal["chatflow-generation-permit-v2"]
    slot_mapping_digest: Annotated[StrictStr, Field(pattern=_SHA256)]
    reference_manifest_digest: Annotated[StrictStr, Field(pattern=_SHA256)]
    token: Annotated[StrictStr, Field(min_length=1, max_length=4096)] = Field(repr=False)
    digest: Annotated[StrictStr, Field(pattern=_SHA256)]
    expires_at: str


class ChatflowPromptPackageAccepted(_InternalModel):
    status: Literal["accepted", "replayed"]
    contract_version: Literal["chatflow-handoff-v1"]
    task_id: UUID
    submission_id: UUID
    generation_request_id: UUID
    package_digest: Annotated[StrictStr, Field(pattern=_SHA256)]
    slots: Annotated[list[ChatflowPromptSlot], Field(min_length=1, max_length=8)]
    permit: GenerationPermit
