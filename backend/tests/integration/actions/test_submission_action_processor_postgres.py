from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from datetime import timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from app.db.action_models import Action
from app.db.models import User
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.prompt_models import (
    MockPromptSubmission,
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
from app.integrations.dify.contracts import (
    DifyNotConfigured,
    DifyPromptWorkflowPort,
    PromptRecoveryResult,
    PromptSubmissionCommand,
    PromptSubmitResult,
)
from app.integrations.dify.factory import create_test_prompt_workflow_adapter
from app.integrations.dify.mock import build_mock_options, build_mock_original
from app.schemas.prompt_workflow import (
    PromptCallback,
    canonical_sha256,
    prompt_callback_digest_payload,
)
from app.schemas.recommendations import IMAGE_TYPES
from app.schemas.tasks import TaskCreateRequest
from app.services.prompt_callbacks import PromptCallbackService
from app.services.prompt_workflows import (
    PROMPT_INITIAL_RECOVER_ACTION_KIND,
    PROMPT_INITIAL_SUBMIT_ACTION_KIND,
    PROMPT_MOCK_RESULT_QUERY_ACTION_KIND,
    PROMPT_MOCK_RESULT_QUERY_MAX_ATTEMPTS,
    build_prompt_action_registrations,
)
from app.services.tasks import TaskOptions, TaskService, task_prompt_handlers
from app.services.trusted_analysis import (
    TRUSTED_ANALYSIS_STATE_KEY,
    build_trusted_analysis_snapshot,
)
from app.workers.action_processor import ActionProcessor
from app.workers.messages import OutboxMessage
from app.workers.options import QueueOptions
from app.workers.processor import EventProcessor
from sqlalchemy import Engine, create_engine, delete, func, inspect, or_, select
from sqlalchemy.orm import Session


@dataclass
class _PGContext:
    engine: Engine
    owner_ids: set[UUID] = field(default_factory=set)
    reference_set_ids: set[UUID] = field(default_factory=set)
    recommendation_ids: set[UUID] = field(default_factory=set)
    task_ids: set[UUID] = field(default_factory=set)


@pytest.fixture
def pg_context() -> Iterator[_PGContext]:
    database_url = os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires the dedicated PostgreSQL test runtime URL")
    engine = create_engine(database_url, pool_pre_ping=True)
    required = {
        "users",
        "reference_sets",
        "image_assets",
        "reference_set_images",
        "recommendations",
        "outbox_events",
        "outbox_receipts",
        "tasks",
        "task_items",
        "task_actions",
        "task_prompt_workflows",
        "task_prompt_submissions",
        "mock_prompt_submissions",
        "task_prompt_packages",
        "task_prompt_items",
    }
    if not required.issubset(set(inspect(engine).get_table_names())):
        engine.dispose()
        pytest.skip("requires the integrated PostgreSQL schema through s009_prompt_workflows")
    context = _PGContext(engine)
    try:
        yield context
    finally:
        _cleanup(context)
        engine.dispose()


def _cleanup(context: _PGContext) -> None:
    if not context.task_ids:
        return
    task_ids = tuple(context.task_ids)
    item_ids = select(TaskItem.id).where(TaskItem.task_id.in_(task_ids))
    action_ids = select(Action.id).where(Action.task_id.in_(task_ids))
    submission_ids = select(PromptSubmission.submission_id).where(
        PromptSubmission.task_id.in_(task_ids)
    )
    workflow_ids = select(TaskPromptWorkflow.id).where(TaskPromptWorkflow.task_id.in_(task_ids))
    with Session(context.engine) as db, db.begin():
        event_ids = select(OutboxEvent.id).where(
            or_(
                (OutboxEvent.event_type == "task.prompt.v1")
                & OutboxEvent.aggregate_id.in_(task_ids),
                (OutboxEvent.event_type == "task.action.v1")
                & OutboxEvent.aggregate_id.in_(action_ids),
                (OutboxEvent.event_type == "task.generation.start.v1")
                & OutboxEvent.aggregate_id.in_(item_ids),
            )
        )
        db.execute(delete(OutboxReceipt).where(OutboxReceipt.event_id.in_(event_ids)))
        db.execute(delete(Action).where(Action.task_id.in_(task_ids)))
        db.execute(delete(OutboxEvent).where(OutboxEvent.id.in_(event_ids)))
        db.execute(delete(TaskPromptItem).where(TaskPromptItem.task_id.in_(task_ids)))
        db.execute(delete(TaskPromptPackage).where(TaskPromptPackage.task_id.in_(task_ids)))
        db.execute(
            delete(MockPromptSubmission).where(
                MockPromptSubmission.submission_id.in_(submission_ids)
            )
        )
        db.execute(delete(PromptSubmission).where(PromptSubmission.task_id.in_(task_ids)))
        db.execute(delete(TaskPromptWorkflow).where(TaskPromptWorkflow.id.in_(workflow_ids)))
        db.execute(delete(TaskItemAttempt).where(TaskItemAttempt.task_item_id.in_(item_ids)))
        db.execute(delete(TaskItem).where(TaskItem.task_id.in_(task_ids)))
        db.execute(
            delete(TaskRecommendationConsumption).where(
                TaskRecommendationConsumption.task_id.in_(task_ids)
            )
        )
        db.execute(delete(TaskIdempotencyRecord).where(TaskIdempotencyRecord.task_id.in_(task_ids)))
        db.execute(delete(Task).where(Task.id.in_(task_ids)))
        if context.recommendation_ids:
            db.execute(
                delete(Recommendation).where(Recommendation.id.in_(context.recommendation_ids))
            )
        if context.reference_set_ids:
            db.execute(
                delete(ReferenceSetImage).where(
                    ReferenceSetImage.reference_set_id.in_(context.reference_set_ids)
                )
            )
            db.execute(delete(ReferenceSet).where(ReferenceSet.id.in_(context.reference_set_ids)))
        if context.owner_ids:
            db.execute(delete(ImageAsset).where(ImageAsset.owner_user_id.in_(context.owner_ids)))
            db.execute(delete(User).where(User.id.in_(context.owner_ids)))


def _create_task(context: _PGContext) -> UUID:
    owner_id = uuid4()
    reference_set_id = uuid4()
    recommendation_id = uuid4()
    with Session(context.engine) as db:
        now = db.scalar(select(func.clock_timestamp()))
    category = "R2 submission action synthetic fixture"
    reference_image_id = uuid4()
    source_id = uuid4()
    model_id = uuid4()
    digest = hashlib.sha256(reference_image_id.bytes).hexdigest()
    reference_summary = {
        "alias": "PRIMARY",
        "position": 0,
        "reference_set_image_id": reference_image_id,
        "model_asset_id": model_id,
        "source_asset_id": source_id,
        "model_sha256": digest,
        "source_sha256": digest,
        "model_mime_type": "image/png",
        "source_mime_type": "image/png",
        "model_size_bytes": 256,
        "source_size_bytes": 512,
        "model_width": 40,
        "model_height": 40,
        "source_width": 80,
        "source_height": 80,
    }
    trusted_snapshot = build_trusted_analysis_snapshot(
        reference_set_id=reference_set_id,
        recommendation_id=recommendation_id,
        recommendation_revision=1,
        category=category,
        references=[reference_summary],
        created_at=now,
    )
    public_options = build_mock_options()
    original = build_mock_original(category, public_options)
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
        db.add_all(
            [
                ImageAsset(
                    id=source_id,
                    owner_user_id=owner_id,
                    asset_kind="reference_source",
                    object_key=f"r2-action-test/{source_id}/source.png",
                    content_sha256=digest,
                    mime_type="image/png",
                    size_bytes=512,
                    width=80,
                    height=80,
                ),
                ImageAsset(
                    id=model_id,
                    owner_user_id=owner_id,
                    asset_kind="reference_model_input",
                    object_key=f"r2-action-test/{model_id}/model.png",
                    content_sha256=digest,
                    mime_type="image/png",
                    size_bytes=256,
                    width=40,
                    height=40,
                ),
            ]
        )
        db.flush()
        db.add(
            ReferenceSetImage(
                id=reference_image_id,
                reference_set_id=reference_set_id,
                owner_user_id=owner_id,
                slot="PRIMARY",
                position=0,
                source_asset_id=source_id,
                model_asset_id=model_id,
                content_sha256=digest,
                created_at=now,
            )
        )
        db.add(
            Recommendation(
                id=recommendation_id,
                owner_user_id=owner_id,
                reference_set_id=reference_set_id,
                status="ready",
                revision=1,
                original_recommendation=original,
                public_options=public_options,
                image_types=list(IMAGE_TYPES),
                audit={"image_analysis_performed": False},
                state={TRUSTED_ANALYSIS_STATE_KEY: trusted_snapshot.model_dump(mode="json")},
                run_ref=f"r2-action:{recommendation_id}",
                form_ref=f"r2-action-form:{recommendation_id}",
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
                "selling_points": {"mode": "custom", "text": "只使用已固定的模拟数据"},
            },
            "image_types": list(IMAGE_TYPES[:1]),
        }
    )
    accepted = TaskService(context.engine, TaskOptions()).create_task(
        owner_user_id=owner_id,
        idempotency_key=f"r2-action-{uuid4().hex}",
        request=request,
    )
    context.owner_ids.add(owner_id)
    context.reference_set_ids.add(reference_set_id)
    context.recommendation_ids.add(recommendation_id)
    context.task_ids.add(accepted.task_id)
    return accepted.task_id


def _queue_options() -> QueueOptions:
    return QueueOptions(
        broker_url="amqp://integration:integration@localhost:5672/image-backend-test",
        queue_name="submission-action-integration",
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


def _process_task_prompt_intent(engine: Engine, task_id: UUID) -> None:
    with Session(engine) as db:
        event_id = db.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.event_type == "task.prompt.v1",
                OutboxEvent.aggregate_id == task_id,
            )
        )
    assert event_id is not None
    processor = EventProcessor(engine, _queue_options(), task_prompt_handlers())
    assert processor.process_payload(_message_for_event(engine, event_id)).status == "completed"


def _action_row(engine: Engine, submission_id: UUID, sequence: int) -> Action:
    with Session(engine) as db:
        action = db.execute(
            select(Action).where(
                Action.scope_kind == "submission",
                Action.scope_id == submission_id,
                Action.sequence == sequence,
            )
        ).scalar_one_or_none()
        assert action is not None
        db.expunge(action)
        return action


def _make_action_due_now(engine: Engine, action_id: UUID) -> None:
    with Session(engine) as db, db.begin():
        action = db.get(Action, action_id)
        assert action is not None
        event = db.get(OutboxEvent, action.outbox_event_id)
        receipt = db.get(OutboxReceipt, action.outbox_event_id)
        assert event is not None and receipt is not None
        now = db.scalar(select(func.clock_timestamp()))
        action.due_at = now
        event.available_at = now
        receipt.available_at = now


def _process_submission_action(
    engine: Engine,
    *,
    action: Action,
    port: DifyPromptWorkflowPort,
    mock_result_callback_service: PromptCallbackService | None = None,
) -> str:
    processor = ActionProcessor(
        engine,
        _queue_options(),
        build_prompt_action_registrations(
            port, mock_result_callback_service=mock_result_callback_service
        ),
    )
    _make_action_due_now(engine, action.id)
    message = _message_for_event(engine, action.outbox_event_id)
    return processor.process_payload(message).status


def test_task_prompt_intent_redelivery_and_submission_action_use_real_processors(
    pg_context: _PGContext,
) -> None:
    task_id = _create_task(pg_context)
    with Session(pg_context.engine) as db:
        task_event_id = db.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.event_type == "task.prompt.v1",
                OutboxEvent.aggregate_id == task_id,
            )
        )
    assert task_event_id is not None
    prompt_processor = EventProcessor(pg_context.engine, _queue_options(), task_prompt_handlers())
    message = _message_for_event(pg_context.engine, task_event_id)
    assert prompt_processor.process_payload(message).status == "completed"
    assert prompt_processor.process_payload(message).status == "completed"

    with Session(pg_context.engine) as db:
        workflow = db.execute(
            select(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_id)
        ).scalar_one()
        submission = db.execute(
            select(PromptSubmission).where(PromptSubmission.task_id == task_id)
        ).scalar_one()
        task = db.get(Task, task_id)
        assert task is not None and task.status == "prompting"
        assert workflow.workflow_revision == 1
        assert submission.workflow_id == workflow.id
        assert db.scalar(select(func.count(Action.id)).where(Action.task_id == task_id)) == 1
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.event_type == "task.action.v1",
                    OutboxEvent.aggregate_id.in_(
                        select(Action.id).where(Action.task_id == task_id)
                    ),
                )
            )
            == 1
        )
        submission_id = submission.submission_id

    adapter = create_test_prompt_workflow_adapter(
        session_factory=lambda: Session(pg_context.engine)
    )
    first_action = _action_row(pg_context.engine, submission_id, 1)
    assert (
        _process_submission_action(
            pg_context.engine,
            action=first_action,
            port=adapter,
        )
        == "completed"
    )
    with Session(pg_context.engine) as db:
        action = db.get(Action, first_action.id)
        receipt = db.get(OutboxReceipt, first_action.outbox_event_id)
        mock_submission = db.get(MockPromptSubmission, submission_id)
        assert action is not None and action.status == "completed"
        assert receipt is not None and receipt.status == "completed"
        assert mock_submission is not None and mock_submission.outcome == "accepted"


def test_standard_mock_result_action_applies_persisted_result_after_worker_restart(
    pg_context: _PGContext,
) -> None:
    task_id = _create_task(pg_context)
    _process_task_prompt_intent(pg_context.engine, task_id)
    with Session(pg_context.engine) as db:
        submission_id = db.scalar(
            select(PromptSubmission.submission_id).where(PromptSubmission.task_id == task_id)
        )
    assert submission_id is not None
    adapter = create_test_prompt_workflow_adapter(
        session_factory=lambda: Session(pg_context.engine)
    )
    callback_service = PromptCallbackService(pg_context.engine)
    registrations = build_prompt_action_registrations(
        adapter, mock_result_callback_service=callback_service
    )

    submit_action = _action_row(pg_context.engine, submission_id, 1)
    submit_processor = ActionProcessor(pg_context.engine, _queue_options(), registrations)
    _make_action_due_now(pg_context.engine, submit_action.id)
    assert (
        submit_processor.process_payload(
            _message_for_event(pg_context.engine, submit_action.outbox_event_id)
        ).status
        == "completed"
    )
    query_action = _action_row(pg_context.engine, submission_id, 2)
    assert query_action.action_kind == PROMPT_MOCK_RESULT_QUERY_ACTION_KIND
    with Session(pg_context.engine) as db:
        submission = db.get(PromptSubmission, submission_id)
        assert submission is not None and submission.callback_result_digest is None

    # A newly constructed processor represents an ordinary Worker restart. It
    # resumes the persisted query Action without a callback pump or HTTP client.
    query_processor = ActionProcessor(pg_context.engine, _queue_options(), registrations)
    _make_action_due_now(pg_context.engine, query_action.id)
    query_message = _message_for_event(pg_context.engine, query_action.outbox_event_id)
    assert query_processor.process_payload(query_message).status == "completed"
    assert query_processor.process_payload(query_message).status == "completed"

    callback = adapter.read_prompt_result(submission_id)
    with Session(pg_context.engine) as db:
        submission = db.get(PromptSubmission, submission_id)
        task = db.get(Task, task_id)
        query_after = db.get(Action, query_action.id)
        assert submission is not None
        assert submission.status == "package_ready"
        assert submission.callback_result_digest == callback.result_digest
        assert task is not None and task.status == "generating"
        assert query_after is not None and query_after.status == "completed"
        assert (
            db.scalar(
                select(func.count(TaskPromptPackage.id)).where(TaskPromptPackage.task_id == task_id)
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.event_type == "task.generation.start.v1",
                    OutboxEvent.aggregate_id.in_(
                        select(TaskItem.id).where(TaskItem.task_id == task_id)
                    ),
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.scope_kind == "submission",
                    Action.scope_id == submission_id,
                    Action.action_kind == PROMPT_MOCK_RESULT_QUERY_ACTION_KIND,
                )
            )
            == 1
        )


class _BlockingPromptResultPort:
    def __init__(self, adapter, started: Event, resume: Event) -> None:
        self.adapter = adapter
        self.started = started
        self.resume = resume

    def submit_preferences(self, command: PromptSubmissionCommand) -> PromptSubmitResult:
        return self.adapter.submit_preferences(command)

    def recover_submission(self, command: PromptSubmissionCommand) -> PromptRecoveryResult:
        return self.adapter.recover_submission(command)

    def read_prompt_result(self, submission_id: UUID) -> PromptCallback:
        callback = self.adapter.read_prompt_result(submission_id)
        self.started.set()
        if not self.resume.wait(timeout=10):
            raise DifyNotConfigured("synthetic result query synchronization timed out")
        return callback


def _callback_with_different_digest(callback: PromptCallback) -> PromptCallback:
    assert isinstance(callback.package, dict)
    package = dict(callback.package)
    images = package.get("images")
    assert isinstance(images, list) and images and isinstance(images[0], dict)
    images = [dict(image) if isinstance(image, dict) else image for image in images]
    assert isinstance(images[0], dict)
    images[0]["design_prompt"] = f"{images[0]['design_prompt']} Alternate valid wording."
    package["images"] = images
    changed = callback.model_copy(update={"package": package, "result_digest": bytes(32)})
    return changed.model_copy(
        update={"result_digest": canonical_sha256(prompt_callback_digest_payload(changed))}
    )


def test_mock_result_query_and_callback_race_with_same_digest_creates_one_package_and_intent(
    pg_context: _PGContext,
) -> None:
    task_id = _create_task(pg_context)
    _process_task_prompt_intent(pg_context.engine, task_id)
    with Session(pg_context.engine) as db:
        submission_id = db.scalar(
            select(PromptSubmission.submission_id).where(PromptSubmission.task_id == task_id)
        )
    assert submission_id is not None
    adapter = create_test_prompt_workflow_adapter(
        session_factory=lambda: Session(pg_context.engine)
    )
    callback_service = PromptCallbackService(pg_context.engine)
    submit_action = _action_row(pg_context.engine, submission_id, 1)
    assert (
        _process_submission_action(
            pg_context.engine,
            action=submit_action,
            port=adapter,
            mock_result_callback_service=callback_service,
        )
        == "completed"
    )
    callback = adapter.read_prompt_result(submission_id)
    query_action = _action_row(pg_context.engine, submission_id, 2)
    started, resume = Event(), Event()
    blocking_port = _BlockingPromptResultPort(adapter, started, resume)
    processor = ActionProcessor(
        pg_context.engine,
        _queue_options(),
        build_prompt_action_registrations(
            blocking_port, mock_result_callback_service=callback_service
        ),
    )
    _make_action_due_now(pg_context.engine, query_action.id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        query_future = pool.submit(
            processor.process_payload,
            _message_for_event(pg_context.engine, query_action.outbox_event_id),
        )
        assert started.wait(timeout=5), "mock result query did not reach its external read"
        try:
            callback_result = callback_service.accept(callback, phase="initial_preferences")
        finally:
            resume.set()
        query_result = query_future.result(timeout=10)

    assert callback_result.status == "accepted"
    assert query_result.status == "completed"
    with Session(pg_context.engine) as db:
        submission = db.get(PromptSubmission, submission_id)
        query_after = db.get(Action, query_action.id)
        assert (
            submission is not None and submission.callback_result_digest == callback.result_digest
        )
        assert query_after is not None and query_after.status == "completed"
        assert (
            db.scalar(
                select(func.count(TaskPromptPackage.id)).where(TaskPromptPackage.task_id == task_id)
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.event_type == "task.generation.start.v1",
                    OutboxEvent.aggregate_id.in_(
                        select(TaskItem.id).where(TaskItem.task_id == task_id)
                    ),
                )
            )
            == 1
        )


def test_mock_result_query_rejects_digest_different_from_callback_winner(
    pg_context: _PGContext,
) -> None:
    task_id = _create_task(pg_context)
    _process_task_prompt_intent(pg_context.engine, task_id)
    with Session(pg_context.engine) as db:
        submission_id = db.scalar(
            select(PromptSubmission.submission_id).where(PromptSubmission.task_id == task_id)
        )
    assert submission_id is not None
    adapter = create_test_prompt_workflow_adapter(
        session_factory=lambda: Session(pg_context.engine)
    )
    callback_service = PromptCallbackService(pg_context.engine)
    submit_action = _action_row(pg_context.engine, submission_id, 1)
    assert (
        _process_submission_action(
            pg_context.engine,
            action=submit_action,
            port=adapter,
            mock_result_callback_service=callback_service,
        )
        == "completed"
    )
    callback = adapter.read_prompt_result(submission_id)
    changed_callback = _callback_with_different_digest(callback)
    assert (
        callback_service.accept(changed_callback, phase="initial_preferences").status == "accepted"
    )

    query_action = _action_row(pg_context.engine, submission_id, 2)
    processor = ActionProcessor(
        pg_context.engine,
        _queue_options(),
        build_prompt_action_registrations(adapter, mock_result_callback_service=callback_service),
    )
    _make_action_due_now(pg_context.engine, query_action.id)
    query_result = processor.process_payload(
        _message_for_event(pg_context.engine, query_action.outbox_event_id)
    )

    assert query_result.status == "rejected"
    with Session(pg_context.engine) as db:
        submission = db.get(PromptSubmission, submission_id)
        query_after = db.get(Action, query_action.id)
        assert submission is not None
        assert submission.callback_result_digest == changed_callback.result_digest
        assert query_after is not None and query_after.status == "rejected"
        assert query_after.last_error_code == "PROMPT_RESULT_DIGEST_CONFLICT"
        assert (
            db.scalar(
                select(func.count(TaskPromptPackage.id)).where(TaskPromptPackage.task_id == task_id)
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.event_type == "task.generation.start.v1",
                    OutboxEvent.aggregate_id.in_(
                        select(TaskItem.id).where(TaskItem.task_id == task_id)
                    ),
                )
            )
            == 1
        )


class _CountingPromptPort:
    def __init__(self, adapter) -> None:
        self.adapter = adapter
        self.submit_calls = 0
        self.recovery_calls = 0

    def submit_preferences(self, command: PromptSubmissionCommand) -> PromptSubmitResult:
        self.submit_calls += 1
        return self.adapter.submit_preferences(command)

    def recover_submission(self, command: PromptSubmissionCommand) -> PromptRecoveryResult:
        self.recovery_calls += 1
        return self.adapter.recover_submission(command)

    def read_prompt_result(self, submission_id: UUID) -> PromptCallback:
        return self.adapter.read_prompt_result(submission_id)


def test_accepted_response_lost_recovery_schedules_query_without_resubmission(
    pg_context: _PGContext,
) -> None:
    task_id = _create_task(pg_context)
    _process_task_prompt_intent(pg_context.engine, task_id)
    with Session(pg_context.engine) as db:
        submission_id = db.scalar(
            select(PromptSubmission.submission_id).where(PromptSubmission.task_id == task_id)
        )
    assert submission_id is not None
    adapter = create_test_prompt_workflow_adapter(
        session_factory=lambda: Session(pg_context.engine),
        accepted_response_lost_once=True,
    )
    port = _CountingPromptPort(adapter)
    callback_service = PromptCallbackService(pg_context.engine)
    processor = ActionProcessor(
        pg_context.engine,
        _queue_options(),
        build_prompt_action_registrations(port, mock_result_callback_service=callback_service),
    )
    submit_action = _action_row(pg_context.engine, submission_id, 1)
    _make_action_due_now(pg_context.engine, submit_action.id)
    assert (
        processor.process_payload(
            _message_for_event(pg_context.engine, submit_action.outbox_event_id)
        ).status
        == "unknown"
    )
    recover_action = _action_row(pg_context.engine, submission_id, 2)
    assert recover_action.action_kind == PROMPT_INITIAL_RECOVER_ACTION_KIND
    _make_action_due_now(pg_context.engine, recover_action.id)
    assert (
        processor.process_payload(
            _message_for_event(pg_context.engine, recover_action.outbox_event_id)
        ).status
        == "completed"
    )

    query_action = _action_row(pg_context.engine, submission_id, 3)
    assert query_action.action_kind == PROMPT_MOCK_RESULT_QUERY_ACTION_KIND
    assert port.submit_calls == 1 and port.recovery_calls == 1
    _make_action_due_now(pg_context.engine, query_action.id)
    assert (
        processor.process_payload(
            _message_for_event(pg_context.engine, query_action.outbox_event_id)
        ).status
        == "completed"
    )
    assert port.submit_calls == 1 and port.recovery_calls == 1
    with Session(pg_context.engine) as db:
        assert (
            db.scalar(
                select(func.count(MockPromptSubmission.submission_id)).where(
                    MockPromptSubmission.submission_id == submission_id
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(TaskPromptPackage.id)).where(TaskPromptPackage.task_id == task_id)
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.event_type == "task.generation.start.v1",
                    OutboxEvent.aggregate_id.in_(
                        select(TaskItem.id).where(TaskItem.task_id == task_id)
                    ),
                )
            )
            == 1
        )


class _UnavailablePromptResultPort:
    def __init__(self, adapter) -> None:
        self.adapter = adapter

    def submit_preferences(self, command: PromptSubmissionCommand) -> PromptSubmitResult:
        return self.adapter.submit_preferences(command)

    def recover_submission(self, command: PromptSubmissionCommand) -> PromptRecoveryResult:
        return self.adapter.recover_submission(command)

    def read_prompt_result(self, _submission_id: UUID) -> PromptCallback:
        raise DifyNotConfigured("synthetic result is not ready")


def test_mock_result_query_retries_are_persisted_and_bounded(pg_context: _PGContext) -> None:
    task_id = _create_task(pg_context)
    _process_task_prompt_intent(pg_context.engine, task_id)
    with Session(pg_context.engine) as db:
        submission_id = db.scalar(
            select(PromptSubmission.submission_id).where(PromptSubmission.task_id == task_id)
        )
    assert submission_id is not None
    adapter = create_test_prompt_workflow_adapter(
        session_factory=lambda: Session(pg_context.engine)
    )
    port = _UnavailablePromptResultPort(adapter)
    registrations = build_prompt_action_registrations(
        port, mock_result_callback_service=PromptCallbackService(pg_context.engine)
    )
    processor = ActionProcessor(pg_context.engine, _queue_options(), registrations)
    submit_action = _action_row(pg_context.engine, submission_id, 1)
    _make_action_due_now(pg_context.engine, submit_action.id)
    assert (
        processor.process_payload(
            _message_for_event(pg_context.engine, submit_action.outbox_event_id)
        ).status
        == "completed"
    )

    for query_attempt in range(1, PROMPT_MOCK_RESULT_QUERY_MAX_ATTEMPTS + 1):
        query_action = _action_row(pg_context.engine, submission_id, query_attempt + 1)
        assert query_action.action_kind == PROMPT_MOCK_RESULT_QUERY_ACTION_KIND
        _make_action_due_now(pg_context.engine, query_action.id)
        outcome = processor.process_payload(
            _message_for_event(pg_context.engine, query_action.outbox_event_id)
        )
        expected = (
            "completed" if query_attempt < PROMPT_MOCK_RESULT_QUERY_MAX_ATTEMPTS else "unknown"
        )
        assert outcome.status == expected

    with Session(pg_context.engine) as db:
        query_actions = list(
            db.scalars(
                select(Action)
                .where(
                    Action.scope_kind == "submission",
                    Action.scope_id == submission_id,
                    Action.action_kind == PROMPT_MOCK_RESULT_QUERY_ACTION_KIND,
                )
                .order_by(Action.sequence)
            )
        )
        task = db.get(Task, task_id)
        assert len(query_actions) == PROMPT_MOCK_RESULT_QUERY_MAX_ATTEMPTS
        assert [action.status for action in query_actions] == [
            "completed",
            "completed",
            "completed",
            "unknown",
        ]
        assert task is not None and task.status == "reconciling"


class _AbsentFirstSubmitPort:
    def __init__(self, adapter) -> None:
        self.adapter = adapter
        self.submit_calls = 0
        self.recovery_calls = 0

    def submit_preferences(self, command: PromptSubmissionCommand) -> PromptSubmitResult:
        self.submit_calls += 1
        if self.submit_calls == 1:
            return PromptSubmitResult(
                submission_id=command.submission_id,
                task_id=command.task_id,
                workflow_run_id=command.workflow_run_id,
                workflow_revision=command.workflow_revision,
                outcome="unknown",
                safe_reason_code="MOCK_TEST_RESPONSE_UNKNOWN",
            )
        return self.adapter.submit_preferences(command)

    def recover_submission(self, command: PromptSubmissionCommand) -> PromptRecoveryResult:
        self.recovery_calls += 1
        return self.adapter.recover_submission(command)

    def read_prompt_result(self, submission_id: UUID) -> PromptCallback:
        return self.adapter.read_prompt_result(submission_id)


def test_confirmed_absence_resubmits_sequence_three_through_action_processor_prepare(
    pg_context: _PGContext,
) -> None:
    task_id = _create_task(pg_context)
    _process_task_prompt_intent(pg_context.engine, task_id)
    with Session(pg_context.engine) as db:
        submission_id = db.scalar(
            select(PromptSubmission.submission_id).where(PromptSubmission.task_id == task_id)
        )
    assert submission_id is not None
    adapter = create_test_prompt_workflow_adapter(
        session_factory=lambda: Session(pg_context.engine)
    )
    port = _AbsentFirstSubmitPort(adapter)
    processor = ActionProcessor(
        pg_context.engine,
        _queue_options(),
        build_prompt_action_registrations(port),
    )

    first = _action_row(pg_context.engine, submission_id, 1)
    result_1 = processor.process_payload(
        _message_for_event(pg_context.engine, first.outbox_event_id)
    )
    assert result_1.status == "unknown"
    second = _action_row(pg_context.engine, submission_id, 2)
    assert second.action_kind == PROMPT_INITIAL_RECOVER_ACTION_KIND
    _make_action_due_now(pg_context.engine, second.id)
    result_2 = processor.process_payload(
        _message_for_event(pg_context.engine, second.outbox_event_id)
    )
    # Confirmed absence makes this recovery step definitely rejected. The
    # finalizer persists a separate sequence-3 submit, which is the next step
    # that must complete successfully.
    assert result_2.status == "rejected"
    third = _action_row(pg_context.engine, submission_id, 3)
    assert third.action_kind == PROMPT_INITIAL_SUBMIT_ACTION_KIND
    _make_action_due_now(pg_context.engine, third.id)
    result_3 = processor.process_payload(
        _message_for_event(pg_context.engine, third.outbox_event_id)
    )

    assert result_3.status == "completed"
    assert port.submit_calls == 2
    assert port.recovery_calls == 1
    with Session(pg_context.engine) as db:
        first_after = db.get(Action, first.id)
        second_after = db.get(Action, second.id)
        third_after = db.get(Action, third.id)
        submission = db.get(PromptSubmission, submission_id)
        assert first_after is not None and first_after.last_outcome == "unknown"
        assert second_after is not None and second_after.status == "rejected"
        assert third_after is not None and third_after.status == "completed"
        assert submission is not None and submission.submit_outcome == "accepted"


def test_expired_submission_action_at_recovery_limit_marks_domain_unknown_without_io(
    pg_context: _PGContext,
) -> None:
    task_id = _create_task(pg_context)
    _process_task_prompt_intent(pg_context.engine, task_id)
    with Session(pg_context.engine) as db:
        submission_id = db.scalar(
            select(PromptSubmission.submission_id).where(PromptSubmission.task_id == task_id)
        )
    assert submission_id is not None
    action = _action_row(pg_context.engine, submission_id, 1)
    action_id = action.id
    event_id = action.outbox_event_id
    with Session(pg_context.engine) as db, db.begin():
        action = db.get(Action, action_id)
        assert action is not None
        event = db.get(OutboxEvent, action.outbox_event_id)
        receipt = db.get(OutboxReceipt, action.outbox_event_id)
        assert event is not None and receipt is not None
        expired = db.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
        action.status = "claimed"
        action.claim_token = uuid4()
        action.lease_until = expired
        action.claim_count = _queue_options().max_attempts
        receipt.status = "processing"
        receipt.claim_token = uuid4()
        receipt.lease_until = expired
        receipt.attempt_count = 1
        event.status = "published"

    adapter = create_test_prompt_workflow_adapter(
        session_factory=lambda: Session(pg_context.engine)
    )
    port = _AbsentFirstSubmitPort(adapter)
    processor = ActionProcessor(
        pg_context.engine,
        _queue_options(),
        build_prompt_action_registrations(port),
    )
    result = processor.process_payload(_message_for_event(pg_context.engine, event_id))

    assert result.status == "unknown"
    assert port.submit_calls == 0
    assert port.recovery_calls == 0
    with Session(pg_context.engine) as db:
        workflow = db.execute(
            select(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_id)
        ).scalar_one()
        submission = db.get(PromptSubmission, submission_id)
        task = db.get(Task, task_id)
        action_after = db.get(Action, action_id)
        assert workflow.status == "reconciling"
        assert submission is not None and submission.status == "unknown"
        assert task is not None and task.status == "reconciling"
        assert action_after is not None and action_after.status == "unknown"


class _BlockingRecoveryPort:
    def __init__(self, adapter, started: Event, resume: Event) -> None:
        self.adapter = adapter
        self.started = started
        self.resume = resume

    def submit_preferences(self, command: PromptSubmissionCommand) -> PromptSubmitResult:
        return self.adapter.submit_preferences(command)

    def recover_submission(self, command: PromptSubmissionCommand) -> PromptRecoveryResult:
        self.started.set()
        if not self.resume.wait(timeout=10):
            return PromptRecoveryResult(
                submission_id=command.submission_id,
                task_id=command.task_id,
                workflow_run_id=command.workflow_run_id,
                workflow_revision=command.workflow_revision,
                outcome="unknown",
                safe_reason_code="MOCK_TEST_RECOVERY_TIMEOUT",
            )
        return PromptRecoveryResult(
            submission_id=command.submission_id,
            task_id=command.task_id,
            workflow_run_id=command.workflow_run_id,
            workflow_revision=command.workflow_revision,
            outcome="unknown",
            safe_reason_code="MOCK_LATE_RECOVERY_UNKNOWN",
        )

    def read_prompt_result(self, submission_id: UUID) -> PromptCallback:
        return self.adapter.read_prompt_result(submission_id)


def test_callback_can_commit_during_action_io_before_processor_relocks_finalize_parents(
    pg_context: _PGContext,
) -> None:
    task_id = _create_task(pg_context)
    _process_task_prompt_intent(pg_context.engine, task_id)
    with Session(pg_context.engine) as db:
        submission_id = db.scalar(
            select(PromptSubmission.submission_id).where(PromptSubmission.task_id == task_id)
        )
    assert submission_id is not None
    adapter = create_test_prompt_workflow_adapter(
        session_factory=lambda: Session(pg_context.engine),
        accepted_response_lost_once=True,
    )
    first = _action_row(pg_context.engine, submission_id, 1)
    # Simulate a submit that was accepted durably but whose response was lost.
    # The real finalizer must have already scheduled recovery sequence 2.
    assert _process_submission_action(pg_context.engine, action=first, port=adapter) == "unknown"
    callback = adapter.read_prompt_result(submission_id)
    recover_action = _action_row(pg_context.engine, submission_id, 2)
    assert recover_action.action_kind == PROMPT_INITIAL_RECOVER_ACTION_KIND
    _make_action_due_now(pg_context.engine, recover_action.id)
    started, resume = Event(), Event()
    blocking_port = _BlockingRecoveryPort(adapter, started, resume)
    processor = ActionProcessor(
        pg_context.engine,
        _queue_options(),
        build_prompt_action_registrations(blocking_port),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        action_future = pool.submit(
            processor.process_payload,
            _message_for_event(pg_context.engine, recover_action.outbox_event_id),
        )
        assert started.wait(timeout=5), "submission Action did not reach its I/O phase"

        def accept_callback():
            from app.services.prompt_workflows import accept_prompt_callback

            with Session(pg_context.engine) as db, db.begin():
                return accept_prompt_callback(db, callback)

        callback_future = pool.submit(accept_callback)
        try:
            callback_result = callback_future.result(timeout=5)
        except TimeoutError:
            resume.set()
            action_future.result(timeout=10)
            pytest.fail("callback was blocked by a database lock held across Action I/O")
        finally:
            resume.set()
        action_result = action_future.result(timeout=10)

    assert callback_result.status == "accepted"
    assert action_result.status == "unknown"
    with Session(pg_context.engine) as db:
        action = db.get(Action, recover_action.id)
        receipt = db.get(OutboxReceipt, recover_action.outbox_event_id)
        task = db.get(Task, task_id)
        workflow = db.execute(
            select(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_id)
        ).scalar_one()
        submission = db.get(PromptSubmission, submission_id)
        assert action is not None and action.status == "unknown"
        assert receipt is not None and receipt.status == "completed"
        assert task is not None and task.status == "generating"
        assert workflow.status == "package_ready"
        assert submission is not None and submission.status == "package_ready"
        assert (
            db.scalar(
                select(func.count(TaskPromptPackage.id)).where(TaskPromptPackage.task_id == task_id)
            )
            == 1
        )
