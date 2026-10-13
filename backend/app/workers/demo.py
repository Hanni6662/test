from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.outbox_models import OutboxDemoEffect, OutboxEvent
from app.services.outbox import OutboxService
from app.workers.messages import OutboxMessage
from app.workers.processor import HandlerRegistration

DEMO_EVENT_TYPE = "demo.persisted.v1"


def process_demo_event(db: Session, event: OutboxEvent, message: OutboxMessage) -> None:
    """Persist one harmless effect row; the primary key makes retries idempotent."""
    existing = db.execute(
        select(OutboxDemoEffect.event_id).where(OutboxDemoEffect.event_id == event.id)
    ).scalar_one_or_none()
    if existing is not None:
        return
    db.execute(
        pg_insert(OutboxDemoEffect)
        .values(
            event_id=event.id,
            aggregate_id=message.aggregate_id,
            operation_version=message.operation_version,
            observed_schema_version=message.schema_version,
        )
        .on_conflict_do_nothing(index_elements=[OutboxDemoEffect.event_id])
    )


def demo_handlers() -> Mapping[str, HandlerRegistration]:
    return {DEMO_EVENT_TYPE: HandlerRegistration(process_demo_event, frozenset({1}))}


def enqueue_demo_event(
    outbox_service: OutboxService, aggregate_id: UUID, *, operation_version: int = 1
) -> UUID:
    """Use an OutboxService-like object to enqueue the non-paid acceptance event."""
    return outbox_service.enqueue(
        event_type=DEMO_EVENT_TYPE,
        aggregate_id=aggregate_id,
        operation_version=operation_version,
        schema_version=1,
    )
