from __future__ import annotations

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import UUID, uuid4, uuid5

import pytest
from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.generation_models import GenerationCapacityControl, GenerationCapacityReservation
from app.db.outbox_models import OutboxEvent, OutboxReceipt, OutboxRejectedMessage
from app.db.prompt_models import PromptSubmission, TaskPromptWorkflow
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ImageAsset, ReferenceSet
from app.db.retention_models import CleanupJob, CleanupRun, CleanupTarget
from app.db.task_models import (
    Task,
    TaskIdempotencyRecord,
    TaskItemAttempt,
    TaskRecommendationConsumption,
)
from app.integrations.storage.local import LocalPrivateStorage
from app.integrations.storage.protocol import (
    StorageError,
    StorageInventoryEntry,
    StorageInventoryPage,
)
from app.schemas.retention import CleanupRunRequest
from app.schemas.task_history import TaskRetryRequest
from app.services.retention_cleanup import (
    _CLEANUP_NAMESPACE,
    RetentionCleanupService,
    _eligible_task_ids,
)
from app.services.tasks import (
    TASK_RETRY_OPERATION,
    TaskOptions,
    TaskService,
    idempotency_key_hash,
    retry_request_digest,
)
from sqlalchemy import create_engine, delete, event, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from tests.integration.tasks.test_task_history_retry_postgres import (
    _create_reference_and_recommendation,
    _track_reference_resources,
)

pytest_plugins = ("tests.integration.tasks.test_task_history_retry_postgres",)


class _BlockingStorage(LocalPrivateStorage):
    def __init__(self, root) -> None:
        super().__init__(root)
        self.delete_entered = threading.Event()
        self.allow_delete = threading.Event()

    def delete(self, key: str) -> None:
        self.delete_entered.set()
        if not self.allow_delete.wait(timeout=20):
            raise StorageError("synthetic storage gate timed out")
        super().delete(key)


class _FailFirstDeleteStorage(LocalPrivateStorage):
    def __init__(self, root) -> None:
        super().__init__(root)
        self._lock = threading.Lock()
        self._failed_once = False

    def delete(self, key: str) -> None:
        with self._lock:
            if not self._failed_once:
                self._failed_once = True
                raise StorageError("synthetic one-shot delete failure")
        super().delete(key)


class _ContendedPageStorage(LocalPrivateStorage):
    def __init__(self, root) -> None:
        super().__init__(root)
        self.first_page_ready = threading.Event()
        self.release_first_page = threading.Event()
        self.first_page = None
        self._calls = 0
        self._lock = threading.Lock()

    def iter_inventory_page(self, **kwargs):
        page = super().iter_inventory_page(**kwargs)
        with self._lock:
            self._calls += 1
            call_number = self._calls
            if call_number == 1:
                self.first_page = page
        if call_number == 1:
            self.first_page_ready.set()
            if not self.release_first_page.wait(timeout=20):
                raise StorageError("synthetic inventory page gate timed out")
        return page


class _InvalidFirstPageStorage(LocalPrivateStorage):
    def __init__(self, root) -> None:
        super().__init__(root)
        self.injected = False

    def iter_inventory_page(self, **kwargs):
        page = super().iter_inventory_page(**kwargs)
        if self.injected or len(page.entries) < 2:
            return page
        self.injected = True
        invalid = StorageInventoryEntry(
            kind="object",
            relative_locator=f"{page.entries[0].relative_locator}/../invalid.png",
            object_key=f"{page.entries[0].relative_locator}/../invalid.png",
            modified_at=page.entries[0].modified_at,
            size_bytes=page.entries[0].size_bytes,
        )
        return StorageInventoryPage(
            entries=(invalid, *page.entries[1:]),
            next_cursor=page.next_cursor,
            exhausted=page.exhausted,
            scanned_items=page.scanned_items,
        )


class _FailFirstPageStorage(LocalPrivateStorage):
    def __init__(self, root) -> None:
        super().__init__(root)
        self.failed_once = False

    def iter_inventory_page(self, **kwargs):
        if not self.failed_once:
            self.failed_once = True
            raise StorageError("synthetic inventory scan failure")
        return super().iter_inventory_page(**kwargs)


class _StorageWithoutInventory:
    """Required storage surface only, to exercise unavailable inventory runs."""

    def __init__(self, inner: LocalPrivateStorage) -> None:
        self._inner = inner

    def put_immutable(self, key, stream, metadata):
        return self._inner.put_immutable(key, stream, metadata)

    def open_reader(self, key):
        return self._inner.open_reader(key)

    def stat(self, key):
        return self._inner.stat(key)

    def delete(self, key):
        return self._inner.delete(key)


def _wait_for_lock_wait(
    engine: Engine, *, application_name: str, query_marker: str, timeout: float = 10
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            waiting = connection.execute(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_stat_activity "
                    "WHERE application_name = :application_name "
                    "AND wait_event_type = 'Lock' "
                    "AND query ILIKE :query_marker)"
                ),
                {
                    "application_name": application_name,
                    "query_marker": f"%{query_marker}%",
                },
            ).scalar_one()
        if waiting:
            return True
        time.sleep(0.05)
    return False


def _write_old_object(storage: LocalPrivateStorage, key: str, *, age: timedelta) -> None:
    storage.put_immutable(
        key,
        BytesIO(b"synthetic retention fixture"),
        {"case": "r7"},
    )
    timestamp = (datetime.now(UTC) - age).timestamp()
    object_path = storage.root.joinpath(*key.split("/"))
    sidecar_path = object_path.with_name(f".{object_path.name}.metadata.json")
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.touch(exist_ok=True)
    os.utime(object_path, (timestamp, timestamp))
    if sidecar_path.exists():
        os.utime(sidecar_path, (timestamp, timestamp))


def _fixture_ids(state: dict[str, object]) -> tuple[Engine, UUID, UUID, list[UUID]]:
    engine = state["engine"]
    owner_id = state["owner_id"]
    task_id = state["task_id"]
    item_ids = state["item_ids"]
    assert isinstance(engine, Engine)
    assert isinstance(owner_id, UUID)
    assert isinstance(task_id, UUID)
    assert isinstance(item_ids, list) and all(isinstance(item_id, UUID) for item_id in item_ids)
    return engine, owner_id, task_id, item_ids


def _make_task_cleanup_eligible(engine: Engine, task_id: UUID) -> None:
    with Session(engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        task = db.get(Task, task_id)
        assert task is not None
        task.status = "failed"
        task.terminal_at = now - timedelta(minutes=1)
        task.expires_at = now - timedelta(seconds=1)
        task.updated_at = now
        prompt_events = db.scalars(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "task.prompt.v1",
                OutboxEvent.aggregate_id == task_id,
            )
        ).all()
        for event in prompt_events:
            event.status = "published"
            event.published_at = now
            event.claim_token = None
            event.lease_until = None
            event.attempt_count = max(1, event.attempt_count)
        prompt_event_ids = [event.id for event in prompt_events]
        if prompt_event_ids:
            prompt_receipts = db.scalars(
                select(OutboxReceipt)
                .where(OutboxReceipt.event_id.in_(prompt_event_ids))
                .with_for_update()
            ).all()
            for receipt in prompt_receipts:
                receipt.status = "completed"
                receipt.completed_at = now
                receipt.claim_token = None
                receipt.lease_until = None
                receipt.attempt_count = max(1, receipt.attempt_count)


def _create_cleanup_run(service: RetentionCleanupService) -> UUID:
    accepted = service.create_run(CleanupRunRequest(), idempotency_key=f"r7-run-{uuid4().hex}")
    return accepted.run_id


def _claim_specific_job(engine: Engine, *, run_id: UUID, aggregate_id: UUID) -> tuple[UUID, UUID]:
    with Session(engine) as db, db.begin():
        job = db.scalar(
            select(CleanupJob)
            .where(CleanupJob.run_id == run_id, CleanupJob.aggregate_id == aggregate_id)
            .with_for_update()
        )
        assert job is not None
        now = db.scalar(select(func.clock_timestamp()))
        token = uuid4()
        job.status = "claimed"
        job.lease_token = token
        job.lease_until = now + timedelta(minutes=5)
        job.attempt_count += 1
        return job.id, token


def _delete_run(engine: Engine, run_id: UUID) -> None:
    with Session(engine) as db, db.begin():
        db.execute(delete(CleanupRun).where(CleanupRun.id == run_id))


def _task_is_eligible_for_cleanup(engine: Engine, task_id: UUID) -> bool:
    with Session(engine) as db:
        now = db.scalar(select(func.clock_timestamp()))
        return task_id in _eligible_task_ids(db, now)


def _add_completed_action_envelope(
    db: Session, *, action_id: UUID, event_id: UUID, now: datetime
) -> None:
    db.add(
        OutboxEvent(
            id=event_id,
            event_type="task.action.v1",
            aggregate_id=action_id,
            operation_version=1,
            schema_version=1,
            payload={"action_id": str(action_id)},
            status="published",
            available_at=now,
            published_at=now,
            attempt_count=1,
        )
    )
    db.flush()
    db.add(
        OutboxReceipt(
            event_id=event_id,
            status="completed",
            available_at=now,
            attempt_count=1,
            completed_at=now,
        )
    )


@pytest.mark.integration
def test_task_delete_blocks_retry_and_expires_existing_retry_key(
    retry_state: dict[str, object], tmp_path
) -> None:
    engine, owner_id, task_id, item_ids = _fixture_ids(retry_state)
    selected_item_id = item_ids[0]
    old_retry_key = f"r7-existing-retry-{uuid4().hex}"
    request = TaskRetryRequest(failed_item_ids=[selected_item_id])
    digest = retry_request_digest(task_id, (selected_item_id,))
    with Session(engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        db.add(
            TaskIdempotencyRecord(
                owner_user_id=owner_id,
                operation=TASK_RETRY_OPERATION,
                key_hash=idempotency_key_hash(old_retry_key),
                request_digest=digest,
                digest_version=1,
                task_id=task_id,
                planned_count=1,
                state="active",
                created_at=now,
                updated_at=now,
            )
        )
    task_service = TaskService(engine, TaskOptions())
    replay = task_service.retry_failed_items(
        owner_user_id=owner_id,
        task_id=task_id,
        idempotency_key=old_retry_key,
        request=request,
    )
    assert replay.task_id == task_id

    _make_task_cleanup_eligible(engine, task_id)
    storage = _BlockingStorage(tmp_path / "private")
    cleanup = RetentionCleanupService(engine, storage)
    run_id = _create_cleanup_run(cleanup)
    try:
        cleanup._discover_run(run_id, batch_size=100)
        job_id, cleanup_token = _claim_specific_job(engine, run_id=run_id, aggregate_id=task_id)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(cleanup._process_claimed_job, job_id, cleanup_token)
            assert storage.delete_entered.wait(timeout=10), "cleanup did not reach storage I/O"
            with pytest.raises(DomainError) as deleting_retry:
                task_service.retry_failed_items(
                    owner_user_id=owner_id,
                    task_id=task_id,
                    idempotency_key=f"r7-new-during-delete-{uuid4().hex}",
                    request=TaskRetryRequest(failed_item_ids=[item_ids[1]]),
                )
            assert deleting_retry.value.status_code == 409
            assert deleting_retry.value.code == "TASK_DELETING"
            storage.allow_delete.set()
            outcome, _ = future.result(timeout=30)
        assert outcome == "completed"

        with Session(engine) as db:
            assert db.get(Task, task_id) is None
            tombstone = db.scalar(
                select(TaskIdempotencyRecord).where(
                    TaskIdempotencyRecord.owner_user_id == owner_id,
                    TaskIdempotencyRecord.operation == TASK_RETRY_OPERATION,
                    TaskIdempotencyRecord.key_hash == idempotency_key_hash(old_retry_key),
                )
            )
            consumption = db.scalar(
                select(TaskRecommendationConsumption).where(
                    TaskRecommendationConsumption.task_id == task_id
                )
            )
            assert tombstone is not None and tombstone.state == "expired"
            assert consumption is not None and consumption.state == "expired"
            assert (
                db.scalar(
                    select(func.count(OutboxEvent.id)).where(
                        OutboxEvent.event_type == "task.prompt.v1",
                        OutboxEvent.aggregate_id == task_id,
                    )
                )
                == 0
            )

        with pytest.raises(DomainError) as deleted_retry:
            task_service.retry_failed_items(
                owner_user_id=owner_id,
                task_id=task_id,
                idempotency_key=old_retry_key,
                request=request,
            )
        assert deleted_retry.value.status_code == 410
        assert deleted_retry.value.code == "TASK_EXPIRED"
    finally:
        storage.allow_delete.set()
        _delete_run(engine, run_id)


@pytest.mark.integration
def test_unknown_actions_block_until_recovery_or_callback_is_persisted(
    retry_state: dict[str, object], tmp_path
) -> None:
    engine, _owner_id, task_id, item_ids = _fixture_ids(retry_state)
    attempt_id = retry_state["attempt_ids"][item_ids[0]]
    _make_task_cleanup_eligible(engine, task_id)
    submission_action_id, submission_event_id = uuid4(), uuid4()
    recovery_action_id, recovery_event_id = uuid4(), uuid4()

    with Session(engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        attempt = db.get(TaskItemAttempt, attempt_id)
        submit_action = db.scalar(
            select(Action).where(
                Action.attempt_id == attempt_id,
                Action.action_kind == "generation.submit.v1",
            )
        )
        submission = db.scalar(select(PromptSubmission).where(PromptSubmission.task_id == task_id))
        workflow = db.scalar(
            select(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_id)
        )
        assert attempt is not None and attempt.status in {"completed", "failed"}
        assert submit_action is not None and submit_action.sequence == 1
        assert submission is not None and workflow is not None
        submit_action.status = "unknown"
        submit_action.last_outcome = "unknown"
        submit_action.last_error_code = "MOCK_ACCEPTED_RESPONSE_LOST"
        submit_action.completed_at = now
        submission.status = "package_ready"
        submission.callback_result_digest_version = None
        submission.callback_result_digest = None
        workflow.status = "package_ready"

    # A terminal attempt alone does not resolve an unknown submit.
    assert not _task_is_eligible_for_cleanup(engine, task_id)

    with Session(engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        submission = db.scalar(select(PromptSubmission).where(PromptSubmission.task_id == task_id))
        assert submission is not None
        _add_completed_action_envelope(
            db, action_id=submission_action_id, event_id=submission_event_id, now=now
        )
        db.add(
            Action(
                id=submission_action_id,
                task_id=task_id,
                submission_id=submission.submission_id,
                scope_kind="submission",
                scope_id=submission.submission_id,
                action_kind="prompt.initial.submit",
                sequence=1,
                due_at=now,
                status="unknown",
                claim_count=1,
                input_digest_version=1,
                input_digest=hashlib.sha256(
                    f"unknown-prompt:{submission.submission_id}".encode()
                ).digest(),
                outbox_event_id=submission_event_id,
                last_outcome="unknown",
                last_error_code="PROMPT_SUBMIT_UNKNOWN",
                completed_at=now,
            )
        )

    # An unknown prompt submit also remains protected without a persisted
    # callback digest, even though its workflow row currently looks terminal.
    assert not _task_is_eligible_for_cleanup(engine, task_id)

    with Session(engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        attempt = db.get(TaskItemAttempt, attempt_id)
        assert attempt is not None
        recovery_sequence = db.scalar(
            select(func.max(Action.sequence)).where(Action.attempt_id == attempt_id)
        )
        assert recovery_sequence == 1
        _add_completed_action_envelope(
            db, action_id=recovery_action_id, event_id=recovery_event_id, now=now
        )
        db.add(
            Action(
                id=recovery_action_id,
                task_id=task_id,
                task_item_id=attempt.task_item_id,
                attempt_id=attempt_id,
                scope_kind="attempt",
                scope_id=attempt_id,
                action_kind="generation.recover.v1",
                sequence=2,
                due_at=now,
                status="completed",
                claim_count=1,
                input_digest_version=1,
                input_digest=hashlib.sha256(f"recovery:{attempt_id}".encode()).digest(),
                outbox_event_id=recovery_event_id,
                last_outcome="accepted",
                completed_at=now,
            )
        )

    # A confirmed generation recovery resolves its earlier unknown Action, but
    # the prompt unknown still blocks until the callback receipt is durable.
    assert not _task_is_eligible_for_cleanup(engine, task_id)

    with Session(engine) as db, db.begin():
        submission = db.scalar(select(PromptSubmission).where(PromptSubmission.task_id == task_id))
        assert submission is not None
        submission.callback_result_digest_version = 2
        submission.callback_result_digest = hashlib.sha256(b"persisted callback").digest()

    # A non-NULL but unsupported digest version is not terminal proof.
    assert not _task_is_eligible_for_cleanup(engine, task_id)

    with Session(engine) as db, db.begin():
        submission = db.scalar(select(PromptSubmission).where(PromptSubmission.task_id == task_id))
        assert submission is not None
        submission.callback_result_digest_version = 1

    with Session(engine) as db:
        assert db.get(Action, recovery_action_id) is not None
        unknown_actions = db.scalars(
            select(Action).where(
                Action.task_id == task_id,
                Action.status == "unknown",
            )
        ).all()
        assert len(unknown_actions) == 2

    assert _task_is_eligible_for_cleanup(engine, task_id)

    cleanup = RetentionCleanupService(engine, LocalPrivateStorage(tmp_path / "unknown-actions"))
    run_id = _create_cleanup_run(cleanup)
    try:
        cleanup._discover_run(run_id, batch_size=100)
        job_id, token = _claim_specific_job(engine, run_id=run_id, aggregate_id=task_id)
        outcome, _deleted_count = cleanup._process_claimed_job(job_id, token)
        assert outcome == "completed"
        with Session(engine) as db:
            assert db.get(Task, task_id) is None
    finally:
        _delete_run(engine, run_id)


@pytest.mark.integration
def test_pending_generation_start_event_blocks_then_is_removed_with_task(
    retry_state: dict[str, object], tmp_path
) -> None:
    engine, _owner_id, task_id, item_ids = _fixture_ids(retry_state)
    item_id = item_ids[0]
    _make_task_cleanup_eligible(engine, task_id)
    cleanup = RetentionCleanupService(engine, LocalPrivateStorage(tmp_path / "generation-event"))
    blocked_run = _create_cleanup_run(cleanup)
    gated_run: UUID | None = None
    receipt_gated_run: UUID | None = None
    recovery_run: UUID | None = None
    try:
        # First materialize the candidate, then insert a start event to exercise
        # the prepare-time race gate after discovery.
        cleanup._discover_run(blocked_run, batch_size=100)
        with Session(engine) as db:
            assert (
                db.scalar(
                    select(CleanupJob.id).where(
                        CleanupJob.run_id == blocked_run,
                        CleanupJob.aggregate_kind == "task",
                        CleanupJob.aggregate_id == task_id,
                    )
                )
                is not None
            )

        eligible_before = cleanup.preview().eligible_tasks
        with Session(engine) as db, db.begin():
            prompt_event = db.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.event_type == "task.prompt.v1",
                    OutboxEvent.aggregate_id == task_id,
                )
                .order_by(OutboxEvent.id)
                .limit(1)
            )
            assert prompt_event is not None
            prompt_receipt = db.get(OutboxReceipt, prompt_event.id)
            assert prompt_receipt is not None
            prompt_receipt.status = "pending"
            prompt_receipt.completed_at = None
        assert cleanup.preview().eligible_tasks == eligible_before - 1
        with Session(engine) as db, db.begin():
            prompt_event = db.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == "task.prompt.v1",
                    OutboxEvent.aggregate_id == task_id,
                )
            )
            assert prompt_event is not None
            prompt_receipt = db.get(OutboxReceipt, prompt_event.id)
            assert prompt_receipt is not None
            prompt_receipt.status = "completed"
            prompt_receipt.completed_at = db.scalar(select(func.clock_timestamp()))

        event_id = uuid4()
        now = datetime.now(UTC)
        with Session(engine) as db, db.begin():
            db.add(
                OutboxEvent(
                    id=event_id,
                    event_type="task.generation.start.v1",
                    aggregate_id=item_id,
                    operation_version=1,
                    schema_version=1,
                    payload={"task_item_id": str(item_id)},
                    status="pending",
                    available_at=now,
                    attempt_count=0,
                )
            )
            db.flush()
            db.add(
                OutboxReceipt(
                    event_id=event_id,
                    status="pending",
                    available_at=now,
                    attempt_count=0,
                )
            )

        job_id, token = _claim_specific_job(engine, run_id=blocked_run, aggregate_id=task_id)
        outcome, _ = cleanup._process_claimed_job(job_id, token)
        assert outcome == "blocked"
        with Session(engine) as db:
            task = db.get(Task, task_id)
            assert task is not None and task.status == "failed"

        # A new discovery excludes the pending generation event.
        gated_run = _create_cleanup_run(cleanup)
        cleanup._discover_run(gated_run, batch_size=100)
        with Session(engine) as db:
            assert (
                db.scalar(
                    select(CleanupJob.id).where(
                        CleanupJob.run_id == gated_run,
                        CleanupJob.aggregate_kind == "task",
                        CleanupJob.aggregate_id == task_id,
                    )
                )
                is None
            )

        # A published generation envelope with an unresolved receipt also stays
        # outside the candidate set; event status alone is not sufficient.
        with Session(engine) as db, db.begin():
            outbox_event = db.get(OutboxEvent, event_id)
            assert outbox_event is not None
            outbox_event.status = "published"
            outbox_event.published_at = db.scalar(select(func.clock_timestamp()))
        receipt_gated_run = _create_cleanup_run(cleanup)
        cleanup._discover_run(receipt_gated_run, batch_size=100)
        with Session(engine) as db:
            assert (
                db.scalar(
                    select(CleanupJob.id).where(
                        CleanupJob.run_id == receipt_gated_run,
                        CleanupJob.aggregate_kind == "task",
                        CleanupJob.aggregate_id == task_id,
                    )
                )
                is None
            )

        # Once the event and receipt are terminal, a later run can safely delete
        # the task and clear all event evidence under the same locked event set.
        with Session(engine) as db, db.begin():
            outbox_event = db.get(OutboxEvent, event_id)
            receipt = db.get(OutboxReceipt, event_id)
            assert outbox_event is not None and receipt is not None
            outbox_event.status = "published"
            outbox_event.published_at = db.scalar(select(func.clock_timestamp()))
            receipt.status = "completed"
            receipt.completed_at = db.scalar(select(func.clock_timestamp()))
            db.add(
                OutboxRejectedMessage(
                    event_id=event_id,
                    message_fingerprint=hashlib.sha256(
                        f"r7-generation-event:{event_id}".encode()
                    ).digest(),
                    error_code="SYNTHETIC_REJECTED",
                    occurrence_count=1,
                )
            )
        recovery_run = _create_cleanup_run(cleanup)
        cleanup._discover_run(recovery_run, batch_size=100)
        recovery_job, recovery_token = _claim_specific_job(
            engine, run_id=recovery_run, aggregate_id=task_id
        )
        completed, _ = cleanup._process_claimed_job(recovery_job, recovery_token)
        assert completed == "completed"
        with Session(engine) as db:
            assert db.get(Task, task_id) is None
            assert db.get(OutboxEvent, event_id) is None
            assert db.get(OutboxReceipt, event_id) is None
            assert (
                db.scalar(
                    select(OutboxRejectedMessage.id).where(
                        OutboxRejectedMessage.event_id == event_id
                    )
                )
                is None
            )
    finally:
        _delete_run(engine, blocked_run)
        if gated_run is not None:
            _delete_run(engine, gated_run)
        if receipt_gated_run is not None:
            _delete_run(engine, receipt_gated_run)
        if recovery_run is not None:
            _delete_run(engine, recovery_run)


@pytest.mark.integration
def test_task_storage_failure_resumes_manifest_and_second_run_skips_duplicate(
    retry_state: dict[str, object], tmp_path
) -> None:
    engine, _owner_id, task_id, _item_ids = _fixture_ids(retry_state)
    _make_task_cleanup_eligible(engine, task_id)
    storage = _FailFirstDeleteStorage(tmp_path / "private")
    cleanup = RetentionCleanupService(engine, storage)
    first_run_id = _create_cleanup_run(cleanup)
    second_run_id: UUID | None = None
    try:
        # Materialize both candidates while the aggregate is eligible. A global
        # worker may claim jobs from any run, so this regression claims the exact
        # run/job under test instead of depending on queue history in the shared DB.
        cleanup._discover_run(first_run_id)
        second_run_id = _create_cleanup_run(cleanup)
        cleanup._discover_run(second_run_id)
        first_job_id, first_token = _claim_specific_job(
            engine, run_id=first_run_id, aggregate_id=task_id
        )
        outcome, _ = cleanup._process_claimed_job(first_job_id, first_token)
        assert outcome == "retry"
        with Session(engine) as db:
            first_job = db.scalar(
                select(CleanupJob).where(
                    CleanupJob.run_id == first_run_id,
                    CleanupJob.aggregate_kind == "task",
                    CleanupJob.aggregate_id == task_id,
                )
            )
            assert first_job is not None and first_job.status == "retry"
            first_job_id = first_job.id
            target = db.scalar(
                select(CleanupTarget).where(
                    CleanupTarget.job_id == first_job.id,
                    CleanupTarget.status == "failed",
                )
            )
            task = db.get(Task, task_id)
            assert target is not None and target.status == "failed"
            assert target.relative_locator is not None
            assert task is not None and task.status == "deleting"

        second_job_id, second_token = _claim_specific_job(
            engine, run_id=second_run_id, aggregate_id=task_id
        )
        second_outcome, deleted_count = cleanup._process_claimed_job(second_job_id, second_token)
        assert second_outcome == "completed" and deleted_count == 0
        with Session(engine) as db:
            assert (
                db.scalar(
                    select(CleanupJob.id).where(
                        CleanupJob.run_id == second_run_id,
                        CleanupJob.aggregate_kind == "task",
                        CleanupJob.aggregate_id == task_id,
                    )
                )
                is None
            )

        with Session(engine) as db, db.begin():
            first_job = db.scalar(
                select(CleanupJob)
                .where(CleanupJob.run_id == first_run_id, CleanupJob.aggregate_id == task_id)
                .with_for_update()
            )
            assert first_job is not None
            first_job.next_retry_at = db.scalar(select(func.clock_timestamp())) - timedelta(
                seconds=1
            )
        first_retry_id, first_retry_token = _claim_specific_job(
            engine, run_id=first_run_id, aggregate_id=task_id
        )
        final_outcome, _ = cleanup._process_claimed_job(first_retry_id, first_retry_token)
        assert final_outcome == "completed"
        with Session(engine) as db:
            assert db.get(Task, task_id) is None
            assert (
                db.scalar(select(CleanupTarget.id).where(CleanupTarget.job_id == first_job_id))
                is None
            )
    finally:
        _delete_run(engine, first_run_id)
        if second_run_id is not None:
            _delete_run(engine, second_run_id)


@pytest.mark.integration
def test_inventory_cursor_lease_fences_stale_scanner_and_replays_targets_idempotently(
    retry_state: dict[str, object], tmp_path
) -> None:
    engine, owner_id, _task_id, _item_ids = _fixture_ids(retry_state)
    key = f"users/{owner_id}/r7-inventory/{uuid4().hex}.png"
    storage = _ContendedPageStorage(tmp_path / "private")
    _write_old_object(storage, key, age=timedelta(days=3))
    cleanup = RetentionCleanupService(engine, storage)
    run_id = _create_cleanup_run(cleanup)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            stale_worker = executor.submit(cleanup._discover_run, run_id, batch_size=1)
            assert storage.first_page_ready.wait(timeout=10)
            first_page = storage.first_page
            assert first_page is not None and first_page.entries
            entry = first_page.entries[0]

            # Simulate recovery from a process that persisted the exact target
            # but crashed before recording its cursor. The normal implementation
            # commits both in one transaction, so this partial state is only a
            # defensive replay case.
            with Session(engine) as db, db.begin():
                db.add(
                    CleanupJob(
                        run_id=run_id,
                        aggregate_kind="orphan",
                        aggregate_id=uuid5(
                            _CLEANUP_NAMESPACE,
                            f"{entry.kind}:{entry.relative_locator}",
                        ),
                        status="pending",
                    )
                )
                db.flush()
                job = db.scalar(
                    select(CleanupJob).where(
                        CleanupJob.run_id == run_id,
                        CleanupJob.aggregate_kind == "orphan",
                    )
                )
                assert job is not None
                db.add(
                    CleanupTarget(
                        job_id=job.id,
                        sequence=1,
                        entry_kind=entry.kind,
                        relative_locator=entry.relative_locator,
                        object_key=entry.object_key,
                        source_kind=None,
                        source_id=None,
                        status="pending",
                    )
                )

            with Session(engine) as db, db.begin():
                run = db.get(CleanupRun, run_id)
                assert run is not None and run.orphan_scan_lease_token is not None
                run.orphan_scan_lease_until = db.scalar(select(func.clock_timestamp())) - timedelta(
                    seconds=1
                )

            # The expired lease is reclaimed at the same cursor. The second
            # scanner sees the previously persisted target and advances without
            # inserting a duplicate. The stale first scanner must not roll back
            # the cursor after it resumes.
            cleanup._discover_run(run_id, batch_size=1)
            with Session(engine) as db:
                advanced = db.get(CleanupRun, run_id)
                assert advanced is not None
                assert advanced.orphan_scan_cursor == first_page.next_cursor
                assert advanced.orphan_scan_lease_token is None
                targets = db.scalars(
                    select(CleanupTarget).join(CleanupJob).where(CleanupJob.run_id == run_id)
                ).all()
                assert len(targets) == 1
            storage.release_first_page.set()
            stale_worker.result(timeout=20)

        with Session(engine) as db:
            after_stale_worker = db.get(CleanupRun, run_id)
            assert after_stale_worker is not None
            assert after_stale_worker.orphan_scan_cursor == first_page.next_cursor
            assert after_stale_worker.orphan_scan_lease_token is None
            assert (
                db.scalar(
                    select(func.count(CleanupTarget.id))
                    .join(CleanupJob)
                    .where(CleanupJob.run_id == run_id)
                )
                == 1
            )
    finally:
        storage.release_first_page.set()
        _delete_run(engine, run_id)


@pytest.mark.integration
def test_inventory_storage_error_releases_lease_without_finishing_scan(
    retry_state: dict[str, object], tmp_path
) -> None:
    engine, owner_id, _task_id, _item_ids = _fixture_ids(retry_state)
    storage = _FailFirstPageStorage(tmp_path / "inventory-retry")
    key = f"users/{owner_id}/retry/{uuid4().hex}.png"
    _write_old_object(storage, key, age=timedelta(days=3))
    cleanup = RetentionCleanupService(engine, storage)
    run_id = _create_cleanup_run(cleanup)
    try:
        cleanup._discover_run(run_id, batch_size=2)
        with Session(engine) as db:
            run = db.get(CleanupRun, run_id)
            assert run is not None
            assert run.orphan_scan_cursor is None
            assert run.orphan_scan_completed_at is None
            assert run.orphan_scan_lease_token is None
            assert run.orphan_scan_lease_until is None
            assert run.counts.get("inventory_scan_errors") == 1
        run_summary = cleanup.get_run(str(run_id))
        assert {reason.code: reason.count for reason in run_summary.counters.reason_counts}.get(
            "INVENTORY_SCAN_FAILED"
        ) == 1
        cleanup._discover_run(run_id, batch_size=2)
        with Session(engine) as db:
            run = db.get(CleanupRun, run_id)
            assert run is not None and run.orphan_scan_completed_at is not None
            assert run.orphan_scan_cursor is not None
            targets = db.scalars(
                select(CleanupTarget)
                .join(CleanupJob, CleanupJob.id == CleanupTarget.job_id)
                .where(CleanupJob.run_id == run_id)
            ).all()
            assert len(targets) == 2
    finally:
        _delete_run(engine, run_id)


@pytest.mark.integration
def test_inventory_page_writes_targets_before_run_lock_to_match_worker_order(
    retry_state: dict[str, object], tmp_path
) -> None:
    engine, owner_id, _task_id, _item_ids = _fixture_ids(retry_state)
    application_name = f"r7_page_order_{uuid4().hex[:16]}"
    worker_engine = create_engine(
        engine.url,
        connect_args={"application_name": application_name},
        pool_pre_ping=True,
    )
    storage = LocalPrivateStorage(tmp_path / "inventory-lock-order")
    key = f"users/{owner_id}/lock-order/{uuid4().hex}.png"
    _write_old_object(storage, key, age=timedelta(days=3))
    cleanup = RetentionCleanupService(worker_engine, storage)
    run_id = _create_cleanup_run(cleanup)
    job_locked = threading.Event()
    scanner_waiting = threading.Event()
    run_counter_updated = threading.Event()
    release_holder = threading.Event()
    try:
        page = storage.iter_inventory_page(
            prefixes=("users/", "generated/"),
            modified_before=datetime.now(UTC) - timedelta(days=1),
            after_locator=None,
            max_scanned_items=20,
        )
        assert page.entries and page.next_cursor is not None
        first_entry = page.entries[0]
        aggregate_id = uuid5(
            _CLEANUP_NAMESPACE, f"{first_entry.kind}:{first_entry.relative_locator}"
        )
        with Session(engine) as db, db.begin():
            db.add(
                CleanupJob(
                    run_id=run_id,
                    aggregate_kind="orphan",
                    aggregate_id=aggregate_id,
                    status="pending",
                )
            )
        lease = cleanup._claim_orphan_scan_lease(run_id)
        assert lease is not None
        token, expected_cursor = lease
        assert expected_cursor is None

        def hold_job_then_increment_run() -> None:
            with Session(engine) as db, db.begin():
                db.execute(text("SET LOCAL lock_timeout = '3s'"))
                job = db.execute(
                    select(CleanupJob)
                    .where(
                        CleanupJob.run_id == run_id,
                        CleanupJob.aggregate_kind == "orphan",
                        CleanupJob.aggregate_id == aggregate_id,
                    )
                    .with_for_update()
                ).scalar_one()
                assert job.id
                job_locked.set()
                if not scanner_waiting.wait(timeout=10):
                    raise AssertionError("inventory page did not wait on its target job")
                RetentionCleanupService._increment_run_counter(db, run_id, "jobs_succeeded")
                run_counter_updated.set()
                if not release_holder.wait(timeout=10):
                    raise AssertionError("synthetic job lock gate timed out")

        def persist_page() -> bool:
            return cleanup._persist_inventory_page(
                run_id,
                token=token,
                expected_cursor=expected_cursor,
                page=page,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            holder_future = executor.submit(hold_job_then_increment_run)
            assert job_locked.wait(timeout=10)
            page_future = executor.submit(persist_page)
            assert _wait_for_lock_wait(
                engine,
                application_name=application_name,
                query_marker="cleanup_targets",
            ), "page did not wait on the pre-existing job's target FK before run CAS"
            scanner_waiting.set()
            assert run_counter_updated.wait(timeout=5), (
                "page held the run lock while waiting for a job/target lock"
            )
            release_holder.set()
            holder_future.result(timeout=10)
            assert page_future.result(timeout=10)

        with Session(engine) as db:
            run = db.get(CleanupRun, run_id)
            assert run is not None
            assert run.orphan_scan_cursor == page.next_cursor
            assert run.counts.get("jobs_succeeded") == 1
            assert db.scalar(
                select(func.count(CleanupTarget.id))
                .join(CleanupJob, CleanupJob.id == CleanupTarget.job_id)
                .where(CleanupJob.run_id == run_id)
            ) == len(page.entries)
    finally:
        scanner_waiting.set()
        release_holder.set()
        _delete_run(engine, run_id)
        worker_engine.dispose()


@pytest.mark.integration
def test_recommendation_data_expiry_and_task_reference_gates_cleanup(
    retry_state: dict[str, object], tmp_path
) -> None:
    engine, owner_id, task_id, _item_ids = _fixture_ids(retry_state)
    reference_id = retry_state["reference_resources"][0]["reference_set_id"]
    recommendation_id = retry_state["reference_resources"][0]["recommendation_id"]
    assert isinstance(reference_id, UUID) and isinstance(recommendation_id, UUID)
    cleanup = RetentionCleanupService(engine, LocalPrivateStorage(tmp_path / "shared-reference"))
    baseline = cleanup.preview()
    with Session(engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        reference = db.get(ReferenceSet, reference_id)
        recommendation = db.get(Recommendation, recommendation_id)
        task = db.get(Task, task_id)
        assert reference is not None and recommendation is not None and task is not None
        reference.ready_at = now - timedelta(days=90)
        reference.expires_at = now - timedelta(days=1)
        recommendation.created_at = now - timedelta(days=90)
        recommendation.data_expires_at = now - timedelta(days=1)
        recommendation.valid_until = now + timedelta(days=30)
        recommendation.updated_at = now
        task.expires_at = now + timedelta(days=30)

    preview = cleanup.preview()
    preview_reasons = {reason.code: reason.count for reason in preview.skip_reasons}
    baseline_reasons = {reason.code: reason.count for reason in baseline.skip_reasons}
    assert preview_reasons.get("RECOMMENDATION_STILL_REFERENCED", 0) == (
        baseline_reasons.get("RECOMMENDATION_STILL_REFERENCED", 0) + 1
    )
    assert preview_reasons.get("REFERENCE_STILL_REFERENCED", 0) == (
        baseline_reasons.get("REFERENCE_STILL_REFERENCED", 0) + 1
    )
    assert preview.eligible_recommendations == baseline.eligible_recommendations
    assert preview.eligible_reference_sets == baseline.eligible_reference_sets

    shared_run = _create_cleanup_run(cleanup)
    try:
        cleanup._discover_run(shared_run, batch_size=100)
        with Session(engine) as db:
            assert (
                db.scalar(
                    select(func.count(CleanupJob.id)).where(
                        CleanupJob.run_id == shared_run,
                        CleanupJob.aggregate_kind.in_(("recommendation", "reference_set")),
                        CleanupJob.aggregate_id.in_([recommendation_id, reference_id]),
                    )
                )
                == 0
            )

        # A second recommendation with an expired data TTL remains eligible even
        # while its form-validity window is still open. Its unexpired reference
        # set must survive recommendation deletion.
        independent_reference_id, independent_recommendation_id = (
            _create_reference_and_recommendation(engine, owner_user_id=owner_id)
        )
        _track_reference_resources(
            engine,
            retry_state,
            reference_set_id=independent_reference_id,
            recommendation_id=independent_recommendation_id,
        )
        with Session(engine) as db, db.begin():
            now = db.scalar(select(func.clock_timestamp()))
            reference = db.get(ReferenceSet, independent_reference_id)
            recommendation = db.get(Recommendation, independent_recommendation_id)
            assert reference is not None and recommendation is not None
            reference.ready_at = now - timedelta(days=1)
            reference.expires_at = now + timedelta(days=30)
            recommendation.created_at = now - timedelta(days=2)
            recommendation.data_expires_at = now - timedelta(microseconds=1)
            recommendation.valid_until = now + timedelta(days=30)
            recommendation.updated_at = now

        independent_run = _create_cleanup_run(cleanup)
        try:
            cleanup._discover_run(independent_run, batch_size=100)
            job_id, token = _claim_specific_job(
                engine, run_id=independent_run, aggregate_id=independent_recommendation_id
            )
            outcome, _ = cleanup._process_claimed_job(job_id, token)
            assert outcome == "completed"
            with Session(engine) as db:
                assert db.get(Recommendation, independent_recommendation_id) is None
                assert db.get(ReferenceSet, independent_reference_id) is not None
        finally:
            _delete_run(engine, independent_run)

        # An expired reference set remains blocked while an expired recommendation
        # still points to it. After the recommendation job commits, a later run
        # can safely pick up the now-unreferenced reference set.
        linked_reference_id, linked_recommendation_id = _create_reference_and_recommendation(
            engine, owner_user_id=owner_id
        )
        _track_reference_resources(
            engine,
            retry_state,
            reference_set_id=linked_reference_id,
            recommendation_id=linked_recommendation_id,
        )
        with Session(engine) as db, db.begin():
            now = db.scalar(select(func.clock_timestamp()))
            reference = db.get(ReferenceSet, linked_reference_id)
            recommendation = db.get(Recommendation, linked_recommendation_id)
            assert reference is not None and recommendation is not None
            reference.ready_at = now - timedelta(days=90)
            reference.expires_at = now - timedelta(days=1)
            recommendation.created_at = now - timedelta(days=90)
            recommendation.data_expires_at = now - timedelta(days=1)
            recommendation.valid_until = now + timedelta(days=30)
            recommendation.updated_at = now

        linked_run = _create_cleanup_run(cleanup)
        reference_run: UUID | None = None
        try:
            cleanup._discover_run(linked_run, batch_size=100)
            with Session(engine) as db:
                assert (
                    db.scalar(
                        select(CleanupJob.id).where(
                            CleanupJob.run_id == linked_run,
                            CleanupJob.aggregate_kind == "recommendation",
                            CleanupJob.aggregate_id == linked_recommendation_id,
                        )
                    )
                    is not None
                )
                assert (
                    db.scalar(
                        select(CleanupJob.id).where(
                            CleanupJob.run_id == linked_run,
                            CleanupJob.aggregate_kind == "reference_set",
                            CleanupJob.aggregate_id == linked_reference_id,
                        )
                    )
                    is None
                )
            job_id, token = _claim_specific_job(
                engine, run_id=linked_run, aggregate_id=linked_recommendation_id
            )
            outcome, _ = cleanup._process_claimed_job(job_id, token)
            assert outcome == "completed"
            with Session(engine) as db:
                assert db.get(Recommendation, linked_recommendation_id) is None
                assert db.get(ReferenceSet, linked_reference_id) is not None

            reference_run = _create_cleanup_run(cleanup)
            cleanup._discover_run(reference_run, batch_size=100)
            job_id, token = _claim_specific_job(
                engine, run_id=reference_run, aggregate_id=linked_reference_id
            )
            outcome, _ = cleanup._process_claimed_job(job_id, token)
            assert outcome == "completed"
            with Session(engine) as db:
                assert db.get(ReferenceSet, linked_reference_id) is None
        finally:
            _delete_run(engine, linked_run)
            if reference_run is not None:
                _delete_run(engine, reference_run)
    finally:
        _delete_run(engine, shared_run)


@pytest.mark.integration
def test_inventory_pages_advance_past_unsafe_recent_and_protected_entries(
    retry_state: dict[str, object], tmp_path
) -> None:
    engine, owner_id, _task_id, _item_ids = _fixture_ids(retry_state)
    storage = _InvalidFirstPageStorage(tmp_path / "inventory-pages")
    old_key = f"users/{owner_id}/z-old/{uuid4().hex}.png"
    protected_key = f"users/{owner_id}/a-protected/{uuid4().hex}.png"
    recent_key = f"users/{owner_id}/recent/{uuid4().hex}.png"
    _write_old_object(storage, old_key, age=timedelta(days=3))
    _write_old_object(storage, protected_key, age=timedelta(days=3))
    _write_old_object(storage, recent_key, age=timedelta(hours=2))
    old_staging = f"users/{owner_id}/z-old/.incoming-{uuid4().hex}"
    old_staging_path = storage.root.joinpath(*old_staging.split("/"))
    old_staging_path.parent.mkdir(parents=True, exist_ok=True)
    old_staging_path.write_bytes(b"synthetic stale staging")
    old_timestamp = (datetime.now(UTC) - timedelta(days=3)).timestamp()
    os.utime(old_staging_path, (old_timestamp, old_timestamp))

    upload_reference_id = uuid4()
    active_staging = (
        f"users/{owner_id}/reference-sets/{upload_reference_id}/.incoming-{uuid4().hex}"
    )
    active_staging_path = storage.root.joinpath(*active_staging.split("/"))
    active_staging_path.parent.mkdir(parents=True, exist_ok=True)
    active_staging_path.write_bytes(b"synthetic active upload")
    os.utime(active_staging_path, (old_timestamp, old_timestamp))
    protected_asset_id = uuid4()
    recent_failed_upload_id = uuid4()
    stale_failed_upload_id = uuid4()
    cleanup = RetentionCleanupService(engine, storage)
    baseline = cleanup.preview()
    baseline_active_lease_count = sum(
        reason.count for reason in baseline.skip_reasons if reason.code == "ACTIVE_UPLOAD_LEASE"
    )
    with Session(engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        db.add(
            ReferenceSet(
                id=upload_reference_id,
                owner_user_id=owner_id,
                product_category="R7 active lease fixture",
                status="uploading",
                lease_token=uuid4(),
                upload_lease_until=now + timedelta(hours=1),
                last_activity_at=now,
            )
        )
        recent_failure_at = now - timedelta(hours=2)
        db.add_all(
            [
                ReferenceSet(
                    id=recent_failed_upload_id,
                    owner_user_id=owner_id,
                    product_category="R7 recent failed upload fixture",
                    status="failed",
                    last_activity_at=recent_failure_at,
                    failed_at=recent_failure_at,
                    failure_code="UPLOAD_FAILED",
                ),
                ReferenceSet(
                    id=stale_failed_upload_id,
                    owner_user_id=owner_id,
                    product_category="R7 stale failed upload fixture",
                    status="failed",
                    last_activity_at=now - timedelta(days=2),
                    failed_at=now - timedelta(days=2),
                    failure_code="UPLOAD_FAILED",
                ),
            ]
        )
        db.add(
            ImageAsset(
                id=protected_asset_id,
                owner_user_id=owner_id,
                asset_kind="reference_source",
                object_key=protected_key,
                content_sha256="a" * 64,
                mime_type="image/png",
                size_bytes=30,
                width=2,
                height=2,
            )
        )

    lease_preview = cleanup.preview()
    active_lease_count = sum(
        reason.count
        for reason in lease_preview.skip_reasons
        if reason.code == "ACTIVE_UPLOAD_LEASE"
    )
    assert active_lease_count == baseline_active_lease_count + 1
    assert lease_preview.eligible_upload_groups == baseline.eligible_upload_groups + 1
    run_id = _create_cleanup_run(cleanup)
    try:
        for _ in range(20):
            with Session(engine) as db:
                run = db.get(CleanupRun, run_id)
                assert run is not None
                if run.orphan_scan_completed_at is not None:
                    break
            cleanup._discover_run(run_id, batch_size=2)
        with Session(engine) as db:
            run = db.get(CleanupRun, run_id)
            assert run is not None and run.orphan_scan_completed_at is not None
            assert int(run.counts.get("inventory_scan_items", 0)) >= 6
            targets = db.execute(
                select(CleanupTarget.entry_kind, CleanupTarget.relative_locator)
                .join(CleanupJob, CleanupJob.id == CleanupTarget.job_id)
                .where(CleanupJob.run_id == run_id, CleanupJob.aggregate_kind == "orphan")
            ).all()
        target_pairs = {(kind, locator) for kind, locator in targets}
        assert ("object", old_key) in target_pairs
        expected_sidecar = f"users/{owner_id}/z-old/.{old_key.rsplit('/', 1)[1]}.metadata.json"
        assert ("sidecar", expected_sidecar) in target_pairs
        assert ("staging", old_staging) in target_pairs
        assert all("invalid.png" not in locator for _kind, locator in target_pairs)
        assert all(recent_key not in locator for _kind, locator in target_pairs)
        assert all(protected_key not in locator for _kind, locator in target_pairs)
        assert all(active_staging != locator for _kind, locator in target_pairs)
    finally:
        _delete_run(engine, run_id)
        with Session(engine) as db, db.begin():
            db.execute(
                delete(ReferenceSet).where(
                    ReferenceSet.id.in_(
                        [
                            upload_reference_id,
                            recent_failed_upload_id,
                            stale_failed_upload_id,
                        ]
                    )
                )
            )
            db.execute(delete(ImageAsset).where(ImageAsset.id == protected_asset_id))


@pytest.mark.integration
def test_run_fails_safely_when_storage_inventory_capability_is_unavailable(
    retry_state: dict[str, object], tmp_path
) -> None:
    engine, _owner_id, _task_id, _item_ids = _fixture_ids(retry_state)
    cleanup = RetentionCleanupService(
        engine,
        _StorageWithoutInventory(LocalPrivateStorage(tmp_path / "no-inventory")),
    )
    run_id = _create_cleanup_run(cleanup)
    try:
        # Isolate the inventory outcome from eligible business rows already in
        # the shared test database.
        with Session(engine) as db, db.begin():
            run = db.get(CleanupRun, run_id)
            assert run is not None
            run.status = "running"
            run.counts = {"business_discovery_complete": 1}
        cleanup._discover_orphan_inventory(run_id, batch_size=10)
        cleanup._refresh_run_statuses()
        response = cleanup.get_run(str(run_id))
        assert response.status == "failed"
        assert response.safe_error_code == "INVENTORY_UNAVAILABLE"
        assert any(
            reason.code == "INVENTORY_UNAVAILABLE" and reason.count == 1
            for reason in response.counters.reason_counts
        )
        with Session(engine) as db:
            run = db.get(CleanupRun, run_id)
            assert run is not None
            assert run.orphan_scan_completed_at is not None
            assert run.counts.get("inventory_available") == 0
            assert run.counts.get("jobs_failed") == 1
    finally:
        _delete_run(engine, run_id)


@pytest.mark.integration
def test_task_cleanup_locks_capacity_before_reservation_during_concurrent_release(
    retry_state: dict[str, object], tmp_path
) -> None:
    engine, owner_id, task_id, item_ids = _fixture_ids(retry_state)
    attempt_ids = retry_state["attempt_ids"]
    attempt_id = attempt_ids[item_ids[0]]
    _make_task_cleanup_eligible(engine, task_id)
    with Session(engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        assert db.get(GenerationCapacityControl, 1) is not None
        db.add(
            GenerationCapacityReservation(
                attempt_id=attempt_id,
                task_id=task_id,
                task_item_id=item_ids[0],
                owner_user_id=owner_id,
                status="active",
                reserved_at=now,
                released_at=None,
            )
        )

    application_name = f"r7_cleanup_{uuid4().hex[:16]}"
    cleanup_engine = create_engine(
        engine.url,
        connect_args={"application_name": application_name},
        pool_pre_ping=True,
    )
    cleanup = RetentionCleanupService(cleanup_engine, LocalPrivateStorage(tmp_path / "capacity"))
    run_id = _create_cleanup_run(cleanup)
    cleanup._discover_run(run_id, batch_size=100)
    with Session(engine) as db, db.begin():
        job = db.scalar(
            select(CleanupJob)
            .where(
                CleanupJob.run_id == run_id,
                CleanupJob.aggregate_kind == "task",
                CleanupJob.aggregate_id == task_id,
            )
            .with_for_update()
        )
        assert job is not None
        job_id = job.id
        cleanup_token = uuid4()
        now = db.scalar(select(func.clock_timestamp()))
        job.status = "claimed"
        job.lease_token = cleanup_token
        job.lease_until = now + timedelta(minutes=5)
        job.attempt_count += 1
    capacity_locked = threading.Event()
    allow_release = threading.Event()
    capacity_query_entered = threading.Event()

    def observe_capacity_query(_connection, _cursor, statement, _parameters, _context, _many):
        if "generation_capacity_control" in statement.lower():
            capacity_query_entered.set()

    event.listen(cleanup_engine, "before_cursor_execute", observe_capacity_query)

    def release_capacity() -> None:
        with Session(engine) as db, db.begin():
            control = db.execute(
                select(GenerationCapacityControl)
                .where(GenerationCapacityControl.id == 1)
                .with_for_update()
            ).scalar_one()
            assert control.id == 1
            capacity_locked.set()
            if not allow_release.wait(timeout=20):
                raise AssertionError("synthetic capacity lock gate timed out")
            reservation = db.execute(
                select(GenerationCapacityReservation)
                .where(GenerationCapacityReservation.attempt_id == attempt_id)
                .with_for_update()
            ).scalar_one()
            reservation.status = "released"
            reservation.released_at = db.scalar(select(func.clock_timestamp()))

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            release_future = executor.submit(release_capacity)
            assert capacity_locked.wait(timeout=10)
            cleanup_future = executor.submit(cleanup._process_claimed_job, job_id, cleanup_token)
            assert capacity_query_entered.wait(timeout=10), (
                "cleanup did not issue the capacity singleton lock before reservations"
            )
            time.sleep(0.15)
            assert not cleanup_future.done(), (
                "cleanup passed the held capacity lock before reservation release"
            )
            allow_release.set()
            release_future.result(timeout=20)
            outcome, _ = cleanup_future.result(timeout=30)
        assert outcome == "completed"
        with Session(engine) as db:
            assert db.get(Task, task_id) is None
            assert (
                db.scalar(
                    select(func.count(GenerationCapacityReservation.id)).where(
                        GenerationCapacityReservation.task_id == task_id
                    )
                )
                == 0
            )
    finally:
        event.remove(cleanup_engine, "before_cursor_execute", observe_capacity_query)
        allow_release.set()
        _delete_run(engine, run_id)
        cleanup_engine.dispose()
