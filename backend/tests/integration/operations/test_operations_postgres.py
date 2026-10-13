from __future__ import annotations

import hashlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.generation_models import (
    GenerationAttemptContext,
    GenerationCapacityReservation,
    MockGenerationInvocation,
)
from app.db.operations_models import OperationsLoopHeartbeat, OperationsRecoveryAudit
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.prompt_models import PromptSubmission, TaskPromptWorkflow
from app.db.task_models import Task, TaskItem, TaskItemAttempt
from app.schemas.prompt_workflow import PromptCallback
from app.services.generation_runtime import _schedule_generation_action
from app.services.operations import OperationsDiagnosticsService
from app.services.operations_heartbeat import OperationsHeartbeatWriter
from app.services.prompt_callbacks import PromptCallbackService
from sqlalchemy import delete, func, inspect, select
from sqlalchemy.orm import Session
from tests.integration.actions.test_submission_action_processor_postgres import (
    _create_task,
    _PGContext,
    _process_task_prompt_intent,
)
from tests.integration.actions.test_submission_action_processor_postgres import (
    pg_context as _pg_context_fixture,  # noqa: F401 - register the imported pytest fixture
)
from tests.integration.generation.test_generation_runtime_postgres import (
    _create_task_with_prompt,
    _process_start_event,
)

pytestmark = pytest.mark.integration


def _require_n2_schema(context: _PGContext) -> None:
    if not {"operations_loop_heartbeats", "operations_recovery_audits"}.issubset(
        set(inspect(context.engine).get_table_names())
    ):
        pytest.skip("requires the isolated PostgreSQL schema through s012_operations_visibility")


@pytest.fixture(name="pg_context")
def pg_context_fixture() -> Iterator[_PGContext]:
    generator = _pg_context_fixture._fixture_function()
    context = next(generator)
    try:
        yield context
    finally:
        if context.task_ids:
            with Session(context.engine) as db, db.begin():
                db.execute(
                    delete(GenerationAttemptContext).where(
                        GenerationAttemptContext.task_id.in_(context.task_ids)
                    )
                )
                db.execute(
                    delete(GenerationCapacityReservation).where(
                        GenerationCapacityReservation.task_id.in_(context.task_ids)
                    )
                )
        try:
            next(generator)
        except StopIteration:
            pass


def test_diagnostics_reports_loop_evidence_and_redacted_operational_state(
    pg_context: _PGContext,
) -> None:
    _require_n2_schema(pg_context)
    dispatcher = OperationsHeartbeatWriter(pg_context.engine, "dispatcher")
    scheduler = OperationsHeartbeatWriter(pg_context.engine, "scheduler")
    dispatcher.iteration_started(
        {
            "event_handler_count": 2,
            "action_registration_count": 8,
            "prompt_runtime_registered": True,
            "generation_runtime_registered": True,
        }
    )

    class _DispatcherReport:
        claimed = 2
        published = 2
        failed = 0
        dead = 0
        quarantined = 0

    dispatcher.dispatcher_completed(_DispatcherReport())
    scheduler.iteration_started()

    class _SchedulerReport:
        scanned = 3
        worker_leases_recovered = 0
        published_events_redriven = 0
        dead = 0
        action_leases_marked_unknown = 0

    scheduler.scheduler_outbox_scan_completed(_SchedulerReport())

    class _CleanupReport:
        scanned = 2
        completed = 2
        retried = 0
        blocked = 0
        safe_error_code = None

    class _SchedulerCompletion:
        scheduled_runs = 1
        work_report = _CleanupReport()

    scheduler.scheduler_completed(_SchedulerCompletion())

    snapshot = OperationsDiagnosticsService(
        pg_context.engine,
        broker_url=None,
        queue_name="ops-test-queue",
        heartbeat_stale_seconds=90,
        long_state_seconds=1800,
        detail_limit=10,
    ).snapshot()

    assert snapshot.api_process == "responding"
    assert snapshot.database == "connected"
    assert snapshot.broker.state == "not_configured"
    assert snapshot.dispatcher.state == "fresh"
    assert snapshot.dispatcher.registrations is not None
    assert snapshot.dispatcher.registrations.generation_runtime_registered
    assert snapshot.dispatcher.last_progress["published"] == 2
    assert snapshot.scheduler.state == "fresh"
    assert snapshot.scheduler.last_outbox_scan_at is not None
    assert snapshot.scheduler.last_cleanup_scan_at is not None
    assert snapshot.scheduler.last_progress["cleanup_jobs_completed"] == 2
    assert snapshot.worker_progress.evidence_scope == "aggregate_database_evidence"
    rendered = snapshot.model_dump_json().lower()
    for forbidden in ("prompt_text", "provider_ref", "digest", "object_key", "secret", "url"):
        assert forbidden not in rendered


def test_expired_followup_races_idempotently_with_duplicate_callback_without_submit(
    pg_context: _PGContext,
) -> None:
    _require_n2_schema(pg_context)
    task_id = _create_task(pg_context)
    _process_task_prompt_intent(pg_context.engine, task_id)

    with Session(pg_context.engine) as db, db.begin():
        task = db.get(Task, task_id)
        assert task is not None
        workflow = db.execute(
            select(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_id)
        ).scalar_one()
        submission = db.execute(
            select(PromptSubmission)
            .where(PromptSubmission.workflow_id == workflow.id)
            .order_by(PromptSubmission.followup_round.desc())
            .limit(1)
        ).scalar_one()
        action = db.execute(
            select(Action).where(Action.submission_id == submission.submission_id)
        ).scalar_one()
        event = db.get(OutboxEvent, action.outbox_event_id)
        receipt = db.get(OutboxReceipt, action.outbox_event_id)
        assert event is not None and receipt is not None
        now = db.scalar(select(func.clock_timestamp()))
        valid_until = now - timedelta(seconds=1)
        result_digest = hashlib.sha256(b"already-accepted-needs-input-result").digest()

        task.status = "needs_input"
        workflow.status = "needs_input"
        submission.status = "callback_received"
        submission.callback_outcome = "needs_input"
        submission.callback_result_digest_version = 1
        submission.callback_result_digest = result_digest
        submission.callback_round = 1
        submission.callback_allowed_fields = ["platform"]
        submission.callback_locked_fields = ["language"]
        submission.callback_valid_until = valid_until
        submission.callback_received_at = valid_until - timedelta(seconds=1)
        submission.updated_at = now
        action.status = "completed"
        action.last_outcome = "accepted"
        action.completed_at = now
        action.updated_at = now
        event.status = "published"
        event.published_at = now
        receipt.status = "completed"
        receipt.completed_at = now

        callback = PromptCallback(
            task_id=task.id,
            workflow_run_id=workflow.workflow_run_id,
            workflow_revision=workflow.workflow_revision,
            submission_id=submission.submission_id,
            submission_content_digest_version=submission.content_digest_version,
            submission_content_digest=bytes(submission.content_digest),
            result_digest_version=1,
            result_digest=result_digest,
            outcome="needs_input",
            next_question_round=1,
            allowed_fields=("platform",),
            locked_fields=("language",),
            valid_until=valid_until,
        )
        action_id = action.id

    service = OperationsDiagnosticsService(
        pg_context.engine,
        broker_url=None,
        queue_name="ops-test-queue",
        heartbeat_stale_seconds=90,
        long_state_seconds=1800,
        detail_limit=10,
    )
    callback_service = PromptCallbackService(pg_context.engine)
    idempotency_key = f"ops-expire-{uuid4().hex}"
    barrier = Barrier(2)

    def _expire():
        barrier.wait(timeout=10)
        return service.expire_followup(task_id, idempotency_key=idempotency_key)

    def _duplicate_callback():
        barrier.wait(timeout=10)
        try:
            return callback_service.accept(callback, phase="initial_preferences")
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        expire_future = executor.submit(_expire)
        callback_future = executor.submit(_duplicate_callback)
        expired = expire_future.result(timeout=15)
        callback_result = callback_future.result(timeout=15)

    assert expired.outcome == "expired"
    assert not expired.replayed
    assert callback_result == "PROMPT_CALLBACK_STALE" or (
        callback_result.status in {"needs_input", "rejected"}
    )

    replay = service.expire_followup(task_id, idempotency_key=idempotency_key)
    assert replay.outcome == "expired"
    assert replay.replayed

    with Session(pg_context.engine) as db:
        task = db.get(Task, task_id)
        submission = db.execute(
            select(PromptSubmission).where(PromptSubmission.task_id == task_id)
        ).scalar_one()
        actions = db.execute(select(Action).where(Action.task_id == task_id)).scalars().all()
        audit_count = db.scalar(
            select(func.count())
            .select_from(OperationsRecoveryAudit)
            .where(
                OperationsRecoveryAudit.idempotency_key_hash
                == hashlib.sha256(idempotency_key.encode("ascii")).digest()
            )
        )
        submit_count = db.scalar(
            select(func.count())
            .select_from(Action)
            .where(
                Action.task_id == task_id,
                Action.action_kind == "generation.submit.v1",
            )
        )
        attempts = db.scalar(
            select(func.count())
            .select_from(TaskItemAttempt)
            .where(
                TaskItemAttempt.task_item_id.in_(
                    select(TaskItem.id).where(TaskItem.task_id == task_id)
                )
            )
        )
        task_attempt_ids = (
            select(TaskItemAttempt.id).join(TaskItem).where(TaskItem.task_id == task_id)
        )
        invocation_count = db.scalar(
            select(func.count())
            .select_from(MockGenerationInvocation)
            .where(MockGenerationInvocation.request_key.in_(task_attempt_ids))
        )
        assert task is not None and task.status == "failed"
        assert task.terminal_at is not None and task.expires_at is not None
        assert submission.callback_reason_code == "PROMPT_QUESTION_EXPIRED"
        assert len(actions) == 1 and actions[0].id == action_id
        assert audit_count == 1
        assert submit_count == 0
        assert attempts == 0
        assert invocation_count == 0

    with Session(pg_context.engine) as db:
        audit = db.execute(
            select(OperationsRecoveryAudit).where(
                OperationsRecoveryAudit.task_id == task_id,
                OperationsRecoveryAudit.idempotency_key_hash
                == hashlib.sha256(idempotency_key.encode("ascii")).digest(),
            )
        ).scalar_one()
        assert audit.action_id == expired.action_id
        assert audit.operation_kind == "expire_followup"
        assert audit.result_code == "FOLLOWUP_TERMINATED"
        heartbeat_count = db.scalar(select(func.count()).select_from(OperationsLoopHeartbeat))
        assert heartbeat_count >= 2


def test_same_attempt_save_retry_only_expedites_existing_action_and_replays_idempotently(
    pg_context: _PGContext,
) -> None:
    _require_n2_schema(pg_context)
    task_id, item_id, _, _ = _create_task_with_prompt(pg_context)
    _process_start_event(pg_context.engine, item_id)

    with Session(pg_context.engine) as db, db.begin():
        attempt = db.execute(
            select(TaskItemAttempt).where(TaskItemAttempt.task_item_id == item_id)
        ).scalar_one()
        task_item = db.get(TaskItem, item_id)
        task = db.get(Task, task_id)
        context = db.get(GenerationAttemptContext, attempt.id)
        first_action = db.execute(
            select(Action).where(
                Action.scope_kind == "attempt",
                Action.scope_id == attempt.id,
                Action.sequence == 1,
            )
        ).scalar_one()
        event = db.get(OutboxEvent, first_action.outbox_event_id)
        receipt = db.get(OutboxReceipt, first_action.outbox_event_id)
        assert task_item is not None and task is not None and context is not None
        assert event is not None and receipt is not None
        now = db.scalar(select(func.clock_timestamp()))

        first_action.status = "completed"
        first_action.last_outcome = "accepted"
        first_action.completed_at = now
        first_action.updated_at = now
        event.status = "published"
        event.published_at = now
        receipt.status = "completed"
        receipt.completed_at = now

        task.status = "generating"
        task_item.status = "saving"
        attempt.status = "saving"
        context.save_state = "original_stored"
        context.original_sha256 = "a" * 64
        context.original_size_bytes = 128
        context.original_mime_type = "image/png"
        context.original_width = 32
        context.original_height = 32
        attempt.next_action_at = now + timedelta(hours=1)
        save_action = _schedule_generation_action(
            db,
            task_id=task_id,
            task_item_id=item_id,
            attempt_id=attempt.id,
            sequence=2,
            action_kind="generation.save.v1",
            due_at=now + timedelta(hours=1),
            request_digest=bytes(context.request_digest),
            original_sha256=context.original_sha256,
        )
        action_id = save_action.id
        attempt_id = attempt.id
        original_due_at = save_action.due_at

    service = OperationsDiagnosticsService(
        pg_context.engine,
        broker_url=None,
        queue_name="ops-test-queue",
        heartbeat_stale_seconds=90,
        long_state_seconds=1800,
        detail_limit=10,
    )
    idempotency_key = f"ops-save-{uuid4().hex}"
    expedited = service.expedite_same_attempt_save(action_id, idempotency_key=idempotency_key)
    replay = service.expedite_same_attempt_save(action_id, idempotency_key=idempotency_key)

    assert expedited.outcome == "expedited" and not expedited.replayed
    assert replay.outcome == "expedited" and replay.replayed
    with Session(pg_context.engine) as db:
        action = db.get(Action, action_id)
        attempt_count = db.scalar(
            select(func.count())
            .select_from(TaskItemAttempt)
            .where(TaskItemAttempt.task_item_id == item_id)
        )
        save_action_count = db.scalar(
            select(func.count())
            .select_from(Action)
            .where(
                Action.scope_kind == "attempt",
                Action.scope_id == attempt_id,
                Action.action_kind == "generation.save.v1",
            )
        )
        submit_action_count = db.scalar(
            select(func.count())
            .select_from(Action)
            .where(
                Action.scope_kind == "attempt",
                Action.scope_id == attempt_id,
                Action.action_kind == "generation.submit.v1",
            )
        )
        invocation_count = db.scalar(
            select(func.count())
            .select_from(MockGenerationInvocation)
            .where(MockGenerationInvocation.request_key == attempt_id)
        )
        audit_count = db.scalar(
            select(func.count())
            .select_from(OperationsRecoveryAudit)
            .where(
                OperationsRecoveryAudit.idempotency_key_hash
                == hashlib.sha256(idempotency_key.encode("ascii")).digest()
            )
        )
        assert action is not None
        assert action.id == action_id and action.sequence == 2
        assert action.action_kind == "generation.save.v1" and action.status == "scheduled"
        assert action.due_at < original_due_at
        assert attempt_count == 1
        assert save_action_count == 1
        assert submit_action_count == 1
        assert invocation_count == 0
        assert audit_count == 1
