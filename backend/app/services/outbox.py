from __future__ import annotations

import re
from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.db.outbox_models import OutboxEvent, OutboxReceipt

_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{0,99}$")
_PAYLOAD_KEY = re.compile(r"^[a-z][a-z0-9_]{0,59}_id$")


class OutboxActionConflict(ValueError):
    """A repeated business action attempted to change its persisted event data."""


def enqueue_in_transaction(
    db: Session,
    *,
    event_type: str,
    aggregate_id: UUID,
    operation_version: int,
    schema_version: int,
    payload: Mapping[str, UUID] | None = None,
) -> UUID:
    """Insert an idempotent outbox event inside the caller's business transaction."""
    if not _EVENT_TYPE.fullmatch(event_type):
        raise ValueError("event_type must be a safe lowercase identifier")
    if operation_version < 1 or schema_version < 1:
        raise ValueError("event versions must be positive")
    source_payload = payload or {}
    if any(
        not _PAYLOAD_KEY.fullmatch(key) or not isinstance(value, UUID)
        for key, value in source_payload.items()
    ):
        raise ValueError("outbox payload values must be internal UUID fields")
    stored_payload = {key: str(value) for key, value in source_payload.items()}
    action_key = {
        "event_type": event_type,
        "aggregate_id": aggregate_id,
        "operation_version": operation_version,
    }
    inserted_id = db.execute(
        pg_insert(OutboxEvent)
        .values(
            **action_key,
            schema_version=schema_version,
            payload=stored_payload,
            status="pending",
        )
        .on_conflict_do_nothing(
            index_elements=[
                OutboxEvent.event_type,
                OutboxEvent.aggregate_id,
                OutboxEvent.operation_version,
            ]
        )
        .returning(OutboxEvent.id)
    ).scalar_one_or_none()
    if inserted_id is not None:
        db.execute(
            pg_insert(OutboxReceipt)
            .values(event_id=inserted_id, status="pending", attempt_count=0)
            .on_conflict_do_nothing(index_elements=[OutboxReceipt.event_id])
        )
        return inserted_id

    existing = db.execute(
        select(OutboxEvent).where(
            OutboxEvent.event_type == event_type,
            OutboxEvent.aggregate_id == aggregate_id,
            OutboxEvent.operation_version == operation_version,
        )
    ).scalar_one()
    if existing.schema_version != schema_version or existing.payload != stored_payload:
        raise OutboxActionConflict("the business action already has different persisted event data")
    db.execute(
        pg_insert(OutboxReceipt)
        .values(event_id=existing.id, status="pending", attempt_count=0)
        .on_conflict_do_nothing(index_elements=[OutboxReceipt.event_id])
    )
    return existing.id


class OutboxService:
    """Convenience service for queue-only work; atomic callers use the helper above."""

    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    def enqueue(
        self,
        *,
        event_type: str,
        aggregate_id: UUID,
        operation_version: int,
        schema_version: int,
        payload: Mapping[str, UUID] | None = None,
    ) -> UUID:
        with self._sessions() as db, db.begin():
            return enqueue_in_transaction(
                db,
                event_type=event_type,
                aggregate_id=aggregate_id,
                operation_version=operation_version,
                schema_version=schema_version,
                payload=payload,
            )
