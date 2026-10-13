from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from app.schemas.business_sessions import DifyOriginalRecommendation
from app.schemas.recommendations import IMAGE_TYPES, ImageType, PlatformName
from app.schemas.tasks import TaskPreferences
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

V34_FLOW_VERSION = "visual-v3.4-single-selection"
V34_STAGE_CONTRACT = "visual-v34-stage-v1"
V34_EVENT_TYPES = (
    "recommendation.ready",
    "selection.applied",
    "pre_generation.incomplete",
    "selection.cancelled",
    "selection.expired",
    "workflow.failed",
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,320}$")
_SAFE_EVENT_KEY = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")
_MAX_EVENT_BYTES = 96 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class V34RecommendationReadyPayload(_StrictModel):
    audit: Annotated[StrictStr, Field(min_length=2, max_length=64 * 1024)]


class V34ParsedSelection(_StrictModel):
    """Normalized values reconstructed from parse_0's executed output."""

    preferences: TaskPreferences
    image_types: Annotated[list[ImageType], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def validate_image_types(self) -> V34ParsedSelection:
        selected = set(self.image_types)
        expected_order = [image_type for image_type in IMAGE_TYPES if image_type in selected]
        if len(self.image_types) != len(selected) or self.image_types != expected_order:
            raise ValueError("image types must be unique and in the supported order")
        return self


class V34SelectionAppliedPayload(_StrictModel):
    selection_submission_id: UUID
    parse_state: V34ParsedSelection


class V34IncompletePayload(_StrictModel):
    reason_code: Literal["PRODUCT_IDENTITY_CONFLICT"]
    conflicting_field_ids: Annotated[
        list[Literal["3", "4", "5"]],
        Field(min_length=1, max_length=3),
    ]

    @model_validator(mode="after")
    def validate_conflict_fields(self) -> V34IncompletePayload:
        if len(self.conflicting_field_ids) != len(set(self.conflicting_field_ids)):
            raise ValueError("conflicting field ids must be unique")
        return self


class V34TerminalPayload(_StrictModel):
    reason_code: Literal["FORM_CANCELLED", "FORM_EXPIRED"]


class V34WorkflowFailurePayload(_StrictModel):
    phase: Literal["audit", "prepare", "resolve_0", "review_0", "design", "deliver"]
    failed_node_id: Annotated[StrictStr, Field(min_length=1, max_length=80)]

    @model_validator(mode="after")
    def validate_failed_node(self) -> V34WorkflowFailurePayload:
        if self.failed_node_id != self.phase:
            raise ValueError("failed node does not match the fixed phase allowlist")
        return self


class ChatflowV34StageEvent(_StrictModel):
    contract_version: Literal["visual-v34-stage-v1"]
    flow_version: Literal["visual-v3.4-single-selection"]
    business_session_id: UUID
    recommendation_id: UUID
    recommendation_revision: Annotated[StrictInt, Field(ge=1)]
    dify_run_id: Annotated[StrictStr, Field(min_length=1, max_length=320)]
    node_id: Annotated[StrictStr, Field(min_length=1, max_length=80)]
    event_key: Annotated[StrictStr, Field(min_length=1, max_length=160)]
    digest_version: Literal[1]
    digest: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    event_type: Literal[
        "recommendation.ready",
        "selection.applied",
        "pre_generation.incomplete",
        "selection.cancelled",
        "selection.expired",
        "workflow.failed",
    ]
    payload: dict[str, object] = Field(repr=False)

    @model_validator(mode="after")
    def validate_stage_payload(self) -> ChatflowV34StageEvent:
        if not _SAFE_ID.fullmatch(self.dify_run_id) or not _SAFE_ID.fullmatch(self.node_id):
            raise ValueError("stage event identity is invalid")
        if not _SAFE_EVENT_KEY.fullmatch(self.event_key):
            raise ValueError("stage event key is invalid")
        expected_nodes = {
            "recommendation.ready": "prepare",
            "selection.applied": "parse_0",
            "pre_generation.incomplete": "ready_0",
            "selection.cancelled": "selection_cancelled_callback",
            "selection.expired": "selection_expired_callback",
            "workflow.failed": "workflow_failed_callback",
        }
        if self.event_type in expected_nodes and self.node_id != expected_nodes[self.event_type]:
            raise ValueError("stage event node does not match its event type")
        payload_models: dict[str, type[BaseModel]] = {
            "recommendation.ready": V34RecommendationReadyPayload,
            "selection.applied": V34SelectionAppliedPayload,
            "pre_generation.incomplete": V34IncompletePayload,
            "selection.cancelled": V34TerminalPayload,
            "selection.expired": V34TerminalPayload,
            "workflow.failed": V34WorkflowFailurePayload,
        }
        try:
            validated_payload = payload_models[self.event_type].model_validate_json(
                json.dumps(self.payload, ensure_ascii=False, allow_nan=False), strict=True
            )
            expected_reason_codes = {
                "selection.cancelled": "FORM_CANCELLED",
                "selection.expired": "FORM_EXPIRED",
            }
            expected_reason = expected_reason_codes.get(self.event_type)
            if (
                expected_reason is not None
                and getattr(validated_payload, "reason_code", None) != expected_reason
            ):
                raise ValueError("stage event reason does not match its event type")
            serialized = json.dumps(
                self.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError):
            raise ValueError("stage event payload is invalid") from None
        if len(serialized.encode("utf-8")) > _MAX_EVENT_BYTES:
            raise ValueError("stage event is too large")
        return self


class ChatflowV34StageEventAccepted(_StrictModel):
    status: Literal["accepted", "replayed", "received_pending", "rejected"]
    business_session_id: UUID
    event_key: Annotated[StrictStr, Field(min_length=1, max_length=160)]


class V34SelectionContextResponse(_StrictModel):
    contract_version: Literal["visual-v34-stage-v1"]
    flow_version: Literal["visual-v3.4-single-selection"]
    phase: Literal["selection"]
    business_session_id: UUID
    recommendation_id: UUID
    recommendation_revision: Annotated[StrictInt, Field(ge=1)]
    dify_run_id: Annotated[StrictStr, Field(min_length=1, max_length=320)]
    task_id: UUID
    selection_submission_id: UUID
    selection_status: Literal["pending", "unknown", "accepted"]
    selection_input_digest: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    expires_at: datetime

    @model_validator(mode="after")
    def validate_expiry(self) -> V34SelectionContextResponse:
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() != timedelta(0):
            raise ValueError("selection context expiry must be UTC")
        return self


class V34PromptPreferenceRecommended(_StrictModel):
    mode: Literal["A", "B", "C"]
    text: Annotated[StrictStr, Field(min_length=1, max_length=3500)]
    source: Literal["recommendation_original"]
    recommendation_revision: Annotated[StrictInt, Field(ge=1)]


class V34PromptPreferenceCustom(_StrictModel):
    mode: Literal["custom"]
    text: Annotated[StrictStr, Field(min_length=1, max_length=3500)]


class V34PromptPreferenceDesign(_StrictModel):
    mode: Literal["D"]
    kind: Literal["agent_design"]
    instruction: Annotated[StrictStr, Field(min_length=1, max_length=3500)]


V34PromptPreference = Annotated[
    V34PromptPreferenceRecommended | V34PromptPreferenceCustom | V34PromptPreferenceDesign,
    Field(discriminator="mode"),
]


class V34PromptPlatformPreference(_StrictModel):
    mode: Literal["platform"]
    value: PlatformName


class V34PromptResolvedPreferences(_StrictModel):
    language: V34PromptPreference
    platform: V34PromptPlatformPreference
    usage_scene: V34PromptPreference
    visual_style: V34PromptPreference
    selling_points: V34PromptPreference


class V34PromptReference(_StrictModel):
    ref_id: Literal["PRIMARY", "AUX01", "AUX02", "AUX03"]
    order: Annotated[StrictInt, Field(ge=1, le=4)]
    asset_id: UUID
    content_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class V34PromptSelection(_StrictModel):
    preferences: TaskPreferences
    image_types: Annotated[list[ImageType], Field(min_length=1, max_length=8)]


class V34PromptContextResponse(_StrictModel):
    contract_version: Literal["visual-v34-stage-v1"]
    flow_version: Literal["visual-v3.4-single-selection"]
    phase: Literal["prompt"]
    business_session_id: UUID
    recommendation_id: UUID
    recommendation_revision: Annotated[StrictInt, Field(ge=1)]
    dify_run_id: Annotated[StrictStr, Field(min_length=1, max_length=320)]
    task_id: UUID
    selection_submission_id: UUID
    selection_status: Literal["accepted"]
    selection_evidence_kind: Literal["http_200", "selection_applied"]
    selection_input_digest: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    prompt_submission_id: UUID
    selection: V34PromptSelection
    resolved_preferences: V34PromptResolvedPreferences
    original_recommendation: DifyOriginalRecommendation
    image_types: Annotated[list[ImageType], Field(min_length=1, max_length=8)]
    references: Annotated[list[V34PromptReference], Field(min_length=1, max_length=4)]
    expires_at: datetime

    @model_validator(mode="after")
    def validate_prompt_context(self) -> V34PromptContextResponse:
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() != timedelta(0):
            raise ValueError("prompt context expiry must be UTC")
        if (
            self.selection.image_types != self.image_types
            or len(self.references) < 1
            or [item.order for item in self.references] != list(range(1, len(self.references) + 1))
            or [item.ref_id for item in self.references]
            != ["PRIMARY", *[f"AUX{index:02d}" for index in range(1, len(self.references))]]
        ):
            raise ValueError("prompt context manifest is not ordered")
        return self


def chatflow_v34_stage_event_digest(event: ChatflowV34StageEvent) -> bytes:
    canonical = json.dumps(
        event.model_dump(mode="json", exclude={"digest"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).digest()
