from __future__ import annotations

import io
import os
import time
from uuid import UUID, uuid4

import httpx
import pytest
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.recommendation_models import MockRecommendationRun, Recommendation
from app.workers.messages import ClaimedEvent
from app.workers.options import QueueOptions
from app.workers.publisher import CeleryTaskPublisher
from kombu import Connection
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session


def _image_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (48, 36), (80, 130, 190)).save(output, format="PNG")
    return output.getvalue()


def _queue_state(broker_url: str, queue_name: str) -> tuple[int, int]:
    with Connection(broker_url, connect_timeout=5) as connection:
        channel = connection.channel()
        try:
            state = channel.queue_declare(queue=queue_name, passive=True)
            return state.message_count, state.consumer_count
        finally:
            channel.close()


def _wait_until_ready(client: httpx.Client, recommendation_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 60
    latest: dict[str, object] | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/recommendations/{recommendation_id}")
        response.raise_for_status()
        latest = response.json()
        if latest.get("status") != "analyzing":
            return latest
        time.sleep(0.25)
    raise AssertionError(f"worker did not finish recommendation; last={latest}")


@pytest.mark.integration
def test_http_upload_outbox_real_worker_duplicate_and_owner_isolation() -> None:
    base_url = os.getenv("IMAGE_BACKEND_TEST_API_URL")
    origin = os.getenv("IMAGE_BACKEND_TEST_API_ORIGIN")
    runtime_url = os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL")
    broker_url = os.getenv("IMAGE_BACKEND_TEST_BROKER_URL")
    if not all((base_url, origin, runtime_url, broker_url)):
        pytest.skip("requires the isolated HTTP API, PostgreSQL, and RabbitMQ Worker stack")

    queue_name = os.getenv("IMAGE_BACKEND_QUEUE_NAME", "image-backend-events")
    options = QueueOptions(broker_url=broker_url, queue_name=queue_name)
    engine = create_engine(runtime_url, pool_pre_ping=True)
    try:
        with httpx.Client(base_url=base_url, timeout=30) as owner:
            health = owner.get("/health/ready")
            health.raise_for_status()
            _messages, consumers = _queue_state(broker_url, queue_name)
            assert consumers >= 1, "the independent Celery Worker is not consuming"

            entered = owner.post("/api/v1/auth/guest", headers={"Origin": origin})
            assert entered.status_code == 200
            owner_user_id = UUID(entered.json()["user_id"])

            uploaded = owner.post(
                "/api/v1/reference-sets",
                headers={"Origin": origin},
                files=[
                    ("product_category", (None, "测试商品")),
                    ("primary_product_image", ("reference.png", _image_bytes(), "image/png")),
                ],
            )
            assert uploaded.status_code == 201, uploaded.text
            reference_set_id = UUID(uploaded.json()["reference_set_id"])

            created = owner.post(
                "/api/v1/recommendations",
                headers={"Origin": origin},
                json={"reference_set_id": str(reference_set_id)},
            )
            assert created.status_code == 202, created.text
            recommendation_id = UUID(created.json()["recommendation_id"])
            assert created.json()["status"] == "analyzing"

            public = _wait_until_ready(owner, str(recommendation_id))
            assert public["status"] == "ready"
            assert public["revision"] == 1
            assert public["reference_set_id"] == str(reference_set_id)
            assert public["image_types"] == [
                "品牌导购主视觉",
                "核心卖点信息图",
                "生活方式海报",
                "多场景展示图",
                "场景化卖点图",
                "使用体验图",
                "SKU展示图",
                "细节展示图",
            ]
            assert [
                choice["code"] for choice in public["options"]["language"]["choices"]
            ] == ["A", "B", "C"]
            assert not {
                "audit",
                "basis",
                "state",
                "run_ref",
                "form_ref",
                "original_recommendation",
                "credentials",
                "token",
            } & set(public)

            deadline = time.monotonic() + 15
            event = None
            receipt = None
            while time.monotonic() < deadline:
                with Session(engine) as db:
                    event = db.execute(
                        select(OutboxEvent).where(
                            OutboxEvent.event_type == "recommendation.analyze.v1",
                            OutboxEvent.aggregate_id == recommendation_id,
                        )
                    ).scalar_one()
                    receipt = db.get(OutboxReceipt, event.id)
                    if event.status == "published" and receipt is not None:
                        break
                time.sleep(0.1)
            assert event is not None and event.status == "published"
            assert receipt is not None and receipt.status == "completed"

            with Session(engine) as db:
                event = db.get(OutboxEvent, event.id)
                assert event is not None
                receipt = db.get(OutboxReceipt, event.id)
                assert receipt is not None
                assert event.payload == {"reference_set_id": str(reference_set_id)}
                assert receipt.status == "completed"
                assert receipt.attempt_count == 1
                row = db.get(Recommendation, recommendation_id)
                assert row is not None and row.owner_user_id == owner_user_id
                assert row.audit["image_analysis_performed"] is False
                original = row.original_recommendation
                valid_until = row.valid_until
                data_expires_at = row.data_expires_at
                run = db.execute(
                    select(MockRecommendationRun).where(
                        MockRecommendationRun.business_id == recommendation_id
                    )
                ).scalar_one()
                assert run.reference_set_id == reference_set_id
                assert run.original_recommendation == original
                assert db.scalar(
                    select(func.count(MockRecommendationRun.id)).where(
                        MockRecommendationRun.business_id == recommendation_id
                    )
                ) == 1
                event_payload = {
                    key: str(value) for key, value in event.payload.items()
                }
                duplicate = ClaimedEvent(
                    event_id=event.id,
                    event_type=event.event_type,
                    aggregate_id=event.aggregate_id,
                    operation_version=event.operation_version,
                    schema_version=event.schema_version,
                    payload=event_payload,
                    claim_token=uuid4(),
                    attempt_count=event.attempt_count,
                )

            publisher = CeleryTaskPublisher(options)
            publisher.publish(duplicate)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                messages, active_consumers = _queue_state(broker_url, queue_name)
                if messages == 0 and active_consumers >= 1:
                    break
                time.sleep(0.2)
            assert messages == 0 and active_consumers >= 1

            with Session(engine) as db:
                row_after_duplicate = db.get(Recommendation, recommendation_id)
                receipt_after_duplicate = db.get(OutboxReceipt, duplicate.event_id)
                assert row_after_duplicate is not None
                assert row_after_duplicate.status == "ready"
                assert row_after_duplicate.revision == 1
                assert row_after_duplicate.original_recommendation == original
                assert row_after_duplicate.public_options == public["options"]
                assert row_after_duplicate.valid_until == valid_until
                assert row_after_duplicate.data_expires_at == data_expires_at
                assert receipt_after_duplicate is not None
                assert receipt_after_duplicate.status == "completed"
                assert receipt_after_duplicate.attempt_count == 1
                assert db.scalar(
                    select(func.count(MockRecommendationRun.id)).where(
                        MockRecommendationRun.business_id == recommendation_id
                    )
                ) == 1

            missing_cookie = httpx.Client(base_url=base_url, timeout=15)
            assert missing_cookie.get(
                f"/api/v1/recommendations/{recommendation_id}"
            ).status_code == 401
            missing_cookie.close()

            other = httpx.Client(base_url=base_url, timeout=30)
            try:
                other_entered = other.post(
                    "/api/v1/auth/guest", headers={"Origin": origin}
                )
                assert other_entered.status_code == 200
                assert other.get(
                    f"/api/v1/recommendations/{recommendation_id}"
                ).status_code == 404
                cross_owner = other.post(
                    "/api/v1/recommendations",
                    headers={"Origin": origin},
                    json={"reference_set_id": str(reference_set_id)},
                )
                assert cross_owner.status_code == 404
            finally:
                other.close()
    finally:
        engine.dispose()
