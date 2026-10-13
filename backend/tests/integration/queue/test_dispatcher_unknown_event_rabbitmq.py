from __future__ import annotations

import os
from datetime import timedelta
from uuid import uuid4

import pytest
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.dispatcher import OutboxDispatcher
from app.workers.celery_app import durable_queue
from app.workers.messages import OutboxMessage
from app.workers.options import QueueOptions
from app.workers.processor import EventProcessor
from kombu import Connection
from sqlalchemy import and_, create_engine, delete, func, inspect, not_, or_, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


def _queue_depth(broker_url: str, queue_name: str) -> int:
    with Connection(broker_url, connect_timeout=5) as connection:
        channel = connection.channel()
        try:
            return channel.queue_declare(queue=queue_name, passive=True).message_count
        finally:
            channel.close()


def test_unallowlisted_task_prompt_stays_pending_and_never_reaches_rabbit() -> None:
    runtime_url = os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL")
    broker_url = os.getenv("IMAGE_BACKEND_TEST_BROKER_URL")
    if runtime_url is None or broker_url is None:
        pytest.skip("requires the dedicated image-backend-test PostgreSQL and RabbitMQ URLs")

    engine = create_engine(runtime_url, pool_pre_ping=True)
    if not {"outbox_events", "outbox_receipts"}.issubset(set(inspect(engine).get_table_names())):
        engine.dispose()
        pytest.skip("requires the integrated PostgreSQL outbox schema")

    queue_name = f"unknown-event-{uuid4().hex[:20]}"
    options = QueueOptions(broker_url=broker_url, queue_name=queue_name, claim_batch_size=1)
    dispatcher = OutboxDispatcher(engine, options)
    task_id = uuid4()
    event_id = uuid4()
    event_payload = {"task_id": str(task_id)}
    try:
        with Connection(broker_url, connect_timeout=5) as connection:
            channel = connection.channel()
            try:
                durable_queue(options).declare(channel=channel)
            finally:
                channel.close()

        with Session(engine) as db, db.begin():
            now = db.scalar(select(func.clock_timestamp()))
            # The test database is shared by the isolated runner. Put this
            # fixture ahead of already-due unsupported rows so a one-row
            # quarantine batch deterministically reaches the event under test.
            oldest_unsupported_due = db.scalar(
                select(func.min(OutboxEvent.available_at)).where(
                    not_(dispatcher._publishable_event_predicate()),
                    or_(
                        and_(
                            OutboxEvent.status == "pending",
                            OutboxEvent.available_at <= now,
                        ),
                        and_(
                            OutboxEvent.status == "claimed",
                            OutboxEvent.lease_until <= now,
                        ),
                    ),
                )
            )
            old_due = (
                now
                if oldest_unsupported_due is None
                else min(now, oldest_unsupported_due - timedelta(microseconds=1))
            )
            event = OutboxEvent(
                id=event_id,
                event_type="task.prompt.v1",
                aggregate_id=task_id,
                operation_version=1,
                schema_version=1,
                payload=event_payload,
                status="pending",
                available_at=old_due,
            )
            db.add(event)
            db.flush()
            db.add(
                OutboxReceipt(
                    event_id=event_id,
                    status="pending",
                    available_at=old_due,
                    attempt_count=0,
                )
            )

        dispatcher._quarantine_unregistered_events()

        with Session(engine) as db:
            event = db.get(OutboxEvent, event_id)
            receipt = db.get(OutboxReceipt, event_id)
            assert event.status == "pending"
            assert receipt.status == "pending"
            assert event.last_error_code == "EVENT_TYPE_NOT_ALLOWLISTED"
            assert receipt.last_error_code == "EVENT_TYPE_NOT_ALLOWLISTED"
        assert _queue_depth(broker_url, queue_name) == 0

        # A worker receiving a message that bypassed the dispatcher keeps S4's
        # established unsupported-event dead-letter behavior.
        payload = OutboxMessage(
            event_id=event_id,
            aggregate_id=task_id,
            operation_version=1,
            schema_version=1,
            payload=event_payload,
        ).model_dump(mode="json")
        result = EventProcessor(engine, options, {}).process_payload(payload)
        assert result.status == "dead"
        assert result.error_code == "EVENT_TYPE_UNSUPPORTED"
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(OutboxReceipt).where(OutboxReceipt.event_id == event_id))
            db.execute(delete(OutboxEvent).where(OutboxEvent.id == event_id))
        try:
            with Connection(broker_url, connect_timeout=5) as connection:
                channel = connection.channel()
                try:
                    channel.queue_delete(queue=queue_name, if_unused=False, if_empty=False)
                finally:
                    channel.close()
        finally:
            engine.dispose()
