from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from app.api.auth import router as auth_router
from app.api.tasks import router as tasks_router
from app.core.domain_errors import DomainError
from app.core.errors import install_exception_handlers
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ImageAsset, ReferenceSet, ReferenceSetImage
from app.db.task_models import (
    Task,
    TaskIdempotencyRecord,
    TaskItem,
    TaskRecommendationConsumption,
)
from app.integrations.dify.mock import build_mock_options, build_mock_original
from app.schemas.recommendations import IMAGE_TYPES
from app.schemas.tasks import TaskCreateRequest
from app.services.auth import AuthOptions, AuthService
from app.services.tasks import TASK_PROMPT_EVENT_TYPE, TaskOptions, TaskService
from app.services.trusted_analysis import (
    TRUSTED_ANALYSIS_STATE_KEY,
    build_trusted_analysis_snapshot,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, Table, create_engine, delete, event, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

TRUSTED_ORIGIN = "http://testserver"


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    database_url = os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires the dedicated PostgreSQL test runtime URL")
    engine = create_engine(database_url, pool_pre_ping=True)
    required_tables = {
        "users",
        "sessions",
        "session_policies",
        "reference_sets",
        "image_assets",
        "reference_set_images",
        "recommendations",
        "outbox_events",
        "outbox_receipts",
        "tasks",
        "task_items",
        "task_item_attempts",
        "task_idempotency_records",
        "task_recommendation_consumptions",
    }
    if not required_tables.issubset(set(inspect(engine).get_table_names())):
        engine.dispose()
        pytest.skip("requires the integrated PostgreSQL schema through the S6 task migration")
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def remove_task_events_created_by_test(request: pytest.FixtureRequest) -> Iterator[None]:
    """Keep test-only unsupported task events away from later queue tests."""
    engine: Engine = request.getfixturevalue("pg_engine")
    with Session(engine) as db:
        preexisting = set(
            db.scalars(
                select(OutboxEvent.id).where(OutboxEvent.event_type == TASK_PROMPT_EVENT_TYPE)
            )
        )
    yield
    with Session(engine) as db, db.begin():
        created = (
            set(
                db.scalars(
                    select(OutboxEvent.id).where(OutboxEvent.event_type == TASK_PROMPT_EVENT_TYPE)
                )
            )
            - preexisting
        )
        if created:
            db.execute(delete(OutboxReceipt).where(OutboxReceipt.event_id.in_(created)))
            db.execute(delete(OutboxEvent).where(OutboxEvent.id.in_(created)))


def _build_app(engine: Engine) -> FastAPI:
    app = FastAPI()
    app.state.auth_service = AuthService(
        engine,
        AuthOptions(
            trusted_origins=frozenset({TRUSTED_ORIGIN}),
            production=False,
            use_dev_cookie=True,
        ),
    )
    app.state.task_service = TaskService(engine, TaskOptions())
    app.include_router(auth_router)
    app.include_router(tasks_router)
    install_exception_handlers(app)
    return app


def _new_guest(engine: Engine) -> tuple[TestClient, UUID]:
    client = TestClient(_build_app(engine))
    response = client.post("/api/v1/auth/guest", headers={"Origin": TRUSTED_ORIGIN})
    assert response.status_code == 200
    return client, UUID(response.json()["user_id"])


def _database_now(engine: Engine):
    with Session(engine) as db:
        return db.scalar(select(func.clock_timestamp()))


def _create_reference_and_recommendation(
    engine: Engine,
    *,
    owner_user_id: UUID,
    reference_expiry: timedelta = timedelta(days=10),
    recommendation_expiry: timedelta = timedelta(days=3),
    revision: int = 1,
    image_count: int = 1,
    include_trusted_snapshot: bool = True,
) -> tuple[UUID, UUID]:
    if not 1 <= image_count <= 4:
        raise ValueError("image_count must be between 1 and 4")
    now = _database_now(engine)
    reference_set_id = uuid4()
    recommendation_id = uuid4()
    expires_at = now + reference_expiry
    ready_at = min(now, expires_at - timedelta(seconds=1))
    slots = ("PRIMARY", "AUX01", "AUX02", "AUX03")[:image_count]
    reference_images = []
    source_assets = []
    model_assets = []
    trusted_references = []
    for position, slot in enumerate(slots):
        source_id = uuid4()
        model_id = uuid4()
        image_link_id = uuid4()
        digest = hashlib.sha256(image_link_id.bytes).hexdigest()
        source_assets.append(
            ImageAsset(
                id=source_id,
                owner_user_id=owner_user_id,
                asset_kind="reference_source",
                object_key=f"s6-test/{source_id}/source.png",
                content_sha256=digest,
                mime_type="image/png",
                size_bytes=256,
                width=64,
                height=64,
            )
        )
        model_assets.append(
            ImageAsset(
                id=model_id,
                owner_user_id=owner_user_id,
                asset_kind="reference_model_input",
                object_key=f"s6-test/{model_id}/model.png",
                content_sha256=digest,
                mime_type="image/png",
                size_bytes=128,
                width=32,
                height=32,
            )
        )
        reference_images.append(
            ReferenceSetImage(
                id=image_link_id,
                reference_set_id=reference_set_id,
                owner_user_id=owner_user_id,
                slot=slot,
                position=position,
                source_asset_id=source_id,
                model_asset_id=model_id,
                content_sha256=digest,
                created_at=ready_at,
            )
        )
        trusted_references.append(
            {
                "alias": slot,
                "position": position,
                "reference_set_image_id": image_link_id,
                "model_asset_id": model_id,
                "source_asset_id": source_id,
                "model_sha256": digest,
                "source_sha256": digest,
                "model_mime_type": "image/png",
                "source_mime_type": "image/png",
                "model_size_bytes": 128,
                "source_size_bytes": 256,
                "model_width": 32,
                "model_height": 32,
                "source_width": 64,
                "source_height": 64,
            }
        )
    options = build_mock_options()
    original = build_mock_original("测试品类", options)
    recommendation_state: dict[str, object] = {"fixture_id": "synthetic-recommendation-v1"}
    if include_trusted_snapshot:
        trusted_snapshot = build_trusted_analysis_snapshot(
            reference_set_id=reference_set_id,
            recommendation_id=recommendation_id,
            recommendation_revision=revision,
            category="测试品类",
            references=trusted_references,
            created_at=ready_at,
        )
        recommendation_state[TRUSTED_ANALYSIS_STATE_KEY] = trusted_snapshot.model_dump(mode="json")
    with Session(engine) as db, db.begin():
        db.add(
            ReferenceSet(
                id=reference_set_id,
                owner_user_id=owner_user_id,
                product_category="测试品类",
                status="ready",
                lease_token=None,
                upload_lease_until=None,
                created_at=ready_at,
                last_activity_at=ready_at,
                ready_at=ready_at,
                expires_at=expires_at,
            )
        )
        db.flush()
        db.add_all(source_assets + model_assets)
        db.flush()
        db.add_all(reference_images)
        db.add(
            Recommendation(
                id=recommendation_id,
                owner_user_id=owner_user_id,
                reference_set_id=reference_set_id,
                status="ready",
                revision=revision,
                original_recommendation=original,
                public_options=options,
                image_types=list(IMAGE_TYPES),
                audit={"image_analysis_performed": False},
                state=recommendation_state,
                run_ref=f"mock:{recommendation_id}",
                form_ref=f"mock-form:{recommendation_id}",
                valid_until=now + recommendation_expiry,
                data_expires_at=now + timedelta(days=60),
                ready_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    return reference_set_id, recommendation_id


def _request(
    reference_set_id: UUID,
    recommendation_id: UUID,
    *,
    selected_types: list[str] | None = None,
    revision: int = 1,
    custom_text: str = "保留商品可核验的细节",
) -> TaskCreateRequest:
    return TaskCreateRequest.model_validate(
        {
            "schema_version": 2,
            "reference_set_id": str(reference_set_id),
            "recommendation_id": str(recommendation_id),
            "recommendation_revision": revision,
            "preferences": {
                "language": {"mode": "A"},
                "platform": "TEMU",
                "usage_scene": {"mode": "B"},
                "visual_style": {"mode": "D"},
                "selling_points": {"mode": "custom", "text": custom_text},
            },
            "image_types": selected_types or list(IMAGE_TYPES[:3]),
        }
    )


def _count_for_task(db: Session, model: type, task_id: UUID) -> int:
    if model is OutboxEvent:
        return db.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.event_type == TASK_PROMPT_EVENT_TYPE,
                OutboxEvent.aggregate_id == task_id,
            )
        )
    if model is Task:
        return db.scalar(select(func.count(Task.id)).where(Task.id == task_id))
    return db.scalar(select(func.count()).select_from(model).where(model.task_id == task_id))


def test_create_one_three_eight_items_and_public_result_projection(pg_engine: Engine) -> None:
    _client, owner_id = _new_guest(pg_engine)
    # This test covers task sizes and response projection, not the owner cap.
    service = TaskService(pg_engine, TaskOptions(max_nonterminal_per_owner=8))
    for count in (1, 3, 8):
        reference_set_id, recommendation_id = _create_reference_and_recommendation(
            pg_engine, owner_user_id=owner_id
        )
        requested = list(IMAGE_TYPES[:count])
        result = service.create_task(
            owner_user_id=owner_id,
            idempotency_key=f"types-{count}-{uuid4()}",
            request=_request(reference_set_id, recommendation_id, selected_types=requested),
        )
        assert result.status == "queued"
        assert result.planned_count == count
        public = service.get_task(owner_user_id=owner_id, task_id=result.task_id)
        assert public.counts.planned == count
        assert [item.image_type for item in public.items] == requested
        assert all(item.status == "queued" for item in public.items)


def test_owner_nonterminal_task_limit_is_idempotent_and_serialized(pg_engine: Engine) -> None:
    _client, owner_id = _new_guest(pg_engine)
    service = TaskService(pg_engine, TaskOptions())
    submissions: list[tuple[str, TaskCreateRequest]] = []
    for index in range(5):
        reference_set_id, recommendation_id = _create_reference_and_recommendation(
            pg_engine, owner_user_id=owner_id
        )
        submissions.append(
            (
                f"owner-limit-{index}-{uuid4().hex}",
                _request(reference_set_id, recommendation_id, selected_types=[IMAGE_TYPES[0]]),
            )
        )

    first_key, first_request = submissions[0]
    first = service.create_task(
        owner_user_id=owner_id, idempotency_key=first_key, request=first_request
    )
    second_key, second_request = submissions[1]
    second = service.create_task(
        owner_user_id=owner_id, idempotency_key=second_key, request=second_request
    )
    with Session(pg_engine) as db, db.begin():
        task = db.get(Task, second.task_id)
        assert task is not None
        task.status = "reconciling"

    # A retry of an already committed creation remains valid at the owner cap.
    assert (
        service.create_task(
            owner_user_id=owner_id, idempotency_key=first_key, request=first_request
        ).task_id
        == first.task_id
    )
    with pytest.raises(DomainError) as capped:
        service.create_task(
            owner_user_id=owner_id,
            idempotency_key=submissions[2][0],
            request=submissions[2][1],
        )
    assert capped.value.status_code == 429
    assert capped.value.code == "TASK_ACTIVE_LIMIT_REACHED"

    with Session(pg_engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        terminal = db.get(Task, first.task_id)
        assert terminal is not None
        terminal.status = "succeeded"
        terminal.terminal_at = now
        terminal.expires_at = now + timedelta(days=60)
    created_after_terminal = service.create_task(
        owner_user_id=owner_id,
        idempotency_key=submissions[2][0],
        request=submissions[2][1],
    )
    assert created_after_terminal.status == "queued"

    # Keep one reconciling task active and free the third task's slot before
    # testing two concurrent requests against that final slot.
    with Session(pg_engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        terminal = db.get(Task, created_after_terminal.task_id)
        assert terminal is not None
        terminal.status = "succeeded"
        terminal.terminal_at = now
        terminal.expires_at = now + timedelta(days=60)
    with Session(pg_engine) as db:
        assert (
            db.scalar(
                select(func.count(Task.id)).where(
                    Task.owner_user_id == owner_id,
                    Task.status.not_in({"succeeded", "partially_succeeded", "failed"}),
                )
            )
            == 1
        )

    # Two concurrent submissions serialize on User; only one may consume the final slot.
    concurrent_requests = submissions[3:5]

    def submit(pair: tuple[str, TaskCreateRequest]):
        key, request = pair
        try:
            return service.create_task(
                owner_user_id=owner_id,
                idempotency_key=key,
                request=request,
            )
        except DomainError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, concurrent_requests))
    assert sum(not isinstance(result, DomainError) for result in results) == 1
    failures = [result for result in results if isinstance(result, DomainError)]
    assert len(failures) == 1
    assert failures[0].code == "TASK_ACTIVE_LIMIT_REACHED"


def test_reference_asset_snapshot_keeps_primary_auxiliary_ids_and_position_order(
    pg_engine: Engine,
) -> None:
    _client, owner_id = _new_guest(pg_engine)
    reference_set_id, recommendation_id = _create_reference_and_recommendation(
        pg_engine, owner_user_id=owner_id, image_count=4
    )
    result = TaskService(pg_engine, TaskOptions()).create_task(
        owner_user_id=owner_id,
        idempotency_key="asset-order-snapshot",
        request=_request(reference_set_id, recommendation_id, selected_types=[IMAGE_TYPES[0]]),
    )
    with Session(pg_engine) as db:
        task = db.get(Task, result.task_id)
        assert task is not None
        assets = task.reference_assets_snapshot
    assert [asset["slot"] for asset in assets] == ["PRIMARY", "AUX01", "AUX02", "AUX03"]
    assert [asset["position"] for asset in assets] == [0, 1, 2, 3]
    assert all(asset["source_asset_id"] and asset["model_asset_id"] for asset in assets)
    assert all(asset["source_sha256"] and asset["model_sha256"] for asset in assets)


def test_task_pins_trusted_analysis_snapshot_and_replay_keeps_its_digest(
    pg_engine: Engine,
) -> None:
    _client, owner_id = _new_guest(pg_engine)
    reference_set_id, recommendation_id = _create_reference_and_recommendation(
        pg_engine, owner_user_id=owner_id, image_count=3
    )
    service = TaskService(pg_engine, TaskOptions())
    request = _request(reference_set_id, recommendation_id, selected_types=[IMAGE_TYPES[0]])
    first = service.create_task(
        owner_user_id=owner_id, idempotency_key="trusted-snapshot-replay", request=request
    )
    replay = service.create_task(
        owner_user_id=owner_id, idempotency_key="trusted-snapshot-replay", request=request
    )

    assert replay.task_id == first.task_id
    with Session(pg_engine) as db:
        task = db.get(Task, first.task_id)
        recommendation = db.get(Recommendation, recommendation_id)
        assert task is not None and recommendation is not None
        pinned = task.recommendation_snapshot
        source = recommendation.state[TRUSTED_ANALYSIS_STATE_KEY]

    assert pinned["trusted_analysis_snapshot"] == source
    assert pinned["trusted_analysis_snapshot_id"] == source["snapshot_id"]
    assert pinned["trusted_analysis_snapshot_digest"] == source["snapshot_digest"]
    assert pinned["trusted_analysis_snapshot"]["reference_set_id"] == str(reference_set_id)
    assert pinned["trusted_analysis_snapshot"]["recommendation_id"] == str(recommendation_id)
    assert pinned["trusted_analysis_snapshot"]["recommendation_revision"] == 1


def test_task_creation_rejects_legacy_recommendation_without_trusted_snapshot(
    pg_engine: Engine,
) -> None:
    _client, owner_id = _new_guest(pg_engine)
    reference_set_id, recommendation_id = _create_reference_and_recommendation(
        pg_engine, owner_user_id=owner_id, include_trusted_snapshot=False
    )
    with Session(pg_engine) as db:
        prompt_events_before = db.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.event_type == TASK_PROMPT_EVENT_TYPE
            )
        )
    with pytest.raises(DomainError) as missing:
        TaskService(pg_engine, TaskOptions()).create_task(
            owner_user_id=owner_id,
            idempotency_key="legacy-recommendation-without-snapshot",
            request=_request(reference_set_id, recommendation_id),
        )

    assert missing.value.status_code == 409
    assert missing.value.code == "RECOMMENDATION_UNAVAILABLE"
    with Session(pg_engine) as db:
        task_count = db.scalar(
            select(func.count(Task.id)).where(Task.recommendation_id == recommendation_id)
        )
        assert task_count == 0
        assert (
            db.scalar(
                select(func.count(TaskRecommendationConsumption.recommendation_id)).where(
                    TaskRecommendationConsumption.recommendation_id == recommendation_id
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.event_type == TASK_PROMPT_EVENT_TYPE
                )
            )
            == prompt_events_before
        )


def test_task_creation_rejects_a_tampered_trusted_snapshot_digest(
    pg_engine: Engine,
) -> None:
    _client, owner_id = _new_guest(pg_engine)
    reference_set_id, recommendation_id = _create_reference_and_recommendation(
        pg_engine, owner_user_id=owner_id
    )
    with Session(pg_engine) as db, db.begin():
        recommendation = db.get(Recommendation, recommendation_id)
        assert recommendation is not None
        state = dict(recommendation.state)
        snapshot = dict(state[TRUSTED_ANALYSIS_STATE_KEY])
        snapshot["snapshot_digest"] = "0" * 64
        state[TRUSTED_ANALYSIS_STATE_KEY] = snapshot
        recommendation.state = state

    with pytest.raises(DomainError) as mismatch:
        TaskService(pg_engine, TaskOptions()).create_task(
            owner_user_id=owner_id,
            idempotency_key="tampered-trusted-snapshot-digest",
            request=_request(reference_set_id, recommendation_id),
        )

    assert mismatch.value.code == "RECOMMENDATION_UNAVAILABLE"
    with Session(pg_engine) as db:
        task_count = db.scalar(
            select(func.count(Task.id)).where(Task.recommendation_id == recommendation_id)
        )
        assert task_count == 0


def test_task_creation_rejects_reference_model_digest_mismatch_without_writes(
    pg_engine: Engine,
) -> None:
    _client, owner_id = _new_guest(pg_engine)
    reference_set_id, recommendation_id = _create_reference_and_recommendation(
        pg_engine, owner_user_id=owner_id
    )
    with Session(pg_engine) as db, db.begin():
        link = db.scalar(
            select(ReferenceSetImage).where(ReferenceSetImage.reference_set_id == reference_set_id)
        )
        assert link is not None
        model_asset = db.get(ImageAsset, link.model_asset_id)
        assert model_asset is not None
        link.content_sha256 = "0" * 64 if model_asset.content_sha256 != "0" * 64 else "1" * 64

    with Session(pg_engine) as db:
        prompt_events_before = db.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.event_type == TASK_PROMPT_EVENT_TYPE
            )
        )
    with pytest.raises(DomainError) as mismatch:
        TaskService(pg_engine, TaskOptions()).create_task(
            owner_user_id=owner_id,
            idempotency_key="reference-model-digest-mismatch",
            request=_request(reference_set_id, recommendation_id),
        )

    assert mismatch.value.status_code == 409
    assert mismatch.value.code == "REFERENCE_SET_UNAVAILABLE"
    with Session(pg_engine) as db:
        assert (
            db.scalar(
                select(func.count(Task.id)).where(Task.recommendation_id == recommendation_id)
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(TaskRecommendationConsumption.recommendation_id)).where(
                    TaskRecommendationConsumption.recommendation_id == recommendation_id
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(TaskIdempotencyRecord.id)).where(
                    TaskIdempotencyRecord.owner_user_id == owner_id
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.event_type == TASK_PROMPT_EVENT_TYPE
                )
            )
            == prompt_events_before
        )


def test_api_returns_202_and_owner_scoped_queued_result(pg_engine: Engine) -> None:
    client, owner_id = _new_guest(pg_engine)
    reference_set_id, recommendation_id = _create_reference_and_recommendation(
        pg_engine, owner_user_id=owner_id
    )
    payload = _request(reference_set_id, recommendation_id).model_dump(mode="json")
    response = client.post(
        "/api/v1/tasks",
        json=payload,
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": f"api-{uuid4()}"},
    )
    assert response.status_code == 202
    accepted = response.json()
    assert set(accepted) == {"schema_version", "task_id", "status", "planned_count"}
    assert accepted["schema_version"] == 2
    assert accepted["planned_count"] == 3

    public_response = client.get(f"/api/v1/tasks/{accepted['task_id']}")
    assert public_response.status_code == 200
    public = public_response.json()
    assert public["status"] == "queued"
    assert public["counts"] == {
        "planned": 3,
        "succeeded": 0,
        "failed": 0,
        "pending": 3,
        "unresolved": 0,
    }
    assert not {
        "request_snapshot",
        "resolved_preferences",
        "recommendation_snapshot",
        "reference_assets_snapshot",
        "prompt",
        "audit",
    } & set(public)

    other_client, _other_owner = _new_guest(pg_engine)
    assert other_client.get(f"/api/v1/tasks/{accepted['task_id']}").status_code == 404


def test_twenty_concurrent_same_key_create_exactly_one_task(pg_engine: Engine) -> None:
    _client, owner_id = _new_guest(pg_engine)
    reference_set_id, recommendation_id = _create_reference_and_recommendation(
        pg_engine, owner_user_id=owner_id
    )
    service = TaskService(pg_engine, TaskOptions())
    request = _request(reference_set_id, recommendation_id, selected_types=list(IMAGE_TYPES[:3]))
    idempotency_key = "shared-browser-retry-key"

    def submit(_index: int):
        return service.create_task(
            owner_user_id=owner_id,
            idempotency_key=idempotency_key,
            request=request,
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(submit, range(20)))

    task_ids = {result.task_id for result in results}
    assert len(task_ids) == 1
    task_id = next(iter(task_ids))
    with Session(pg_engine) as db:
        assert _count_for_task(db, Task, task_id) == 1
        assert _count_for_task(db, TaskItem, task_id) == 3
        assert _count_for_task(db, OutboxEvent, task_id) == 1
        prompt_event = db.execute(
            select(OutboxEvent).where(
                OutboxEvent.event_type == TASK_PROMPT_EVENT_TYPE,
                OutboxEvent.aggregate_id == task_id,
            )
        ).scalar_one()
        assert prompt_event.operation_version == 1
        assert prompt_event.schema_version == 1
        assert prompt_event.payload == {}
        assert (
            db.scalar(
                select(func.count(TaskIdempotencyRecord.id)).where(
                    TaskIdempotencyRecord.owner_user_id == owner_id
                )
            )
            == 1
        )


def test_lost_response_and_different_key_same_input_replay_same_task_but_other_input_conflicts(
    pg_engine: Engine,
) -> None:
    _client, owner_id = _new_guest(pg_engine)
    reference_set_id, recommendation_id = _create_reference_and_recommendation(
        pg_engine, owner_user_id=owner_id
    )
    service = TaskService(pg_engine, TaskOptions())
    request = _request(reference_set_id, recommendation_id)
    first = service.create_task(
        owner_user_id=owner_id, idempotency_key="lost-response-key", request=request
    )
    same_key = service.create_task(
        owner_user_id=owner_id, idempotency_key="lost-response-key", request=request
    )
    different_key = service.create_task(
        owner_user_id=owner_id, idempotency_key="new-key-same-input", request=request
    )
    assert same_key.task_id == first.task_id == different_key.task_id

    changed = _request(
        reference_set_id,
        recommendation_id,
        selected_types=list(IMAGE_TYPES[:2]),
    )
    with pytest.raises(DomainError) as conflict:
        service.create_task(
            owner_user_id=owner_id,
            idempotency_key="new-key-different-input",
            request=changed,
        )
    assert conflict.value.status_code == 409
    with pytest.raises(DomainError) as same_key_conflict:
        service.create_task(
            owner_user_id=owner_id,
            idempotency_key="lost-response-key",
            request=changed,
        )
    assert same_key_conflict.value.code == "IDEMPOTENCY_CONFLICT"

    with Session(pg_engine) as db:
        assert (
            db.scalar(
                select(func.count(Task.id)).where(Task.recommendation_id == recommendation_id)
            )
            == 1
        )


def test_cross_owner_expired_reference_and_stale_recommendation_are_rejected(
    pg_engine: Engine,
) -> None:
    _client, owner_id = _new_guest(pg_engine)
    _other_client, other_owner = _new_guest(pg_engine)
    other_ref, other_rec = _create_reference_and_recommendation(
        pg_engine, owner_user_id=other_owner
    )
    service = TaskService(pg_engine, TaskOptions())

    with pytest.raises(DomainError) as cross_owner:
        service.create_task(
            owner_user_id=owner_id,
            idempotency_key="cross-owner",
            request=_request(other_ref, other_rec),
        )
    assert cross_owner.value.status_code == 404

    expired_ref, expired_rec = _create_reference_and_recommendation(
        pg_engine,
        owner_user_id=owner_id,
        reference_expiry=timedelta(seconds=-1),
    )
    with pytest.raises(DomainError) as expired:
        service.create_task(
            owner_user_id=owner_id,
            idempotency_key="expired-ref",
            request=_request(expired_ref, expired_rec),
        )
    assert expired.value.status_code == 409

    stale_ref, stale_rec = _create_reference_and_recommendation(
        pg_engine, owner_user_id=owner_id, revision=2
    )
    with pytest.raises(DomainError) as stale:
        service.create_task(
            owner_user_id=owner_id,
            idempotency_key="stale-revision",
            request=_request(stale_ref, stale_rec, revision=1),
        )
    assert stale.value.code == "RECOMMENDATION_EXPIRED"

    unready_ref, unready_rec = _create_reference_and_recommendation(
        pg_engine, owner_user_id=owner_id
    )
    with Session(pg_engine) as db, db.begin():
        reference_set = db.get(ReferenceSet, unready_ref)
        assert reference_set is not None
        reference_set.status = "uploading"
    with pytest.raises(DomainError) as unready:
        service.create_task(
            owner_user_id=owner_id,
            idempotency_key="unready-reference",
            request=_request(unready_ref, unready_rec),
        )
    assert unready.value.code == "REFERENCE_SET_UNAVAILABLE"

    ready_ref, pending_rec = _create_reference_and_recommendation(pg_engine, owner_user_id=owner_id)
    with Session(pg_engine) as db, db.begin():
        recommendation = db.get(Recommendation, pending_rec)
        assert recommendation is not None
        recommendation.status = "analyzing"
    with pytest.raises(DomainError) as pending:
        service.create_task(
            owner_user_id=owner_id,
            idempotency_key="not-ready-recommendation",
            request=_request(ready_ref, pending_rec),
        )
    assert pending.value.code == "RECOMMENDATION_EXPIRED"

    expired_rec_ref, expired_rec_id = _create_reference_and_recommendation(
        pg_engine,
        owner_user_id=owner_id,
        recommendation_expiry=timedelta(seconds=-1),
    )
    with pytest.raises(DomainError) as expired_recommendation:
        service.create_task(
            owner_user_id=owner_id,
            idempotency_key="expired-recommendation",
            request=_request(expired_rec_ref, expired_rec_id),
        )
    assert expired_recommendation.value.code == "RECOMMENDATION_EXPIRED"


def test_enqueue_failure_rolls_back_task_items_tombstones_and_event(
    pg_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, owner_id = _new_guest(pg_engine)
    reference_set_id, recommendation_id = _create_reference_and_recommendation(
        pg_engine, owner_user_id=owner_id
    )
    service = TaskService(pg_engine, TaskOptions())

    failed_task_ids: list[UUID] = []

    def fail_enqueue(*_args: object, **kwargs: object) -> UUID:
        task_id = kwargs.get("aggregate_id")
        assert isinstance(task_id, UUID)
        failed_task_ids.append(task_id)
        raise RuntimeError("synthetic outbox failure")

    monkeypatch.setattr("app.services.tasks.enqueue_in_transaction", fail_enqueue)
    with pytest.raises(RuntimeError, match="synthetic outbox failure"):
        service.create_task(
            owner_user_id=owner_id,
            idempotency_key="rollback-key",
            request=_request(reference_set_id, recommendation_id),
        )

    with Session(pg_engine) as db:
        assert len(failed_task_ids) == 1
        failed_task_id = failed_task_ids[0]
        assert db.scalar(select(func.count(Task.id)).where(Task.id == failed_task_id)) == 0
        assert (
            db.scalar(select(func.count(TaskItem.id)).where(TaskItem.task_id == failed_task_id))
            == 0
        )
        assert (
            db.scalar(
                select(func.count(TaskIdempotencyRecord.id)).where(
                    TaskIdempotencyRecord.owner_user_id == owner_id
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(TaskRecommendationConsumption.recommendation_id)).where(
                    TaskRecommendationConsumption.recommendation_id == recommendation_id
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id))
                .where(OutboxEvent.event_type == TASK_PROMPT_EVENT_TYPE)
                .where(OutboxEvent.aggregate_id == failed_task_id)
            )
            == 0
        )


def test_request_and_recommendation_snapshots_survive_source_change_and_keep_exact_custom_text(
    pg_engine: Engine,
) -> None:
    _client, owner_id = _new_guest(pg_engine)
    reference_set_id, recommendation_id = _create_reference_and_recommendation(
        pg_engine, owner_user_id=owner_id
    )
    exact_custom = "  引号 '；换行\n保留 SQL SELECT 与 Unicode：雪  "
    request = _request(
        reference_set_id,
        recommendation_id,
        selected_types=list(IMAGE_TYPES[:1]),
        custom_text=exact_custom,
    )
    service = TaskService(pg_engine, TaskOptions())
    result = service.create_task(
        owner_user_id=owner_id,
        idempotency_key="exact-json-snapshot",
        request=request,
    )
    original_before = build_mock_original("测试品类", build_mock_options())

    with Session(pg_engine) as db, db.begin():
        recommendation = db.get(Recommendation, recommendation_id)
        assert recommendation is not None
        recommendation.original_recommendation = {"fixture_id": "mutated-after-task"}

    with Session(pg_engine) as db:
        task = db.get(Task, result.task_id)
        assert task is not None
        assert task.request_snapshot["preferences"]["selling_points"]["text"] == exact_custom
        assert task.resolved_preferences["selling_points"]["text"] == exact_custom
        assert task.recommendation_snapshot["original_recommendation"] == original_before
        assert task.category_snapshot == "测试品类"
        assert task.billing_mode == "disabled"
        assert task.billing_snapshot["reserved_units"] == 0
        assert task.billing_snapshot["charged_units"] == 0
        assert task.retention_days == 60
        assert task.reference_assets_snapshot[0]["slot"] == "PRIMARY"
        assert "object_key" not in task.reference_assets_snapshot[0]


def test_disabled_billing_does_not_change_any_existing_credit_tables(pg_engine: Engine) -> None:
    _client, owner_id = _new_guest(pg_engine)
    reference_set_id, recommendation_id = _create_reference_and_recommendation(
        pg_engine, owner_user_id=owner_id
    )
    credit_names = {"credit_accounts", "credit_reservations", "credit_ledger"}
    existing = credit_names & set(inspect(pg_engine).get_table_names())
    before: dict[str, int] = {}
    with Session(pg_engine) as db:
        for name in existing:
            table = Table(name, MetaData(), autoload_with=pg_engine)
            before[name] = db.scalar(select(func.count()).select_from(table))

    account_writes: list[str] = []

    def capture_account_write(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        normalized = statement.lstrip().lower()
        if normalized.startswith(("insert", "update", "delete")) and any(
            name in normalized for name in credit_names
        ):
            account_writes.append(statement)

    event.listen(pg_engine, "before_cursor_execute", capture_account_write)
    try:
        TaskService(pg_engine, TaskOptions()).create_task(
            owner_user_id=owner_id,
            idempotency_key="free-snapshot-no-account-write",
            request=_request(reference_set_id, recommendation_id),
        )
    finally:
        event.remove(pg_engine, "before_cursor_execute", capture_account_write)

    with Session(pg_engine) as db:
        after = {
            name: db.scalar(
                select(func.count()).select_from(Table(name, MetaData(), autoload_with=pg_engine))
            )
            for name in existing
        }
    assert after == before
    assert account_writes == []


def test_deleted_task_leaves_tombstones_and_replays_return_expired(pg_engine: Engine) -> None:
    _client, owner_id = _new_guest(pg_engine)
    reference_set_id, recommendation_id = _create_reference_and_recommendation(
        pg_engine, owner_user_id=owner_id
    )
    request = _request(reference_set_id, recommendation_id)
    service = TaskService(pg_engine, TaskOptions())
    created = service.create_task(
        owner_user_id=owner_id,
        idempotency_key="before-cleanup",
        request=request,
    )
    with Session(pg_engine) as db, db.begin():
        task = db.get(Task, created.task_id)
        assert task is not None
        db.delete(task)

    with pytest.raises(DomainError) as same_key:
        service.create_task(
            owner_user_id=owner_id,
            idempotency_key="before-cleanup",
            request=request,
        )
    assert same_key.value.status_code == 410
    with pytest.raises(DomainError) as new_key:
        service.create_task(
            owner_user_id=owner_id,
            idempotency_key="after-cleanup-new-key",
            request=request,
        )
    assert new_key.value.status_code == 410

    with Session(pg_engine) as db:
        assert (
            db.scalar(
                select(func.count(TaskRecommendationConsumption.recommendation_id)).where(
                    TaskRecommendationConsumption.recommendation_id == recommendation_id
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(TaskIdempotencyRecord.id)).where(
                    TaskIdempotencyRecord.owner_user_id == owner_id
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(Task.id)).where(Task.recommendation_id == recommendation_id)
            )
            == 0
        )


def test_expired_task_marks_key_and_recommendation_tombstones_expired(pg_engine: Engine) -> None:
    _client, owner_id = _new_guest(pg_engine)
    reference_set_id, recommendation_id = _create_reference_and_recommendation(
        pg_engine, owner_user_id=owner_id
    )
    request = _request(reference_set_id, recommendation_id)
    service = TaskService(pg_engine, TaskOptions())
    created = service.create_task(
        owner_user_id=owner_id,
        idempotency_key="before-expiration",
        request=request,
    )

    with Session(pg_engine) as db, db.begin():
        db.execute(
            text(
                "UPDATE tasks "
                "SET terminal_at = clock_timestamp() - interval '2 seconds', "
                "expires_at = clock_timestamp() - interval '1 second' "
                "WHERE id = :task_id"
            ),
            {"task_id": created.task_id},
        )

    with pytest.raises(DomainError) as expired_read:
        service.get_task(owner_user_id=owner_id, task_id=created.task_id)
    assert expired_read.value.status_code == 410
    with pytest.raises(DomainError) as expired_key_replay:
        service.create_task(
            owner_user_id=owner_id,
            idempotency_key="before-expiration",
            request=request,
        )
    assert expired_key_replay.value.status_code == 410
    with pytest.raises(DomainError) as expired_recommendation_replay:
        service.create_task(
            owner_user_id=owner_id,
            idempotency_key="new-key-after-expiration",
            request=request,
        )
    assert expired_recommendation_replay.value.status_code == 410

    with Session(pg_engine) as db:
        key_record = db.scalar(
            select(TaskIdempotencyRecord).where(TaskIdempotencyRecord.task_id == created.task_id)
        )
        consumption = db.scalar(
            select(TaskRecommendationConsumption).where(
                TaskRecommendationConsumption.recommendation_id == recommendation_id
            )
        )
        assert key_record is not None and key_record.state == "expired"
        assert consumption is not None and consumption.state == "expired"
