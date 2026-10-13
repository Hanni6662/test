from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from app.schemas.chatflow_generation import ChatflowGenerationEvent
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

_FILE_ID = re.compile(r"^slot_0[1-8]-original-1\.(png|jpg|jpeg|webp)$")
_MAX_SOURCE_BYTES = 32 * 1024 * 1024


class ChatflowGenerationSourceArtifact(BaseModel):
    """Opaque image-generation file identity; source URLs are deliberately forbidden."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_id: Annotated[str, Field(min_length=1, max_length=255)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    mime_type: Literal["image/jpeg", "image/png", "image/webp"]
    size_bytes: Annotated[StrictInt, Field(gt=0, le=_MAX_SOURCE_BYTES)]
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_source_identity(self) -> ChatflowGenerationSourceArtifact:
        match = _FILE_ID.fullmatch(self.file_id)
        if match is None:
            raise ValueError("source file identity is invalid")
        extension = match.group(1)
        expected_mime = (
            "image/png" if extension == "png" else "image/jpeg"
            if extension in {"jpg", "jpeg"} else "image/webp"
        )
        if self.mime_type != expected_mime:
            raise ValueError("source file extension and MIME type differ")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("source expiry must include a timezone")
        return self


class ChatflowGenerationManifestSlot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_id: Annotated[str, Field(pattern=r"^slot_0[1-8]$")]
    task_item_id: UUID
    attempt_id: UUID
    position: Annotated[StrictInt, Field(ge=1, le=8)]
    image_type: Annotated[str, Field(min_length=1, max_length=80)]
    status_version: Annotated[StrictInt, Field(ge=1)]
    original_artifact: ChatflowGenerationSourceArtifact | None = None


class ChatflowGenerationResultManifest(BaseModel):
    """Dify result receipt for an already accepted H4/H5 generation request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["chatflow-generation-result-manifest-v1"]
    status_event: ChatflowGenerationEvent
    slots: Annotated[list[ChatflowGenerationManifestSlot], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def validate_against_status_event(self) -> ChatflowGenerationResultManifest:
        event_slots = self.status_event.slots
        if len(self.slots) != len(event_slots):
            raise ValueError("result slots differ from the accepted generation snapshot")
        for result_slot, status_slot in zip(self.slots, event_slots, strict=True):
            if (
                result_slot.slot_id != status_slot.slot_id
                or result_slot.task_item_id != status_slot.task_item_id
                or result_slot.attempt_id != status_slot.attempt_id
                or result_slot.position != status_slot.position
                or result_slot.image_type != status_slot.image_type
                or result_slot.status_version != status_slot.status_version
            ):
                raise ValueError(
                    "result slot mapping differs from the accepted generation snapshot"
                )
            if (status_slot.status == "completed") != (result_slot.original_artifact is not None):
                raise ValueError("completed slots require one original result artifact")
            if (
                result_slot.original_artifact is not None
                and not result_slot.original_artifact.file_id.startswith(
                    result_slot.slot_id + "-"
                )
            ):
                raise ValueError("source artifact belongs to a different slot")
        if [slot.position for slot in self.slots] != list(range(1, len(self.slots) + 1)):
            raise ValueError("result positions must be contiguous and ordered")
        return self


class ChatflowGenerationManifestAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["accepted", "replayed"]
    manifest_status: Literal["durably_received"] = "durably_received"
    image_save_status: Literal["pending", "not_required"]
    generation_request_id: UUID
    batch_id: str
    source_sequence: Annotated[StrictInt, Field(ge=1)]


def chatflow_generation_manifest_digest(manifest: ChatflowGenerationResultManifest) -> bytes:
    value = manifest.model_dump(mode="json")
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).digest()
