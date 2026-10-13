from __future__ import annotations

import os
import time
from collections.abc import Iterator

import pytest
from app.db.generation_models import (
    GenerationAttemptContext,
    GenerationCapacityReservation,
    GenerationResult,
    MockGenerationInvocation,
)
from app.db.reference_models import ImageAsset
from app.db.task_models import Task, TaskItem, TaskItemAttempt
from app.integrations.storage.local import LocalPrivateStorage
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tests.integration.generation.test_generation_runtime_postgres import (
    _cleanup,
    _create_task_with_prompt,
    _PGContext,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    if os.getenv("IMAGE_BACKEND_GENERATION_E2E") != "true":
        pytest.skip("requires the isolated two-worker generation runner")
    database_url = os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL")
    if not database_url:
        pytest.skip("requires the image-backend-test PostgreSQL runtime URL")
    engine = create_engine(database_url, pool_pre_ping=True)
    yield engine
    engine.dispose()


def test_two_prefork_workers_complete_and_persist_private_outputs(pg_engine: Engine) -> None:
    context = _PGContext(pg_engine)
    task_id, _first_item_id, owner_id, _excluded_reference_id = _create_task_with_prompt(
        context, image_count=2
    )
    storage = LocalPrivateStorage(os.environ["IMAGE_BACKEND_PRIVATE_STORAGE_ROOT"])
    object_keys: list[str] = []
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            with Session(pg_engine) as db:
                task = db.get(Task, task_id)
                items = (
                    db.execute(
                        select(TaskItem)
                        .where(TaskItem.task_id == task_id)
                        .order_by(TaskItem.position)
                    )
                    .scalars()
                    .all()
                )
                if task is not None and task.status in {
                    "succeeded",
                    "partially_succeeded",
                    "failed",
                }:
                    break
            time.sleep(1)
        else:
            pytest.fail("two-worker generation run did not reach a terminal task state")

        assert task is not None and task.status == "succeeded"
        assert len(items) == 2
        assert all(item.status == "succeeded" and item.preview_ready for item in items)

        with Session(pg_engine) as db:
            results = (
                db.execute(select(GenerationResult).where(GenerationResult.task_id == task_id))
                .scalars()
                .all()
            )
            attempts = (
                db.execute(
                    select(TaskItemAttempt)
                    .join(TaskItem, TaskItem.id == TaskItemAttempt.task_item_id)
                    .where(TaskItem.task_id == task_id)
                )
                .scalars()
                .all()
            )
            contexts = (
                db.execute(
                    select(GenerationAttemptContext).where(
                        GenerationAttemptContext.task_id == task_id
                    )
                )
                .scalars()
                .all()
            )
            reservations = (
                db.execute(
                    select(GenerationCapacityReservation).where(
                        GenerationCapacityReservation.task_id == task_id
                    )
                )
                .scalars()
                .all()
            )
            accepted_submits = db.scalar(
                select(func.count(MockGenerationInvocation.id)).where(
                    MockGenerationInvocation.request_key.in_([attempt.id for attempt in attempts]),
                    MockGenerationInvocation.outcome == "accepted",
                )
            )
            assets = {
                asset.id: asset
                for asset in db.execute(
                    select(ImageAsset).where(ImageAsset.owner_user_id == owner_id)
                ).scalars()
            }

        assert len(results) == 2
        assert len(attempts) == len(contexts) == 2
        assert accepted_submits == 2
        assert len(reservations) == 2
        assert all(reservation.status == "released" for reservation in reservations)
        for context_row in contexts:
            object_keys.extend([context_row.original_object_key, context_row.preview_object_key])
            original = storage.stat(context_row.original_object_key)
            preview = storage.stat(context_row.preview_object_key)
            assert original is not None and original.sha256 == context_row.original_sha256
            assert preview is not None and preview.sha256 == context_row.preview_sha256
            assert context_row.save_state == "attached"
        for result in results:
            assert assets[result.original_asset_id].asset_kind == "generation_original"
            assert assets[result.preview_asset_id].asset_kind == "generation_preview"
    finally:
        with Session(pg_engine) as db:
            object_keys.extend(
                key
                for context_row in db.execute(
                    select(GenerationAttemptContext).where(
                        GenerationAttemptContext.task_id == task_id
                    )
                ).scalars()
                for key in (
                    context_row.original_object_key,
                    context_row.preview_object_key,
                )
            )
        for object_key in object_keys:
            storage.delete(object_key)
        _cleanup(context)
