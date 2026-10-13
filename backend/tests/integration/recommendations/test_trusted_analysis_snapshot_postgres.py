from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from app.core.domain_errors import DomainError
from app.db.models import User
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.recommendation_models import MockRecommendationRun, Recommendation
from app.db.reference_models import ImageAsset, ReferenceSet, ReferenceSetImage
from app.integrations.dify.factory import create_test_dify_adapter
from app.integrations.dify.mock import MockDifyAdapter
from app.services.recommendations import (
    RECOMMENDATION_EVENT_TYPE,
    RecommendationOptions,
    RecommendationService,
    process_recommendation_event,
)
from app.services.trusted_analysis import (
    TRUSTED_ANALYSIS_STATE_KEY,
    TrustedAnalysisSnapshotError,
    load_trusted_analysis_snapshot,
    reference_asset_payload,
)
from app.workers.messages import OutboxMessage
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
        "outbox_events",
        "outbox_receipts",
        "recommendations",
        "mock_recommendation_runs",
    }
    if not required.issubset(set(inspect(engine).get_table_names())):
        engine.dispose()
        pytest.skip("requires the integrated S1-S5 PostgreSQL migration chain")
    context = _PGContext(engine)
    yield context
    with Session(engine) as db, db.begin():
        if context.recommendation_ids:
            event_ids = select(OutboxEvent.id).where(
                OutboxEvent.event_type == RECOMMENDATION_EVENT_TYPE,
                OutboxEvent.aggregate_id.in_(context.recommendation_ids),
            )
            db.execute(delete(OutboxReceipt).where(OutboxReceipt.event_id.in_(event_ids)))
            db.execute(delete(OutboxEvent).where(OutboxEvent.id.in_(event_ids)))
            db.execute(
                delete(MockRecommendationRun).where(
                    MockRecommendationRun.business_id.in_(context.recommendation_ids)
                )
            )
            db.execute(
                delete(Recommendation).where(Recommendation.id.in_(context.recommendation_ids))
            )
        if context.reference_set_ids:
            db.execute(
                delete(ReferenceSetImage).where(
                    ReferenceSetImage.reference_set_id.in_(context.reference_set_ids)
                )
            )
            db.execute(
                delete(ReferenceSet).where(ReferenceSet.id.in_(context.reference_set_ids))
            )
        if context.owner_ids:
            db.execute(delete(ImageAsset).where(ImageAsset.owner_user_id.in_(context.owner_ids)))
            db.execute(delete(User).where(User.id.in_(context.owner_ids)))
    engine.dispose()


def _new_owner(context: _PGContext) -> UUID:
    owner_id = uuid4()
    with Session(context.engine) as db, db.begin():
        db.add(User(id=owner_id, identity_kind="guest", status="active"))
    context.owner_ids.add(owner_id)
    return owner_id


def _database_now(engine: Engine):
    with Session(engine) as db:
        return db.scalar(select(func.clock_timestamp()))


def _create_reference_set(
    context: _PGContext,
    *,
    owner_id: UUID,
    slots: tuple[str, ...] = ("PRIMARY",),
) -> UUID:
    reference_set_id = uuid4()
    now = _database_now(context.engine)
    with Session(context.engine) as db, db.begin():
        db.add(
            ReferenceSet(
                id=reference_set_id,
                owner_user_id=owner_id,
                product_category="测试品类",
                status="ready",
                created_at=now,
                last_activity_at=now,
                ready_at=now,
                expires_at=now + timedelta(days=10),
            )
        )
        db.flush()
        for slot in slots:
            position = {"PRIMARY": 0, "AUX01": 1, "AUX02": 2, "AUX03": 3}[slot]
            reference_image_id = uuid4()
            source_id = uuid4()
            model_id = uuid4()
            source_digest = hashlib.sha256(source_id.bytes).hexdigest()
            model_digest = hashlib.sha256(model_id.bytes).hexdigest()
            db.add_all(
                [
                    ImageAsset(
                        id=source_id,
                        owner_user_id=owner_id,
                        asset_kind="reference_source",
                        object_key=f"trusted-test/{source_id}/source.png",
                        content_sha256=source_digest,
                        mime_type="image/png",
                        size_bytes=256,
                        width=64,
                        height=64,
                    ),
                    ImageAsset(
                        id=model_id,
                        owner_user_id=owner_id,
                        asset_kind="reference_model_input",
                        object_key=f"trusted-test/{model_id}/model.png",
                        content_sha256=model_digest,
                        mime_type="image/webp",
                        size_bytes=128,
                        width=32,
                        height=32,
                    ),
                ]
            )
            db.flush()
            db.add(
                ReferenceSetImage(
                    id=reference_image_id,
                    reference_set_id=reference_set_id,
                    owner_user_id=owner_id,
                    slot=slot,
                    position=position,
                    source_asset_id=source_id,
                    model_asset_id=model_id,
                    content_sha256=model_digest,
                    created_at=now,
                )
            )
    context.reference_set_ids.add(reference_set_id)
    return reference_set_id


def _create_and_process(
    context: _PGContext,
    *,
    owner_id: UUID,
    reference_set_id: UUID,
    adapter: MockDifyAdapter | None = None,
) -> UUID:
    service = RecommendationService(
        context.engine,
        RecommendationOptions(),
        adapter or MockDifyAdapter(),
    )
    recommendation_id = service.create_recommendation(
        owner_user_id=owner_id,
        reference_set_id=reference_set_id,
    )
    context.recommendation_ids.add(recommendation_id)
    with Session(context.engine) as db, db.begin():
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
            dify_port=adapter or MockDifyAdapter(),
        )
    return recommendation_id


@pytest.mark.parametrize(
    "slots",
    [("PRIMARY",), ("PRIMARY", "AUX01"), ("PRIMARY", "AUX01", "AUX02", "AUX03")],
)
def test_postgres_persists_complete_synthetic_snapshot_for_primary_and_auxiliary_slots(
    pg_context: _PGContext, slots: tuple[str, ...]
) -> None:
    owner_id = _new_owner(pg_context)
    reference_set_id = _create_reference_set(pg_context, owner_id=owner_id, slots=slots)
    recommendation_id = _create_and_process(
        pg_context, owner_id=owner_id, reference_set_id=reference_set_id
    )

    with Session(pg_context.engine) as db:
        recommendation = db.get(Recommendation, recommendation_id)
        run = db.execute(
            select(MockRecommendationRun).where(
                MockRecommendationRun.business_id == recommendation_id
            )
        ).scalar_one()
        assert recommendation is not None
        assert recommendation.status == "ready"
        assert recommendation.audit["image_analysis_performed"] is False
        assert recommendation.state[TRUSTED_ANALYSIS_STATE_KEY] == run.state[
            TRUSTED_ANALYSIS_STATE_KEY
        ]
        snapshot = recommendation.state[TRUSTED_ANALYSIS_STATE_KEY]
        assert [reference["alias"] for reference in snapshot["references"]] == list(slots)
        assert [relation["alias"] for relation in snapshot["auxiliary_relations"]] == list(
            slots[1:]
        )
        assert all(
            relation["provenance"]["synthetic"] is True
            for relation in snapshot["auxiliary_relations"]
        )


def test_postgres_persists_ignored_conflict_from_test_only_seam(
    pg_context: _PGContext,
) -> None:
    owner_id = _new_owner(pg_context)
    reference_set_id = _create_reference_set(
        pg_context, owner_id=owner_id, slots=("PRIMARY", "AUX01", "AUX02")
    )
    adapter = create_test_dify_adapter(
        auxiliary_relation_overrides={"AUX01": "ignored_conflict"}
    )
    recommendation_id = _create_and_process(
        pg_context,
        owner_id=owner_id,
        reference_set_id=reference_set_id,
        adapter=adapter,
    )

    with Session(pg_context.engine) as db:
        recommendation = db.get(Recommendation, recommendation_id)
        assert recommendation is not None
        relations = recommendation.state[TRUSTED_ANALYSIS_STATE_KEY]["auxiliary_relations"]
        assert relations[0]["relation"] == "ignored_conflict"
        assert relations[0]["provenance"]["rule_version"] == "synthetic-test-override-v1"


def test_postgres_sparse_aliases_fail_before_recommendation_is_created(
    pg_context: _PGContext,
) -> None:
    owner_id = _new_owner(pg_context)
    sparse_reference_set_id = _create_reference_set(
        pg_context, owner_id=owner_id, slots=("PRIMARY", "AUX02")
    )
    service = RecommendationService(
        pg_context.engine, RecommendationOptions(), MockDifyAdapter()
    )

    with pytest.raises(DomainError) as caught:
        service.create_recommendation(
            owner_user_id=owner_id,
            reference_set_id=sparse_reference_set_id,
        )

    assert caught.value.code == "REFERENCE_SET_NOT_FOUND"
    with Session(pg_context.engine) as db:
        assert db.scalar(
            select(func.count(Recommendation.id)).where(
                Recommendation.reference_set_id == sparse_reference_set_id
            )
        ) == 0


def test_postgres_owner_foreign_keys_reject_assets_from_another_owner(
    pg_context: _PGContext,
) -> None:
    owner_id = _new_owner(pg_context)
    other_owner_id = _new_owner(pg_context)
    reference_set_id = uuid4()
    source_id = uuid4()
    model_id = uuid4()
    reference_image_id = uuid4()
    now = _database_now(pg_context.engine)
    digest = hashlib.sha256(source_id.bytes).hexdigest()
    with Session(pg_context.engine) as db, db.begin():
        db.add(
            ReferenceSet(
                id=reference_set_id,
                owner_user_id=owner_id,
                product_category="测试品类",
                status="ready",
                created_at=now,
                last_activity_at=now,
                ready_at=now,
                expires_at=now + timedelta(days=10),
            )
        )
    with Session(pg_context.engine) as db, db.begin():
        db.add_all(
            [
                ImageAsset(
                    id=source_id,
                    owner_user_id=other_owner_id,
                    asset_kind="reference_source",
                    object_key=f"trusted-test/{source_id}/source.png",
                    content_sha256=digest,
                    mime_type="image/png",
                    size_bytes=256,
                    width=64,
                    height=64,
                ),
                ImageAsset(
                    id=model_id,
                    owner_user_id=other_owner_id,
                    asset_kind="reference_model_input",
                    object_key=f"trusted-test/{model_id}/model.png",
                    content_sha256=hashlib.sha256(model_id.bytes).hexdigest(),
                    mime_type="image/webp",
                    size_bytes=128,
                    width=32,
                    height=32,
                ),
            ]
        )
        db.flush()
    with pytest.raises(IntegrityError):
        with Session(pg_context.engine) as db, db.begin():
            db.add(
                ReferenceSetImage(
                    id=reference_image_id,
                    reference_set_id=reference_set_id,
                    owner_user_id=owner_id,
                    slot="PRIMARY",
                    position=0,
                    source_asset_id=source_id,
                    model_asset_id=model_id,
                    content_sha256=hashlib.sha256(model_id.bytes).hexdigest(),
                    created_at=now,
                )
            )
            db.flush()
    pg_context.reference_set_ids.add(reference_set_id)


def test_postgres_new_recommendation_does_not_change_an_older_run_snapshot(
    pg_context: _PGContext,
) -> None:
    owner_id = _new_owner(pg_context)
    reference_set_id = _create_reference_set(
        pg_context, owner_id=owner_id, slots=("PRIMARY", "AUX01", "AUX02", "AUX03")
    )
    first_id = _create_and_process(
        pg_context, owner_id=owner_id, reference_set_id=reference_set_id
    )
    with Session(pg_context.engine) as db:
        first = db.get(Recommendation, first_id)
        assert first is not None
        first_snapshot = first.state[TRUSTED_ANALYSIS_STATE_KEY]
        first_run = db.execute(
            select(MockRecommendationRun).where(MockRecommendationRun.business_id == first_id)
        ).scalar_one()
        first_run_snapshot = first_run.state[TRUSTED_ANALYSIS_STATE_KEY]

    second_id = _create_and_process(
        pg_context,
        owner_id=owner_id,
        reference_set_id=reference_set_id,
        adapter=create_test_dify_adapter(
            auxiliary_relation_overrides={"AUX01": "ignored_conflict"}
        ),
    )
    assert second_id != first_id
    with Session(pg_context.engine) as db:
        first_after = db.get(Recommendation, first_id)
        first_run_after = db.execute(
            select(MockRecommendationRun).where(MockRecommendationRun.business_id == first_id)
        ).scalar_one()
        second = db.get(Recommendation, second_id)
        assert first_after is not None and second is not None
        assert first_after.state[TRUSTED_ANALYSIS_STATE_KEY] == first_snapshot
        assert first_run_after.state[TRUSTED_ANALYSIS_STATE_KEY] == first_run_snapshot
        assert (
            second.state[TRUSTED_ANALYSIS_STATE_KEY]["snapshot_id"]
            != first_snapshot["snapshot_id"]
        )


def test_postgres_missing_old_snapshot_fails_closed(pg_context: _PGContext) -> None:
    owner_id = _new_owner(pg_context)
    reference_set_id = _create_reference_set(
        pg_context, owner_id=owner_id, slots=("PRIMARY", "AUX01")
    )
    recommendation_id = _create_and_process(
        pg_context, owner_id=owner_id, reference_set_id=reference_set_id
    )
    with Session(pg_context.engine) as db, db.begin():
        recommendation = db.get(Recommendation, recommendation_id)
        assert recommendation is not None
        recommendation.state = {"stage": "ready"}  # existing S5-shaped row with no R1 snapshot
    with Session(pg_context.engine) as db:
        recommendation = db.get(Recommendation, recommendation_id)
        assert recommendation is not None
        raw_snapshot = recommendation.state.get(TRUSTED_ANALYSIS_STATE_KEY)
        revision = recommendation.revision
        saved_assets = RecommendationService._reference_asset_summary(
            db,
            owner_user_id=owner_id,
            reference_set_id=reference_set_id,
        )
        references = [reference_asset_payload(asset) for asset in saved_assets]
    with pytest.raises(TrustedAnalysisSnapshotError):
        load_trusted_analysis_snapshot(
            raw_snapshot,
            reference_set_id=reference_set_id,
            recommendation_id=recommendation_id,
            recommendation_revision=revision,
            category="测试品类",
            expected_references=references,
        )


def test_postgres_model_digest_mismatch_fails_before_trusted_snapshot_or_mock_run(
    pg_context: _PGContext,
) -> None:
    owner_id = _new_owner(pg_context)
    reference_set_id = _create_reference_set(
        pg_context, owner_id=owner_id, slots=("PRIMARY",)
    )
    options = RecommendationOptions()
    adapter = MockDifyAdapter()
    service = RecommendationService(pg_context.engine, options, adapter)
    recommendation_id = service.create_recommendation(
        owner_user_id=owner_id, reference_set_id=reference_set_id
    )
    pg_context.recommendation_ids.add(recommendation_id)

    with Session(pg_context.engine) as db, db.begin():
        reference_image = db.execute(
            select(ReferenceSetImage).where(
                ReferenceSetImage.reference_set_id == reference_set_id
            )
        ).scalar_one()
        model_asset = db.get(ImageAsset, reference_image.model_asset_id)
        assert model_asset is not None
        reference_image.content_sha256 = (
            "0" if model_asset.content_sha256[0] != "0" else "1"
        ) + model_asset.content_sha256[1:]

    with Session(pg_context.engine) as db, db.begin():
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
            options=options,
            dify_port=adapter,
        )

    with Session(pg_context.engine) as db:
        recommendation = db.get(Recommendation, recommendation_id)
        assert recommendation is not None
        assert recommendation.status == "failed"
        assert recommendation.failure_code == "REFERENCE_SET_UNAVAILABLE"
        assert TRUSTED_ANALYSIS_STATE_KEY not in recommendation.state
        assert (
            db.execute(
                select(MockRecommendationRun).where(
                    MockRecommendationRun.business_id == recommendation_id
                )
            ).scalar_one_or_none()
            is None
        )
