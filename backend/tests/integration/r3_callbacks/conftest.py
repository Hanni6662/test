from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from tests.integration.prompt_workflows.test_prompt_workflows_postgres import (
    _cleanup,
    _PGContext,
)


@pytest.fixture
def pg_context() -> Iterator[_PGContext]:
    database_url = os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires the dedicated PostgreSQL test runtime URL")
    engine: Engine = create_engine(database_url, pool_pre_ping=True)
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
