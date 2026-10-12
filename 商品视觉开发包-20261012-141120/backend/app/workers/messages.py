from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_PAYLOAD_KEY = re.compile(r"^[a-z][a-z0-9_]{0,59}_id$")


class OutboxMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    aggregate_id: UUID
    operation_version: int = Field(ge=1)
    schema_version: int = Field(ge=1)
    payload: dict[str, UUID] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, UUID]) -> dict[str, UUID]:
        if any(not _PAYLOAD_KEY.fullmatch(key) for key in value):
            raise ValueError("event payload keys must identify internal entities")
        return value


@dataclass(frozen=True, slots=True)
class ClaimedEvent:
    event_id: UUID
    event_type: str
    aggregate_id: UUID
    operation_version: int
    schema_version: int
    payload: dict[str, str]
    claim_token: UUID
    attempt_count: int

    def to_message(self) -> dict[str, str | int | dict[str, str]]:
        return {
            "event_id": str(self.event_id),
            "aggregate_id": str(self.aggregate_id),
            "operation_version": self.operation_version,
            "schema_version": self.schema_version,
            "payload": self.payload,
        }
