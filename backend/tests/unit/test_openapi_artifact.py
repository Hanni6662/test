from __future__ import annotations

import json
from pathlib import Path

from app.core.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

_DATABASE_URL = "postgresql+psycopg://test:test@127.0.0.1/image_backend"


def test_checked_in_openapi_contract_matches_explicit_mock_task_app(tmp_path: Path) -> None:
    engine = create_engine(_DATABASE_URL)
    try:
        app = create_app(
            settings=Settings(
                database_url=_DATABASE_URL,
                trusted_origins=frozenset({"http://127.0.0.1:8001"}),
                private_storage_root=tmp_path,
                generation_runtime_enabled=True,
                tasks_api_enabled=True,
            ),
            engine=engine,
        )
        artifact_path = Path(__file__).resolve().parents[2] / "docs" / "openapi.json"
        saved = json.loads(artifact_path.read_text(encoding="utf-8"))
        actual = app.openapi()

        assert saved == actual
        assert "/api/v1/tasks" in saved["paths"]
        assert "/api/v1/tasks/{task_id}/retry" in saved["paths"]
        assert all(not path.startswith("/internal/") for path in saved["paths"])
        with TestClient(app) as client:
            assert client.get("/openapi.json").status_code == 404
            assert client.get("/docs").status_code == 404
    finally:
        engine.dispose()
