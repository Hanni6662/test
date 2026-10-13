from __future__ import annotations

import io
import os
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from app.db.models import User
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.recommendation_models import Recommendation
from app.db.reference_models import (
    ImageAsset,
    ReferenceSet,
    ReferenceSetImage,
    ReferenceUploadObject,
)
from app.db.retention_models import RetentionPolicy
from app.integrations.dify.mock import MockDifyAdapter
from app.integrations.storage.local import LocalPrivateStorage
from app.services.recommendations import RecommendationOptions, RecommendationService
from app.services.references import ReferenceOptions, ReferenceService, ReferenceUploadFile
from PIL import Image
from sqlalchemy import create_engine, delete, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


def _image_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (12, 8), (40, 80, 120)).save(output, format="PNG")
    return output.getvalue()


def _cleanup_owner(engine: Engine, owner_user_id: UUID) -> None:
    with Session(engine) as db, db.begin():
        recommendation_ids = list(
            db.scalars(
                select(Recommendation.id).where(Recommendation.owner_user_id == owner_user_id)
            ).all()
        )
        if recommendation_ids:
            event_ids = list(
                db.scalars(
                    select(OutboxEvent.id).where(OutboxEvent.aggregate_id.in_(recommendation_ids))
                ).all()
            )
            if event_ids:
                db.execute(delete(OutboxReceipt).where(OutboxReceipt.event_id.in_(event_ids)))
                db.execute(delete(OutboxEvent).where(OutboxEvent.id.in_(event_ids)))
            db.execute(delete(Recommendation).where(Recommendation.id.in_(recommendation_ids)))
        db.execute(
            delete(ReferenceSetImage).where(ReferenceSetImage.owner_user_id == owner_user_id)
        )
        db.execute(
            delete(ReferenceUploadObject).where(
                ReferenceUploadObject.owner_user_id == owner_user_id
            )
        )
        db.execute(delete(ImageAsset).where(ImageAsset.owner_user_id == owner_user_id))
        db.execute(delete(ReferenceSet).where(ReferenceSet.owner_user_id == owner_user_id))
        db.execute(delete(User).where(User.id == owner_user_id))


@pytest.mark.integration
def test_new_reference_and_recommendation_snapshot_the_current_policy(tmp_path) -> None:
    database_url = os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires the dedicated PostgreSQL test runtime URL")
    engine = create_engine(database_url, pool_pre_ping=True)
    if not {"retention_policies", "cleanup_runs"}.issubset(set(inspect(engine).get_table_names())):
        engine.dispose()
        pytest.skip("requires the s011 retention migration")

    owner_user_id = uuid4()
    previous_policy: tuple[int, int, int, int] | None = None
    try:
        with Session(engine) as db, db.begin():
            db.add(User(id=owner_user_id, identity_kind="guest", status="active"))
            policy = db.execute(
                select(RetentionPolicy).where(RetentionPolicy.id == 1).with_for_update()
            ).scalar_one()
            previous_policy = (
                policy.task_retention_days,
                policy.reference_retention_days,
                policy.recommendation_retention_days,
                policy.revision,
            )
            policy.task_retention_days = 14
            policy.reference_retention_days = 12
            policy.recommendation_retention_days = 13
            policy.revision += 1
            test_revision = policy.revision

        storage = LocalPrivateStorage(tmp_path / "private")
        references = ReferenceService(engine, storage, ReferenceOptions())
        reference = references.upload_reference_set(
            owner_user_id=owner_user_id,
            product_category="测试品类",
            primary_file=ReferenceUploadFile(io.BytesIO(_image_bytes()), "image/png"),
            supplementary_files=[],
        )
        recommendation_service = RecommendationService(
            engine,
            RecommendationOptions(),
            MockDifyAdapter(),
        )
        recommendation_id = recommendation_service.create_recommendation(
            owner_user_id=owner_user_id,
            reference_set_id=reference.reference_set_id,
        )

        with Session(engine) as db:
            reference_row = db.get(ReferenceSet, reference.reference_set_id)
            recommendation = db.get(Recommendation, recommendation_id)
            assert reference_row is not None and reference_row.ready_at is not None
            assert reference_row.expires_at is not None
            assert (reference_row.expires_at - reference_row.ready_at) == timedelta(days=12)
            assert (reference_row.retention_days, reference_row.retention_policy_revision) == (
                12,
                test_revision,
            )
            assert recommendation is not None
            assert (
                recommendation.retention_days,
                recommendation.retention_policy_revision,
            ) == (13, test_revision)
            assert recommendation.data_expires_at - recommendation.created_at == timedelta(days=13)
    finally:
        try:
            _cleanup_owner(engine, owner_user_id)
            if previous_policy is not None:
                with Session(engine) as db, db.begin():
                    policy = db.execute(
                        select(RetentionPolicy).where(RetentionPolicy.id == 1).with_for_update()
                    ).scalar_one()
                    (
                        policy.task_retention_days,
                        policy.reference_retention_days,
                        policy.recommendation_retention_days,
                        policy.revision,
                    ) = previous_policy
        finally:
            engine.dispose()
