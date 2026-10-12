from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from app.db.action_models import Action
from app.db.generation_models import (
    GenerationAttemptContext,
    GenerationCapacityControl,
    GenerationCapacityReservation,
    GenerationResult,
    MockGenerationInvocation,
    MockGenerationJob,
)
from app.db.models import User
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.prompt_models import (
    PromptSubmission,
    TaskPromptItem,
    TaskPromptPackage,
    TaskPromptWorkflow,
)
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ImageAsset, ReferenceSet, ReferenceSetImage
from app.db.task_models import (
    Task,
    TaskIdempotencyRecord,
    TaskItem,
    TaskItemAttempt,
    TaskRecommendationConsumption,
)
from app.integrations.dify.mock import build_mock_options, build_mock_original
from app.integrations.generation.mock import MockGenerationPort, _TestOnlyFaults
from app.integrations.generation.protocol import GenerationUnknown
from app.integrations.storage.local import LocalPrivateStorage
from app.schemas.recommendations import IMAGE_TYPES
from app.schemas.tasks import TaskCreateRequest
from app.services.generation_runtime import (
    GENERATION_START_EVENT_TYPE,
    GenerationActionRuntime,
    _aggregate_task,
    handle_generation_start_event,
    start_generation_item,
)
from app.services.prompt_validation import PROMPT_PACKAGE_VALIDATOR_VERSION
from app.services.tasks import TaskOptions, TaskService
from app.services.trusted_analysis import (
    TRUSTED_ANALYSIS_STATE_KEY,
    build_trusted_analysis_snapshot,
)
from app.workers.action_processor import ActionProcessor
from app.workers.messages import OutboxMessage
from app.workers.options import QueueOptions
from app.workers.processor import EventProcessor, HandlerRegistration, WorkerStateUnavailable
from sqlalchemy import Engine, create_engine, delete, func, inspect, or_, select
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

TEST_DATABASE_URL = os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="dedicated test PostgreSQL is not configured",
    ),
]


@dataclass
class _PGContext:
    engine: Engine
    owner_ids: set[UUID] = field(default_factory=set)
    reference_set_ids: set[UUID] = field(default_factory=set)
    recommendation_ids: set[UUID] = field(default_factory=set)
    task_ids: set[UUID] = field(default_factory=set)


@pytest.fixture
def pg_context() -> Iterator[_PGContext]:
    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    required = {
        "users",
        "image_assets",
        "reference_sets",
        "reference_set_images",
        "recommendations",
        "tasks",
        "task_items",
        "task_item_attempts",
        "task_actions",
        "task_prompt_workflows",
        "task_prompt_submissions",
        "task_prompt_packages",
        "task_prompt_items",
        "outbox_events",
        "outbox_receipts",
        "generation_capacity_control",
        "generation_capacity_reservations",
        "generation_attempt_contexts",
        "generation_results",
        "mock_generation_invocations",
        "mock_generation_jobs",
    }
    if not required.issubset(set(inspect(engine).get_table_names())):
        engine.dispose()
        pytest.skip("requires A's integrated schema and R5 generation migration")
    _cleanup_previous_r5_fixtures(engine)
    with Session(engine) as db:
        if db.get(GenerationCapacityControl, 1) is None:
            engine.dispose()
            pytest.skip("requires the seeded generation capacity singleton")
    context = _PGContext(engine)
    try:
        yield context
    finally:
        _cleanup(context)
        engine.dispose()


def _cleanup(context: _PGContext) -> None:
    if not any(
        (context.owner_ids, context.reference_set_ids, context.recommendation_ids, context.task_ids)
    ):
        return
    task_ids = tuple(context.task_ids)
    item_ids = select(TaskItem.id).where(TaskItem.task_id.in_(task_ids))
    action_ids = select(Action.id).where(Action.task_id.in_(task_ids))
    attempt_ids = select(TaskItemAttempt.id).where(
        TaskItemAttempt.task_item_id.in_(item_ids)
    )
    with Session(context.engine) as db, db.begin():
        event_ids = select(OutboxEvent.id).where(
            or_(
                (OutboxEvent.event_type == "task.prompt.v1")
                & OutboxEvent.aggregate_id.in_(task_ids),
                (OutboxEvent.event_type == "task.action.v1")
                & OutboxEvent.aggregate_id.in_(action_ids),
                (OutboxEvent.event_type == GENERATION_START_EVENT_TYPE)
                & OutboxEvent.aggregate_id.in_(item_ids),
            )
        )
        db.execute(delete(OutboxReceipt).where(OutboxReceipt.event_id.in_(event_ids)))
        db.execute(delete(Action).where(Action.task_id.in_(task_ids)))
        db.execute(delete(OutboxEvent).where(OutboxEvent.id.in_(event_ids)))
        db.execute(delete(GenerationResult).where(GenerationResult.task_id.in_(task_ids)))
        db.execute(
            delete(GenerationAttemptContext).where(
                GenerationAttemptContext.task_id.in_(task_ids)
            )
        )
        db.execute(
            delete(GenerationCapacityReservation).where(
                GenerationCapacityReservation.task_id.in_(task_ids)
            )
        )
        db.execute(
            delete(MockGenerationInvocation).where(
                MockGenerationInvocation.request_key.in_(attempt_ids)
            )
        )
        db.execute(
            delete(MockGenerationJob).where(MockGenerationJob.request_key.in_(attempt_ids))
        )
        db.execute(delete(TaskPromptItem).where(TaskPromptItem.task_id.in_(task_ids)))
        db.execute(delete(TaskPromptPackage).where(TaskPromptPackage.task_id.in_(task_ids)))
        db.execute(delete(PromptSubmission).where(PromptSubmission.task_id.in_(task_ids)))
        db.execute(delete(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id.in_(task_ids)))
        db.execute(delete(TaskItemAttempt).where(TaskItemAttempt.task_item_id.in_(item_ids)))
        db.execute(delete(TaskItem).where(TaskItem.task_id.in_(task_ids)))
        db.execute(
            delete(TaskRecommendationConsumption).where(
                TaskRecommendationConsumption.task_id.in_(task_ids)
            )
        )
        db.execute(delete(TaskIdempotencyRecord).where(TaskIdempotencyRecord.task_id.in_(task_ids)))
        db.execute(delete(Task).where(Task.id.in_(task_ids)))
        db.execute(
            delete(Recommendation).where(Recommendation.id.in_(context.recommendation_ids))
        )
        db.execute(
            delete(ReferenceSetImage).where(
                ReferenceSetImage.reference_set_id.in_(context.reference_set_ids)
            )
        )
        db.execute(
            delete(ReferenceSet).where(ReferenceSet.id.in_(context.reference_set_ids))
        )
        db.execute(delete(ImageAsset).where(ImageAsset.owner_user_id.in_(context.owner_ids)))
        db.execute(delete(User).where(User.id.in_(context.owner_ids)))


def _cleanup_previous_r5_fixtures(engine: Engine) -> None:
    """Recover only this module's synthetic rows left by an interrupted prior run."""

    with Session(engine) as db:
        recommendations = db.execute(
            select(Recommendation).where(Recommendation.run_ref.like("r5:%"))
        ).scalars().all()
        recommendation_ids = {row.id for row in recommendations}
        reference_set_ids = {row.reference_set_id for row in recommendations}
        owner_ids = {row.owner_user_id for row in recommendations}
        task_ids = set(
            db.scalars(
                select(Task.id).where(Task.recommendation_id.in_(recommendation_ids))
            ).all()
        )
    _cleanup(
        _PGContext(
            engine=engine,
            owner_ids=owner_ids,
            reference_set_ids=reference_set_ids,
            recommendation_ids=recommendation_ids,
            task_ids=task_ids,
        )
    )


def _create_task_with_prompt(
    context: _PGContext, *, image_count: int = 1
) -> tuple[UUID, UUID, UUID, tuple[UUID, UUID]]:
    owner_id = uuid4()
    reference_set_id = uuid4()
    recommendation_id = uuid4()
    with Session(context.engine) as db:
        now = db.scalar(select(func.clock_timestamp()))
    category = "R5 synthetic fixture"
    reference_rows: list[ReferenceSetImage] = []
    reference_assets: list[ImageAsset] = []
    analysis_references: list[dict[str, object]] = []
    primary_model_id: UUID | None = None
    excluded_model_id: UUID | None = None
    for position, slot in enumerate(("PRIMARY", "AUX01")):
        relation_id, source_id, model_id = uuid4(), uuid4(), uuid4()
        if slot == "PRIMARY":
            primary_model_id = model_id
        else:
            excluded_model_id = model_id
        digest = hashlib.sha256(relation_id.bytes).hexdigest()
        reference_assets.extend(
            [
                ImageAsset(
                    id=source_id,
                    owner_user_id=owner_id,
                    asset_kind="reference_source",
                    object_key=f"r5-test/{source_id}/source.png",
                    content_sha256=digest,
                    mime_type="image/png",
                    size_bytes=512,
                    width=64,
                    height=64,
                ),
                ImageAsset(
                    id=model_id,
                    owner_user_id=owner_id,
                    asset_kind="reference_model_input",
                    object_key=f"r5-test/{model_id}/model.png",
                    content_sha256=digest,
                    mime_type="image/png",
                    size_bytes=256,
                    width=32,
                    height=32,
                ),
            ]
        )
        reference_rows.append(
            ReferenceSetImage(
                id=relation_id,
                reference_set_id=reference_set_id,
                owner_user_id=owner_id,
                slot=slot,
                position=position,
                source_asset_id=source_id,
                model_asset_id=model_id,
                content_sha256=digest,
                created_at=now,
            )
        )
        analysis_references.append(
            {
                "alias": slot,
                "position": position,
                "reference_set_image_id": relation_id,
                "model_asset_id": model_id,
                "source_asset_id": source_id,
                "model_sha256": digest,
                "source_sha256": digest,
                "model_mime_type": "image/png",
                "source_mime_type": "image/png",
                "model_size_bytes": 256,
                "source_size_bytes": 512,
                "model_width": 32,
                "model_height": 32,
                "source_width": 64,
                "source_height": 64,
            }
        )
    assert primary_model_id is not None and excluded_model_id is not None
    analysis = build_trusted_analysis_snapshot(
        reference_set_id=reference_set_id,
        recommendation_id=recommendation_id,
        recommendation_revision=1,
        category=category,
        references=analysis_references,
        created_at=now,
    )
    mock_options = build_mock_options()
    recommendation_original = build_mock_original(category, mock_options)
    with Session(context.engine) as db, db.begin():
        db.add(User(id=owner_id, identity_kind="guest", status="active"))
        db.add(
            ReferenceSet(
                id=reference_set_id,
                owner_user_id=owner_id,
                product_category=category,
                status="ready",
                ready_at=now,
                expires_at=now + timedelta(days=10),
                created_at=now,
                last_activity_at=now,
            )
        )
        db.flush()
        db.add_all(reference_assets)
        db.flush()
        db.add_all(reference_rows)
        db.add(
            Recommendation(
                id=recommendation_id,
                owner_user_id=owner_id,
                reference_set_id=reference_set_id,
                status="ready",
                revision=1,
                original_recommendation=recommendation_original,
                public_options=mock_options,
                image_types=list(IMAGE_TYPES),
                audit={"image_analysis_performed": False},
                state={TRUSTED_ANALYSIS_STATE_KEY: analysis.model_dump(mode="json")},
                run_ref=f"r5:{recommendation_id}",
                form_ref=f"r5-form:{recommendation_id}",
                valid_until=now + timedelta(days=2),
                data_expires_at=now + timedelta(days=60),
                ready_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    request = TaskCreateRequest.model_validate(
        {
            "schema_version": 2,
            "reference_set_id": str(reference_set_id),
            "recommendation_id": str(recommendation_id),
            "recommendation_revision": 1,
            "preferences": {
                "language": {"mode": "A"},
                "platform": "TEMU",
                "usage_scene": {"mode": "B"},
                "visual_style": {"mode": "D"},
                "selling_points": {"mode": "custom", "text": "合成验收"},
            },
            "image_types": list(IMAGE_TYPES[:image_count]),
        }
    )
    created = TaskService(context.engine, TaskOptions()).create_task(
        owner_user_id=owner_id,
        idempotency_key=f"r5-{uuid4().hex}",
        request=request,
    )
    task_id = created.task_id
    with Session(context.engine) as db:
        task_items = (
            db.execute(
                select(TaskItem)
                .where(TaskItem.task_id == task_id)
                .order_by(TaskItem.position)
            )
            .scalars()
            .all()
        )
    assert len(task_items) == image_count
    item_id = task_items[0].id
    workflow_id, run_id, submission_id = uuid4(), uuid4(), uuid4()
    snapshot_digest = bytes.fromhex(analysis.snapshot_digest)
    package_digest = hashlib.sha256(b"r5 package").digest()
    with Session(context.engine) as db, db.begin():
        db.add(
            TaskPromptWorkflow(
                id=workflow_id,
                task_id=task_id,
                workflow_run_id=run_id,
                workflow_revision=1,
                trusted_analysis_snapshot_id=analysis.snapshot_id,
                trusted_analysis_snapshot_digest=snapshot_digest,
                status="package_ready",
            )
        )
        db.flush()
        db.add(
            PromptSubmission(
                submission_id=submission_id,
                workflow_id=workflow_id,
                task_id=task_id,
                workflow_run_id=run_id,
                workflow_revision=1,
                phase="initial_preferences",
                followup_round=0,
                content_digest_version=1,
                content_digest=hashlib.sha256(b"r5 submission").digest(),
                private_payload={"synthetic": True},
                status="package_ready",
                submit_outcome="accepted",
                callback_outcome="completed",
            )
        )
        db.flush()
        package_id = uuid4()
        db.add(
            TaskPromptPackage(
                id=package_id,
                workflow_id=workflow_id,
                submission_id=submission_id,
                task_id=task_id,
                workflow_run_id=run_id,
                workflow_revision=1,
                trusted_analysis_snapshot_id=analysis.snapshot_id,
                trusted_analysis_snapshot_digest=snapshot_digest,
                chatflow_contract_version="v3.3",
                validator_version=PROMPT_PACKAGE_VALIDATOR_VERSION,
                package_digest_version=1,
                package_digest=package_digest,
                package={"synthetic": True},
            )
        )
        db.flush()
        for item in task_items:
            db.add(
                TaskPromptItem(
                    id=uuid4(),
                    package_id=package_id,
                    task_id=task_id,
                    task_item_id=item.id,
                    position=item.position,
                    image_type=item.image_type,
                    design_prompt="synthetic bounded design prompt",
                    identity_prompt="synthetic identity prompt",
                    identity_anchor_asset_id=primary_model_id,
                    allowed_reference_aliases=["PRIMARY", "AUX01"],
                    excluded_reference_aliases=[],
                    allowed_reference_asset_ids=[str(primary_model_id), str(excluded_model_id)],
                )
            )
            start_event_id = uuid4()
            db.add(
                OutboxEvent(
                    id=start_event_id,
                    event_type=GENERATION_START_EVENT_TYPE,
                    aggregate_id=item.id,
                    operation_version=1,
                    schema_version=1,
                    payload={"task_item_id": str(item.id)},
                    status="pending",
                    available_at=now,
                )
            )
            db.add(
                OutboxReceipt(event_id=start_event_id, status="pending", attempt_count=0)
            )
        db.flush()
    context.owner_ids.add(owner_id)
    context.reference_set_ids.add(reference_set_id)
    context.recommendation_ids.add(recommendation_id)
    context.task_ids.add(task_id)
    return task_id, item_id, owner_id, (primary_model_id, excluded_model_id)


def _queue_options() -> QueueOptions:
    return QueueOptions(
        broker_url="amqp://integration:integration@localhost:5672/image-backend-test",
        queue_name="generation-runtime-integration",
    )


def _message_for_event(engine: Engine, event_id: UUID) -> dict[str, object]:
    with Session(engine) as db:
        event = db.get(OutboxEvent, event_id)
        assert event is not None
        message = OutboxMessage(
            event_id=event.id,
            aggregate_id=event.aggregate_id,
            operation_version=event.operation_version,
            schema_version=event.schema_version,
            payload={key: UUID(value) for key, value in event.payload.items()},
        )
        return message.model_dump(mode="json")


def _process_start_event(engine: Engine, item_id: UUID) -> None:
    with Session(engine) as db:
        event_id = db.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.event_type == GENERATION_START_EVENT_TYPE,
                OutboxEvent.aggregate_id == item_id,
            )
        )
    assert event_id is not None
    processor = EventProcessor(
        engine,
        _queue_options(),
        {
            GENERATION_START_EVENT_TYPE: HandlerRegistration(
                handle_generation_start_event, frozenset({1})
            )
        },
    )
    assert processor.process_payload(_message_for_event(engine, event_id)).status == "completed"


def _process_action(
    engine: Engine,
    runtime: GenerationActionRuntime,
    attempt_id: UUID,
    sequence: int,
) -> str:
    with Session(engine) as db, db.begin():
        action = db.execute(
            select(Action).where(
                Action.scope_kind == "attempt",
                Action.scope_id == attempt_id,
                Action.sequence == sequence,
            )
        ).scalar_one_or_none()
        assert action is not None
        now = db.scalar(select(func.clock_timestamp()))
        event = db.get(OutboxEvent, action.outbox_event_id)
        receipt = db.get(OutboxReceipt, action.outbox_event_id)
        assert event is not None and receipt is not None
        action.due_at = now
        event.available_at = now
        receipt.available_at = now
        event_id = event.id
    processor = ActionProcessor(engine, _queue_options(), runtime.registrations())
    database_errors: list[BaseException] = []

    def _capture_database_error(exception_context) -> None:
        database_errors.append(exception_context.original_exception)

    sqlalchemy_event.listen(engine, "handle_error", _capture_database_error)
    try:
        return processor.process_payload(_message_for_event(engine, event_id)).status
    except WorkerStateUnavailable:
        if database_errors:
            raise AssertionError(
                f"generation action sequence {sequence} database error: "
                f"{database_errors[-1]!r}"
            ) from None
        raise
    finally:
        sqlalchemy_event.remove(engine, "handle_error", _capture_database_error)


def test_persisted_generation_recovers_lost_submit_and_retries_without_resubmitting(
    pg_context: _PGContext, tmp_path: Path
) -> None:
    task_id, item_id, _, expected_reference_ids = _create_task_with_prompt(pg_context)
    _process_start_event(pg_context.engine, item_id)
    with Session(pg_context.engine) as db:
        attempt_id = db.scalar(
            select(TaskItemAttempt.id).where(TaskItemAttempt.task_item_id == item_id)
        )
    assert attempt_id is not None
    mock_port = MockGenerationPort(
        sessionmaker(bind=pg_context.engine, expire_on_commit=False, class_=Session),
        _test_faults=_TestOnlyFaults(
            accepted_response_lost_once=True,
            query_failure_once=True,
            download_failure_once=True,
        ),
    )
    observed_refs: list[tuple[UUID, ...]] = []

    class _ObservedPort:
        def submit(self, command):
            observed_refs.append(command.reference_asset_ids)
            return mock_port.submit(command)

        def query(self, provider_job_id):
            return mock_port.query(provider_job_id)

        def recover(self, request_key):
            return mock_port.recover(request_key)

        def download(self, result_ref):
            return mock_port.download(result_ref)

    runtime = GenerationActionRuntime(
        _ObservedPort(),
        LocalPrivateStorage(tmp_path / "generation-storage"),
    )
    with Session(pg_context.engine) as db:
        attempt = db.get(TaskItemAttempt, attempt_id)
        assert attempt is not None
        assert attempt.idempotency_key == attempt.id

    statuses = [
        _process_action(pg_context.engine, runtime, attempt_id, sequence)
        for sequence in range(1, 8)
    ]
    finalize_sql_order: list[str] = []

    def _observe_finalize_sql(connection, cursor, statement, parameters, context, executemany):
        lowered = statement.lower()
        if "generation_capacity_control" in lowered and "for update" in lowered:
            finalize_sql_order.append("capacity")
        if lowered.lstrip().startswith("insert into image_assets"):
            finalize_sql_order.append("asset")

    sqlalchemy_event.listen(
        pg_context.engine, "before_cursor_execute", _observe_finalize_sql
    )
    try:
        final_status = _process_action(pg_context.engine, runtime, attempt_id, 8)
    finally:
        sqlalchemy_event.remove(
            pg_context.engine, "before_cursor_execute", _observe_finalize_sql
        )
    statuses.append(final_status)
    assert statuses == ["unknown", *("completed" for _ in range(7))]
    assert finalize_sql_order.index("capacity") < finalize_sql_order.index("asset")
    assert len(observed_refs) == 1
    assert observed_refs[0] == expected_reference_ids

    with Session(pg_context.engine) as db:
        item = db.get(TaskItem, item_id)
        task = db.get(Task, task_id)
        attempt = db.get(TaskItemAttempt, attempt_id)
        context = db.get(GenerationAttemptContext, attempt_id)
        result = db.get(GenerationResult, attempt_id)
        reservations = db.execute(
            select(GenerationCapacityReservation).where(
                GenerationCapacityReservation.attempt_id == attempt_id
            )
        ).scalars().all()
        assert item is not None and task is not None and attempt is not None
        assert context is not None and result is not None
        assert item.status == "succeeded" and item.preview_ready
        assert task.status == "succeeded"
        assert task.terminal_at is not None and task.expires_at is not None
        assert task.expires_at - task.terminal_at == timedelta(days=task.retention_days)
        assert attempt.status == "completed"
        assert context.save_state == "attached"
        assert len(reservations) == 1 and reservations[0].status == "released"

    with Session(pg_context.engine) as db:
        invocation_total = db.scalar(
            select(func.count()).select_from(MockGenerationInvocation).where(
                MockGenerationInvocation.request_key == attempt_id
            )
        )
        job_total = db.scalar(
            select(func.count()).select_from(MockGenerationJob).where(
                MockGenerationJob.request_key == attempt_id
            )
        )
    assert invocation_total == 1 and job_total == 1


def test_capacity_prepare_grants_only_two_global_reservations(
    pg_context: _PGContext, tmp_path: Path
) -> None:
    task_items = [_create_task_with_prompt(pg_context) for _ in range(3)]
    attempts: list[UUID] = []
    for _, item_id, _, _ in task_items:
        # Invoke the same transaction hook used after an Action lock.
        with Session(pg_context.engine) as db, db.begin():
            attempt_id = start_generation_item(db, item_id)
            assert attempt_id is not None
        attempts.append(attempt_id)

    runtime = GenerationActionRuntime(
        MockGenerationPort(
            sessionmaker(bind=pg_context.engine, expire_on_commit=False, class_=Session)
        ),
        LocalPrivateStorage(tmp_path / "capacity-storage"),
    )
    grants: list[bool] = []
    for attempt_id in attempts:
        with Session(pg_context.engine) as db, db.begin():
            action = db.execute(
                select(Action).where(
                    Action.scope_kind == "attempt",
                    Action.scope_id == attempt_id,
                    Action.sequence == 1,
                )
            ).scalar_one()
            prepared = runtime.prepare(db, action)
            grants.append(bool(prepared.payload["capacity_granted"]))
    assert grants == [True, True, False]
    with Session(pg_context.engine) as db:
        active = db.scalar(
            select(func.count(GenerationCapacityReservation.id)).where(
                GenerationCapacityReservation.status == "active"
            )
        )
    assert active == 2


def test_confirmed_missing_recovery_schedules_same_key_submit_again(
    pg_context: _PGContext, tmp_path: Path
) -> None:
    _, item_id, _, _ = _create_task_with_prompt(pg_context)
    _process_start_event(pg_context.engine, item_id)
    with Session(pg_context.engine) as db:
        attempt_id = db.scalar(
            select(TaskItemAttempt.id).where(TaskItemAttempt.task_item_id == item_id)
        )
    assert attempt_id is not None
    mock_port = MockGenerationPort(
        sessionmaker(bind=pg_context.engine, expire_on_commit=False, class_=Session)
    )
    submitted_keys: list[UUID] = []

    class _AcceptAfterConfirmedAbsence:
        first = True

        def submit(self, command):
            submitted_keys.append(command.request_key)
            if self.first:
                self.first = False
                return GenerationUnknown(
                    "unknown", "SYNTHETIC_AMBIGUOUS", reconcile_after_seconds=1
                )
            return mock_port.submit(command)

        def recover(self, request_key):
            return mock_port.recover(request_key)

        def query(self, provider_job_id):
            return mock_port.query(provider_job_id)

        def download(self, result_ref):
            return mock_port.download(result_ref)

    runtime = GenerationActionRuntime(
        _AcceptAfterConfirmedAbsence(),
        LocalPrivateStorage(tmp_path / "confirmed-absence-storage"),
    )
    assert _process_action(pg_context.engine, runtime, attempt_id, 1) == "unknown"
    assert _process_action(pg_context.engine, runtime, attempt_id, 2) == "completed"
    with Session(pg_context.engine) as db:
        retry = db.execute(
            select(Action).where(
                Action.scope_id == attempt_id,
                Action.sequence == 3,
            )
        ).scalar_one()
        attempt = db.get(TaskItemAttempt, attempt_id)
        reservation = db.execute(
            select(GenerationCapacityReservation).where(
                GenerationCapacityReservation.attempt_id == attempt_id
            )
        ).scalar_one()
        assert retry.action_kind == "generation.submit.v1"
        assert attempt is not None and attempt.status == "prepared"
        assert reservation.status == "active"
    assert _process_action(pg_context.engine, runtime, attempt_id, 3) == "completed"
    assert submitted_keys == [attempt_id, attempt_id]
    with Session(pg_context.engine) as db:
        invocation_total = db.scalar(
            select(func.count()).select_from(MockGenerationInvocation).where(
                MockGenerationInvocation.request_key == attempt_id
            )
        )
        job_total = db.scalar(
            select(func.count()).select_from(MockGenerationJob).where(
                MockGenerationJob.request_key == attempt_id
            )
        )
    assert invocation_total == 1 and job_total == 1


def test_task_aggregation_keeps_unresolved_open_then_sets_partial_retention(
    pg_context: _PGContext,
) -> None:
    task_id, _, _, _ = _create_task_with_prompt(pg_context, image_count=2)
    with Session(pg_context.engine) as db, db.begin():
        task = db.execute(select(Task).where(Task.id == task_id).with_for_update()).scalar_one()
        items = (
            db.execute(
                select(TaskItem)
                .where(TaskItem.task_id == task_id)
                .order_by(TaskItem.position)
            )
            .scalars()
            .all()
        )
        items[0].status = "succeeded"
        items[1].status = "reconciling"
        _aggregate_task(db, task)
        assert task.status == "reconciling"
        assert task.terminal_at is None and task.expires_at is None
        items[1].status = "failed"
        items[1].error_code = "SYNTHETIC_FAILURE"
        _aggregate_task(db, task)
        assert task.status == "partially_succeeded"
        assert task.terminal_at is not None and task.expires_at is not None
        assert task.expires_at - task.terminal_at == timedelta(days=60)


def test_generation_context_rejects_partial_original_and_preview_metadata(
    pg_context: _PGContext,
) -> None:
    _, item_id, _, _ = _create_task_with_prompt(pg_context)
    with Session(pg_context.engine) as db, db.begin():
        attempt_id = start_generation_item(db, item_id)
    assert attempt_id is not None

    with Session(pg_context.engine) as db:
        with pytest.raises(IntegrityError):
            with db.begin():
                context = db.get(GenerationAttemptContext, attempt_id)
                assert context is not None
                context.original_size_bytes = 1
                db.flush()

    with Session(pg_context.engine) as db:
        with pytest.raises(IntegrityError):
            with db.begin():
                context = db.get(GenerationAttemptContext, attempt_id)
                assert context is not None
                context.preview_size_bytes = 1
                db.flush()
