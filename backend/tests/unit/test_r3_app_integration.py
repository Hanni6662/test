from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.main import create_app
from app.services.prompt_callbacks import PromptCallbackService
from app.services.prompt_preferences import PromptPreferenceService
from app.services.recommendation_callbacks import RecommendationCallbackService
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

TEST_DATABASE_URL = "postgresql+psycopg://test:test@127.0.0.1/image_backend"
CALLBACK_SECRET = "app-integration-callback-secret-value-123456789"


def _route_methods(app) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }


def _build_app(tmp_path: Path, *, callback_secret: str | None):
    engine = create_engine("sqlite://")
    settings = Settings(
        database_url=TEST_DATABASE_URL,
        private_storage_root=tmp_path,
        dify_callback_secret=callback_secret,
    )
    return create_app(settings=settings, engine=engine), engine


def test_main_app_fails_closed_when_callback_secret_is_unset(tmp_path: Path) -> None:
    app, engine = _build_app(tmp_path, callback_secret=None)
    try:
        methods = _route_methods(app)
        assert ("POST", "/api/v1/tasks/{task_id}/preferences") in methods
        assert ("POST", "/api/v1/tasks") not in methods
        assert ("POST", "/internal/v1/dify/prompt-results") not in methods
        assert ("POST", "/internal/v1/dify/recommendation-results") not in methods
        assert isinstance(app.state.prompt_callback_service, PromptCallbackService)
        assert isinstance(app.state.recommendation_callback_service, RecommendationCallbackService)
        assert isinstance(app.state.prompt_preference_service, PromptPreferenceService)

        response = TestClient(app).post(
            "/internal/v1/dify/prompt-results",
            json={},
        )
        assert response.status_code == 404
    finally:
        engine.dispose()


def test_main_app_mounts_internal_routes_only_with_callback_secret(tmp_path: Path) -> None:
    app, engine = _build_app(tmp_path, callback_secret=CALLBACK_SECRET)
    try:
        methods = _route_methods(app)
        assert ("POST", "/api/v1/tasks/{task_id}/preferences") in methods
        assert ("POST", "/api/v1/tasks") not in methods
        assert ("POST", "/internal/v1/dify/prompt-results") in methods
        assert ("POST", "/internal/v1/dify/recommendation-results") in methods

        response = TestClient(app).post(
            "/internal/v1/dify/prompt-results",
            json={},
        )
        assert response.status_code == 401
        assert response.json()["code"] == "CALLBACK_AUTH_REQUIRED"
    finally:
        engine.dispose()
