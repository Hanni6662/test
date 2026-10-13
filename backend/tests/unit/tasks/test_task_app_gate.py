from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import create_engine


def test_task_creation_api_stays_closed_in_runtime_app(tmp_path: Path) -> None:
    engine = create_engine("postgresql+psycopg://test:test@127.0.0.1/image_backend")
    try:
        app = create_app(
            settings=Settings(
                database_url="postgresql+psycopg://test:test@127.0.0.1/image_backend",
                trusted_origins=frozenset({"http://127.0.0.1:8001"}),
                private_storage_root=tmp_path,
            ),
            engine=engine,
        )

        route_methods = {
            (method.upper(), path)
            for path, operations in app.openapi()["paths"].items()
            for method in operations
        }
        assert ("POST", "/api/v1/tasks") not in route_methods
        assert ("POST", "/api/v1/tasks/{task_id}/preferences") in route_methods
        assert not hasattr(app.state, "task_service")
        with TestClient(app) as client:
            assert client.post("/api/v1/tasks", json={}).status_code == 404
    finally:
        engine.dispose()


def test_task_router_is_mounted_only_with_explicit_runtime_gates(tmp_path: Path) -> None:
    engine = create_engine("postgresql+psycopg://test:test@127.0.0.1/image_backend")
    try:
        app = create_app(
            settings=Settings(
                database_url="postgresql+psycopg://test:test@127.0.0.1/image_backend",
                trusted_origins=frozenset({"http://127.0.0.1:8001"}),
                private_storage_root=tmp_path,
                generation_runtime_enabled=True,
                tasks_api_enabled=True,
            ),
            engine=engine,
        )

        paths = app.openapi()["paths"]
        assert "/api/v1/tasks" in paths
        assert "/api/v1/tasks/{task_id}" in paths
        assert "/api/v1/tasks/{task_id}/retry" in paths
        assert "/api/v1/tasks/{task_id}/images/{image_id}/preview" in paths
        assert hasattr(app.state, "task_service")
        with TestClient(app) as client:
            assert client.get("/api/v1/tasks").status_code == 401
    finally:
        engine.dispose()
