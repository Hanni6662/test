from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

_GENERATION_STATUSES = (
    "queued",
    "submitting",
    "submission_unknown",
    "pending",
    "processing",
    "persisting",
    "completed",
    "failed",
    "cancelled",
    "protocol_error",
)
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "protocol_error"})


class ChatflowGenerationSlotEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_id: Annotated[str, Field(pattern=r"^slot_0[1-8]$")]
    task_item_id: UUID
    attempt_id: UUID
    position: Annotated[StrictInt, Field(ge=1, le=8)]
    image_type: Annotated[str, Field(min_length=1, max_length=80)]
    status: Literal[
        "queued",
        "submitting",
        "submission_unknown",
        "pending",
        "processing",
        "persisting",
        "completed",
        "failed",
        "cancelled",
        "protocol_error",
    ]
    status_version: Annotated[StrictInt, Field(ge=1)]
    error_code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")] | None = None
    known_retry_count: Annotated[StrictInt, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def hide_unobservable_retries(self) -> ChatflowGenerationSlotEvent:
        if (
            self.status in {"submitting", "submission_unknown"}
            and self.known_retry_count is not None
        ):
            raise ValueError("retry count must be null while provider acceptance is ambiguous")
        return self


class ChatflowGenerationEvent(BaseModel):
    """Authenticated Dify-forwarded snapshot from the original image-generation batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["chatflow-generation-event-v1"]
    business_session_id: UUID
    task_id: UUID
    generation_request_id: UUID
    dify_run_id: Annotated[str, Field(min_length=1, max_length=320)]
    batch_id: Annotated[
        str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    ]
    permit_version: Literal[2]
    permit_contract_version: Literal["chatflow-generation-permit-v2"]
    package_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    slot_mapping_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    reference_manifest_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    event_id: UUID
    source: Literal["image_generation"]
    source_sequence: Annotated[StrictInt, Field(ge=1)]
    status: Literal[
        "queued", "running", "submission_unknown", "completed", "partial_failed", "failed"
    ]
    digest_version: Literal[1]
    digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    workflow_retry_count: Annotated[StrictInt, Field(ge=0)] | None = None
    slots: Annotated[list[ChatflowGenerationSlotEvent], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def validate_slot_order(self) -> ChatflowGenerationEvent:
        if [slot.position for slot in self.slots] != list(range(1, len(self.slots) + 1)):
            raise ValueError("slot positions must be contiguous and ordered")
        if [slot.slot_id for slot in self.slots] != [
            f"slot_{position:02d}" for position in range(1, len(self.slots) + 1)
        ]:
            raise ValueError("slot ids must match their positions")
        if len({slot.task_item_id for slot in self.slots}) != len(self.slots):
            raise ValueError("task item ids must be unique")
        if len({slot.attempt_id for slot in self.slots}) != len(self.slots):
            raise ValueError("attempt ids must be unique")
        if self.status != service_generation_status(self.slots):
            raise ValueError("event aggregate status does not match slot statuses")
        return self


class ChatflowGenerationEventAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["accepted", "replayed", "ignored"]
    generation_request_id: UUID
    batch_id: str


def chatflow_generation_event_digest(event: ChatflowGenerationEvent) -> str:
    value = event.model_dump(mode="json", exclude={"digest"})
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def terminal_generation_slots(slots: list[ChatflowGenerationSlotEvent]) -> bool:
    return bool(slots) and all(slot.status in _TERMINAL_STATUSES for slot in slots)


def aggregate_generation_status(slots: list[ChatflowGenerationSlotEvent]) -> str:
    if not slots:
        return "awaiting_batch"
    if terminal_generation_slots(slots):
        if all(slot.status == "completed" for slot in slots):
            return "completed"
        if any(slot.status == "completed" for slot in slots):
            return "partial_failed"
        return "failed"
    if any(slot.status == "submission_unknown" for slot in slots):
        return "submission_unknown"
    if all(slot.status == "queued" for slot in slots):
        return "queued"
    return "running"


def service_generation_status(slots: list[ChatflowGenerationSlotEvent]) -> str:
    """Match the H5 response summary: any nonterminal slot means `running`."""
    if not terminal_generation_slots(slots):
        return "running"
    if all(slot.status == "completed" for slot in slots):
        return "completed"
    if any(slot.status == "completed" for slot in slots):
        return "partial_failed"
    return "failed"


def generation_snapshot_digest(
    *,
    batch_id: str,
    source_sequence: int,
    slots: list[ChatflowGenerationSlotEvent],
) -> bytes:
    """Digest the shared H5 snapshot shape, independent of delivery channel."""
    normalized_slots = sorted(slots, key=lambda slot: slot.position)
    value = {
        "batch_id": batch_id,
        "source_sequence": source_sequence,
        "status": service_generation_status(normalized_slots),
        "slots": [slot.model_dump(mode="json") for slot in normalized_slots],
    }
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).digest()
