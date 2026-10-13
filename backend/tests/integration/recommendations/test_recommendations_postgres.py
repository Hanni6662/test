from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from app.api.auth import router as auth_router
from app.api.recommendations import router as recommendations_router
from app.core.domain_errors import DomainError
from app.core.errors import install_exception_handlers
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.recommendation_models import MockRecommendationRun, Recommendation
from app.db.reference_models import ImageAsset, ReferenceSet, ReferenceSetImage
from app.integrations.dify.mock import MockDifyAdapter
from app.schemas.recommendations import IMAGE_TYPES
from app.services.auth import AuthOptions, AuthService
from app.services.recommendations import (
    RECOMMENDATION_EVENT_TYPE,
    RecommendationOptions,
    RecommendationService,
    process_recommendation_event,
)
from app.workers.messages import OutboxMessage
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, inspect, select
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
        "outbox_events",
        "outbox_receipts",
        "recommendations",
        "mock_recommendation_runs",
    }
    if not required_tables.issubset(set(inspect(engine).get_table_names())):
        engine.dispose()
        pytest.skip("requires the integrated S1-S5 PostgreSQL migration chain")
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def cleanup_new_recommendation_outbox_events(pg_engine: Engine) -> Iterator[None]:
    """Keep service-level recommendation tests isolated from dispatcher tests."""
    with Session(pg_engine) as db:
        existing_ids = set(
            db.scalars(
                select(OutboxEvent.id).where(
                    OutboxEvent.event_type == RECOMMENDATION_EVENT_TYPE
                )
            ).all()
        )
    yield
    with Session(pg_engine) as db, db.begin():
        created_ids = set(
            db.scalars(
                select(OutboxEvent.id).where(
                    OutboxEvent.event_type == RECOMMENDATION_EVENT_TYPE
                )
            ).all()
        ) - existing_ids
        if created_ids:
            db.execute(
                delete(OutboxReceipt).where(OutboxReceipt.event_id.in_(created_ids))
            )
            db.execute(delete(OutboxEvent).where(OutboxEvent.id.in_(created_ids)))


def _build_app(engine: Engine, *, outcome: str = "ready") -> FastAPI:
    app = FastAPI()
    app.state.auth_service = AuthService(
        engine,
        AuthOptions(
            trusted_origins=frozenset({TRUSTED_ORIGIN}),
            production=False,
            use_dev_cookie=True,
        ),
    )
    app.state.recommendation_service = RecommendationService(
        engine,
        RecommendationOptions(),
        MockDifyAdapter(outcome=outcome),  # type: ignore[arg-type]
    )
    app.include_router(auth_router)
    app.include_router(recommendations_router)
    install_exception_handlers(app)
    return app


def _guest_client(engine: Engine) -> tuple[TestClient, UUID]:
    client = TestClient(_build_app(engine))
    response = client.post("/api/v1/auth/guest", headers={"Origin": TRUSTED_ORIGIN})
    assert response.status_code == 200
    return client, UUID(response.json()["user_id"])


def _create_reference_set(
    engine: Engine,
    *,
    owner_user_id: UUID,
    status: str = "ready",
    expires_in: timedelta = timedelta(days=10),
) -> UUID:
    reference_set_id = uuid4()
    now = _database_now(engine)
    ready_at = now if expires_in > timedelta(0) else now + expires_in - timedelta(seconds=1)
    with Session(engine) as db, db.begin():
        db.add(
            ReferenceSet(
                id=reference_set_id,
                owner_user_id=owner_user_id,
                product_category="测试品类",
                status=status,
                lease_token=None,
                upload_lease_until=None,
                created_at=now,
                last_activity_at=now,
                ready_at=ready_at if status == "ready" else None,
                expires_at=now + expires_in if status == "ready" else None,
            )
        )
        db.flush()
        if status == "ready":
            slot_id = uuid4()
            digest = hashlib.sha256(slot_id.bytes).hexdigest()
            source_id = uuid4()
            model_id = uuid4()
            db.add_all(
                [
                    ImageAsset(
                        id=source_id,
                        owner_user_id=owner_user_id,
                        asset_kind="reference_source",
                        object_key=f"test/{source_id}/source.png",
                        content_sha256=digest,
                        mime_type="image/png",
                        size_bytes=128,
                        width=32,
                        height=32,
                    ),
                    ImageAsset(
                        id=model_id,
                        owner_user_id=owner_user_id,
                        asset_kind="reference_model_input",
                        object_key=f"test/{model_id}/model-input.png",
                        content_sha256=digest,
                        mime_type="image/png",
                        size_bytes=128,
                        width=32,
                        height=32,
                    ),
                ]
            )
            db.flush()
            db.add(
                ReferenceSetImage(
                    id=slot_id,
                    reference_set_id=reference_set_id,
                    owner_user_id=owner_user_id,
                    slot="PRIMARY",
                    position=0,
                    source_asset_id=source_id,
                    model_asset_id=model_id,
                    content_sha256=digest,
                    created_at=now,
                )
            )
    return reference_set_id


def _database_now(engine: Engine):
    with Session(engine) as db:
        return db.scalar(select(func.clock_timestamp()))


def _process_event(engine: Engine, recommendation_id: UUID, *, outcome: str = "ready") -> None:
    with Session(engine) as db, db.begin():
        event = db.execute(
            select(OutboxEvent).where(
                OutboxEvent.event_type == RECOMMENDATION_EVENT_TYPE,
                OutboxEvent.aggregate_id == recommendation_id,
            )
        ).scalar_one()
        message = OutboxMessage(
            event_id=event.id,
            aggregate_id=event.aggregate_id,
            operation_version=event.operation_version,
            schema_version=event.schema_version,
            payload={key: UUID(value) for key, value in event.payload.items()},
        )
        process_recommendation_event(
            db,
            event,
            message,
            options=RecommendationOptions(),
            dify_port=MockDifyAdapter(outcome=outcome),  # type: ignore[arg-type]
        )


def test_post_requires_owned_ready_unexpired_set_and_get_is_owner_scoped(
    pg_engine: Engine,
) -> None:
    client, owner_id = _guest_client(pg_engine)
    ready_set_id = _create_reference_set(pg_engine, owner_user_id=owner_id)
    expired_set_id = _create_reference_set(
        pg_engine, owner_user_id=owner_id, expires_in=timedelta(seconds=-1)
    )
    uploading_set_id = _create_reference_set(
        pg_engine, owner_user_id=owner_id, status="uploading"
    )

    expired = client.post(
        "/api/v1/recommendations",
        json={"reference_set_id": str(expired_set_id)},
        headers={"Origin": TRUSTED_ORIGIN},
    )
    uploading = client.post(
        "/api/v1/recommendations",
        json={"reference_set_id": str(uploading_set_id)},
        headers={"Origin": TRUSTED_ORIGIN},
    )
    assert expired.status_code == 404
    assert uploading.status_code == 404

    created = client.post(
        "/api/v1/recommendations",
        json={"reference_set_id": str(ready_set_id)},
        headers={"Origin": TRUSTED_ORIGIN},
    )
    assert created.status_code == 202
    assert set(created.json()) == {"recommendation_id", "status"}
    assert created.json()["status"] == "analyzing"
    recommendation_id = UUID(created.json()["recommendation_id"])

    other_client, _other_owner = _guest_client(pg_engine)
    other_post = other_client.post(
        "/api/v1/recommendations",
        json={"reference_set_id": str(ready_set_id)},
        headers={"Origin": TRUSTED_ORIGIN},
    )
    assert other_post.status_code == 404
    response = other_client.get(f"/api/v1/recommendations/{recommendation_id}")
    assert response.status_code == 404


def test_owner_limit_serializes_creates_and_enqueue_failure_rolls_back(
    pg_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, owner_id = _guest_client(pg_engine)
    reference_set_id = _create_reference_set(pg_engine, owner_user_id=owner_id)
    service = RecommendationService(
        pg_engine, RecommendationOptions(), MockDifyAdapter()
    )

    def create_one() -> UUID | str:
        try:
            return service.create_recommendation(
                owner_user_id=owner_id, reference_set_id=reference_set_id
            )
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: create_one(), range(2)))
    assert sum(isinstance(value, UUID) for value in outcomes) == 1
    assert outcomes.count("RECOMMENDATION_LIMIT_REACHED") == 1

    # Use a fresh owner so the active recommendation limit does not short-circuit the failure.
    rollback_client, rollback_owner = _guest_client(pg_engine)
    rollback_reference = _create_reference_set(pg_engine, owner_user_id=rollback_owner)
    rollback_service = RecommendationService(
        pg_engine, RecommendationOptions(), MockDifyAdapter()
    )

    def fail_enqueue(*_args: object, **_kwargs: object) -> UUID:
        failed_id = _kwargs.get("aggregate_id")
        assert isinstance(failed_id, UUID)
        failed_recommendation_ids.append(failed_id)
        raise RuntimeError("synthetic enqueue failure")

    failed_recommendation_ids: list[UUID] = []
    monkeypatch.setattr("app.services.recommendations.enqueue_in_transaction", fail_enqueue)
    with pytest.raises(RuntimeError, match="synthetic enqueue failure"):
        rollback_service.create_recommendation(
            owner_user_id=rollback_owner, reference_set_id=rollback_reference
        )
    assert rollback_client.cookies.get("guest_session_dev")
    assert len(failed_recommendation_ids) == 1
    failed_recommendation_id = failed_recommendation_ids[0]

    with Session(pg_engine) as db:
        assert db.scalar(
            select(func.count(Recommendation.id)).where(
                Recommendation.id == failed_recommendation_id
            )
        ) == 0
    assert db.scalar(
        select(func.count(OutboxEvent.id)).where(
            OutboxEvent.event_type == RECOMMENDATION_EVENT_TYPE
        ).where(OutboxEvent.aggregate_id == failed_recommendation_id)
    ) == 0


def test_ready_recommendation_is_preserved_and_does_not_consume_active_limit(
    pg_engine: Engine,
) -> None:
    client, owner_id = _guest_client(pg_engine)
    reference_set_id = _create_reference_set(pg_engine, owner_user_id=owner_id)
    first_response = client.post(
        "/api/v1/recommendations",
        json={"reference_set_id": str(reference_set_id)},
        headers={"Origin": TRUSTED_ORIGIN},
    )
    assert first_response.status_code == 202
    first_id = UUID(first_response.json()["recommendation_id"])
    _process_event(pg_engine, first_id)

    with Session(pg_engine) as db:
        first_before = db.get(Recommendation, first_id)
        assert first_before is not None and first_before.status == "ready"
        first_original = deepcopy(first_before.original_recommendation)
        first_options = deepcopy(first_before.public_options)
        first_run_ref = first_before.run_ref

    second_response = client.post(
        "/api/v1/recommendations",
        json={"reference_set_id": str(reference_set_id)},
        headers={"Origin": TRUSTED_ORIGIN},
    )
    assert second_response.status_code == 202
    second_id = UUID(second_response.json()["recommendation_id"])

    third_response = client.post(
        "/api/v1/recommendations",
        json={"reference_set_id": str(reference_set_id)},
        headers={"Origin": TRUSTED_ORIGIN},
    )
    assert third_response.status_code == 429
    assert third_response.json()["code"] == "RECOMMENDATION_LIMIT_REACHED"

    with Session(pg_engine) as db:
        first_after = db.get(Recommendation, first_id)
        second = db.get(Recommendation, second_id)
        assert first_after is not None and first_after.status == "ready"
        assert first_after.revision == 1
        assert first_after.original_recommendation == first_original
        assert first_after.public_options == first_options
        assert first_after.run_ref == first_run_ref
        assert second is not None and second.status == "analyzing"
        assert db.scalar(
            select(func.count(Recommendation.id)).where(
                Recommendation.owner_user_id == owner_id
            )
        ) == 2


def test_duplicate_analysis_event_persists_original_text_and_never_increments_revision(
    pg_engine: Engine,
) -> None:
    client, owner_id = _guest_client(pg_engine)
    reference_set_id = _create_reference_set(pg_engine, owner_user_id=owner_id)
    created = client.post(
        "/api/v1/recommendations",
        json={"reference_set_id": str(reference_set_id)},
        headers={"Origin": TRUSTED_ORIGIN},
    )
    recommendation_id = UUID(created.json()["recommendation_id"])

    _process_event(pg_engine, recommendation_id)
    _process_event(pg_engine, recommendation_id)

    response = client.get(f"/api/v1/recommendations/{recommendation_id}")
    assert response.status_code == 200
    public = response.json()
    assert public["status"] == "ready"
    assert public["revision"] == 1
    assert public["image_types"] == list(IMAGE_TYPES)
    assert len(public["options"]["language"]["choices"]) == 3
    assert not {"audit", "state", "run_ref", "form_ref", "basis", "original_recommendation"} & set(
        public
    )

    with Session(pg_engine) as db:
        row = db.get(Recommendation, recommendation_id)
        assert row is not None
        assert row.original_recommendation == {
            "fixture_id": "synthetic-recommendation-v1",
            "category_snapshot": "测试品类",
            "field_texts": {
                "language": [
                    "使用简体中文，文案准确简洁。",
                    "Use concise English copy with clear wording.",
                    "不添加新文案，以纯视觉展示商品。",
                ],
                "usage_scene": [
                    "在日常使用场景中展示商品与使用动作。",
                    "以简洁中性场景呈现商品的典型用途。",
                    "使用清晰背景突出商品外观与摆放方式。",
                ],
                "visual_style": [
                    "采用明亮、克制且清楚易读的商业视觉。",
                    "采用自然生活方式风格与柔和光线。",
                    "采用简洁的信息分区和明确视觉层级。",
                ],
                "selling_points": [
                    "突出可核验的外观与结构，不添加未证实信息。",
                    "围绕已知用途和可见细节组织卖点表达。",
                    "避免未提供的性能、认证、材质和尺寸描述。",
                ],
                "platform": [
                    "亚马逊主图",
                    "亚马逊高级A+",
                    "TEMU",
                    "TEMU服饰类",
                    "TIKTOK",
                    "淘宝",
                    "京东",
                    "拼多多",
                    "1688",
                ],
            },
            "image_types": list(IMAGE_TYPES),
        }
        assert row.audit["image_analysis_performed"] is False
        mock_run = db.execute(
            select(MockRecommendationRun).where(
                MockRecommendationRun.business_id == recommendation_id
            )
        ).scalar_one()
        assert mock_run.run_ref == row.run_ref


def test_mock_analyzing_and_failure_are_persistent_and_recommendation_survives_engine_restart(
    pg_engine: Engine,
) -> None:
    client, owner_id = _guest_client(pg_engine)
    reference_set_id = _create_reference_set(pg_engine, owner_user_id=owner_id)
    created = client.post(
        "/api/v1/recommendations",
        json={"reference_set_id": str(reference_set_id)},
        headers={"Origin": TRUSTED_ORIGIN},
    )
    recommendation_id = UUID(created.json()["recommendation_id"])
    cookie = client.cookies.get("guest_session_dev")
    assert cookie is not None

    _process_event(pg_engine, recommendation_id)
    database_url = os.environ["IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL"]
    pg_engine.dispose()
    restarted_engine = create_engine(database_url, pool_pre_ping=True)
    restarted = TestClient(_build_app(restarted_engine))
    restarted.cookies.set("guest_session_dev", cookie)
    response = restarted.get(f"/api/v1/recommendations/{recommendation_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    restarted_engine.dispose()

    pending_client, pending_owner = _guest_client(pg_engine)
    pending_set = _create_reference_set(pg_engine, owner_user_id=pending_owner)
    pending_id = pending_client.post(
        "/api/v1/recommendations",
        json={"reference_set_id": str(pending_set)},
        headers={"Origin": TRUSTED_ORIGIN},
    ).json()["recommendation_id"]
    _process_event(pg_engine, UUID(pending_id), outcome="analyzing")
    assert pending_client.get(f"/api/v1/recommendations/{pending_id}").json()["status"] == (
        "analyzing"
    )

    failed_client, failed_owner = _guest_client(pg_engine)
    failed_set = _create_reference_set(pg_engine, owner_user_id=failed_owner)
    failed_id = failed_client.post(
        "/api/v1/recommendations",
        json={"reference_set_id": str(failed_set)},
        headers={"Origin": TRUSTED_ORIGIN},
    ).json()["recommendation_id"]
    _process_event(pg_engine, UUID(failed_id), outcome="failed")
    failed = failed_client.get(f"/api/v1/recommendations/{failed_id}").json()
    assert failed["status"] == "failed"
    assert failed["error"] == {
        "code": "RECOMMENDATION_FAILED",
        "message": "推荐分析暂时不可用，请稍后重试",
    }
