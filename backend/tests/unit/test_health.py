from __future__ import annotations

from uuid import UUID

from app.core.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

TEST_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:1/image_backend"


class UnavailableEngine:
    def connect(self) -> None:
        raise OperationalError(
            "SELECT 1",
            {},
            RuntimeError("sensitive connection detail must not leave this boundary"),
        )

    def dispose(self) -> None:
        pass


def make_app() -> tuple[object, UnavailableEngine]:
    engine = UnavailableEngine()
    settings = Settings(database_url=TEST_DATABASE_URL)
    return create_app(settings=settings, engine=engine), engine


def test_live_does_not_depend_on_database() -> None:
    app, _ = make_app()

    with TestClient(app) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "live"}
    assert UUID(response.headers["x-request-id"])


def test_ready_returns_non_ready_without_database_details() -> None:
    app, _ = make_app()

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "sensitive connection detail" not in response.text


def test_public_errors_use_server_request_id_and_sanitized_shape() -> None:
    app, _ = make_app()

    with TestClient(app) as client:
        response = client.get("/missing", headers={"X-Request-ID": "client-controlled-id"})

    payload = response.json()
    assert response.status_code == 404
    assert set(payload) == {"code", "message", "request_id"}
    assert payload["request_id"] == response.headers["x-request-id"]
    assert payload["request_id"] != "client-controlled-id"
    UUID(payload["request_id"])

