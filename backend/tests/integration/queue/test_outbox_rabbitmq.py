from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from app.db.outbox_models import (
    OutboxDemoEffect,
    OutboxEvent,
    OutboxReceipt,
    OutboxRejectedMessage,
)
from app.dispatcher import OutboxDispatcher
from app.scheduler import OutboxScheduler
from app.services.outbox import OutboxService
from app.workers.demo import DEMO_EVENT_TYPE, demo_handlers
from app.workers.messages import ClaimedEvent
from app.workers.options import QueueOptions
from app.workers.processor import EventProcessor
from app.workers.publisher import CeleryTaskPublisher
from kombu import Connection
from sqlalchemy import and_, create_engine, delete, func, or_, select, text, update
from sqlalchemy.orm import Session


class _PublishThenCrash:
    """Model the process exit after Rabbit confirms but before PG marks published."""

    def __init__(self, publisher: CeleryTaskPublisher) -> None:
        self._publisher = publisher

    def publish(self, event: ClaimedEvent) -> None:
        self._publisher.publish(event)
        raise SystemExit("injected dispatcher exit after broker confirmation")


class _ConfirmPublisher:
    def publish(self, _event: ClaimedEvent) -> None:
        return None


def _wait_for(predicate, *, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def _queue_depth(broker_url: str, queue_name: str) -> int:
    with Connection(broker_url, connect_timeout=5) as connection:
        channel = connection.channel()
        try:
            return channel.queue_declare(queue=queue_name, passive=True).message_count
        finally:
            channel.close()


def _postrun_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _kill_worker_group(worker: subprocess.Popen) -> None:
    if worker.poll() is None:
        try:
            os.killpg(worker.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    worker.wait(timeout=10)


def _prioritize_event(engine, event_id) -> None:
    """Make this test row the first due row despite persistent suite data."""
    with engine.begin() as connection:
        earliest = connection.scalar(
            select(func.min(OutboxEvent.available_at)).where(
                OutboxEvent.id != event_id,
                or_(
                    and_(
                        OutboxEvent.status == "pending",
                        OutboxEvent.available_at <= func.clock_timestamp(),
                    ),
                    and_(
                        OutboxEvent.status == "claimed",
                        OutboxEvent.lease_until <= func.clock_timestamp(),
                    ),
                ),
            )
        )
        available_at = (
            earliest - timedelta(microseconds=1)
            if earliest is not None
            else text("clock_timestamp() - interval '1 microsecond'")
        )
        connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(available_at=available_at)
        )


@pytest.mark.integration
def test_pg_outbox_survives_broker_outage_and_process_crashes_with_prefork_worker(
    tmp_path: Path,
) -> None:
    """Use dedicated real PG/RabbitMQ and an independent prefork Celery process."""
    runtime_url = os.environ["IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL"]
    broker_url = os.environ["IMAGE_BACKEND_TEST_BROKER_URL"]
    options = QueueOptions(
        broker_url=broker_url,
        queue_name=f"image-backend-s4-{uuid4().hex[:20]}",
        claim_batch_size=1,
    )
    engine = create_engine(runtime_url, pool_pre_ping=True)
    aggregate_id = uuid4()
    event_id = None
    workers: list[subprocess.Popen] = []
    postrun_marker = tmp_path / "postrun-events.txt"
    repo_backend = Path(__file__).resolve().parents[3]
    child_env = os.environ.copy()
    child_env.update(
        IMAGE_BACKEND_DATABASE_URL=runtime_url,
        IMAGE_BACKEND_BROKER_URL=broker_url,
        IMAGE_BACKEND_QUEUE_NAME=options.queue_name,
        IMAGE_BACKEND_TEST_POST_COMMIT_MARKER=str(postrun_marker),
    )

    def start_worker(mode: str) -> subprocess.Popen:
        worker_env = child_env.copy()
        worker_env["IMAGE_BACKEND_TEST_POST_COMMIT_MODE"] = mode
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "tests.integration.queue.post_commit_worker:app",
                "worker",
                "--pool=prefork",
                "--concurrency=1",
                "--loglevel=WARNING",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
            ],
            cwd=repo_backend,
            env=worker_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        workers.append(process)
        return process

    try:
        event_id = OutboxService(engine).enqueue(
            event_type=DEMO_EVENT_TYPE,
            aggregate_id=aggregate_id,
            operation_version=1,
            schema_version=1,
        )
        _prioritize_event(engine, event_id)

        # A connection failure leaves the PostgreSQL event pending with a safe
        # code. No broker-side state is treated as the source of truth.
        unavailable = QueueOptions(
            broker_url="amqp://guest:guest@127.0.0.1:1//",
            queue_name=options.queue_name,
            claim_batch_size=1,
            lease_seconds=10,
            max_attempts=8,
            retry_base_seconds=1,
            retry_max_seconds=5,
            confirm_timeout_seconds=1,
            republish_after_seconds=10,
        )
        outage_report = OutboxDispatcher(engine, unavailable).dispatch_once()
        assert outage_report.failed == 1
        with Session(engine) as db:
            event = db.get(OutboxEvent, event_id)
            assert event is not None
            assert event.status == "pending"
            assert event.last_error_code == "BROKER_PUBLISH_FAILED"
        with engine.begin() as connection:
            connection.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event_id)
                .values(available_at=text("clock_timestamp()"))
            )
        _prioritize_event(engine, event_id)

        worker = start_worker("pause")

        def queue_ready() -> bool:
            try:
                _queue_depth(broker_url, options.queue_name)
                return True
            except Exception:
                return False

        assert _wait_for(queue_ready), "prefork worker did not declare its durable queue"
        publisher = CeleryTaskPublisher(options)
        with pytest.raises(SystemExit):
            OutboxDispatcher(
                engine, options, publisher=_PublishThenCrash(publisher)
            ).dispatch_once()

        # task_postrun happens in the prefork child after the PG transaction
        # commits and before the parent can acknowledge the late-ack message.
        assert _wait_for(lambda: _postrun_lines(postrun_marker) == [str(event_id)])
        with Session(engine) as db:
            receipt = db.get(OutboxReceipt, event_id)
            assert receipt is not None and receipt.status == "completed"
            assert db.scalar(
                select(func.count())
                .select_from(OutboxDemoEffect)
                .where(OutboxDemoEffect.event_id == event_id)
            ) == 1

        _kill_worker_group(worker)
        assert _wait_for(lambda: queue_ready() and _queue_depth(broker_url, options.queue_name) > 0)

        # Rabbit redelivers the unacknowledged message to a new independent
        # prefork worker. Its completed receipt must suppress the second effect.
        worker = start_worker("observe")
        expected_postruns = [str(event_id), str(event_id)]
        assert _wait_for(lambda: _postrun_lines(postrun_marker) == expected_postruns)
        time.sleep(0.5)
        assert _postrun_lines(postrun_marker) == expected_postruns
        assert _queue_depth(broker_url, options.queue_name) == 0

        # The dispatcher exited after broker confirmation, so its claim is
        # still present. Expire that test lease and let the completed receipt
        # resolve the ambiguous publish without creating another message.
        with engine.begin() as connection:
            connection.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event_id, OutboxEvent.status == "claimed")
                .values(lease_until=text("clock_timestamp() - interval '1 second'"))
            )

        recovered = OutboxDispatcher(engine, options).dispatch_once()
        assert recovered.claimed == recovered.published == 0
        with Session(engine) as db:
            event = db.get(OutboxEvent, event_id)
            assert event is not None and event.status == "published"
            assert db.scalar(
                select(func.count())
                .select_from(OutboxDemoEffect)
                .where(OutboxDemoEffect.event_id == event_id)
            ) == 1
            receipt = db.get(OutboxReceipt, event_id)
            assert receipt is not None and receipt.status == "completed"
    finally:
        for worker in workers:
            _kill_worker_group(worker)
        if event_id is not None:
            with engine.begin() as connection:
                connection.execute(
                    delete(OutboxRejectedMessage).where(
                        OutboxRejectedMessage.event_id == event_id
                    )
                )
                connection.execute(
                    delete(OutboxDemoEffect).where(OutboxDemoEffect.event_id == event_id)
                )
                connection.execute(
                    delete(OutboxReceipt).where(OutboxReceipt.event_id == event_id)
                )
                connection.execute(delete(OutboxEvent).where(OutboxEvent.id == event_id))
        try:
            with Connection(broker_url, connect_timeout=5) as connection:
                channel = connection.channel()
                try:
                    channel.queue_delete(options.queue_name)
                    channel.exchange_delete(options.queue_name)
                finally:
                    channel.close()
        except Exception:
            # The test can fail before declaring its isolated queue. Cleanup is
            # best effort and must not hide the original failure.
            pass
        finally:
            engine.dispose()


@pytest.mark.integration
def test_dispatcher_skips_a_locked_row_and_claims_it_after_lock_release() -> None:
    runtime_url = os.environ["IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL"]
    engine = create_engine(runtime_url, pool_pre_ping=True)
    event_id = None
    options = QueueOptions(broker_url="amqp://guest:guest@localhost:5672//")
    try:
        event_id = OutboxService(engine).enqueue(
            event_type=DEMO_EVENT_TYPE,
            aggregate_id=uuid4(),
            operation_version=1,
            schema_version=1,
        )
        _prioritize_event(engine, event_id)
        dispatcher = OutboxDispatcher(
            engine,
            QueueOptions(broker_url=options.broker_url, claim_batch_size=1),
            publisher=_ConfirmPublisher(),
        )
        with Session(engine) as locker:
            transaction = locker.begin()
            try:
                locker.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.id == event_id)
                    .with_for_update()
                ).scalar_one()
                pool = ThreadPoolExecutor(max_workers=1)
                future = pool.submit(dispatcher.dispatch_once)
                try:
                    future.result(timeout=3)
                finally:
                    transaction.rollback()
                    pool.shutdown(wait=True)
            finally:
                if transaction.is_active:
                    transaction.rollback()

        with Session(engine) as db:
            locked_event = db.get(OutboxEvent, event_id)
            assert locked_event is not None and locked_event.status == "pending"
            assert locked_event.claim_token is None
        _prioritize_event(engine, event_id)
        released = dispatcher.dispatch_once()
        assert released.claimed >= 1
        with Session(engine) as db:
            event = db.get(OutboxEvent, event_id)
            assert event is not None and event.status == "published"
    finally:
        if event_id is not None:
            with engine.begin() as connection:
                connection.execute(
                    delete(OutboxReceipt).where(OutboxReceipt.event_id == event_id)
                )
                connection.execute(delete(OutboxEvent).where(OutboxEvent.id == event_id))
        engine.dispose()


@pytest.mark.integration
def test_invalid_message_records_safe_evidence_and_dead_letters_event() -> None:
    runtime_url = os.environ["IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL"]
    engine = create_engine(runtime_url, pool_pre_ping=True)
    event_id = None
    options = QueueOptions(broker_url="amqp://guest:guest@localhost:5672//")
    try:
        event_id = OutboxService(engine).enqueue(
            event_type=DEMO_EVENT_TYPE,
            aggregate_id=uuid4(),
            operation_version=1,
            schema_version=1,
        )
        processor = EventProcessor(engine, options, demo_handlers())
        for _ in range(2):
            result = processor.process_payload(
                {"event_id": str(event_id), "prompt": "private malformed test value"}
            )
            assert result.status == "dead"

        with Session(engine) as db:
            event = db.get(OutboxEvent, event_id)
            receipt = db.get(OutboxReceipt, event_id)
            assert event is not None and event.status == "dead"
            assert receipt is not None and receipt.status == "dead"
            rejection = db.scalar(
                select(OutboxRejectedMessage).where(OutboxRejectedMessage.event_id == event_id)
            )
            assert rejection is not None
            assert rejection.error_code == "MESSAGE_INVALID"
            assert rejection.occurrence_count == 2
            assert not hasattr(rejection, "payload")
    finally:
        if event_id is not None:
            with engine.begin() as connection:
                connection.execute(
                    delete(OutboxRejectedMessage).where(
                        OutboxRejectedMessage.event_id == event_id
                    )
                )
                connection.execute(
                    delete(OutboxReceipt).where(OutboxReceipt.event_id == event_id)
                )
                connection.execute(delete(OutboxEvent).where(OutboxEvent.id == event_id))
        engine.dispose()


@pytest.mark.integration
def test_scheduler_requeues_expired_worker_lease_and_old_published_event() -> None:
    runtime_url = os.environ["IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL"]
    engine = create_engine(runtime_url, pool_pre_ping=True)
    lease_event_id = redrive_event_id = None
    options = QueueOptions(broker_url="amqp://guest:guest@localhost:5672//")
    try:
        service = OutboxService(engine)
        lease_event_id = service.enqueue(
            event_type=DEMO_EVENT_TYPE,
            aggregate_id=uuid4(),
            operation_version=1,
            schema_version=1,
        )
        redrive_event_id = service.enqueue(
            event_type=DEMO_EVENT_TYPE,
            aggregate_id=uuid4(),
            operation_version=1,
            schema_version=1,
        )
        lease_token = uuid4()
        with engine.begin() as connection:
            connection.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == lease_event_id)
                .values(
                    status="published",
                    published_at=text("clock_timestamp() - interval '1 hour'"),
                )
            )
            connection.execute(
                update(OutboxReceipt)
                .where(OutboxReceipt.event_id == lease_event_id)
                .values(
                    status="processing",
                    claim_token=lease_token,
                    lease_until=text("clock_timestamp() - interval '1 second'"),
                    attempt_count=1,
                )
            )
            connection.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == redrive_event_id)
                .values(
                    status="published",
                    published_at=text("clock_timestamp() - interval '1 hour'"),
                )
            )
            connection.execute(
                update(OutboxReceipt)
                .where(OutboxReceipt.event_id == redrive_event_id)
                .values(
                    status="pending",
                    available_at=text("clock_timestamp() - interval '1 second'"),
                )
            )

        scheduler = OutboxScheduler(engine, options)
        # The dedicated database persists test data across runs. Drain eligible
        # rows in bounded batches until these two event IDs are recovered rather
        # than asserting global counters that include unrelated test events.
        for _ in range(20):
            scheduler.scan_once()
            with Session(engine) as db:
                lease_event = db.get(OutboxEvent, lease_event_id)
                lease_receipt = db.get(OutboxReceipt, lease_event_id)
                redrive_event = db.get(OutboxEvent, redrive_event_id)
                if (
                    lease_event is not None
                    and lease_event.status == "pending"
                    and lease_receipt is not None
                    and lease_receipt.status == "pending"
                    and lease_receipt.last_error_code == "WORKER_LEASE_EXPIRED"
                    and redrive_event is not None
                    and redrive_event.status == "pending"
                ):
                    break
        else:
            pytest.fail("scheduler did not recover the two targeted outbox rows")

        with Session(engine) as db:
            lease_event = db.get(OutboxEvent, lease_event_id)
            lease_receipt = db.get(OutboxReceipt, lease_event_id)
            redrive_event = db.get(OutboxEvent, redrive_event_id)
            assert lease_event is not None
            assert lease_receipt is not None
            assert redrive_event is not None
            recovered_lease_available_at = lease_receipt.available_at
            recovered_event_available_at = lease_event.available_at
            recovered_redrive_available_at = redrive_event.available_at
            assert lease_event is not None and lease_event.status == "pending"
            assert lease_receipt is not None and lease_receipt.status == "pending"
            assert lease_receipt.last_error_code == "WORKER_LEASE_EXPIRED"
            assert redrive_event is not None and redrive_event.status == "pending"

        scheduler.scan_once()
        with Session(engine) as db:
            lease_event = db.get(OutboxEvent, lease_event_id)
            lease_receipt = db.get(OutboxReceipt, lease_event_id)
            redrive_event = db.get(OutboxEvent, redrive_event_id)
            assert lease_event is not None and lease_event.status == "pending"
            assert lease_receipt is not None and lease_receipt.status == "pending"
            assert lease_receipt.available_at == recovered_lease_available_at
            assert lease_event.available_at == recovered_event_available_at
            assert redrive_event is not None and redrive_event.status == "pending"
            assert redrive_event.available_at == recovered_redrive_available_at
    finally:
        event_ids = [item for item in (lease_event_id, redrive_event_id) if item is not None]
        if event_ids:
            with engine.begin() as connection:
                connection.execute(
                    delete(OutboxReceipt).where(OutboxReceipt.event_id.in_(event_ids))
                )
                connection.execute(delete(OutboxEvent).where(OutboxEvent.id.in_(event_ids)))
        engine.dispose()
