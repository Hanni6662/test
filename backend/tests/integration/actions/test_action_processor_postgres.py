from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from app.db.action_models import Action
from app.db.models import User
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.prompt_models import PromptSubmission, TaskPromptWorkflow
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ReferenceSet
from app.db.task_models import Task, TaskItem, TaskItemAttempt
from app.dispatcher import OutboxDispatcher
from app.workers.action_processor import (
    ACTION_EVENT_TYPE,
    ActionExecutionResult,
    ActionInput,
    ActionProcessor,
    ActionRegistration,
    _lock_parent_chain,
    schedule_action_in_transaction,
)
from app.workers.messages import OutboxMessage
from app.workers.options import QueueOptions
from app.workers.processor import WorkerStateUnavailable
from sqlalchemy import create_engine, delete, event, func, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _TaskTree:
    owner_id: UUID
    reference_set_id: UUID
    recommendation_id: UUID
    task_id: UUID
    item_id: UUID
    attempt_id: UUID


def _engine_or_skip() -> Engine:
    runtime_url = os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL")
    if runtime_url is None:
        pytest.skip("requires the dedicated image-backend-test PostgreSQL runtime URL")
    engine = create_engine(runtime_url, pool_pre_ping=True)
    required = {
        "users",
        "reference_sets",
        "recommendations",
        "tasks",
        "task_items",
        "task_item_attempts",
        "outbox_events",
        "outbox_receipts",
        "task_actions",
        "task_prompt_workflows",
        "task_prompt_submissions",
        "generation_attempt_contexts",
    }
    if not required.issubset(set(inspect(engine).get_table_names())):
        engine.dispose()
        pytest.skip("requires the integrated PostgreSQL schema through s009_prompt_workflows")
    return engine


def _insert_task_tree(engine: Engine) -> _TaskTree:
    tree = _TaskTree(uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), uuid4())
    with Session(engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        db.add(User(id=tree.owner_id, identity_kind="guest", status="active"))
        db.add(
            ReferenceSet(
                id=tree.reference_set_id,
                owner_user_id=tree.owner_id,
                product_category="mock integration",
                status="ready",
                ready_at=now,
                expires_at=now + timedelta(days=2),
                created_at=now,
                last_activity_at=now,
            )
        )
        db.flush()
        db.add(
            Recommendation(
                id=tree.recommendation_id,
                owner_user_id=tree.owner_id,
                reference_set_id=tree.reference_set_id,
                status="ready",
                revision=1,
                original_recommendation={"synthetic": True},
                public_options={"language": ["en"]},
                image_types=["MAIN"],
                audit={"image_analysis_performed": False},
                state={"source": "test"},
                run_ref=f"mock:{tree.recommendation_id}",
                form_ref=f"mock-form:{tree.recommendation_id}",
                valid_until=now + timedelta(hours=2),
                data_expires_at=now + timedelta(days=2),
                ready_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        db.flush()
        db.add(
            Task(
                id=tree.task_id,
                owner_user_id=tree.owner_id,
                reference_set_id=tree.reference_set_id,
                recommendation_id=tree.recommendation_id,
                recommendation_revision=1,
                recommendation_snapshot_version="test-analysis-v1",
                workflow_version="test-workflow-v1",
                request_digest=hashlib.sha256(b"action-test-request").digest(),
                request_snapshot={"synthetic": True},
                resolved_preferences={"language": "en"},
                recommendation_snapshot={"synthetic": True},
                reference_assets_snapshot=[],
                category_snapshot="mock integration",
                image_types=["MAIN"],
                planned_count=1,
                billing_snapshot={"mode": "disabled"},
                status="generating",
            )
        )
        db.flush()
        db.add(
            TaskItem(
                id=tree.item_id,
                task_id=tree.task_id,
                position=1,
                image_type="MAIN",
                status="running",
            )
        )
        db.add(
            TaskItemAttempt(
                id=tree.attempt_id,
                task_item_id=tree.item_id,
                attempt_no=1,
                idempotency_key=uuid4(),
                status="submitting",
                started_at=now,
            )
        )
    return tree


def _cleanup_task_tree(engine: Engine, tree: _TaskTree) -> None:
    with Session(engine) as db, db.begin():
        event_ids = list(
            db.scalars(select(Action.outbox_event_id).where(Action.task_id == tree.task_id))
        )
        db.execute(delete(Action).where(Action.task_id == tree.task_id))
        if event_ids:
            db.execute(delete(OutboxReceipt).where(OutboxReceipt.event_id.in_(event_ids)))
            db.execute(delete(OutboxEvent).where(OutboxEvent.id.in_(event_ids)))
        db.execute(delete(TaskItemAttempt).where(TaskItemAttempt.id == tree.attempt_id))
        db.execute(delete(TaskItem).where(TaskItem.id == tree.item_id))
        db.execute(delete(PromptSubmission).where(PromptSubmission.task_id == tree.task_id))
        db.execute(delete(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == tree.task_id))
        db.execute(delete(Task).where(Task.id == tree.task_id))
        db.execute(delete(Recommendation).where(Recommendation.id == tree.recommendation_id))
        db.execute(delete(ReferenceSet).where(ReferenceSet.id == tree.reference_set_id))
        db.execute(delete(User).where(User.id == tree.owner_id))


def _schedule_attempt_action(
    engine: Engine,
    tree: _TaskTree,
    *,
    action_kind: str = "test.persisted",
    sequence: int = 1,
) -> UUID:
    action_id = uuid4()
    with Session(engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        schedule_action_in_transaction(
            db,
            action_id=action_id,
            task_id=tree.task_id,
            task_item_id=tree.item_id,
            attempt_id=tree.attempt_id,
            scope_kind="attempt",
            scope_id=tree.attempt_id,
            action_kind=action_kind,
            sequence=sequence,
            due_at=now - timedelta(seconds=1),
            input_digest_version=1,
            input_digest=hashlib.sha256(b"action-input").digest(),
        )
    return action_id


def _message_for_action(engine: Engine, action_id: UUID) -> dict[str, object]:
    with Session(engine) as db:
        action = db.get(Action, action_id)
        assert action is not None
        event = db.get(OutboxEvent, action.outbox_event_id)
        assert event is not None
        return OutboxMessage(
            event_id=event.id,
            aggregate_id=event.aggregate_id,
            operation_version=event.operation_version,
            schema_version=event.schema_version,
            payload=event.payload,
        ).model_dump(mode="json")


def _registration(
    *,
    execute,
    recover=None,
    final_status: str = "submitted",
    action_kind: str = "test.persisted",
) -> ActionRegistration:
    def prepare(_db: Session, action: Action) -> ActionInput:
        return ActionInput(
            action_id=action.id,
            task_id=action.task_id,
            task_item_id=action.task_item_id,
            attempt_id=action.attempt_id,
            scope_kind=action.scope_kind,
            scope_id=action.scope_id,
            action_kind=action.action_kind,
            sequence=action.sequence,
            input_digest_version=action.input_digest_version,
            input_digest=action.input_digest,
            payload={"synthetic": {"facts": ["no provider call"]}},
        )

    def finalize(
        db: Session,
        action: Action,
        _action_input: ActionInput,
        result: ActionExecutionResult,
    ) -> None:
        attempt = db.get(TaskItemAttempt, action.attempt_id)
        assert attempt is not None
        if result.outcome == "accepted":
            attempt.status = final_status
            attempt.provider_job_id = "mock-job-action-integration"

    return ActionRegistration(action_kind, prepare, execute, finalize, recover=recover)


def _options(*, max_attempts: int = 3) -> QueueOptions:
    return QueueOptions(
        broker_url="amqp://integration:integration@localhost:5672/image-backend-test",
        queue_name="action-integration",
        max_attempts=max_attempts,
    )


def test_action_claim_io_finalize_and_replay_are_durable() -> None:
    engine = _engine_or_skip()
    tree = _insert_task_tree(engine)
    try:
        action_id = _schedule_attempt_action(engine, tree)
        calls: list[ActionInput] = []

        def execute(action_input: ActionInput) -> ActionExecutionResult:
            calls.append(action_input)
            assert action_input.payload["synthetic"]["facts"] == ("no provider call",)
            with pytest.raises(TypeError):
                action_input.payload["synthetic"]["new"] = "blocked"
            return ActionExecutionResult("accepted", value={"mock_job": "private"})

        processor = ActionProcessor(
            engine,
            _options(),
            {"test.persisted": _registration(execute=execute)},
        )
        message = _message_for_action(engine, action_id)
        first = processor.process_payload(message)
        replay = processor.process_payload(message)

        assert first.status == "completed"
        assert replay.status == "completed"
        assert len(calls) == 1
        with Session(engine) as db:
            action = db.get(Action, action_id)
            receipt = db.get(OutboxReceipt, action.outbox_event_id)
            attempt = db.get(TaskItemAttempt, tree.attempt_id)
            assert action.status == "completed"
            assert action.last_outcome == "accepted"
            assert receipt.status == "completed"
            assert attempt.status == "submitted"
            assert attempt.provider_job_id == "mock-job-action-integration"
    finally:
        _cleanup_task_tree(engine, tree)
        engine.dispose()


def test_safe_retry_flushes_a_valid_terminal_action_and_schedules_next_step() -> None:
    engine = _engine_or_skip()
    tree = _insert_task_tree(engine)
    action_id = _schedule_attempt_action(engine, tree, action_kind="test.safe-retry")
    scheduled_ids: list[UUID] = []
    try:
        base_registration = _registration(
            execute=lambda _action_input: ActionExecutionResult(
                "safe_retry", safe_reason_code="POLL_NOT_STARTED", retry_after_seconds=5
            ),
            action_kind="test.safe-retry",
        )

        def finalize(
            db: Session,
            action: Action,
            action_input: ActionInput,
            result: ActionExecutionResult,
        ) -> None:
            base_registration.finalize(db, action, action_input, result)
            due_at = db.scalar(select(func.clock_timestamp())) + timedelta(
                seconds=(result.retry_after_seconds or 0) + 1
            )
            scheduled = schedule_action_in_transaction(
                db,
                action_id=uuid4(),
                task_id=action.task_id,
                task_item_id=action.task_item_id,
                attempt_id=action.attempt_id,
                scope_kind="attempt",
                scope_id=action.attempt_id,
                action_kind=action.action_kind,
                sequence=action.sequence + 1,
                due_at=due_at,
                input_digest_version=1,
                input_digest=hashlib.sha256(b"safe-retry-next-step").digest(),
            )
            scheduled_ids.append(scheduled.id)

        registration = replace(base_registration, finalize=finalize)
        result = ActionProcessor(
            engine, _options(), {registration.kind: registration}
        ).process_payload(_message_for_action(engine, action_id))

        assert result.status == "completed"
        assert len(scheduled_ids) == 1
        with Session(engine) as db:
            action = db.get(Action, action_id)
            receipt = db.get(OutboxReceipt, action.outbox_event_id)
            next_action = db.get(Action, scheduled_ids[0])
            next_receipt = db.get(OutboxReceipt, next_action.outbox_event_id)
            assert action.status == "completed"
            assert action.last_outcome == "safe_retry"
            assert action.claim_token is None and action.lease_until is None
            assert receipt.status == "completed"
            assert receipt.claim_token is None and receipt.lease_until is None
            assert next_action.sequence == action.sequence + 1
            assert next_action.status == "scheduled"
            assert next_receipt.status == "pending"
    finally:
        _cleanup_task_tree(engine, tree)
        engine.dispose()


def test_action_finalize_failure_rolls_back_domain_action_and_receipt_writes() -> None:
    engine = _engine_or_skip()
    tree = _insert_task_tree(engine)
    action_id = _schedule_attempt_action(engine, tree, action_kind="test.finalize-failure")
    try:
        base_registration = _registration(
            execute=lambda _action_input: ActionExecutionResult(
                "safe_retry", safe_reason_code="POLL_NOT_STARTED", retry_after_seconds=5
            ),
            action_kind="test.finalize-failure",
        )

        def finalize(
            db: Session,
            action: Action,
            action_input: ActionInput,
            result: ActionExecutionResult,
        ) -> None:
            attempt = db.get(TaskItemAttempt, action.attempt_id)
            assert attempt is not None
            attempt.status = "polling"
            due_at = db.scalar(select(func.clock_timestamp())) + timedelta(seconds=10)
            schedule_action_in_transaction(
                db,
                action_id=uuid4(),
                task_id=action.task_id,
                task_item_id=action.task_item_id,
                attempt_id=action.attempt_id,
                scope_kind="attempt",
                scope_id=action.attempt_id,
                action_kind=action.action_kind,
                sequence=action.sequence + 1,
                due_at=due_at,
                input_digest_version=1,
                input_digest=hashlib.sha256(b"rolled-back-next-step").digest(),
            )
            raise RuntimeError("synthetic domain finalize failure")

        registration = replace(base_registration, finalize=finalize)
        processor = ActionProcessor(engine, _options(), {registration.kind: registration})
        with pytest.raises(WorkerStateUnavailable):
            processor.process_payload(_message_for_action(engine, action_id))

        with Session(engine) as db:
            action = db.get(Action, action_id)
            receipt = db.get(OutboxReceipt, action.outbox_event_id)
            attempt = db.get(TaskItemAttempt, tree.attempt_id)
            assert action.status == "claimed"
            assert action.claim_token is not None and action.lease_until is not None
            assert receipt.status == "processing"
            assert receipt.claim_token is not None and receipt.lease_until is not None
            assert attempt.status == "submitting"
            assert (
                db.scalar(
                    select(func.count(Action.id)).where(
                        Action.scope_kind == "attempt", Action.scope_id == tree.attempt_id
                    )
                )
                == 1
            )
    finally:
        _cleanup_task_tree(engine, tree)
        engine.dispose()


def test_unknown_action_reconciles_without_blind_replay() -> None:
    engine = _engine_or_skip()
    tree = _insert_task_tree(engine)
    try:
        action_id = _schedule_attempt_action(engine, tree)
        io_calls: list[UUID] = []

        def execute(action_input: ActionInput) -> ActionExecutionResult:
            io_calls.append(action_input.action_id)
            return ActionExecutionResult(
                "unknown", safe_reason_code="MOCK_TRANSPORT_OUTCOME_UNKNOWN"
            )

        processor = ActionProcessor(
            engine,
            _options(),
            {"test.persisted": _registration(execute=execute)},
        )
        message = _message_for_action(engine, action_id)
        first = processor.process_payload(message)
        replay = processor.process_payload(message)

        assert first.status == "unknown"
        assert replay.status == "completed"
        assert io_calls == [action_id]
        with Session(engine) as db:
            action = db.get(Action, action_id)
            task = db.get(Task, tree.task_id)
            item = db.get(TaskItem, tree.item_id)
            attempt = db.get(TaskItemAttempt, tree.attempt_id)
            assert action.status == "unknown"
            assert action.last_outcome == "unknown"
            assert task.status == "reconciling"
            assert item.status == "reconciling"
            assert attempt.status == "reconciling"
    finally:
        _cleanup_task_tree(engine, tree)
        engine.dispose()


def test_expired_action_lease_calls_only_explicit_recovery() -> None:
    engine = _engine_or_skip()
    tree = _insert_task_tree(engine)
    try:
        action_id = _schedule_attempt_action(engine, tree)
        with Session(engine) as db, db.begin():
            action = db.get(Action, action_id)
            receipt = db.get(OutboxReceipt, action.outbox_event_id)
            event = db.get(OutboxEvent, action.outbox_event_id)
            expired = db.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
            action.status = "claimed"
            action.claim_token = uuid4()
            action.lease_until = expired
            action.claim_count = 1
            receipt.status = "processing"
            receipt.claim_token = uuid4()
            receipt.lease_until = expired
            receipt.attempt_count = 1
            event.status = "published"

        execute_calls: list[UUID] = []
        recover_calls: list[UUID] = []

        def execute(action_input: ActionInput) -> ActionExecutionResult:
            execute_calls.append(action_input.action_id)
            return ActionExecutionResult("accepted")

        def recover(action_input: ActionInput) -> ActionExecutionResult:
            recover_calls.append(action_input.action_id)
            return ActionExecutionResult("accepted")

        processor = ActionProcessor(
            engine,
            _options(),
            {"test.persisted": _registration(execute=execute, recover=recover)},
        )
        result = processor.process_payload(_message_for_action(engine, action_id))

        assert result.status == "completed"
        assert execute_calls == []
        assert recover_calls == [action_id]
    finally:
        _cleanup_task_tree(engine, tree)
        engine.dispose()


def test_action_event_publish_gate_requires_the_specific_registered_kind() -> None:
    engine = _engine_or_skip()
    tree = _insert_task_tree(engine)
    registered_kind = f"integration.registered.{uuid4().hex[:10]}"
    unknown_kind = f"integration.unknown.{uuid4().hex[:10]}"
    try:
        registered_id = _schedule_attempt_action(
            engine, tree, action_kind=registered_kind, sequence=1
        )
        unknown_id = _schedule_attempt_action(engine, tree, action_kind=unknown_kind, sequence=2)
        submission_action_id = uuid4()
        submission_id = uuid4()
        with Session(engine) as db, db.begin():
            now = db.scalar(select(func.clock_timestamp()))
            workflow_run_id = uuid4()
            workflow_id = uuid4()
            db.add(
                TaskPromptWorkflow(
                    id=workflow_id,
                    task_id=tree.task_id,
                    workflow_run_id=workflow_run_id,
                    workflow_revision=1,
                    trusted_analysis_snapshot_id=uuid4(),
                    trusted_analysis_snapshot_digest=hashlib.sha256(b"snapshot").digest(),
                    status="pending_submission",
                )
            )
            db.flush()
            db.add(
                PromptSubmission(
                    submission_id=submission_id,
                    workflow_id=workflow_id,
                    task_id=tree.task_id,
                    workflow_run_id=workflow_run_id,
                    workflow_revision=1,
                    phase="initial_preferences",
                    followup_round=0,
                    content_digest_version=1,
                    content_digest=hashlib.sha256(b"submission").digest(),
                    private_payload={"synthetic": True},
                    status="prepared",
                )
            )
            db.flush()
            schedule_action_in_transaction(
                db,
                action_id=submission_action_id,
                task_id=tree.task_id,
                scope_kind="submission",
                scope_id=submission_id,
                action_kind=registered_kind,
                sequence=1,
                due_at=now - timedelta(seconds=1),
                input_digest_version=1,
                input_digest=hashlib.sha256(b"submission-action-input").digest(),
            )
        with Session(engine) as db:
            registered = db.get(Action, registered_id)
            unknown = db.get(Action, unknown_id)
            submission_action = db.get(Action, submission_action_id)
            registered_event_id = registered.outbox_event_id
            unknown_event_id = unknown.outbox_event_id
            submission_event_id = submission_action.outbox_event_id

        dispatcher = OutboxDispatcher(
            engine,
            _options(),
            action_registrations={
                registered_kind: _registration(
                    action_kind=registered_kind,
                    execute=lambda _input: ActionExecutionResult("accepted"),
                )
            },
        )
        with Session(engine) as db:
            publishable_ids = set(
                db.scalars(
                    select(OutboxEvent.id).where(
                        OutboxEvent.id.in_(
                            [registered_event_id, unknown_event_id, submission_event_id]
                        ),
                        dispatcher._publishable_event_predicate(),
                    )
                )
            )

        assert publishable_ids == {registered_event_id}
    finally:
        _cleanup_task_tree(engine, tree)
        engine.dispose()


def test_attempt_parent_lock_covers_all_items_before_context_and_action() -> None:
    engine = _engine_or_skip()
    tree = _insert_task_tree(engine)
    action_id = _schedule_attempt_action(engine, tree)
    statements: list[str] = []

    def capture_sql(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    try:
        with Session(engine) as db, db.begin():
            db.add(
                TaskItem(
                    task_id=tree.task_id,
                    position=2,
                    image_type="SECONDARY",
                    status="queued",
                )
            )
            db.flush()
        event.listen(engine, "before_cursor_execute", capture_sql)
        with Session(engine) as db, db.begin():
            hint = db.get(Action, action_id)
            assert hint is not None
            parents = _lock_parent_chain(db, hint)
            assert parents is not None
            assert [item.position for item in parents.task_items] == [1, 2]
            assert parents.task_item is not None and parents.task_item.id == tree.item_id
            assert parents.attempt is not None and parents.generation_context is None

        locked_tables = []
        for statement in statements:
            if re.search(r"\bfor\s+update\b", statement, re.IGNORECASE) is None:
                continue
            from_match = re.search(
                r'\bfrom\s+(?:"?[\w$]+"?\.)?"?([\w$]+)"?',
                statement,
                re.IGNORECASE,
            )
            if from_match is not None:
                locked_tables.append(from_match.group(1).lower())
        expected_lock_order = [
            "users",
            "reference_sets",
            "recommendations",
            "tasks",
            "task_items",
            "task_item_attempts",
            "generation_attempt_contexts",
            "task_actions",
        ]
        assert locked_tables == expected_lock_order, f"unexpected lock order: {locked_tables}"
        task_item_lock = next(
            statement
            for statement in statements
            if re.search(r'\bfrom\s+(?:"task_items"|task_items\b)', statement, re.IGNORECASE)
            and re.search(r"\bfor\s+update\b", statement, re.IGNORECASE)
        )
        assert (
            re.search(
                r'\border\s+by\s+"?task_items"?\."?position"?\s*,\s*"?task_items"?\."?id"?',
                task_item_lock,
                re.IGNORECASE,
            )
            is not None
        )
    finally:
        if event.contains(engine, "before_cursor_execute", capture_sql):
            event.remove(engine, "before_cursor_execute", capture_sql)
        _cleanup_task_tree(engine, tree)
        engine.dispose()


def test_generation_action_without_locked_context_fails_before_io() -> None:
    engine = _engine_or_skip()
    tree = _insert_task_tree(engine)
    action_id = _schedule_attempt_action(
        engine, tree, action_kind="generation.submit.v1", sequence=1
    )
    calls: list[UUID] = []

    def execute(action_input: ActionInput) -> ActionExecutionResult:
        calls.append(action_input.action_id)
        return ActionExecutionResult("accepted")

    try:
        result = ActionProcessor(
            engine,
            _options(),
            {
                "generation.submit.v1": _registration(
                    execute=execute, action_kind="generation.submit.v1"
                )
            },
        ).process_payload(_message_for_action(engine, action_id))
        assert result.status == "dead"
        assert result.error_code == "ACTION_PARENT_NOT_FOUND"
        assert calls == []
        with Session(engine) as db:
            action = db.get(Action, action_id)
            assert action is not None
            receipt = db.get(OutboxReceipt, action.outbox_event_id)
            assert receipt is not None and receipt.status == "dead"
    finally:
        _cleanup_task_tree(engine, tree)
        engine.dispose()


def test_action_event_type_is_the_internal_action_wakeup() -> None:
    assert ACTION_EVENT_TYPE == "task.action.v1"
