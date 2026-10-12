from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.generation_models import (
    GenerationAttemptContext,
    GenerationResult,
    MockGenerationInvocation,
    MockGenerationJob,
)
from app.db.models import GuestSession, User
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
from app.integrations.generation.mock import MockGenerationPort, _TestOnlyFaults
from app.integrations.generation.protocol import GenerationCommand
from app.integrations.storage.local import LocalPrivateStorage
from app.schemas.recommendations import IMAGE_TYPES
from app.schemas.task_history import TaskRetryRequest
from app.services.generation_runtime import GenerationActionRuntime
from app.services.prompt_validation import PROMPT_PACKAGE_VALIDATOR_VERSION
from app.services.tasks import TaskOptions, TaskService
from app.workers.action_processor import ActionProcessor
from app.workers.messages import OutboxMessage
from app.workers.options import QueueOptions
from sqlalchemy import delete, func, inspect, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from tests.integration.tasks.test_tasks_postgres import (
    _create_reference_and_recommendation,
    _new_guest,
    _request,
)

pytest_plugins = ("tests.integration.tasks.test_tasks_postgres",)

_REQUIRED_R6_TABLES = {
    "task_actions",
    "task_prompt_items",
    "task_prompt_packages",
    "task_prompt_workflows",
    "generation_attempt_contexts",
    "generation_results",
}


def _reference_resource_ids(
    engine: Engine,
    *,
    reference_set_id: UUID,
    recommendation_id: UUID,
) -> dict[str, object]:
    with Session(engine) as db:
        rows = db.execute(
            select(
                ReferenceSetImage.id,
                ReferenceSetImage.source_asset_id,
                ReferenceSetImage.model_asset_id,
            )
            .where(ReferenceSetImage.reference_set_id == reference_set_id)
            .order_by(ReferenceSetImage.position)
        ).all()
    if not rows:
        raise AssertionError("synthetic reference helper did not create image links")
    return {
        "reference_set_id": reference_set_id,
        "recommendation_id": recommendation_id,
        "image_link_ids": [row.id for row in rows],
        "asset_ids": [
            asset_id for row in rows for asset_id in (row.source_asset_id, row.model_asset_id)
        ],
    }


def _guest_resource_ids(engine: Engine, user_id: UUID) -> dict[str, object]:
    with Session(engine) as db:
        session_ids = list(
            db.scalars(select(GuestSession.id).where(GuestSession.user_id == user_id))
        )
    if not session_ids:
        raise AssertionError("synthetic guest helper did not create a session")
    return {"user_id": user_id, "session_ids": session_ids}


def _track_reference_resources(
    engine: Engine,
    state: dict[str, object],
    *,
    reference_set_id: UUID,
    recommendation_id: UUID,
) -> None:
    resources = state["reference_resources"]
    assert isinstance(resources, list)
    resources.append(
        _reference_resource_ids(
            engine,
            reference_set_id=reference_set_id,
            recommendation_id=recommendation_id,
        )
    )


def _track_guest_resource(engine: Engine, state: dict[str, object], user_id: UUID) -> None:
    resources = state["guest_resources"]
    assert isinstance(resources, list)
    resources.append(_guest_resource_ids(engine, user_id))


@pytest.fixture
def retry_state(pg_engine: Engine) -> Iterator[dict[str, object]]:
    if not _REQUIRED_R6_TABLES.issubset(set(inspect(pg_engine).get_table_names())):
        pytest.skip("requires the integrated PostgreSQL schema through the R5 migrations")
    client, owner_id = _new_guest(pg_engine)
    owner_resource = _guest_resource_ids(pg_engine, owner_id)
    reference_set_id, recommendation_id = _create_reference_and_recommendation(
        pg_engine, owner_user_id=owner_id
    )
    reference_resource = _reference_resource_ids(
        pg_engine,
        reference_set_id=reference_set_id,
        recommendation_id=recommendation_id,
    )
    task_result = TaskService(pg_engine, TaskOptions()).create_task(
        owner_user_id=owner_id,
        idempotency_key=f"r6-create-{uuid4().hex}",
        request=_request(
            reference_set_id,
            recommendation_id,
            selected_types=list(IMAGE_TYPES[:2]),
        ),
    )
    task_id = task_result.task_id
    with Session(pg_engine) as db:
        task = db.get(Task, task_id)
        assert task is not None
        items = (
            db.execute(
                select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.position)
            )
            .scalars()
            .all()
        )
        snapshot_id = UUID(task.recommendation_snapshot["trusted_analysis_snapshot_id"])
        snapshot_digest = bytes.fromhex(
            task.recommendation_snapshot["trusted_analysis_snapshot_digest"]
        )
        primary_asset_id = UUID(task.reference_assets_snapshot[0]["model_asset_id"])
    workflow_id, run_id, submission_id, package_id = uuid4(), uuid4(), uuid4(), uuid4()
    failure_attempts: dict[UUID, UUID] = {}
    with Session(pg_engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        task = db.get(Task, task_id)
        assert task is not None
        db.add(
            TaskPromptWorkflow(
                id=workflow_id,
                task_id=task_id,
                workflow_run_id=run_id,
                workflow_revision=1,
                trusted_analysis_snapshot_id=snapshot_id,
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
                content_digest=hashlib.sha256(b"r6 synthetic submission").digest(),
                private_payload={"synthetic": True},
                status="package_ready",
                submit_outcome="accepted",
                callback_outcome="completed",
            )
        )
        db.flush()
        package_digest = hashlib.sha256(b"r6 synthetic package").digest()
        db.add(
            TaskPromptPackage(
                id=package_id,
                workflow_id=workflow_id,
                submission_id=submission_id,
                task_id=task_id,
                workflow_run_id=run_id,
                workflow_revision=1,
                trusted_analysis_snapshot_id=snapshot_id,
                trusted_analysis_snapshot_digest=snapshot_digest,
                chatflow_contract_version="v3.3",
                validator_version=PROMPT_PACKAGE_VALIDATOR_VERSION,
                package_digest_version=1,
                package_digest=package_digest,
                package={"synthetic": True},
            )
        )
        db.flush()
        for item_snapshot in items:
            item = db.get(TaskItem, item_snapshot.id)
            assert item is not None
            prompt_item_id = uuid4()
            db.add(
                TaskPromptItem(
                    id=prompt_item_id,
                    package_id=package_id,
                    task_id=task_id,
                    task_item_id=item.id,
                    position=item.position,
                    image_type=item.image_type,
                    design_prompt="synthetic R6 design prompt",
                    identity_prompt="synthetic R6 identity prompt",
                    identity_anchor_asset_id=primary_asset_id,
                    allowed_reference_aliases=["PRIMARY"],
                    excluded_reference_aliases=[],
                    allowed_reference_asset_ids=[str(primary_asset_id)],
                )
            )
            attempt_id = uuid4()
            failure_attempts[item.id] = attempt_id
            prompt_text = "synthetic R6 design prompt\n\nsynthetic R6 identity prompt"
            command = GenerationCommand(
                request_key=attempt_id,
                image_type=item.image_type,
                prompt_package_version="v3.3",
                prompt_text=prompt_text,
                reference_asset_ids=(primary_asset_id,),
            )
            db.add(
                TaskItemAttempt(
                    id=attempt_id,
                    task_item_id=item.id,
                    attempt_no=1,
                    idempotency_key=attempt_id,
                    status="failed",
                    channel_id="mock",
                    model_id="mock-generation",
                    model_version="v3.3",
                    failure_code="GENERATION_REJECTED",
                    started_at=now - timedelta(minutes=1),
                    completed_at=now,
                )
            )
            db.flush()
            db.add(
                GenerationAttemptContext(
                    attempt_id=attempt_id,
                    task_id=task_id,
                    task_item_id=item.id,
                    owner_user_id=owner_id,
                    prompt_package_id=package_id,
                    prompt_item_id=prompt_item_id,
                    package_digest_version=1,
                    package_digest=package_digest,
                    request_digest_version=command.request_digest_version,
                    request_digest=command.request_digest,
                    prompt_package_version="v3.3",
                    image_type=item.image_type,
                    prompt_text=prompt_text,
                    reference_asset_ids=[str(primary_asset_id)],
                    excluded_reference_aliases=[],
                    original_object_key=f"generated/{attempt_id}/original",
                    preview_object_key=f"generated/{attempt_id}/preview.webp",
                    save_state="failed",
                )
            )
            event_id, action_id = uuid4(), uuid4()
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
            db.add(
                Action(
                    id=action_id,
                    task_id=task_id,
                    task_item_id=item.id,
                    attempt_id=attempt_id,
                    scope_kind="attempt",
                    scope_id=attempt_id,
                    action_kind="generation.submit.v1",
                    sequence=1,
                    due_at=now,
                    status="rejected",
                    claim_count=1,
                    input_digest_version=1,
                    input_digest=hashlib.sha256(f"action:{attempt_id}".encode()).digest(),
                    outbox_event_id=event_id,
                    last_outcome="definitely_rejected",
                    last_error_code="GENERATION_REJECTED",
                    completed_at=now,
                )
            )
            item.status = "failed"
            item.error_code = "GENERATION_REJECTED"
            item.preview_ready = False
            item.image_id = None
        task.status = "failed"
        task.terminal_at = now - timedelta(minutes=1)
        task.expires_at = now + timedelta(days=59)

    state = {
        "engine": pg_engine,
        "client": client,
        "owner_id": owner_id,
        "task_id": task_id,
        "item_ids": [item.id for item in items],
        "attempt_ids": failure_attempts,
        "extra_task_ids": [],
        "reference_resources": [reference_resource],
        "guest_resources": [owner_resource],
    }
    yield state
    client.close()
    _cleanup_task(pg_engine, task_id)
    for extra_task_id in state["extra_task_ids"]:
        _cleanup_task(pg_engine, extra_task_id)
    for resource in state["reference_resources"]:
        assert isinstance(resource, dict)
        with Session(pg_engine) as db, db.begin():
            image_link_ids = resource["image_link_ids"]
            asset_ids = resource["asset_ids"]
            if image_link_ids:
                db.execute(
                    delete(ReferenceSetImage).where(ReferenceSetImage.id.in_(image_link_ids))
                )
            db.execute(
                delete(Recommendation).where(Recommendation.id == resource["recommendation_id"])
            )
            db.execute(delete(ReferenceSet).where(ReferenceSet.id == resource["reference_set_id"]))
            if asset_ids:
                db.execute(delete(ImageAsset).where(ImageAsset.id.in_(asset_ids)))
    for resource in state["guest_resources"]:
        assert isinstance(resource, dict)
        with Session(pg_engine) as db, db.begin():
            session_ids = resource["session_ids"]
            if session_ids:
                db.execute(delete(GuestSession).where(GuestSession.id.in_(session_ids)))
            db.execute(delete(User).where(User.id == resource["user_id"]))


def _cleanup_task(engine: Engine, task_id: UUID) -> None:
    with Session(engine) as db, db.begin():
        item_ids = list(db.scalars(select(TaskItem.id).where(TaskItem.task_id == task_id)))
        result_asset_ids = list(
            db.scalars(
                select(GenerationResult.original_asset_id).where(
                    GenerationResult.task_id == task_id
                )
            )
        ) + list(
            db.scalars(
                select(GenerationResult.preview_asset_id).where(GenerationResult.task_id == task_id)
            )
        )
        event_ids = list(
            db.scalars(select(Action.outbox_event_id).where(Action.task_id == task_id))
        )
        prompt_event_ids = list(
            db.scalars(
                select(OutboxEvent.id).where(
                    OutboxEvent.event_type == "task.prompt.v1",
                    OutboxEvent.aggregate_id == task_id,
                )
            )
        )
        event_ids = list(set(event_ids + prompt_event_ids))
        if item_ids:
            db.execute(delete(GenerationResult).where(GenerationResult.task_item_id.in_(item_ids)))
        if result_asset_ids:
            db.execute(delete(ImageAsset).where(ImageAsset.id.in_(set(result_asset_ids))))
        db.execute(delete(Action).where(Action.task_id == task_id))
        if event_ids:
            db.execute(delete(OutboxReceipt).where(OutboxReceipt.event_id.in_(event_ids)))
            db.execute(delete(OutboxEvent).where(OutboxEvent.id.in_(event_ids)))
        db.execute(
            delete(GenerationAttemptContext).where(GenerationAttemptContext.task_id == task_id)
        )
        if item_ids:
            db.execute(delete(TaskItemAttempt).where(TaskItemAttempt.task_item_id.in_(item_ids)))
        db.execute(delete(TaskPromptItem).where(TaskPromptItem.task_id == task_id))
        db.execute(delete(TaskPromptPackage).where(TaskPromptPackage.task_id == task_id))
        db.execute(delete(PromptSubmission).where(PromptSubmission.task_id == task_id))
        db.execute(delete(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_id))
        db.execute(delete(TaskIdempotencyRecord).where(TaskIdempotencyRecord.task_id == task_id))
        db.execute(
            delete(TaskRecommendationConsumption).where(
                TaskRecommendationConsumption.task_id == task_id
            )
        )
        db.execute(delete(TaskItem).where(TaskItem.task_id == task_id))
        db.execute(delete(Task).where(Task.id == task_id))


def _mark_item_succeeded(
    engine: Engine,
    *,
    owner_id: UUID,
    task_id: UUID,
    item_id: UUID,
    attempt_id: UUID,
) -> tuple[UUID, UUID]:
    digest = hashlib.sha256(f"r6-success:{item_id}".encode()).hexdigest()
    original_id, preview_id = uuid4(), uuid4()
    original_key = f"r6/{original_id}/original"
    preview_key = f"r6/{preview_id}/preview.webp"
    with Session(engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        task = db.get(Task, task_id)
        item = db.get(TaskItem, item_id)
        attempt = db.get(TaskItemAttempt, attempt_id)
        context = db.get(GenerationAttemptContext, attempt_id)
        action = db.scalar(select(Action).where(Action.attempt_id == attempt_id))
        assert task is not None and item is not None and attempt is not None
        assert context is not None and action is not None
        db.add_all(
            [
                ImageAsset(
                    id=original_id,
                    owner_user_id=owner_id,
                    asset_kind="generation_original",
                    object_key=original_key,
                    content_sha256=digest,
                    mime_type="image/png",
                    size_bytes=8,
                    width=2,
                    height=2,
                ),
                ImageAsset(
                    id=preview_id,
                    owner_user_id=owner_id,
                    asset_kind="generation_preview",
                    object_key=preview_key,
                    content_sha256=digest,
                    mime_type="image/webp",
                    size_bytes=4,
                    width=2,
                    height=2,
                ),
            ]
        )
        db.flush()
        db.add(
            GenerationResult(
                attempt_id=attempt_id,
                task_id=task_id,
                task_item_id=item_id,
                owner_user_id=owner_id,
                original_asset_id=original_id,
                preview_asset_id=preview_id,
            )
        )
        attempt.status = "completed"
        attempt.failure_code = None
        attempt.completed_at = now
        context.save_state = "attached"
        context.original_size_bytes = 8
        context.original_sha256 = digest
        context.original_mime_type = "image/png"
        context.original_width = 2
        context.original_height = 2
        context.preview_size_bytes = 4
        context.preview_sha256 = digest
        context.preview_width = 2
        context.preview_height = 2
        action.status = "completed"
        action.last_outcome = "accepted"
        action.completed_at = now
        item.status = "succeeded"
        item.error_code = None
        item.image_id = original_id
        item.preview_ready = True
        item.width = 2
        item.height = 2
        task.status = "partially_succeeded"
        task.terminal_at = now
        task.expires_at = now + timedelta(days=59)
    return original_id, preview_id


def test_retry_creates_one_new_attempt_and_replays_idempotently(
    retry_state: dict[str, object],
) -> None:
    engine = retry_state["engine"]
    owner_id = retry_state["owner_id"]
    task_id = retry_state["task_id"]
    item_id = retry_state["item_ids"][0]
    successful_sibling_id = retry_state["item_ids"][1]
    assert isinstance(engine, Engine)
    assert isinstance(owner_id, UUID)
    assert isinstance(task_id, UUID)
    assert isinstance(item_id, UUID)
    assert isinstance(successful_sibling_id, UUID)
    successful_sibling_attempt_id = retry_state["attempt_ids"][successful_sibling_id]
    _sibling_original_id, _sibling_preview_id = _mark_item_succeeded(
        engine,
        owner_id=owner_id,
        task_id=task_id,
        item_id=successful_sibling_id,
        attempt_id=successful_sibling_attempt_id,
    )
    service = TaskService(engine, TaskOptions())
    request = TaskRetryRequest(failed_item_ids=[item_id])
    original_attempts = dict(retry_state["attempt_ids"])
    with Session(engine) as db:
        task = db.get(Task, task_id)
        assert task is not None
        original_billing_snapshot = dict(task.billing_snapshot)
        old_expiry = task.expires_at
        assert old_expiry is not None

    accepted = service.retry_failed_items(
        owner_user_id=owner_id,
        task_id=task_id,
        idempotency_key="r6-retry-one",
        request=request,
    )
    replay = service.retry_failed_items(
        owner_user_id=owner_id,
        task_id=task_id,
        idempotency_key="r6-retry-one",
        request=request,
    )
    assert replay == accepted
    new_attempt_id = accepted.attempts[0].attempt_id
    assert new_attempt_id != original_attempts[item_id]
    with Session(engine) as db:
        attempts = list(
            db.scalars(
                select(TaskItemAttempt)
                .where(TaskItemAttempt.task_item_id == item_id)
                .order_by(TaskItemAttempt.attempt_no)
            )
        )
        assert [attempt.attempt_no for attempt in attempts] == [1, 2]
        assert attempts[1].idempotency_key == attempts[1].id
        new_context = db.get(GenerationAttemptContext, new_attempt_id)
        assert new_context is not None and new_context.save_state == "awaiting_result"
        actions = list(db.scalars(select(Action).where(Action.attempt_id == new_attempt_id)))
        assert len(actions) == 1
        action = actions[0]
        assert action.action_kind == "generation.submit.v1"
        assert action.sequence == 1
        assert action.scope_id == new_attempt_id
        expected_action_digest = hashlib.sha256(
            json.dumps(
                {
                    "action_kind": "generation.submit.v1",
                    "attempt_id": str(new_attempt_id),
                    "request_digest": new_context.request_digest.hex(),
                    "sequence": 1,
                    "extra": {},
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).digest()
        assert action.input_digest_version == 1
        assert action.input_digest == expected_action_digest
        event = db.get(OutboxEvent, action.outbox_event_id)
        receipt = db.get(OutboxReceipt, action.outbox_event_id)
        assert event is not None and event.event_type == "task.action.v1"
        assert event.aggregate_id == action.id and event.payload == {"action_id": str(action.id)}
        assert receipt is not None and receipt.status == "pending"
        task = db.get(Task, task_id)
        item = db.get(TaskItem, item_id)
        sibling = db.get(TaskItem, successful_sibling_id)
        assert task is not None and item is not None
        assert sibling is not None
        assert task.terminal_at is None and task.expires_at is None
        assert task.status == "generating"
        assert item.status == "running" and item.error_code is None
        assert sibling.status == "succeeded"
        assert sibling.image_id == _sibling_original_id and sibling.preview_ready
        assert task.billing_mode == "disabled"
        assert task.billing_policy_version == "disabled-v1"
        assert task.billing_snapshot == original_billing_snapshot
        retry_record = db.scalar(
            select(TaskIdempotencyRecord).where(
                TaskIdempotencyRecord.task_id == task_id,
                TaskIdempotencyRecord.operation == "tasks.retry.v1",
            )
        )
        assert retry_record is not None and retry_record.planned_count == 1

    with pytest.raises(DomainError) as same_key_different_items:
        service.retry_failed_items(
            owner_user_id=owner_id,
            task_id=task_id,
            idempotency_key="r6-retry-one",
            request=TaskRetryRequest(failed_item_ids=[retry_state["item_ids"][1]]),
        )
    assert same_key_different_items.value.code == "IDEMPOTENCY_CONFLICT"


def test_retry_rejects_mixed_success_items_unknown_actions_and_expired_tasks(
    retry_state: dict[str, object],
) -> None:
    engine = retry_state["engine"]
    owner_id = retry_state["owner_id"]
    task_id = retry_state["task_id"]
    item_ids = retry_state["item_ids"]
    attempt_ids = retry_state["attempt_ids"]
    assert isinstance(engine, Engine) and isinstance(owner_id, UUID) and isinstance(task_id, UUID)
    service = TaskService(engine, TaskOptions())

    # An item can be in a reconciling state even while a stale UI might still
    # request it as failed. The current business status is authoritative.
    with Session(engine) as db, db.begin():
        reconciling_item = db.get(TaskItem, item_ids[1])
        assert reconciling_item is not None
        reconciling_item.status = "reconciling"
    with pytest.raises(DomainError) as reconciling:
        service.retry_failed_items(
            owner_user_id=owner_id,
            task_id=task_id,
            idempotency_key="r6-reconciling",
            request=TaskRetryRequest(failed_item_ids=[item_ids[1]]),
        )
    assert reconciling.value.code == "ITEMS_NOT_RETRYABLE"

    with Session(engine) as db, db.begin():
        success_item = db.get(TaskItem, item_ids[1])
        assert success_item is not None
        success_item.status = "succeeded"
    with pytest.raises(DomainError) as mixed:
        service.retry_failed_items(
            owner_user_id=owner_id,
            task_id=task_id,
            idempotency_key="r6-mixed",
            request=TaskRetryRequest(failed_item_ids=[item_ids[0], item_ids[1]]),
        )
    assert mixed.value.code == "ITEMS_NOT_RETRYABLE"
    with Session(engine) as db:
        assert (
            db.scalar(
                select(func.count(TaskItemAttempt.id)).where(
                    TaskItemAttempt.task_item_id == item_ids[0]
                )
            )
            == 1
        )
        task = db.get(Task, task_id)
        assert task is not None and task.expires_at is not None

    # The last Action is the durable evidence that the attempt ended in a known
    # failure; an unknown last Action cannot authorize a new generation.
    with Session(engine) as db, db.begin():
        db.execute(
            Action.__table__.update()
            .where(Action.attempt_id == attempt_ids[item_ids[0]])
            .values(status="unknown", last_outcome="unknown")
        )
    with pytest.raises(DomainError) as unknown_action:
        service.retry_failed_items(
            owner_user_id=owner_id,
            task_id=task_id,
            idempotency_key="r6-unknown-action",
            request=TaskRetryRequest(failed_item_ids=[item_ids[0]]),
        )
    assert unknown_action.value.code == "ITEMS_NOT_RETRYABLE"

    with Session(engine) as db, db.begin():
        task = db.get(Task, task_id)
        assert task is not None
        task.expires_at = db.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
    with pytest.raises(DomainError) as expired:
        service.retry_failed_items(
            owner_user_id=owner_id,
            task_id=task_id,
            idempotency_key="r6-expired",
            request=TaskRetryRequest(failed_item_ids=[item_ids[0]]),
        )
    assert expired.value.code == "TASK_EXPIRED"


def test_retry_validates_entire_prompt_batch_before_writing_or_extending_ttl(
    retry_state: dict[str, object],
) -> None:
    engine = retry_state["engine"]
    owner_id = retry_state["owner_id"]
    task_id = retry_state["task_id"]
    item_ids = retry_state["item_ids"]
    assert isinstance(engine, Engine) and isinstance(owner_id, UUID) and isinstance(task_id, UUID)
    with Session(engine) as db, db.begin():
        invalid_prompt = db.scalar(
            select(TaskPromptItem).where(TaskPromptItem.task_item_id == item_ids[1])
        )
        task = db.get(Task, task_id)
        assert invalid_prompt is not None and task is not None
        invalid_prompt.design_prompt = "   "
        original_terminal_at = task.terminal_at
        original_expires_at = task.expires_at

    with pytest.raises(DomainError) as invalid:
        TaskService(engine, TaskOptions()).retry_failed_items(
            owner_user_id=owner_id,
            task_id=task_id,
            idempotency_key="r6-invalid-prompt-batch",
            request=TaskRetryRequest(failed_item_ids=item_ids),
        )
    assert invalid.value.code == "PROMPT_NOT_RETRYABLE"
    with Session(engine) as db:
        task = db.get(Task, task_id)
        assert task is not None
        assert task.terminal_at == original_terminal_at
        assert task.expires_at == original_expires_at
        assert task.status == "failed"
        for item_id in item_ids:
            item = db.get(TaskItem, item_id)
            assert item is not None and item.status == "failed"
            assert (
                db.scalar(
                    select(func.count(TaskItemAttempt.id)).where(
                        TaskItemAttempt.task_item_id == item_id
                    )
                )
                == 1
            )
        assert (
            db.scalar(
                select(func.count(TaskIdempotencyRecord.key_hash)).where(
                    TaskIdempotencyRecord.task_id == task_id,
                    TaskIdempotencyRecord.operation == "tasks.retry.v1",
                )
            )
            == 0
        )


def test_detail_and_retry_are_hidden_from_other_guest_and_expired_detail_is_gone(
    retry_state: dict[str, object],
) -> None:
    engine = retry_state["engine"]
    owner_id = retry_state["owner_id"]
    task_id = retry_state["task_id"]
    item_id = retry_state["item_ids"][0]
    assert isinstance(engine, Engine) and isinstance(owner_id, UUID) and isinstance(task_id, UUID)
    client, other_owner = _new_guest(engine)
    _track_guest_resource(engine, retry_state, other_owner)
    try:
        service = TaskService(engine, TaskOptions())
        with pytest.raises(DomainError) as detail:
            service.get_task(owner_user_id=other_owner, task_id=task_id)
        assert detail.value.code == "TASK_NOT_FOUND"
        with pytest.raises(DomainError) as retry:
            service.retry_failed_items(
                owner_user_id=other_owner,
                task_id=task_id,
                idempotency_key="r6-cross-owner",
                request=TaskRetryRequest(failed_item_ids=[item_id]),
            )
        assert retry.value.code == "TASK_NOT_FOUND"

        with Session(engine) as db, db.begin():
            now = db.scalar(select(func.clock_timestamp()))
            task = db.get(Task, task_id)
            assert task is not None
            task.expires_at = now - timedelta(microseconds=1)
        with pytest.raises(DomainError) as expired:
            service.get_task(owner_user_id=owner_id, task_id=task_id)
        assert expired.value.code == "TASK_EXPIRED"
    finally:
        client.close()


def test_history_cursor_keeps_tied_timestamps_owner_scoped_and_unexpired(
    retry_state: dict[str, object],
) -> None:
    engine = retry_state["engine"]
    owner_id = retry_state["owner_id"]
    assert isinstance(engine, Engine) and isinstance(owner_id, UUID)
    task_ids = [retry_state["task_id"]]
    for index in range(2):
        reference_set_id, recommendation_id = _create_reference_and_recommendation(
            engine, owner_user_id=owner_id
        )
        _track_reference_resources(
            engine,
            retry_state,
            reference_set_id=reference_set_id,
            recommendation_id=recommendation_id,
        )
        result = TaskService(engine, TaskOptions()).create_task(
            owner_user_id=owner_id,
            idempotency_key=f"r6-history-{uuid4().hex}",
            request=_request(
                reference_set_id,
                recommendation_id,
                selected_types=[IMAGE_TYPES[index]],
            ),
        )
        task_ids.append(result.task_id)
        retry_state["extra_task_ids"].append(result.task_id)
        with Session(engine) as db, db.begin():
            now = db.scalar(select(func.clock_timestamp()))
            db.execute(
                update(Task)
                .where(Task.id == result.task_id)
                .values(
                    status="failed",
                    terminal_at=now,
                    expires_at=now + timedelta(days=59),
                )
            )
            db.execute(
                update(TaskItem).where(TaskItem.task_id == result.task_id).values(status="failed")
            )

    with Session(engine) as db, db.begin():
        tie_time = db.scalar(select(func.clock_timestamp()))
        db.execute(
            update(Task)
            .where(Task.id.in_(task_ids))
            .values(
                created_at=tie_time,
                terminal_at=tie_time,
                expires_at=tie_time + timedelta(days=59),
            )
        )
        db.execute(
            update(Task)
            .where(Task.id == task_ids[1])
            .values(expires_at=tie_time - timedelta(seconds=1))
        )

    client, other_owner = _new_guest(engine)
    _track_guest_resource(engine, retry_state, other_owner)
    try:
        reference_set_id, recommendation_id = _create_reference_and_recommendation(
            engine, owner_user_id=other_owner
        )
        _track_reference_resources(
            engine,
            retry_state,
            reference_set_id=reference_set_id,
            recommendation_id=recommendation_id,
        )
        other_task = TaskService(engine, TaskOptions()).create_task(
            owner_user_id=other_owner,
            idempotency_key=f"r6-other-owner-{uuid4().hex}",
            request=_request(reference_set_id, recommendation_id, selected_types=[IMAGE_TYPES[0]]),
        )
        retry_state["extra_task_ids"].append(other_task.task_id)
        with Session(engine) as db, db.begin():
            db.execute(
                update(Task).where(Task.id == other_task.task_id).values(created_at=tie_time)
            )
    finally:
        client.close()

    service = TaskService(engine, TaskOptions())
    expected_visible_ids = {task_ids[0], task_ids[2]}
    with Session(engine) as db:
        query_now = db.scalar(select(func.clock_timestamp()))
        persisted = db.execute(
            select(Task.id, Task.owner_user_id, Task.status, Task.expires_at).where(
                Task.id.in_(task_ids)
            )
        ).all()
    assert {row.id for row in persisted} == set(task_ids), persisted
    assert all(row.owner_user_id == owner_id and row.status == "failed" for row in persisted)
    persisted_by_id = {row.id: row for row in persisted}
    assert persisted_by_id[task_ids[1]].expires_at <= query_now
    assert persisted_by_id[task_ids[0]].expires_at > query_now
    assert persisted_by_id[task_ids[2]].expires_at > query_now
    visible_page = service.list_tasks(owner_user_id=owner_id, limit=50)
    assert {entry.task_id for entry in visible_page.items} == expected_visible_ids
    first_page = service.list_tasks(owner_user_id=owner_id, limit=1)
    assert first_page.next_cursor is not None
    second_page = service.list_tasks(
        owner_user_id=owner_id,
        limit=1,
        cursor=first_page.next_cursor,
    )
    assert second_page.next_cursor is None
    returned_ids = [first_page.items[0].task_id, second_page.items[0].task_id]
    assert returned_ids == sorted(expected_visible_ids, key=str, reverse=True)
    assert len(set(returned_ids)) == 2
    assert all(task_id != other_task.task_id for task_id in returned_ids)


def test_history_counts_project_reconciling_into_unresolved(
    retry_state: dict[str, object],
) -> None:
    engine = retry_state["engine"]
    owner_id = retry_state["owner_id"]
    task_id = retry_state["task_id"]
    item_ids = retry_state["item_ids"]
    assert isinstance(engine, Engine) and isinstance(owner_id, UUID) and isinstance(task_id, UUID)
    with Session(engine) as db, db.begin():
        task = db.get(Task, task_id)
        first = db.get(TaskItem, item_ids[0])
        second = db.get(TaskItem, item_ids[1])
        assert task is not None and first is not None and second is not None
        task.status = "reconciling"
        first.status = "reconciling"
        second.status = "failed"

    entry = (
        TaskService(engine, TaskOptions())
        .list_tasks(
            owner_user_id=owner_id,
            limit=50,
        )
        .items[0]
    )
    assert entry.task_id == task_id
    assert entry.counts.model_dump() == {
        "planned": 2,
        "succeeded": 0,
        "failed": 1,
        "pending": 0,
        "unresolved": 1,
    }
    assert (
        sum(
            entry.counts.model_dump()[key]
            for key in ("succeeded", "failed", "pending", "unresolved")
        )
        == entry.counts.planned
    )


def test_preview_rejects_unrelated_assets_and_opens_private_success_preview(
    retry_state: dict[str, object],
    tmp_path,
) -> None:
    engine = retry_state["engine"]
    owner_id = retry_state["owner_id"]
    task_id = retry_state["task_id"]
    item_id = retry_state["item_ids"][0]
    attempt_id = retry_state["attempt_ids"][item_id]
    assert isinstance(engine, Engine) and isinstance(owner_id, UUID) and isinstance(task_id, UUID)
    digest = hashlib.sha256(b"r6-private-preview").hexdigest()
    original_id, preview_id = uuid4(), uuid4()
    original_key = f"r6/{original_id}/original"
    preview_key = f"r6/{preview_id}/preview.webp"
    now: datetime
    with Session(engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        item = db.get(TaskItem, item_id)
        attempt = db.get(TaskItemAttempt, attempt_id)
        context = db.get(GenerationAttemptContext, attempt_id)
        task = db.get(Task, task_id)
        assert item is not None and attempt is not None and context is not None and task is not None
        db.add_all(
            [
                ImageAsset(
                    id=original_id,
                    owner_user_id=owner_id,
                    asset_kind="generation_original",
                    object_key=original_key,
                    content_sha256=digest,
                    mime_type="image/png",
                    size_bytes=8,
                    width=2,
                    height=2,
                ),
                ImageAsset(
                    id=preview_id,
                    owner_user_id=owner_id,
                    asset_kind="generation_preview",
                    object_key=preview_key,
                    content_sha256=digest,
                    mime_type="image/webp",
                    size_bytes=4,
                    width=2,
                    height=2,
                ),
            ]
        )
        db.flush()
        db.add(
            GenerationResult(
                attempt_id=attempt_id,
                task_id=task_id,
                task_item_id=item_id,
                owner_user_id=owner_id,
                original_asset_id=original_id,
                preview_asset_id=preview_id,
            )
        )
        attempt.status = "completed"
        attempt.failure_code = None
        attempt.completed_at = now
        context.save_state = "attached"
        context.original_size_bytes = 8
        context.original_sha256 = digest
        context.original_mime_type = "image/png"
        context.original_width = 2
        context.original_height = 2
        context.preview_size_bytes = 4
        context.preview_sha256 = digest
        context.preview_width = 2
        context.preview_height = 2
        item.status = "succeeded"
        item.error_code = None
        item.image_id = original_id
        item.preview_ready = True
        item.width = 2
        item.height = 2
        task.status = "partially_succeeded"
        task.terminal_at = now
        task.expires_at = now + timedelta(days=59)

    storage = LocalPrivateStorage(tmp_path / "private-preview")
    from io import BytesIO

    storage.put_immutable(original_key, BytesIO(b"original"), {"kind": "test"})
    storage.put_immutable(preview_key, BytesIO(b"WEBP"), {"kind": "test"})
    service = TaskService(engine, TaskOptions())
    with pytest.raises(DomainError) as unrelated:
        service.open_preview(
            owner_user_id=owner_id,
            task_id=task_id,
            image_id=uuid4(),
            storage=storage,
        )
    assert unrelated.value.code == "PREVIEW_NOT_FOUND"

    with Session(engine) as db:
        task = db.get(Task, task_id)
        assert task is not None
        unrelated_reference_id = UUID(task.reference_assets_snapshot[0]["model_asset_id"])
    with pytest.raises(DomainError) as non_delivery_asset:
        service.open_preview(
            owner_user_id=owner_id,
            task_id=task_id,
            image_id=unrelated_reference_id,
            storage=storage,
        )
    assert non_delivery_asset.value.code == "PREVIEW_NOT_FOUND"

    client, other_owner = _new_guest(engine)
    _track_guest_resource(engine, retry_state, other_owner)
    try:
        with pytest.raises(DomainError) as cross_owner:
            service.open_preview(
                owner_user_id=other_owner,
                task_id=task_id,
                image_id=original_id,
                storage=storage,
            )
        assert cross_owner.value.code == "PREVIEW_NOT_FOUND"
    finally:
        client.close()

    opened = service.open_preview(
        owner_user_id=owner_id,
        task_id=task_id,
        image_id=original_id,
        storage=storage,
    )
    try:
        assert opened.mime_type == "image/webp"
        assert opened.size_bytes == 4
        assert opened.body.read() == b"WEBP"
    finally:
        opened.body.close()
    with Session(engine) as db, db.begin():
        expired_at = db.scalar(select(func.clock_timestamp())) - timedelta(microseconds=1)
        task = db.get(Task, task_id)
        assert task is not None
        task.expires_at = expired_at
    with pytest.raises(DomainError) as expired_preview:
        service.open_preview(
            owner_user_id=owner_id,
            task_id=task_id,
            image_id=original_id,
            storage=storage,
        )
    assert expired_preview.value.code == "PREVIEW_NOT_FOUND"


def _action_message(engine: Engine, action_id: UUID):
    with Session(engine) as db:
        action = db.get(Action, action_id)
        event_row = db.get(OutboxEvent, action.outbox_event_id)
        return OutboxMessage(
            event_id=event_row.id,
            aggregate_id=event_row.aggregate_id,
            operation_version=event_row.operation_version,
            schema_version=event_row.schema_version,
            payload={key: UUID(value) for key, value in event_row.payload.items()},
        ).model_dump(mode="json")


def _make_due(engine: Engine, action_id: UUID):
    with Session(engine) as db, db.begin():
        action = db.get(Action, action_id)
        now = db.scalar(select(func.clock_timestamp()))
        action.due_at = now
        db.get(OutboxEvent, action.outbox_event_id).available_at = now
        db.get(OutboxReceipt, action.outbox_event_id).available_at = now


@pytest.fixture
def recovered_failure_state(retry_state, tmp_path):
    """Execute original submit -> recover -> definite-failed query against PG."""
    engine = retry_state["engine"]
    item_id = retry_state["item_ids"][0]
    attempt_id = retry_state["attempt_ids"][item_id]
    sibling_id = retry_state["item_ids"][1]
    _mark_item_succeeded(
        engine,
        owner_id=retry_state["owner_id"],
        task_id=retry_state["task_id"],
        item_id=sibling_id,
        attempt_id=retry_state["attempt_ids"][sibling_id],
    )
    with Session(engine) as db, db.begin():
        task = db.get(Task, retry_state["task_id"])
        task.status, task.terminal_at, task.expires_at = "queued", None, None
        item = db.get(TaskItem, item_id)
        item.status, item.error_code = "queued", None
        attempt = db.get(TaskItemAttempt, attempt_id)
        attempt.status, attempt.failure_code, attempt.completed_at = "prepared", None, None
        context = db.get(GenerationAttemptContext, attempt_id)
        context.save_state = "awaiting_result"
        submit = db.scalars(select(Action).where(Action.attempt_id == attempt_id)).one()
        submit.status, submit.last_outcome, submit.last_error_code = "scheduled", None, None
        submit.completed_at, submit.claim_count = None, 0
        receipt = db.get(OutboxReceipt, submit.outbox_event_id)
        receipt.status, receipt.completed_at = "pending", None
        submit_id = submit.id
    sessions = sessionmaker(bind=engine, class_=Session)
    port = MockGenerationPort(
        sessions, _test_faults=_TestOnlyFaults(accepted_response_lost_once=True)
    )
    processor = ActionProcessor(
        engine,
        QueueOptions(broker_url=os.environ["IMAGE_BACKEND_TEST_BROKER_URL"]),
        GenerationActionRuntime(
            port, LocalPrivateStorage(tmp_path / "stacked-private")
        ).registrations(),
    )
    try:
        _make_due(engine, submit_id)
        assert processor.process_payload(_action_message(engine, submit_id)).status == "unknown"
        with Session(engine) as db:
            recovery_id = db.scalars(
                select(Action.id).where(
                    Action.attempt_id == attempt_id, Action.action_kind == "generation.recover.v1"
                )
            ).one()
        _make_due(engine, recovery_id)
        assert processor.process_payload(_action_message(engine, recovery_id)).status == "completed"
        with Session(engine) as db, db.begin():
            assert db.get(Action, recovery_id).last_outcome == "accepted"
            job = db.scalars(
                select(MockGenerationJob).where(MockGenerationJob.request_key == attempt_id)
            ).one()
            job.status = "failed"
            query_id = db.scalars(
                select(Action.id).where(
                    Action.attempt_id == attempt_id, Action.action_kind == "generation.query.v1"
                )
            ).one()
        _make_due(engine, query_id)
        assert processor.process_payload(_action_message(engine, query_id)).status == "rejected"
        with Session(engine) as db:
            assert db.get(Task, retry_state["task_id"]).status == "partially_succeeded"
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(MockGenerationInvocation)
                    .where(MockGenerationInvocation.request_key == attempt_id)
                )
                == 1
            )
        retry_state["stacked_submit_id"] = submit_id
        retry_state["stacked_recovery_id"] = recovery_id
        retry_state["stacked_query_id"] = query_id
        retry_state["stacked_processor"] = processor
        yield retry_state
    finally:
        with Session(engine) as db, db.begin():
            keys = (
                select(TaskItemAttempt.id)
                .join(TaskItem, TaskItem.id == TaskItemAttempt.task_item_id)
                .where(TaskItem.task_id == retry_state["task_id"])
            )
            db.execute(
                delete(MockGenerationInvocation).where(
                    MockGenerationInvocation.request_key.in_(keys)
                )
            )
            db.execute(delete(MockGenerationJob).where(MockGenerationJob.request_key.in_(keys)))


def _retry_counts(state):
    engine, item_id = state["engine"], state["item_ids"][0]
    with Session(engine) as db:
        return (
            db.scalar(
                select(func.count())
                .select_from(TaskItemAttempt)
                .where(TaskItemAttempt.task_item_id == item_id)
            ),
            db.scalar(
                select(func.count())
                .select_from(TaskIdempotencyRecord)
                .where(
                    TaskIdempotencyRecord.task_id == state["task_id"],
                    TaskIdempotencyRecord.operation == "tasks.retry.v1",
                )
            ),
            db.scalar(
                select(func.count()).select_from(Action).where(Action.task_id == state["task_id"])
            ),
        )


def _retry_recovered(state, key):
    return TaskService(state["engine"], TaskOptions()).retry_failed_items(
        owner_user_id=state["owner_id"],
        task_id=state["task_id"],
        idempotency_key=key,
        request=TaskRetryRequest(failed_item_ids=[state["item_ids"][0]]),
    )


def test_recovered_then_failed_retry_preserves_unknown_audit_and_success_sibling(
    recovered_failure_state,
):
    state = recovered_failure_state
    with Session(state["engine"]) as db:
        original = db.get(Action, state["stacked_submit_id"])
        audit = (
            original.status,
            original.last_outcome,
            original.last_error_code,
            original.completed_at,
            original.input_digest,
        )
        sibling = db.get(TaskItem, state["item_ids"][1])
        sibling_image = sibling.image_id
    accepted = _retry_recovered(state, "resolved-retry")
    assert _retry_recovered(state, "resolved-retry") == accepted
    assert accepted.attempts[0].attempt_id != state["attempt_ids"][state["item_ids"][0]]
    assert _retry_counts(state) == (2, 1, 5)  # Three historical steps, one sibling, one retry.
    with Session(state["engine"]) as db:
        original = db.get(Action, state["stacked_submit_id"])
        assert audit == (
            original.status,
            original.last_outcome,
            original.last_error_code,
            original.completed_at,
            original.input_digest,
        )
        sibling = db.get(TaskItem, state["item_ids"][1])
        assert sibling.status == "succeeded" and sibling.image_id == sibling_image
    # An old/duplicate outbox delivery cannot replay the original provider submit.
    assert (
        state["stacked_processor"]
        .process_payload(_action_message(state["engine"], state["stacked_submit_id"]))
        .status
        == "completed"
    )
    with Session(state["engine"]) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(MockGenerationInvocation)
                .where(
                    MockGenerationInvocation.request_key
                    == state["attempt_ids"][state["item_ids"][0]]
                )
            )
            == 1
        )


@pytest.mark.parametrize("same_key", [True, False])
def test_recovered_failure_concurrent_retry_creates_only_one_attempt(
    recovered_failure_state, same_key
):
    state = recovered_failure_state

    def call(index):
        try:
            return _retry_recovered(state, "concurrent-same" if same_key else f"concurrent-{index}")
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(call, range(2)))
    accepted = [value for value in replies if not isinstance(value, str)]
    assert len(accepted) == (2 if same_key else 1)
    if same_key:
        assert accepted[0] == accepted[1]
    else:
        assert "ITEMS_NOT_RETRYABLE" in replies
    assert _retry_counts(state) == (2, 1, 5)


def test_recovered_retry_insert_failure_rolls_back_whole_transaction(
    recovered_failure_state, monkeypatch
):
    import app.services.tasks as tasks_module

    state = recovered_failure_state
    before = _retry_counts(state)
    with Session(state["engine"]) as db:
        old_expiry = db.get(Task, state["task_id"]).expires_at

    def fail_schedule(*args, **kwargs):
        raise SQLAlchemyError("injected retry outbox write failure")

    with monkeypatch.context() as patch:
        patch.setattr(tasks_module, "schedule_action_in_transaction", fail_schedule)
        with pytest.raises(DomainError) as error:
            _retry_recovered(state, "rollback-key")
        assert error.value.code == "DATABASE_UNAVAILABLE"
    assert _retry_counts(state) == before
    with Session(state["engine"]) as db:
        assert db.get(Task, state["task_id"]).expires_at == old_expiry
        assert db.get(TaskItem, state["item_ids"][0]).status == "failed"
    _retry_recovered(state, "rollback-key")  # Rolled-back key was not consumed.
    assert _retry_counts(state) == (2, 1, 5)
