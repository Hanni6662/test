from __future__ import annotations

import hashlib
import io
import json
import os
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from app.db.action_models import Action
from app.db.generation_models import (
    GenerationAttemptContext,
    GenerationResult,
    MockGenerationInvocation,
    MockGenerationJob,
)
from app.db.outbox_models import OutboxEvent
from app.db.prompt_models import MockPromptSubmission, PromptSubmission
from app.db.reference_models import ImageAsset
from app.db.retention_models import CleanupRun
from app.db.task_models import (
    Task,
    TaskIdempotencyRecord,
    TaskItem,
    TaskItemAttempt,
    TaskRecommendationConsumption,
)
from app.integrations.dify.mock import MockPromptWorkflowAdapter
from app.integrations.storage.local import LocalPrivateStorage
from app.schemas.prompt_preferences import PromptCallbackRequest
from app.schemas.prompt_workflow import PromptCallback
from app.schemas.recommendations import IMAGE_TYPES
from app.services.prompt_workflows import PROMPT_MOCK_RESULT_QUERY_ACTION_KIND
from kombu import Connection
from kombu.exceptions import ChannelError
from PIL import Image
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

_TASK_PROMPT_EVENT = "task.prompt.v1"
_TERMINAL_TASK_STATES = {"succeeded", "partially_succeeded", "failed"}


def _configured_values() -> tuple[str, str, str, str] | None:
    values = (
        os.getenv("IMAGE_BACKEND_TEST_API_URL"),
        os.getenv("IMAGE_BACKEND_TEST_API_ORIGIN"),
        os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL"),
        os.getenv("IMAGE_BACKEND_TEST_BROKER_URL"),
    )
    return values if all(values) else None


def _queue_state(broker_url: str, queue_name: str) -> tuple[int, int] | None:
    with Connection(broker_url, connect_timeout=5) as connection:
        channel = connection.channel()
        try:
            try:
                state = channel.queue_declare(queue=queue_name, passive=True)
            except ChannelError as exc:
                if getattr(exc, "reply_code", None) == 404:
                    # AMQP passive declare does not create a missing queue.
                    return None
                raise
            return state.message_count, state.consumer_count
        finally:
            channel.close()


def _image_bytes(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (48, 36), color).save(output, format="PNG")
    return output.getvalue()


def _task_payload(
    reference_set_id: UUID,
    recommendation_id: UUID,
    image_types: list[str],
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "reference_set_id": str(reference_set_id),
        "recommendation_id": str(recommendation_id),
        "recommendation_revision": 1,
        "preferences": {
            "language": {"mode": "A"},
            "platform": "TEMU",
            "usage_scene": {"mode": "A"},
            "visual_style": {"mode": "A"},
            "selling_points": {"mode": "A"},
        },
        "image_types": image_types,
    }


def _enter_guest(client: httpx.Client, origin: str) -> UUID:
    response = client.post("/api/v1/auth/guest", headers={"Origin": origin})
    assert response.status_code == 200, response.text
    return UUID(response.json()["user_id"])


def _create_reference_set(client: httpx.Client, origin: str) -> UUID:
    response = client.post(
        "/api/v1/reference-sets",
        headers={"Origin": origin},
        files=[
            ("product_category", (None, "测试商品")),
            ("primary_product_image", ("primary.png", _image_bytes((40, 100, 180)), "image/png")),
            (
                "supplementary_product_images",
                ("detail.png", _image_bytes((180, 100, 40)), "image/png"),
            ),
        ],
        timeout=60,
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["reference_set_id"])


def _create_ready_recommendation(
    client: httpx.Client,
    origin: str,
    reference_set_id: UUID,
) -> UUID:
    accepted = client.post(
        "/api/v1/recommendations",
        headers={"Origin": origin},
        json={"reference_set_id": str(reference_set_id)},
    )
    assert accepted.status_code == 202, accepted.text
    recommendation_id = UUID(accepted.json()["recommendation_id"])
    deadline = time.monotonic() + 90
    latest: dict[str, object] | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/recommendations/{recommendation_id}")
        response.raise_for_status()
        latest = response.json()
        if latest["status"] != "analyzing":
            break
        time.sleep(0.2)
    assert latest is not None and latest["status"] == "ready", latest
    return recommendation_id


def _create_task(
    client: httpx.Client,
    origin: str,
    reference_set_id: UUID,
    *,
    image_types: list[str],
    idempotency_key: str,
) -> tuple[UUID, dict[str, object]]:
    payload = _task_payload(
        reference_set_id,
        _create_ready_recommendation(client, origin, reference_set_id),
        image_types,
    )
    response = client.post(
        "/api/v1/tasks",
        headers={"Origin": origin, "Idempotency-Key": idempotency_key},
        json=payload,
    )
    assert response.status_code == 202, response.text
    accepted = response.json()
    assert accepted["planned_count"] == len(image_types)
    return UUID(accepted["task_id"]), payload


def _wait_for_task(
    client: httpx.Client,
    task_id: UUID,
    *,
    predicate: Callable[[dict[str, object]], bool] | None = None,
    callback_pump: Callable[[], None] | None = None,
    timeout_seconds: int = 180,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, object] | None = None
    while time.monotonic() < deadline:
        if callback_pump is not None:
            callback_pump()
        response = client.get(f"/api/v1/tasks/{task_id}")
        response.raise_for_status()
        latest = response.json()
        if predicate is not None:
            if predicate(latest):
                return latest
        elif latest["status"] in _TERMINAL_TASK_STATES:
            return latest
        time.sleep(0.25)
    raise AssertionError(f"task did not reach the expected state; last={latest}")


def _prompt_callback_wire_payload(callback: PromptCallback) -> dict[str, object]:
    values = callback.model_dump(mode="python")
    values["phase"] = "initial_preferences"
    values["submission_content_digest"] = callback.submission_content_digest.hex()
    values["result_digest"] = callback.result_digest.hex()
    return PromptCallbackRequest.model_validate(values, strict=True).model_dump(mode="json")


def _pump_mock_prompt_callbacks(
    client: httpx.Client,
    engine: Engine,
    task_id: UUID,
    callback_secret: str,
    *,
    delivered_submission_ids: set[UUID] | None = None,
) -> int:
    """Deliver durable mock results through the real authenticated callback HTTP route."""
    if len(callback_secret) < 32 or not callback_secret.isascii():
        raise AssertionError("the isolated mock callback secret must be at least 32 ASCII chars")

    with Session(engine) as db:
        submission_ids = list(
            db.scalars(
                select(PromptSubmission.submission_id)
                .join(
                    MockPromptSubmission,
                    MockPromptSubmission.submission_id == PromptSubmission.submission_id,
                )
                .where(
                    PromptSubmission.task_id == task_id,
                    PromptSubmission.phase == "initial_preferences",
                    PromptSubmission.status.in_(
                        {"accepted", "callback_received", "package_ready", "failed"}
                    ),
                    MockPromptSubmission.outcome == "accepted",
                    *(
                        [PromptSubmission.submission_id.not_in(delivered_submission_ids)]
                        if delivered_submission_ids
                        else []
                    ),
                )
                .order_by(PromptSubmission.created_at, PromptSubmission.submission_id)
            )
        )

    adapter = MockPromptWorkflowAdapter(lambda: Session(engine))
    delivered = 0
    for submission_id in submission_ids:
        callback = adapter.read_prompt_result(submission_id)
        if callback.task_id != task_id or callback.submission_id != submission_id:
            raise AssertionError("persisted mock callback identity does not match its task")
        response = client.post(
            "/internal/v1/dify/prompt-results",
            headers={"Authorization": f"Bearer {callback_secret}"},
            json=_prompt_callback_wire_payload(callback),
            timeout=30,
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] in {"accepted", "replayed"}, response.text
        if delivered_submission_ids is not None:
            delivered_submission_ids.add(submission_id)
        delivered += 1
    return delivered


def _fail_first_pending_mock_job(
    engine: Engine,
    task_id: UUID,
    *,
    callback_pump: Callable[[], None] | None = None,
) -> UUID:
    """Make one accepted mock job fail before its first five-second poll becomes due."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if callback_pump is not None:
            callback_pump()
        with Session(engine) as db, db.begin():
            row = db.execute(
                select(TaskItem.id, MockGenerationJob.id)
                .join(TaskItemAttempt, TaskItemAttempt.task_item_id == TaskItem.id)
                .join(MockGenerationJob, MockGenerationJob.request_key == TaskItemAttempt.id)
                .where(TaskItem.task_id == task_id, MockGenerationJob.status == "pending")
                .order_by(TaskItem.position, TaskItem.id)
                .with_for_update(of=MockGenerationJob, skip_locked=True)
                .limit(1)
            ).one_or_none()
            if row is not None:
                item_id, job_id = row
                updated = db.execute(
                    update(MockGenerationJob)
                    .where(MockGenerationJob.id == job_id, MockGenerationJob.status == "pending")
                    .values(status="failed")
                )
                if updated.rowcount == 1:
                    return item_id
        time.sleep(0.05)
    raise AssertionError(
        "no pending mock generation job was available for partial-failure injection"
    )


def _attempt_ids(engine: Engine, task_ids: list[UUID]) -> list[UUID]:
    with Session(engine) as db:
        return list(
            db.scalars(
                select(TaskItemAttempt.id)
                .join(TaskItem, TaskItem.id == TaskItemAttempt.task_item_id)
                .where(TaskItem.task_id.in_(task_ids))
                .order_by(TaskItemAttempt.created_at, TaskItemAttempt.id)
            )
        )


def _assert_one_persisted_submit_per_attempt(engine: Engine, attempt_ids: list[UUID]) -> None:
    with Session(engine) as db:
        for attempt_id in attempt_ids:
            assert (
                db.scalar(
                    select(func.count(MockGenerationJob.id)).where(
                        MockGenerationJob.request_key == attempt_id
                    )
                )
                == 1
            )
            assert (
                db.scalar(
                    select(func.count(MockGenerationInvocation.id)).where(
                        MockGenerationInvocation.request_key == attempt_id,
                        MockGenerationInvocation.outcome == "accepted",
                    )
                )
                == 1
            )


def _assert_prompt_callbacks_advanced(engine: Engine, task_ids: list[UUID]) -> None:
    with Session(engine) as db:
        submissions = list(
            db.scalars(
                select(PromptSubmission).where(
                    PromptSubmission.task_id.in_(task_ids),
                    PromptSubmission.phase == "initial_preferences",
                )
            )
        )
    assert len(submissions) == len(task_ids)
    assert all(
        submission.status == "package_ready" and submission.callback_result_digest is not None
        for submission in submissions
    )


def _expire_tasks(engine: Engine, task_ids: list[UUID]) -> None:
    with Session(engine) as db, db.begin():
        db.execute(
            update(Task)
            .where(Task.id.in_(task_ids))
            .values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )


def _owner_task_side_effect_counts(engine: Engine, owner_id: UUID) -> tuple[int, int, int, int]:
    owner_task_ids = select(Task.id).where(Task.owner_user_id == owner_id)
    with Session(engine) as db:
        return (
            int(db.scalar(select(func.count(Task.id)).where(Task.owner_user_id == owner_id)) or 0),
            int(
                db.scalar(
                    select(func.count(OutboxEvent.id)).where(
                        OutboxEvent.event_type == _TASK_PROMPT_EVENT,
                        OutboxEvent.aggregate_id.in_(owner_task_ids),
                    )
                )
                or 0
            ),
            int(
                db.scalar(
                    select(func.count(TaskIdempotencyRecord.id)).where(
                        TaskIdempotencyRecord.owner_user_id == owner_id
                    )
                )
                or 0
            ),
            int(
                db.scalar(
                    select(func.count(TaskRecommendationConsumption.recommendation_id)).where(
                        TaskRecommendationConsumption.owner_user_id == owner_id
                    )
                )
                or 0
            ),
        )


def _download_side_effect_snapshot(engine: Engine, task_id: UUID) -> tuple[object, int, int, int]:
    with Session(engine) as db:
        task = db.get(Task, task_id)
        assert task is not None
        attempt_ids = (
            select(TaskItemAttempt.id)
            .join(TaskItem, TaskItemAttempt.task_item_id == TaskItem.id)
            .where(TaskItem.task_id == task_id)
        )
        attempt_count = db.scalar(
            select(func.count(TaskItemAttempt.id))
            .join(TaskItem, TaskItemAttempt.task_item_id == TaskItem.id)
            .where(TaskItem.task_id == task_id)
        )
        action_count = db.scalar(
            select(func.count(Action.id)).where(Action.attempt_id.in_(attempt_ids))
        )
        outbox_count = db.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.event_type == _TASK_PROMPT_EVENT,
                OutboxEvent.aggregate_id == task_id,
            )
        )
        return (
            task.expires_at,
            int(attempt_count or 0),
            int(action_count or 0),
            int(outbox_count or 0),
        )


@pytest.mark.integration
def test_task_api_gate_off_has_no_http_database_or_rabbit_side_effects() -> None:
    if os.getenv("IMAGE_BACKEND_TASK_HTTP_GATE_OFF_E2E") != "true":
        pytest.skip("requires the isolated API with task and generation gates disabled")
    configured = _configured_values()
    if configured is None:
        pytest.skip("requires the isolated HTTP API, PostgreSQL, and RabbitMQ")
    base_url, origin, runtime_url, broker_url = configured
    engine = create_engine(runtime_url, pool_pre_ping=True)
    queue_name = os.getenv("IMAGE_BACKEND_QUEUE_NAME", "image-backend-events")
    try:
        with httpx.Client(base_url=base_url, timeout=30) as client:
            assert client.get("/health/ready").status_code == 200
            owner_id = _enter_guest(client, origin)
            # A passive AMQP 404 means the queue was absent and remains absent;
            # passive inspection does not create it. Existing queue depth is compared exactly.
            before_queue = _queue_state(broker_url, queue_name)
            before_side_effects = _owner_task_side_effect_counts(engine, owner_id)

            payload = _task_payload(uuid4(), uuid4(), [IMAGE_TYPES[0]])
            response = client.post(
                "/api/v1/tasks",
                headers={"Origin": origin, "Idempotency-Key": f"gate-off-{uuid4().hex}"},
                json=payload,
            )
            assert response.status_code == 404
            assert response.json()["code"] == "HTTP_ERROR"

            after_side_effects = _owner_task_side_effect_counts(engine, owner_id)
            after_queue = _queue_state(broker_url, queue_name)
            assert after_side_effects == before_side_effects == (0, 0, 0, 0)
            assert after_queue == before_queue
    finally:
        engine.dispose()


@pytest.mark.integration
def test_standalone_mock_prompt_result_loop_completes_http_tasks_without_runner_pump() -> None:
    if os.getenv("IMAGE_BACKEND_STANDALONE_MOCK_E2E") != "true":
        pytest.skip("requires the isolated API and ordinary mock Worker runtime")
    configured = _configured_values()
    if configured is None:
        pytest.skip("requires the isolated HTTP API, PostgreSQL, and RabbitMQ")
    base_url, origin, runtime_url, _broker_url = configured
    engine = create_engine(runtime_url, pool_pre_ping=True)
    task_ids: list[UUID] = []
    try:
        with httpx.Client(base_url=base_url, timeout=60) as owner:
            assert owner.get("/health/ready").status_code == 200
            _enter_guest(owner, origin)
            reference_set_id = _create_reference_set(owner, origin)

            for image_count in (1, 3, 8):
                task_id, _payload = _create_task(
                    owner,
                    origin,
                    reference_set_id,
                    image_types=list(IMAGE_TYPES[:image_count]),
                    idempotency_key=f"n1-standalone-{image_count}-{uuid4().hex}",
                )
                task_ids.append(task_id)
                result = _wait_for_task(
                    owner,
                    task_id,
                    timeout_seconds=300 if image_count == 8 else 180,
                )
                assert result["status"] == "succeeded"
                assert result["counts"]["planned"] == image_count
                assert result["counts"]["succeeded"] == image_count
                assert result["counts"]["failed"] == 0

        with Session(engine) as db:
            for task_id in task_ids:
                submission = db.execute(
                    select(PromptSubmission).where(
                        PromptSubmission.task_id == task_id,
                        PromptSubmission.phase == "initial_preferences",
                    )
                ).scalar_one()
                assert submission.status == "package_ready"
                assert submission.callback_result_digest is not None
                query_actions = list(
                    db.scalars(
                        select(Action).where(
                            Action.scope_id == submission.submission_id,
                            Action.action_kind == PROMPT_MOCK_RESULT_QUERY_ACTION_KIND,
                        )
                    )
                )
                assert len(query_actions) == 1
                assert query_actions[0].status == "completed"
    finally:
        engine.dispose()


@pytest.mark.integration
def test_http_task_generation_retry_preview_owner_and_cleanup_lifecycle() -> None:
    if os.getenv("IMAGE_BACKEND_TASK_HTTP_E2E") != "true":
        pytest.skip("requires the isolated task API, dispatcher, scheduler, and prefork Workers")
    configured = _configured_values()
    if configured is None:
        pytest.skip("requires the isolated HTTP API, PostgreSQL, and RabbitMQ")
    base_url, origin, runtime_url, broker_url = configured
    maintenance_secret = os.getenv("IMAGE_BACKEND_TEST_MAINTENANCE_SECRET", "")
    assert len(maintenance_secret) >= 32 and maintenance_secret.isascii()
    callback_secret = os.getenv("IMAGE_BACKEND_TEST_DIFY_CALLBACK_SECRET", "")
    assert len(callback_secret) >= 32 and callback_secret.isascii()
    queue_name = os.getenv("IMAGE_BACKEND_QUEUE_NAME", "image-backend-events")
    engine = create_engine(runtime_url, pool_pre_ping=True)
    task_ids: list[UUID] = []
    first_task_idempotency_key = f"r8-task-{uuid4().hex}"
    first_task_payload: dict[str, object] | None = None
    try:
        with httpx.Client(base_url=base_url, timeout=60) as owner:
            assert owner.get("/health/ready").status_code == 200
            queue_snapshot = _queue_state(broker_url, queue_name)
            assert queue_snapshot is not None, "the shared task queue must exist"
            _messages, consumers = queue_snapshot
            assert consumers >= 2, (
                "both independent Celery prefork Workers must consume the task queue"
            )
            owner_id = _enter_guest(owner, origin)
            reference_set_id = _create_reference_set(owner, origin)

            # One-image task exercises the accepted-response-loss fault. The
            # durable mock job must be recovered without a second submit call.
            first_task_id, first_task_payload = _create_task(
                owner,
                origin,
                reference_set_id,
                image_types=[IMAGE_TYPES[0]],
                idempotency_key=first_task_idempotency_key,
            )
            task_ids.append(first_task_id)

            first_delivered_submissions: set[UUID] = set()

            def first_callback_pump() -> None:
                _pump_mock_prompt_callbacks(
                    owner,
                    engine,
                    first_task_id,
                    callback_secret,
                    delivered_submission_ids=first_delivered_submissions,
                )

            replay = owner.post(
                "/api/v1/tasks",
                headers={"Origin": origin, "Idempotency-Key": first_task_idempotency_key},
                json=first_task_payload,
            )
            assert replay.status_code == 202, replay.text
            assert UUID(replay.json()["task_id"]) == first_task_id
            first_result = _wait_for_task(owner, first_task_id, callback_pump=first_callback_pump)
            assert first_result["status"] == "succeeded"
            assert first_result["counts"] == {
                "planned": 1,
                "succeeded": 1,
                "failed": 0,
                "pending": 0,
                "unresolved": 0,
            }

            first_attempt_ids = _attempt_ids(engine, [first_task_id])
            with Session(engine) as db:
                lost_submit_response_count = db.scalar(
                    select(func.count(Action.id)).where(
                        Action.attempt_id.in_(first_attempt_ids),
                        Action.action_kind == "generation.submit.v1",
                        Action.status == "unknown",
                        Action.last_error_code == "MOCK_ACCEPTED_RESPONSE_LOST",
                    )
                )
                recoveries = db.scalar(
                    select(func.count(Action.id)).where(
                        Action.attempt_id.in_(first_attempt_ids),
                        Action.action_kind == "generation.recover.v1",
                        Action.status == "completed",
                        Action.last_outcome == "accepted",
                    )
                )
            assert lost_submit_response_count is not None and lost_submit_response_count >= 1, (
                "the test-only accepted-response-loss fault was not observed"
            )
            assert recoveries is not None and recoveries >= 1, (
                "the test-only lost submit response did not enter durable recovery"
            )
            _assert_one_persisted_submit_per_attempt(engine, first_attempt_ids)

            # Three images exercise partial completion and the explicit failed
            # item retry route. The test changes one persisted mock job to a
            # definite provider failure before its first poll is due.
            partial_task_id, _partial_payload = _create_task(
                owner,
                origin,
                reference_set_id,
                image_types=list(IMAGE_TYPES[:3]),
                idempotency_key=f"r8-task-{uuid4().hex}",
            )
            task_ids.append(partial_task_id)

            partial_delivered_submissions: set[UUID] = set()

            def partial_callback_pump() -> None:
                _pump_mock_prompt_callbacks(
                    owner,
                    engine,
                    partial_task_id,
                    callback_secret,
                    delivered_submission_ids=partial_delivered_submissions,
                )

            failed_item_id = _fail_first_pending_mock_job(
                engine, partial_task_id, callback_pump=partial_callback_pump
            )
            partial_result = _wait_for_task(
                owner, partial_task_id, callback_pump=partial_callback_pump
            )
            assert partial_result["status"] == "partially_succeeded"
            assert partial_result["counts"] == {
                "planned": 3,
                "succeeded": 2,
                "failed": 1,
                "pending": 0,
                "unresolved": 0,
            }
            assert [
                item["task_item_id"]
                for item in partial_result["items"]
                if item["status"] == "failed"
            ] == [str(failed_item_id)]

            # A successful image in a partially failed task is downloadable.
            # Check the public response against both the committed asset row
            # and the exact private object bytes; the failed image stays hidden.
            partial_success_items = [
                item for item in partial_result["items"] if item["status"] == "succeeded"
            ]
            assert len(partial_success_items) == 2
            partial_image_id = UUID(partial_success_items[0]["image_id"])
            partial_original_path = (
                f"/api/v1/tasks/{partial_task_id}/images/{partial_image_id}/original"
            )
            side_effects_before_download = _owner_task_side_effect_counts(engine, owner_id)
            task_effects_before_download = _download_side_effect_snapshot(engine, partial_task_id)
            partial_download = owner.get(partial_original_path)
            assert partial_download.status_code == 200, partial_download.text
            assert partial_download.headers["content-type"] == "image/png"
            assert partial_download.headers["content-disposition"] == (
                f'attachment; filename="task_{partial_task_id}_image_{partial_image_id}.png"'
            )
            assert partial_download.headers["cache-control"] == "private, no-store"
            assert partial_download.headers["accept-ranges"] == "none"
            assert len(partial_download.content) == int(partial_download.headers["content-length"])
            with Session(engine) as db:
                partial_asset = db.get(ImageAsset, partial_image_id)
                partial_result_row = db.execute(
                    select(GenerationResult).where(
                        GenerationResult.task_id == partial_task_id,
                        GenerationResult.original_asset_id == partial_image_id,
                    )
                ).scalar_one()
                assert partial_asset is not None
                assert partial_result_row.original_asset_id == partial_image_id
                assert partial_asset.asset_kind == "generation_original"
                expected_partial_size = partial_asset.size_bytes
                expected_partial_sha256 = partial_asset.content_sha256
                expected_partial_key = partial_asset.object_key
            partial_private_storage = LocalPrivateStorage(
                os.getenv("IMAGE_BACKEND_PRIVATE_STORAGE_ROOT", "/data/private")
            )
            with partial_private_storage.open_reader(expected_partial_key) as stored_original:
                private_original_bytes = stored_original.read()
            assert len(private_original_bytes) == expected_partial_size
            assert hashlib.sha256(private_original_bytes).hexdigest() == expected_partial_sha256
            assert partial_download.content == private_original_bytes
            assert hashlib.sha256(partial_download.content).hexdigest() == expected_partial_sha256
            failed_item = next(
                item for item in partial_result["items"] if item["status"] == "failed"
            )
            assert failed_item["image_id"] is None
            failed_image_id = uuid4()
            failed_download = owner.get(
                f"/api/v1/tasks/{partial_task_id}/images/{failed_image_id}/original"
            )
            assert failed_download.status_code == 404
            ranged_download = owner.get(
                partial_original_path, headers={"Range": "bytes=0-7", "If-None-Match": "*"}
            )
            assert ranged_download.status_code == 200
            assert ranged_download.content == private_original_bytes
            assert _owner_task_side_effect_counts(engine, owner_id) == side_effects_before_download
            assert _download_side_effect_snapshot(engine, partial_task_id) == (
                task_effects_before_download
            )

            retry = owner.post(
                f"/api/v1/tasks/{partial_task_id}/retry",
                headers={"Origin": origin, "Idempotency-Key": f"r8-retry-{uuid4().hex}"},
                json={"failed_item_ids": [str(failed_item_id)]},
            )
            assert retry.status_code == 202, retry.text
            retry_attempts = retry.json()["attempts"]
            assert len(retry_attempts) == 1
            retry_attempt_id = UUID(retry_attempts[0]["attempt_id"])
            retried_result = _wait_for_task(owner, partial_task_id)
            assert retried_result["status"] == "succeeded"
            assert retried_result["counts"]["succeeded"] == 3
            assert retried_result["counts"]["failed"] == 0

            # Eight images exercise the complete public batch limit with the
            # same two prefork consumers and Local shared private storage.
            eight_task_id, _eight_payload = _create_task(
                owner,
                origin,
                reference_set_id,
                image_types=list(IMAGE_TYPES),
                idempotency_key=f"r8-task-{uuid4().hex}",
            )
            task_ids.append(eight_task_id)

            eight_delivered_submissions: set[UUID] = set()

            def eight_callback_pump() -> None:
                _pump_mock_prompt_callbacks(
                    owner,
                    engine,
                    eight_task_id,
                    callback_secret,
                    delivered_submission_ids=eight_delivered_submissions,
                )

            eight_result = _wait_for_task(
                owner,
                eight_task_id,
                callback_pump=eight_callback_pump,
                timeout_seconds=300,
            )
            assert eight_result["status"] == "succeeded"
            assert eight_result["counts"]["planned"] == 8
            assert eight_result["counts"]["succeeded"] == 8
            assert eight_result["counts"]["failed"] == 0

            _assert_prompt_callbacks_advanced(engine, task_ids)
            all_attempt_ids = _attempt_ids(engine, task_ids)
            with Session(engine) as db:
                retry_action = (
                    db.execute(select(Action).where(Action.attempt_id == retry_attempt_id))
                    .scalars()
                    .all()
                )
                assert retry_action
            _assert_one_persisted_submit_per_attempt(engine, all_attempt_ids)

            history = owner.get("/api/v1/tasks", params={"limit": 50})
            assert history.status_code == 200, history.text
            history_ids = {entry["task_id"] for entry in history.json()["items"]}
            assert set(map(str, task_ids)).issubset(history_ids)

            first_success_item = next(
                item for item in first_result["items"] if item["status"] == "succeeded"
            )
            preview_path = (
                f"/api/v1/tasks/{first_task_id}/images/{first_success_item['image_id']}/preview"
            )
            preview = owner.get(preview_path)
            assert preview.status_code == 200, preview.text
            assert preview.headers["content-type"] == "image/webp"
            assert preview.headers["cache-control"] == "private, no-store"
            assert preview.content

            other = httpx.Client(base_url=base_url, timeout=30)
            try:
                _enter_guest(other, origin)
                assert other.get(f"/api/v1/tasks/{first_task_id}").status_code == 404
                assert other.get(preview_path).status_code == 404
                assert other.get(partial_original_path).status_code == 404
                other_history = other.get("/api/v1/tasks", params={"limit": 50})
                assert other_history.status_code == 200
                assert not history_ids.intersection(
                    entry["task_id"] for entry in other_history.json()["items"]
                )
            finally:
                other.close()

            # Exercise the R7 cleanup route and scheduler through real HTTP.
            # Replaying the same manual key must return the same durable run;
            # expired tasks are then removed and old task submission keys return 410.
            _expire_tasks(engine, task_ids)
            with Session(engine) as db:
                generation_contexts = list(
                    db.scalars(
                        select(GenerationAttemptContext).where(
                            GenerationAttemptContext.task_id.in_(task_ids)
                        )
                    )
                )
                private_keys = {
                    key
                    for context in generation_contexts
                    for key in (context.original_object_key, context.preview_object_key)
                    if key
                }

            expired_detail = owner.get(f"/api/v1/tasks/{first_task_id}")
            assert expired_detail.status_code == 410
            expired_history = owner.get("/api/v1/tasks", params={"limit": 50})
            assert expired_history.status_code == 200
            assert not set(map(str, task_ids)).intersection(
                entry["task_id"] for entry in expired_history.json()["items"]
            )
            assert owner.get(preview_path).status_code == 404
            assert owner.get(partial_original_path).status_code == 404

            cleanup_key = f"r8-cleanup-{uuid4().hex}"
            cleanup_headers = {
                "Authorization": f"Bearer {maintenance_secret}",
                "Idempotency-Key": cleanup_key,
                "Content-Type": "application/json",
            }
            created_run = owner.post(
                "/internal/v1/maintenance/cleanup/runs",
                headers=cleanup_headers,
                json={},
                timeout=30,
            )
            assert created_run.status_code == 202, created_run.text
            run_id = created_run.json()["run_id"]
            replayed_run = owner.post(
                "/internal/v1/maintenance/cleanup/runs",
                headers=cleanup_headers,
                json={},
                timeout=30,
            )
            assert replayed_run.status_code == 202, replayed_run.text
            assert replayed_run.json()["run_id"] == run_id
            assert replayed_run.json()["replayed"] is True

            deadline = time.monotonic() + 180
            cleanup_result: dict[str, object] | None = None
            while time.monotonic() < deadline:
                polled = owner.get(
                    f"/internal/v1/maintenance/cleanup/runs/{run_id}",
                    headers={"Authorization": f"Bearer {maintenance_secret}"},
                )
                polled.raise_for_status()
                cleanup_result = polled.json()
                if cleanup_result["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.5)
            assert cleanup_result is not None and cleanup_result["status"] == "completed", (
                cleanup_result
            )
            with Session(engine) as db:
                persisted_run = db.get(CleanupRun, UUID(run_id))
                assert persisted_run is not None and persisted_run.status == "completed"
                assert db.scalar(select(func.count(Task.id)).where(Task.id.in_(task_ids))) == 0
                expired_idempotency_records = db.scalar(
                    select(func.count(TaskIdempotencyRecord.id)).where(
                        TaskIdempotencyRecord.task_id.in_(task_ids),
                        TaskIdempotencyRecord.state == "expired",
                    )
                )
                expired_consumptions = db.scalar(
                    select(func.count(TaskRecommendationConsumption.recommendation_id)).where(
                        TaskRecommendationConsumption.task_id.in_(task_ids),
                        TaskRecommendationConsumption.state == "expired",
                    )
                )
                assert expired_idempotency_records is not None
                assert expired_idempotency_records >= len(task_ids)
                assert expired_consumptions == len(task_ids)

            private_storage = LocalPrivateStorage(
                os.getenv("IMAGE_BACKEND_PRIVATE_STORAGE_ROOT", "/data/private")
            )
            assert all(private_storage.stat(key) is None for key in private_keys)

            assert owner.get(f"/api/v1/tasks/{first_task_id}").status_code == 404
            assert owner.get(preview_path).status_code == 404
            assert first_task_payload is not None
            expired_replay = owner.post(
                "/api/v1/tasks",
                headers={"Origin": origin, "Idempotency-Key": first_task_idempotency_key},
                json=first_task_payload,
            )
            assert expired_replay.status_code == 410, expired_replay.text
            assert expired_replay.json()["code"] == "TASK_EXPIRED"

            # Race a real cleanup run against an already authorized HTTP
            # stream. A valid, high-entropy PNG larger than the loopback
            # socket buffer keeps the sender on the open Linux file descriptor
            # while cleanup unlinks the path. A later request must be denied.
            race_task_id, _race_payload = _create_task(
                owner,
                origin,
                reference_set_id,
                image_types=[IMAGE_TYPES[0]],
                idempotency_key=f"original-cleanup-race-{uuid4().hex}",
            )
            race_delivered_submissions: set[UUID] = set()

            def race_callback_pump() -> None:
                _pump_mock_prompt_callbacks(
                    owner,
                    engine,
                    race_task_id,
                    callback_secret,
                    delivered_submission_ids=race_delivered_submissions,
                )

            race_result = _wait_for_task(
                owner, race_task_id, callback_pump=race_callback_pump
            )
            assert race_result["status"] == "succeeded"
            race_item = next(item for item in race_result["items"] if item["status"] == "succeeded")
            race_image_id = UUID(race_item["image_id"])
            race_asset_key = f"generated/{owner_id}/{race_task_id}/cleanup-race-{uuid4().hex}.png"
            random_pixels = os.urandom(1536 * 1536 * 3)
            large_image_output = io.BytesIO()
            Image.frombytes("RGB", (1536, 1536), random_pixels).save(
                large_image_output, format="PNG", compress_level=0
            )
            large_image_bytes = large_image_output.getvalue()
            race_sha256 = hashlib.sha256(large_image_bytes).hexdigest()
            race_storage = LocalPrivateStorage(
                os.getenv("IMAGE_BACKEND_PRIVATE_STORAGE_ROOT", "/data/private")
            )
            race_storage.put_immutable(
                race_asset_key,
                io.BytesIO(large_image_bytes),
                {
                    "asset_kind": "generation_original",
                    "mime_type": "image/png",
                    "size_bytes": len(large_image_bytes),
                    "sha256": race_sha256,
                    "width": 1536,
                    "height": 1536,
                },
            )
            with Session(engine) as db, db.begin():
                race_asset = db.get(ImageAsset, race_image_id)
                assert race_asset is not None and race_asset.asset_kind == "generation_original"
                race_asset.object_key = race_asset_key
                race_asset.content_sha256 = race_sha256
                race_asset.mime_type = "image/png"
                race_asset.size_bytes = len(large_image_bytes)
                race_asset.width = 1536
                race_asset.height = 1536
            race_original_path = (
                f"/api/v1/tasks/{race_task_id}/images/{race_image_id}/original"
            )
            with owner.stream("GET", race_original_path, timeout=120) as race_stream:
                assert race_stream.status_code == 200
                assert int(race_stream.headers["content-length"]) == len(large_image_bytes)
                time.sleep(0.5)
                _expire_tasks(engine, [race_task_id])
                race_cleanup_headers = {
                    "Authorization": f"Bearer {maintenance_secret}",
                    "Idempotency-Key": f"original-race-cleanup-{uuid4().hex}",
                    "Content-Type": "application/json",
                }
                race_cleanup = owner.post(
                    "/internal/v1/maintenance/cleanup/runs",
                    headers=race_cleanup_headers,
                    json={},
                    timeout=30,
                )
                assert race_cleanup.status_code == 202, race_cleanup.text
                race_run_id = race_cleanup.json()["run_id"]
                race_cleanup_result: dict[str, object] | None = None
                race_deadline = time.monotonic() + 90
                while time.monotonic() < race_deadline:
                    polled = owner.get(
                        f"/internal/v1/maintenance/cleanup/runs/{race_run_id}",
                        headers={"Authorization": f"Bearer {maintenance_secret}"},
                    )
                    polled.raise_for_status()
                    race_cleanup_result = polled.json()
                    if race_cleanup_result["status"] in {"completed", "failed"}:
                        break
                    time.sleep(0.25)
                assert race_cleanup_result is not None
                assert race_cleanup_result["status"] == "completed", race_cleanup_result
                assert race_storage.stat(race_asset_key) is None
                raced_body = b"".join(race_stream.iter_bytes(chunk_size=64 * 1024))
                assert len(raced_body) == len(large_image_bytes)
                assert hashlib.sha256(raced_body).hexdigest() == race_sha256
            assert owner.get(race_original_path).status_code == 404

            # Keep one completed image and its original guest cookie for the
            # cross-instance restore probe. The containing host directory is
            # private and is never attached to public logs or reports.
            restore_task_id, _restore_payload = _create_task(
                owner,
                origin,
                reference_set_id,
                image_types=[IMAGE_TYPES[0]],
                idempotency_key=f"original-restore-probe-{uuid4().hex}",
            )
            restore_delivered_submissions: set[UUID] = set()

            def restore_callback_pump() -> None:
                _pump_mock_prompt_callbacks(
                    owner,
                    engine,
                    restore_task_id,
                    callback_secret,
                    delivered_submission_ids=restore_delivered_submissions,
                )

            restore_result = _wait_for_task(
                owner, restore_task_id, callback_pump=restore_callback_pump
            )
            assert restore_result["status"] == "succeeded"
            restore_item = next(
                item for item in restore_result["items"] if item["status"] == "succeeded"
            )
            restore_image_id = UUID(restore_item["image_id"])
            with Session(engine) as db:
                restore_asset = db.get(ImageAsset, restore_image_id)
                restore_result_row = db.execute(
                    select(GenerationResult).where(
                        GenerationResult.task_id == restore_task_id,
                        GenerationResult.original_asset_id == restore_image_id,
                    )
                ).scalar_one()
                assert restore_asset is not None
                assert restore_asset.asset_kind == "generation_original"
                assert restore_result_row.original_asset_id == restore_image_id
                assert restore_result_row.preview_asset_id != restore_image_id
                restore_sha256 = restore_asset.content_sha256
                restore_size_bytes = restore_asset.size_bytes
                restore_mime_type = restore_asset.mime_type
            cookie_value = owner.cookies.get("guest_session_dev")
            assert cookie_value and cookie_value.isascii()
            private_evidence = Path(
                os.getenv("IMAGE_BACKEND_TEST_PRIVATE_EVIDENCE_DIR", "")
            )
            assert private_evidence.is_dir(), "private acceptance evidence mount is required"
            cookie_path = private_evidence / "guest-cookie.txt"
            cookie_path.write_text(
                f"guest_session_dev={cookie_value}", encoding="ascii"
            )
            os.chmod(cookie_path, 0o600)
            restore_target = {
                "task_id": str(restore_task_id),
                "image_id": str(restore_image_id),
                "sha256": restore_sha256,
                "size_bytes": restore_size_bytes,
                "mime_type": restore_mime_type,
            }
            (private_evidence / "restore-target.json").write_text(
                json.dumps(restore_target, indent=2) + "\n", encoding="utf-8"
            )
    finally:
        # Failure does not launch R7's global aggregate/orphan inventory scan.
        engine.dispose()
