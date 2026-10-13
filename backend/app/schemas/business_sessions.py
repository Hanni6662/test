from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from app.schemas.business_questions import BusinessQuestionPublic
from app.schemas.recommendations import IMAGE_TYPES, ImageType, RecommendationPublicOptions
from app.schemas.tasks import TaskPreferences
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REFERENCE_SLOTS = ("PRIMARY", "AUX01", "AUX02", "AUX03")


class BusinessSessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_set_id: UUID


class BusinessSessionSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recommendation_id: UUID
    recommendation_revision: Annotated[StrictInt, Field(ge=1)]
    preferences: TaskPreferences
    image_types: Annotated[list[ImageType], Field(min_length=1, max_length=8)]

    @field_validator("image_types")
    @classmethod
    def validate_image_types(cls, value: list[ImageType]) -> list[ImageType]:
        if len(value) != len(set(value)) or any(item not in IMAGE_TYPES for item in value):
            raise ValueError("image types must be unique supported values")
        return value


class BusinessSessionCreated(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["chatflow-handoff-v1"] = "chatflow-handoff-v1"
    business_session_id: UUID
    recommendation_id: UUID
    status: Literal["starting", "replayed"]


class BusinessSessionV34Created(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["chatflow-handoff-v1"] = "chatflow-handoff-v1"
    flow_version: Literal["visual-v3.4-single-selection"]
    business_session_id: UUID
    recommendation_id: UUID
    status: Literal["starting", "replayed"]


class DifyOriginalRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category_snapshot: Annotated[str, Field(min_length=1, max_length=100)]
    field_texts: dict[
        str,
        Annotated[
            list[Annotated[str, Field(min_length=1, max_length=3500)]],
            Field(min_length=3, max_length=3),
        ],
    ]
    image_types: Annotated[list[ImageType], Field(min_length=8, max_length=8)]

    @model_validator(mode="after")
    def validate_original_recommendation(self) -> DifyOriginalRecommendation:
        expected_fields = {"language", "usage_scene", "visual_style", "selling_points"}
        if set(self.field_texts) != expected_fields:
            raise ValueError("recommendation fields do not match the selection contract")
        if any(len(values) != 3 for values in self.field_texts.values()):
            raise ValueError("recommendation choices must contain exactly three values")
        if self.image_types != list(IMAGE_TYPES):
            raise ValueError("image types must match the supported order")
        return self


class ChatflowReferenceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_set_image_id: UUID
    position: Annotated[StrictInt, Field(ge=0, le=3)]
    slot: Literal["PRIMARY", "AUX01", "AUX02", "AUX03"]
    source_asset_id: UUID
    model_asset_id: UUID
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ChatflowRecommendationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_set_id: UUID
    reference_asset_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    reference_assets: Annotated[list[ChatflowReferenceIdentity], Field(min_length=1, max_length=4)]
    original_recommendation: DifyOriginalRecommendation
    public_options: RecommendationPublicOptions
    image_types: Annotated[list[ImageType], Field(min_length=8, max_length=8)]
    valid_until: datetime

    @model_validator(mode="after")
    def validate_complete_reference_identity(self) -> ChatflowRecommendationPayload:
        references = self.reference_assets
        expected_slots = _REFERENCE_SLOTS[: len(references)]
        if [item.position for item in references] != list(range(len(references))):
            raise ValueError("reference positions must be contiguous and ordered")
        if [item.slot for item in references] != list(expected_slots):
            raise ValueError("reference slots must include the complete bound group")
        if len({item.reference_set_image_id for item in references}) != len(references):
            raise ValueError("reference set image ids must be unique")
        if len({item.model_asset_id for item in references}) != len(references):
            raise ValueError("model asset ids must be unique")
        if self.image_types != list(IMAGE_TYPES):
            raise ValueError("image types must match the supported order")
        if self.original_recommendation.image_types != self.image_types:
            raise ValueError("original and public image type contracts differ")
        return self


class ChatflowFailurePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    failure_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]


class ChatflowBusinessEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["chatflow-handoff-v1"]
    business_session_id: UUID
    recommendation_id: UUID
    recommendation_revision: Annotated[StrictInt, Field(ge=1)]
    dify_run_id: Annotated[str, Field(min_length=1, max_length=320)]
    event_id: UUID
    source: Literal["dify"]
    source_sequence: Annotated[StrictInt, Field(ge=1)]
    event_type: Literal["recommendation.ready", "recommendation.failed", "human_input_required"]
    digest_version: Literal[1]
    digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    payload: dict[str, object] = Field(repr=False)


class ChatflowBusinessEventAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["accepted", "replayed", "ignored"]
    business_session_id: UUID
    recommendation_id: UUID
    recommendation_revision: Annotated[int, Field(ge=1)]
    question_id: UUID | None = None


class BusinessSessionRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recommendation_id: UUID
    recommendation_revision: Annotated[int, Field(ge=1)]
    original_recommendation: DifyOriginalRecommendation
    public_options: RecommendationPublicOptions
    image_types: Annotated[list[ImageType], Field(min_length=8, max_length=8)]
    valid_until: datetime


class BusinessSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["chatflow-handoff-v1"] = "chatflow-handoff-v1"
    business_session_id: UUID
    status: Literal[
        "starting",
        "awaiting_recommendation",
        "recommendation_ready",
        "task_created",
        "reconciling",
        "failed",
        "expired",
    ]
    recommendation: BusinessSessionRecommendation | None = None
    task_id: UUID | None = None
    question: BusinessQuestionPublic | None = None


class BusinessSessionCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_status: str
    delivery_status: Literal["pending", "confirmed", "unresolved", "not_applicable"]
    dify_run_status: Literal[
        "running", "paused", "succeeded", "partial-succeeded", "failed", "stopped"
    ] | None = None
    reconciliation_status: Literal[
        "pending", "unknown", "completed", "completed_with_execution_warning"
    ]
    reason_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")] | None = None
    confirmed_count: Annotated[int, Field(ge=0, le=8)]
    closed_at: datetime | None = None


class BusinessSessionV34Response(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["chatflow-handoff-v1"] = "chatflow-handoff-v1"
    flow_version: Literal["visual-v3.4-single-selection"]
    business_session_id: UUID
    status: Literal[
        "starting",
        "awaiting_recommendation",
        "recommendation_ready",
        "task_created",
        "reconciling",
        "failed",
        "expired",
        "cancelled",
    ]
    phase: Literal[
        "starting",
        "awaiting_recommendation",
        "awaiting_form",
        "awaiting_selection",
        "selection_pending",
        "reconciling",
        "failed",
        "expired",
        "cancelled",
    ]
    recommendation: BusinessSessionRecommendation | None = None
    task_id: UUID | None = None
    selection_ready: bool
    can_end_before_start: bool = False
    completion: BusinessSessionCompletion | None = None
    safe_reason_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")] | None = None


class BusinessSessionCancelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["chatflow-handoff-v1"] = "chatflow-handoff-v1"
    business_session_id: UUID
    status: Literal["cancelling", "cancelled"]
    replayed: bool
    safe_reason_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")] | None = None


class BusinessSessionHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    business_session_id: UUID
    completion: BusinessSessionCompletion | None = None
    reference_set_id: UUID
    product_category: str
    created_at: datetime
    status: str
    safe_reason_code: str | None = None
    task_id: UUID | None = None
    task_status: str | None = None


class BusinessSessionHistoryPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[BusinessSessionHistoryEntry]
    next_cursor: str | None = None


class BusinessSelectionAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["chatflow-handoff-v1"] = "chatflow-handoff-v1"
    business_session_id: UUID
    task_id: UUID
    status: Literal["queued", "replayed"]
    planned_count: Annotated[int, Field(ge=1, le=8)]


def chatflow_business_event_digest(event: ChatflowBusinessEvent) -> str:
    payload = event.model_dump(mode="json", exclude={"digest"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def chatflow_reference_asset_digest(references: list[ChatflowReferenceIdentity]) -> str:
    canonical = json.dumps(
        [item.model_dump(mode="json") for item in references],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def chatflow_reference_snapshot_digest(snapshots: list[dict[str, object]]) -> str:
    identities = [
        ChatflowReferenceIdentity.model_validate(
            {
                key: entry[key]
                for key in (
                    "reference_set_image_id",
                    "position",
                    "slot",
                    "source_asset_id",
                    "model_asset_id",
                    "content_sha256",
                    "source_sha256",
                )
            },
        )
        for entry in snapshots
    ]
    return chatflow_reference_asset_digest(identities)
