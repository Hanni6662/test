from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from app.schemas.tasks import TaskCounts, TaskItemResponse, TaskStatus
from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    task_id: UUID
    status: TaskStatus
    created_at: datetime
    counts: TaskCounts
    items: list[TaskItemResponse]


class TaskHistoryPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: Annotated[list[TaskHistoryEntry], Field(max_length=50)]
    next_cursor: Annotated[str | None, Field(max_length=256)] = None


class TaskRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    failed_item_ids: Annotated[list[UUID], Field(min_length=1, max_length=8)]

    @field_validator("failed_item_ids")
    @classmethod
    def require_distinct_item_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("failed_item_ids must not contain duplicates")
        return value


class TaskRetryAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_item_id: UUID
    attempt_id: UUID


class TaskRetryAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    task_id: UUID
    attempts: Annotated[list[TaskRetryAttempt], Field(min_length=1, max_length=8)]
