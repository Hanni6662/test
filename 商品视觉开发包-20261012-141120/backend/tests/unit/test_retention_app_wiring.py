from __future__ import annotations

import app.services.retention_cleanup as retention_cleanup
from app.core.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

TEST_DATABASE_URL = "postgresql+psycopg://test:test@127.0.0.1:1/backend"


def test_maintenance_and_task_creation_routes_remain_closed_without_secret() -> None:
    engine = create_engine(TEST_DATABASE_URL)
    try:
        application = create_app(
            settings=Settings(database_url=TEST_DATABASE_URL),
            engine=engine,
            storage=object(),  # No storage I/O occurs while assembling the app.
        )

        with TestClient(application) as client:
            assert client.get("/internal/v1/maintenance/retention-policy").status_code == 404
            assert client.post("/api/v1/tasks", json={}).status_code == 404
    finally:
        engine.dispose()


def test_maintenance_routes_mount_only_with_independent_secret(monkeypatch) -> None:
    monkeypatch.setattr(
        retention_cleanup,
        "RetentionCleanupService",
        lambda engine, storage: object(),
    )
    engine = create_engine(TEST_DATABASE_URL)
    try:
        application = create_app(
            settings=Settings(
                database_url=TEST_DATABASE_URL,
                maintenance_secret="independent-maintenance-secret-value-123456789",
                cleanup_schedule_timezone="UTC",
                cleanup_schedule_daily_time="04:15",
            ),
            engine=engine,
            storage=object(),
        )

        with TestClient(application) as client:
            response = client.get("/internal/v1/maintenance/retention-policy")
            assert response.status_code == 401
            assert client.post("/api/v1/tasks", json={}).status_code == 404
    finally:
        engine.dispose()
