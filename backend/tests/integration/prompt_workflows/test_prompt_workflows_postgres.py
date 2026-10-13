from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
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
from app.integrations.dify.contracts import DifyPromptWorkflowPort
from app.integrations.dify.factory import create_test_prompt_workflow_adapter
from app.integrations.dify.mock import (
    MockPromptWorkflowAdapter,
    build_mock_options,
    build_mock_original,
)
from app.schemas.prompt_workflow import (
    PROMPT_RESULT_DIGEST_VERSION,
    PromptCallback,
    PromptSubmitResult,
    canonical_sha256,
    prompt_callback_digest_payload,
)
from app.schemas.recommendations import IMAGE_TYPES
from app.schemas.tasks import TaskCreateRequest
from app.services.prompt_workflows import (
    GENERATION_START_EVENT_TYPE,
    PROMPT_INITIAL_RECOVER_ACTION_KIND,
    PROMPT_INITIAL_SUBMIT_ACTION_KIND,
    PromptWorkflowConflict,
    PromptWorkflowUnavailable,
    _lock_task_parent_chain,
    accept_prompt_callback,
    create_initial_prompt_submission_in_transaction,
    execute_initial_prompt_submission,
    finalize_initial_prompt_action,
    prepare_initial_prompt_submission,
    recover_initial_prompt_submission,
)
from app.services.tasks import TaskOptions, TaskService
from app.services.trusted_analysis import (
    TRUSTED_ANALYSIS_STATE_KEY,
    build_trusted_analysis_snapshot,
)
from sqlalchemy import create_engine, delete, func, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@dataclass
class _PGContext:
    engine: Engine
    owner_ids: set[UUID] = field(default_factory=set)
    reference_set_ids: set[UUID] = field(default_factory=set)
    recommendation_ids: set[UUID] = field(default_factory=set)
    task_ids: set[UUID] = field(default_factory=set)


class _CountingPromptPort:
    def __init__(self, adapter: MockPromptWorkflowAdapter) -> None:
        self.adapter = adapter
        self.submit_calls = 0
        self.recovery_calls = 0

    def submit_preferences(self, command):
        self.submit_calls += 1
        return self.adapter.submit_preferences(command)

    def recover_submission(self, command):
        self.recovery_calls += 1
        return self.adapter.recover_submission(command)

    def read_prompt_result(self, submission_id):
        return self.adapter.read_prompt_result(submission_id)


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
        "task_item_attempts",
        "task_actions",
        "task_idempotency_records",
        "task_recommendation_consumptions",
        "task_prompt_workflows",
        "task_prompt_submissions",
        "mock_prompt_submissions",
        "task_prompt_packages",
        "task_prompt_items",
    }
    if not required.issubset(set(inspect(engine).get_table_names())):
        engine.dispose()
        pytest.skip("requires the integrated PostgreSQL schema through the R2 prompt migration")
    context = _PGContext(engine)
    yield context
    _cleanup(context)
    engine.dispose()


def _cleanup(context: _PGContext) -> None:
    if not context.task_ids:
        return
    task_ids = tuple(context.task_ids)
    with Session(context.engine) as db, db.begin():
        task_item_ids = select(TaskItem.id).where(TaskItem.task_id.in_(task_ids))
        action_ids = select(Action.id).where(Action.task_id.in_(task_ids))
        event_ids = select(OutboxEvent.id).where(
            (OutboxEvent.event_type == "task.prompt.v1") & (OutboxEvent.aggregate_id.in_(task_ids))
            | (
                (OutboxEvent.event_type == "task.action.v1")
                & (OutboxEvent.aggregate_id.in_(action_ids))
            )
            | (
                (OutboxEvent.event_type == GENERATION_START_EVENT_TYPE)
                & (OutboxEvent.aggregate_id.in_(task_item_ids))
            )
        )
        db.execute(delete(OutboxReceipt).where(OutboxReceipt.event_id.in_(event_ids)))
        db.execute(delete(Action).where(Action.task_id.in_(task_ids)))
        db.execute(delete(OutboxEvent).where(OutboxEvent.id.in_(event_ids)))
        db.execute(delete(TaskPromptItem).where(TaskPromptItem.task_id.in_(task_ids)))
        db.execute(delete(TaskPromptPackage).where(TaskPromptPackage.task_id.in_(task_ids)))
        submission_ids = select(PromptSubmission.submission_id).where(
            PromptSubmission.task_id.in_(task_ids)
        )
        db.execute(
            delete(MockPromptSubmission).where(
                MockPromptSubmission.submission_id.in_(submission_ids)
            )
        )
        db.execute(delete(PromptSubmission).where(PromptSubmission.task_id.in_(task_ids)))
        db.execute(delete(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id.in_(task_ids)))
        db.execute(delete(TaskItemAttempt).where(TaskItemAttempt.task_item_id.in_(task_item_ids)))
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


def _database_now(engine: Engine):
    with Session(engine) as db:
        return db.scalar(select(func.clock_timestamp()))


def _create_task(
    context: _PGContext,
    *,
    reference_count: int,
    relation_overrides: dict[str, str] | None = None,
) -> tuple[UUID, UUID]:
    owner_id = uuid4()
    reference_set_id = uuid4()
    recommendation_id = uuid4()
    now = _database_now(context.engine)
    category = "R2 synthetic fixture"
    slots = ("PRIMARY", "AUX01", "AUX02", "AUX03")[:reference_count]
    reference_rows: list[ReferenceSetImage] = []
    assets: list[ImageAsset] = []
    trusted_references: list[dict[str, object]] = []

    for position, slot in enumerate(slots):
        reference_image_id = uuid4()
        source_id = uuid4()
        model_id = uuid4()
        digest = hashlib.sha256(reference_image_id.bytes).hexdigest()
        assets.extend(
            [
                ImageAsset(
                    id=source_id,
                    owner_user_id=owner_id,
                    asset_kind="reference_source",
                    object_key=f"r2-test/{source_id}/source.png",
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
                    object_key=f"r2-test/{model_id}/model.png",
                    content_sha256=digest,
                    mime_type="image/png",
                    size_bytes=256,
                    width=40,
                    height=40,
                ),
            ]
        )
        reference_rows.append(
            ReferenceSetImage(
                id=reference_image_id,
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
        trusted_references.append(
            {
                "alias": slot,
                "position": position,
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
        )

    snapshot = build_trusted_analysis_snapshot(
        reference_set_id=reference_set_id,
        recommendation_id=recommendation_id,
        recommendation_revision=1,
        category=category,
        references=trusted_references,
        created_at=now,
        relation_overrides=relation_overrides,
    )
    options = build_mock_options()
    original = build_mock_original(category, options)
    with Session(context.engine) as db, db.begin():
        db.add(User(id=owner_id, identity_kind="guest", status="active"))
        db.add(
            ReferenceSet(
                id=reference_set_id,
                owner_user_id=owner_id,
                product_category=category,
                status="ready",
                created_at=now,
                last_activity_at=now,
                ready_at=now,
                expires_at=now + timedelta(days=10),
            )
        )
        db.flush()
        db.add_all(assets)
        db.flush()
        db.add_all(reference_rows)
        db.add(
            Recommendation(
                id=recommendation_id,
                owner_user_id=owner_id,
                reference_set_id=reference_set_id,
                status="ready",
                revision=1,
                original_recommendation=original,
                public_options=options,
                image_types=list(IMAGE_TYPES),
                audit={"image_analysis_performed": False},
                state={TRUSTED_ANALYSIS_STATE_KEY: snapshot.model_dump(mode="json")},
                run_ref=f"r2-mock:{recommendation_id}",
                form_ref=f"r2-mock-form:{recommendation_id}",
                valid_until=now + timedelta(days=2),
                data_expires_at=now + timedelta(days=60),
                ready_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    context.owner_ids.add(owner_id)
    context.reference_set_ids.add(reference_set_id)
    context.recommendation_ids.add(recommendation_id)

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
                "selling_points": {"mode": "custom", "text": "保留可核验的商品卖点"},
            },
            "image_types": list(IMAGE_TYPES[:2]),
        }
    )
    accepted = TaskService(context.engine, TaskOptions()).create_task(
        owner_user_id=owner_id,
        idempotency_key=f"r2-{uuid4().hex}",
        request=request,
    )
    task_id = accepted.task_id
    context.task_ids.add(task_id)
    return owner_id, task_id


def _create_submission(context: _PGContext, task_id: UUID) -> UUID:
    with Session(context.engine) as db, db.begin():
        task = _lock_task_parent_chain(db, task_id)
        assert task is not None
        submission = create_initial_prompt_submission_in_transaction(db, task=task)
        submission_id = submission.submission_id
    return submission_id


def _process_submission_action(
    context: _PGContext,
    *,
    task_id: UUID,
    submission_id: UUID,
    sequence: int,
    adapter: DifyPromptWorkflowPort,
) -> str:
    with Session(context.engine) as db, db.begin():
        hint = db.execute(
            select(Action.id, Action.outbox_event_id).where(
                Action.scope_kind == "submission",
                Action.scope_id == submission_id,
                Action.sequence == sequence,
            )
        ).one()
        event = db.execute(
            select(OutboxEvent).where(OutboxEvent.id == hint.outbox_event_id).with_for_update()
        ).scalar_one()
        receipt = db.execute(
            select(OutboxReceipt).where(OutboxReceipt.event_id == event.id).with_for_update()
        ).scalar_one()
        task = _lock_task_parent_chain(db, task_id)
        assert task is not None
        action = db.execute(
            select(Action).where(Action.id == hint.id).with_for_update()
        ).scalar_one()
        token = uuid4()
        now = db.scalar(select(func.clock_timestamp()))
        if action.due_at > now:
            action.due_at = now
            event.available_at = now
            receipt.available_at = now
        action.status = "claimed"
        action.claim_token = token
        action.lease_until = now + timedelta(minutes=2)
        action.claim_count += 1
        receipt.status = "processing"
        receipt.claim_token = token
        receipt.lease_until = action.lease_until
        receipt.attempt_count += 1
        event.status = "published"
        event.published_at = now
        action_input = prepare_initial_prompt_submission(db, action)

    if action_input.action_kind == PROMPT_INITIAL_RECOVER_ACTION_KIND:
        result = recover_initial_prompt_submission(action_input, adapter)
    else:
        result = execute_initial_prompt_submission(action_input, adapter)

    with Session(context.engine) as db, db.begin():
        event_id = db.scalar(
            select(Action.outbox_event_id).where(Action.id == action_input.action_id)
        )
        event = db.execute(
            select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
        ).scalar_one()
        receipt = db.execute(
            select(OutboxReceipt).where(OutboxReceipt.event_id == event.id).with_for_update()
        ).scalar_one()
        task = _lock_task_parent_chain(db, task_id)
        assert task is not None
        action = db.execute(
            select(Action).where(Action.id == action_input.action_id).with_for_update()
        ).scalar_one()
        finalize_initial_prompt_action(db, action, action_input, result)
        action.status = {
            "unknown": "unknown",
            "definitely_rejected": "rejected",
        }.get(result.outcome, "completed")
        action.last_outcome = result.outcome
        action.last_error_code = result.safe_reason_code
        action.claim_token = None
        action.lease_until = None
        action.completed_at = now = db.scalar(select(func.clock_timestamp()))
        receipt.status = "completed"
        receipt.completed_at = now
        receipt.claim_token = None
        receipt.lease_until = None
        receipt.last_error_code = result.safe_reason_code
    return result.outcome


def _rebind_callback(
    callback: PromptCallback,
    *,
    package: object | None = None,
    workflow_revision: int | None = None,
) -> PromptCallback:
    payload = callback.model_dump(mode="python")
    if package is not None:
        payload["package"] = package
    if workflow_revision is not None:
        payload["workflow_revision"] = workflow_revision
    provisional = PromptCallback.model_validate(payload)
    payload["result_digest_version"] = PROMPT_RESULT_DIGEST_VERSION
    payload["result_digest"] = canonical_sha256(prompt_callback_digest_payload(provisional))
    return PromptCallback.model_validate(payload)


def _accept_callback(context: _PGContext, callback: PromptCallback):
    with Session(context.engine) as db, db.begin():
        return accept_prompt_callback(db, callback)


@pytest.mark.parametrize("reference_count", [1, 2, 3, 4])
def test_postgres_persists_complete_package_and_one_generation_intent_per_item(
    pg_context: _PGContext,
    reference_count: int,
) -> None:
    _, task_id = _create_task(pg_context, reference_count=reference_count)
    submission_id = _create_submission(pg_context, task_id)
    adapter = create_test_prompt_workflow_adapter(
        session_factory=lambda: Session(pg_context.engine)
    )

    assert (
        _process_submission_action(
            pg_context,
            task_id=task_id,
            submission_id=submission_id,
            sequence=1,
            adapter=adapter,
        )
        == "accepted"
    )
    with Session(pg_context.engine) as db:
        assert (
            db.scalar(
                select(func.count(TaskPromptPackage.id)).where(TaskPromptPackage.task_id == task_id)
            )
            == 0
        )
        callback = adapter.read_prompt_result(submission_id)
    accepted = _accept_callback(pg_context, callback)

    with Session(pg_context.engine) as db:
        task = db.get(Task, task_id)
        workflow = db.execute(
            select(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_id)
        ).scalar_one()
        package = db.execute(
            select(TaskPromptPackage).where(TaskPromptPackage.task_id == task_id)
        ).scalar_one()
        task_items = (
            db.execute(
                select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.position)
            )
            .scalars()
            .all()
        )
        stored_items = (
            db.execute(
                select(TaskPromptItem)
                .where(TaskPromptItem.task_id == task_id)
                .order_by(TaskPromptItem.position)
            )
            .scalars()
            .all()
        )
        assert accepted.status == "accepted"
        assert task is not None and task.status == "generating"
        assert workflow.status == "package_ready"
        assert package.package["basis"]["declared_category"] == task.category_snapshot
        assert len(package.package["basis"]["auxiliary_relations"]) == reference_count - 1
        assert len(package.package["images"]) == len(task_items) == len(stored_items) == 2
        assert [item.task_item_id for item in stored_items] == [item.id for item in task_items]
        assert (
            str(package.trusted_analysis_snapshot_id)
            == task.recommendation_snapshot["trusted_analysis_snapshot_id"]
        )
        assert (
            package.trusted_analysis_snapshot_digest.hex()
            == task.recommendation_snapshot["trusted_analysis_snapshot_digest"]
        )
        expected_aliases = ["PRIMARY", *(f"AUX{index:02d}" for index in range(1, reference_count))]
        assert all(item.allowed_reference_aliases == expected_aliases for item in stored_items)
        assert all(item.excluded_reference_aliases == [] for item in stored_items)
        intents = db.scalars(
            select(OutboxEvent).where(
                OutboxEvent.event_type == GENERATION_START_EVENT_TYPE,
                OutboxEvent.aggregate_id.in_([item.id for item in task_items]),
            )
        ).all()
        assert len(intents) == len(task_items)
        assert {event.payload["task_item_id"] for event in intents} == {
            str(item.id) for item in task_items
        }


def test_postgres_definite_submit_rejection_terminally_fails_entire_task(
    pg_context: _PGContext,
) -> None:
    _, task_id = _create_task(pg_context, reference_count=2)
    submission_id = _create_submission(pg_context, task_id)
    adapter = create_test_prompt_workflow_adapter(
        session_factory=lambda: Session(pg_context.engine),
        reject_before_accept_once=True,
    )

    assert (
        _process_submission_action(
            pg_context,
            task_id=task_id,
            submission_id=submission_id,
            sequence=1,
            adapter=adapter,
        )
        == "definitely_rejected"
    )
    with Session(pg_context.engine) as db:
        task = db.get(Task, task_id)
        items = db.scalars(select(TaskItem).where(TaskItem.task_id == task_id)).all()
        submission = db.get(PromptSubmission, submission_id)
        assert task is not None and task.status == "failed"
        assert task.terminal_at is not None and task.expires_at is not None
        assert task.expires_at == task.terminal_at + timedelta(days=task.retention_days)
        assert len(items) == 2 and all(item.status == "failed" for item in items)
        assert all(item.error_code == "PROMPT_WORKFLOW_FAILED" for item in items)
        assert submission is not None and submission.status == "rejected"
        assert (
            db.scalar(
                select(func.count(TaskPromptPackage.id)).where(TaskPromptPackage.task_id == task_id)
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.event_type == GENERATION_START_EVENT_TYPE,
                    OutboxEvent.aggregate_id.in_(
                        select(TaskItem.id).where(TaskItem.task_id == task_id)
                    ),
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(TaskItemAttempt.id)).where(
                    TaskItemAttempt.task_item_id.in_(
                        select(TaskItem.id).where(TaskItem.task_id == task_id)
                    )
                )
            )
            == 0
        )


def test_postgres_accepted_response_loss_uses_independent_recovery_action_once(
    pg_context: _PGContext,
) -> None:
    _, task_id = _create_task(pg_context, reference_count=2)
    submission_id = _create_submission(pg_context, task_id)
    adapter = _CountingPromptPort(
        create_test_prompt_workflow_adapter(
            session_factory=lambda: Session(pg_context.engine),
            accepted_response_lost_once=True,
        )
    )

    assert (
        _process_submission_action(
            pg_context,
            task_id=task_id,
            submission_id=submission_id,
            sequence=1,
            adapter=adapter,
        )
        == "unknown"
    )
    assert (
        _process_submission_action(
            pg_context,
            task_id=task_id,
            submission_id=submission_id,
            sequence=2,
            adapter=adapter,
        )
        == "accepted"
    )

    with Session(pg_context.engine) as db:
        actions = (
            db.execute(
                select(Action).where(Action.scope_id == submission_id).order_by(Action.sequence)
            )
            .scalars()
            .all()
        )
        submission = db.get(PromptSubmission, submission_id)
        mock_rows = db.scalar(
            select(func.count(MockPromptSubmission.submission_id)).where(
                MockPromptSubmission.submission_id == submission_id
            )
        )
        assert [action.action_kind for action in actions] == [
            PROMPT_INITIAL_SUBMIT_ACTION_KIND,
            PROMPT_INITIAL_RECOVER_ACTION_KIND,
        ]
        assert actions[0].status == "unknown"
        assert actions[1].status == "completed"
        assert actions[0].id != actions[1].id
        assert actions[0].outbox_event_id != actions[1].outbox_event_id
        assert submission is not None and submission.status == "accepted"
        assert mock_rows == 1
        task = db.get(Task, task_id)
        assert task is not None and task.status == "prompting"
    assert adapter.submit_calls == 1
    assert adapter.recovery_calls == 1


def test_postgres_unknown_recovery_schedules_another_recover_without_resubmit(
    pg_context: _PGContext,
) -> None:
    _, task_id = _create_task(pg_context, reference_count=1)
    submission_id = _create_submission(pg_context, task_id)
    adapter = _CountingPromptPort(
        create_test_prompt_workflow_adapter(
            session_factory=lambda: Session(pg_context.engine),
            accepted_response_lost_once=True,
            recovery_unknown_once=True,
        )
    )

    for sequence, expected_outcome in ((1, "unknown"), (2, "unknown"), (3, "accepted")):
        assert (
            _process_submission_action(
                pg_context,
                task_id=task_id,
                submission_id=submission_id,
                sequence=sequence,
                adapter=adapter,
            )
            == expected_outcome
        )

    with Session(pg_context.engine) as db:
        actions = (
            db.execute(
                select(Action).where(Action.scope_id == submission_id).order_by(Action.sequence)
            )
            .scalars()
            .all()
        )
        count = db.scalar(
            select(func.count(MockPromptSubmission.submission_id)).where(
                MockPromptSubmission.submission_id == submission_id
            )
        )
        assert [action.action_kind for action in actions] == [
            PROMPT_INITIAL_SUBMIT_ACTION_KIND,
            PROMPT_INITIAL_RECOVER_ACTION_KIND,
            PROMPT_INITIAL_RECOVER_ACTION_KIND,
        ]
        assert [action.status for action in actions] == ["unknown", "unknown", "completed"]
        assert count == 1
    assert adapter.submit_calls == 1
    assert adapter.recovery_calls == 2


def test_postgres_only_resubmits_after_recovery_confirms_absence(
    pg_context: _PGContext,
) -> None:
    _, task_id = _create_task(pg_context, reference_count=1)
    submission_id = _create_submission(pg_context, task_id)
    adapter = _CountingPromptPort(
        create_test_prompt_workflow_adapter(session_factory=lambda: Session(pg_context.engine))
    )
    adapter_first_submit = adapter.submit_preferences

    def lose_before_persisting(command):
        adapter.submit_calls += 1
        return PromptSubmitResult(
            submission_id=command.submission_id,
            task_id=command.task_id,
            workflow_run_id=command.workflow_run_id,
            workflow_revision=command.workflow_revision,
            outcome="unknown",
            safe_reason_code="SYNTHETIC_SUBMIT_UNKNOWN",
        )

    adapter.submit_preferences = lose_before_persisting
    assert (
        _process_submission_action(
            pg_context,
            task_id=task_id,
            submission_id=submission_id,
            sequence=1,
            adapter=adapter,
        )
        == "unknown"
    )
    assert (
        _process_submission_action(
            pg_context,
            task_id=task_id,
            submission_id=submission_id,
            sequence=2,
            adapter=adapter,
        )
        == "definitely_rejected"
    )
    adapter.submit_preferences = adapter_first_submit
    assert (
        _process_submission_action(
            pg_context,
            task_id=task_id,
            submission_id=submission_id,
            sequence=3,
            adapter=adapter,
        )
        == "accepted"
    )

    with Session(pg_context.engine) as db:
        actions = (
            db.execute(
                select(Action).where(Action.scope_id == submission_id).order_by(Action.sequence)
            )
            .scalars()
            .all()
        )
        assert [action.action_kind for action in actions] == [
            PROMPT_INITIAL_SUBMIT_ACTION_KIND,
            PROMPT_INITIAL_RECOVER_ACTION_KIND,
            PROMPT_INITIAL_SUBMIT_ACTION_KIND,
        ]
        assert [action.status for action in actions] == ["unknown", "rejected", "completed"]
        assert adapter.submit_calls == 2
        assert adapter.recovery_calls == 1
        submission = db.get(PromptSubmission, submission_id)
        workflow = db.execute(
            select(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_id)
        ).scalar_one()
        assert submission is not None and submission.status == "accepted"
        assert workflow.status == "submitted"


def test_postgres_callback_replay_is_idempotent_and_different_digest_conflicts(
    pg_context: _PGContext,
) -> None:
    _, task_id = _create_task(pg_context, reference_count=2)
    submission_id = _create_submission(pg_context, task_id)
    adapter = create_test_prompt_workflow_adapter(
        session_factory=lambda: Session(pg_context.engine)
    )
    _process_submission_action(
        pg_context,
        task_id=task_id,
        submission_id=submission_id,
        sequence=1,
        adapter=adapter,
    )
    callback = adapter.read_prompt_result(submission_id)

    assert _accept_callback(pg_context, callback).status == "accepted"
    assert _accept_callback(pg_context, callback).status == "replayed"
    altered_package = callback.package.copy()
    altered_package["basis"] = {
        **altered_package["basis"],
        "product": "different but syntactically valid package",
    }
    conflicting = _rebind_callback(callback, package=altered_package)
    with pytest.raises(PromptWorkflowConflict):
        _accept_callback(pg_context, conflicting)
    with Session(pg_context.engine) as db:
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.event_type == GENERATION_START_EVENT_TYPE,
                    OutboxEvent.aggregate_id.in_(
                        select(TaskItem.id).where(TaskItem.task_id == task_id)
                    ),
                )
            )
            == 2
        )


@pytest.mark.parametrize("detail_field", ["view", "scope", "evidence"])
def test_postgres_forged_snapshot_relation_details_reject_entire_package_without_intents(
    pg_context: _PGContext,
    detail_field: str,
) -> None:
    _, task_id = _create_task(pg_context, reference_count=2)
    submission_id = _create_submission(pg_context, task_id)
    adapter = create_test_prompt_workflow_adapter(
        session_factory=lambda: Session(pg_context.engine)
    )
    _process_submission_action(
        pg_context,
        task_id=task_id,
        submission_id=submission_id,
        sequence=1,
        adapter=adapter,
    )
    callback = adapter.read_prompt_result(submission_id)
    altered_package = callback.package.copy()
    basis = {**altered_package["basis"]}
    relations = list(basis["auxiliary_relations"])
    forged_value = {
        "view": "Forged untrusted view",
        "scope": "detail_only",
        "evidence": "forged untrusted evidence",
    }[detail_field]
    relations[0] = {**relations[0], detail_field: forged_value}
    basis["auxiliary_relations"] = relations
    altered_package["basis"] = basis

    rejected = _accept_callback(
        pg_context,
        _rebind_callback(callback, package=altered_package),
    )

    with Session(pg_context.engine) as db:
        task = db.get(Task, task_id)
        items = db.scalars(select(TaskItem).where(TaskItem.task_id == task_id)).all()
        assert rejected.status == "rejected"
        assert task is not None and task.status == "failed"
        assert task.terminal_at is not None and task.expires_at is not None
        assert task.expires_at == task.terminal_at + timedelta(days=task.retention_days)
        assert all(item.status == "failed" for item in items)
        assert all(item.error_code == "PROMPT_WORKFLOW_FAILED" for item in items)
        assert (
            db.scalar(
                select(func.count(TaskPromptPackage.id)).where(TaskPromptPackage.task_id == task_id)
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(TaskPromptItem.id)).where(TaskPromptItem.task_id == task_id)
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.event_type == GENERATION_START_EVENT_TYPE,
                    OutboxEvent.aggregate_id.in_(
                        select(TaskItem.id).where(TaskItem.task_id == task_id)
                    ),
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(TaskItemAttempt.id)).where(
                    TaskItemAttempt.task_item_id.in_(
                        select(TaskItem.id).where(TaskItem.task_id == task_id)
                    )
                )
            )
            == 0
        )


def test_postgres_task_pinned_snapshot_mismatch_fails_closed_before_package_intent(
    pg_context: _PGContext,
) -> None:
    _, task_id = _create_task(pg_context, reference_count=1)
    submission_id = _create_submission(pg_context, task_id)
    adapter = create_test_prompt_workflow_adapter(
        session_factory=lambda: Session(pg_context.engine)
    )
    _process_submission_action(
        pg_context,
        task_id=task_id,
        submission_id=submission_id,
        sequence=1,
        adapter=adapter,
    )
    callback = adapter.read_prompt_result(submission_id)
    with Session(pg_context.engine) as db, db.begin():
        task = db.get(Task, task_id)
        assert task is not None
        altered = dict(task.recommendation_snapshot)
        altered["trusted_analysis_snapshot_digest"] = "0" * 64
        task.recommendation_snapshot = altered

    assert _accept_callback(pg_context, callback).status == "rejected"
    with Session(pg_context.engine) as db:
        assert (
            db.scalar(
                select(func.count(TaskPromptPackage.id)).where(TaskPromptPackage.task_id == task_id)
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.event_type == GENERATION_START_EVENT_TYPE,
                    OutboxEvent.aggregate_id.in_(
                        select(TaskItem.id).where(TaskItem.task_id == task_id)
                    ),
                )
            )
            == 0
        )


def test_postgres_stale_callback_revision_cannot_change_current_workflow(
    pg_context: _PGContext,
) -> None:
    _, task_id = _create_task(pg_context, reference_count=1)
    submission_id = _create_submission(pg_context, task_id)
    adapter = create_test_prompt_workflow_adapter(
        session_factory=lambda: Session(pg_context.engine)
    )
    _process_submission_action(
        pg_context,
        task_id=task_id,
        submission_id=submission_id,
        sequence=1,
        adapter=adapter,
    )
    callback = _rebind_callback(adapter.read_prompt_result(submission_id), workflow_revision=2)
    with Session(pg_context.engine) as db:
        workflow = db.execute(
            select(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_id)
        ).scalar_one()
        assert workflow.status == "submitted"

    with pytest.raises(PromptWorkflowUnavailable):
        _accept_callback(pg_context, callback)
    with Session(pg_context.engine) as db:
        submission = db.get(PromptSubmission, submission_id)
        workflow = db.execute(
            select(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_id)
        ).scalar_one()
        assert submission is not None and submission.callback_result_digest is None
        assert workflow.workflow_revision == 1
        assert workflow.status == "submitted"


def test_postgres_workflow_revision_row_allows_same_run_id_at_revision_two(
    pg_context: _PGContext,
) -> None:
    _, task_id = _create_task(pg_context, reference_count=1)
    _create_submission(pg_context, task_id)
    with Session(pg_context.engine) as db, db.begin():
        workflow = db.execute(
            select(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_id)
        ).scalar_one()
        second = TaskPromptWorkflow(
            task_id=task_id,
            workflow_run_id=workflow.workflow_run_id,
            workflow_revision=2,
            trusted_analysis_snapshot_id=workflow.trusted_analysis_snapshot_id,
            trusted_analysis_snapshot_digest=workflow.trusted_analysis_snapshot_digest,
            status="pending_submission",
        )
        db.add(second)
        db.flush()
        assert second.id != workflow.id
        assert second.workflow_run_id == workflow.workflow_run_id
        assert second.workflow_revision == 2


def test_postgres_composite_package_foreign_keys_reject_cross_task_link(
    pg_context: _PGContext,
) -> None:
    _, task_one_id = _create_task(pg_context, reference_count=1)
    _, task_two_id = _create_task(pg_context, reference_count=1)
    submission_id = _create_submission(pg_context, task_one_id)
    with Session(pg_context.engine) as db:
        workflow = db.execute(
            select(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_one_id)
        ).scalar_one()
        task = db.get(Task, task_one_id)
        assert task is not None
        with pytest.raises(IntegrityError):
            with Session(pg_context.engine) as insert_db, insert_db.begin():
                insert_db.add(
                    TaskPromptPackage(
                        workflow_id=workflow.id,
                        submission_id=submission_id,
                        task_id=task_two_id,
                        workflow_run_id=workflow.workflow_run_id,
                        workflow_revision=workflow.workflow_revision,
                        trusted_analysis_snapshot_id=workflow.trusted_analysis_snapshot_id,
                        trusted_analysis_snapshot_digest=workflow.trusted_analysis_snapshot_digest,
                        chatflow_contract_version="v3.3",
                        validator_version="test-validator",
                        package_digest_version=1,
                        package_digest=b"p" * 32,
                        package={"basis": {}, "images": []},
                    )
                )
                insert_db.flush()
