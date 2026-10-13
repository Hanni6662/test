from __future__ import annotations

from io import BytesIO
from uuid import UUID, uuid4

from app.core.config import Settings
from app.main import create_app
from app.services.original_downloads import DownloadGate, OriginalDownload
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

DATABASE_URL = "postgresql+psycopg://test:test@127.0.0.1:1/image_backend"
COOKIE_NAME = "guest_session_dev"
COOKIE_VALUE = "isolated-test-cookie"
BODY = b"validated-original-image"


class _AuthService:
    cookie_name = COOKIE_NAME
    identity = type(
        "Identity",
        (),
        {
            "user_id": UUID("11111111-1111-4111-8111-111111111111"),
            "identity_kind": "guest",
            "session_id": UUID("22222222-2222-4222-8222-222222222222"),
        },
    )()

    def __init__(self) -> None:
        self.authenticate_calls = 0

    def authenticate(self, credential: str | None):
        self.authenticate_calls += 1
        return self.identity if credential == COOKIE_VALUE else None


class _TaskService:
    def __init__(self) -> None:
        self.calls = 0

    def open_original(self, **kwargs: object) -> OriginalDownload:
        self.calls += 1
        lease = kwargs["lease"]
        recheck = kwargs["identity_recheck"]
        assert callable(recheck)
        recheck()
        return OriginalDownload(
            reader=BytesIO(BODY),
            content_type="image/png",
            size_bytes=len(BODY),
            filename=(
                f"task_{kwargs['task_id']}_image_{kwargs['image_id']}.png"
            ),
            lease=lease,  # type: ignore[arg-type]
        )


class _UnavailableGate:
    def try_acquire(self):
        return None


def _client(*, tasks_enabled: bool = True, gate: object | None = None):
    engine = create_engine(DATABASE_URL)
    app = create_app(
        settings=Settings(
            database_url=DATABASE_URL,
            trusted_origins=frozenset({"http://testserver"}),
            use_dev_cookie=True,
            generation_runtime_enabled=tasks_enabled,
            tasks_api_enabled=tasks_enabled,
        ),
        engine=engine,
        storage=object(),
    )
    auth = _AuthService()
    tasks = _TaskService()
    resolved_gate = gate or DownloadGate(max_concurrent=1, timeout_seconds=30)
    app.state.auth_service = auth
    app.state.task_service = tasks
    app.state.storage = object()
    if tasks_enabled:
        app.state.original_download_gate = resolved_gate
    return TestClient(app), auth, tasks, resolved_gate, engine


def _path() -> str:
    return f"/api/v1/tasks/{uuid4()}/images/{uuid4()}/original"


def _cookie_header() -> dict[str, str]:
    return {"Cookie": f"{COOKIE_NAME}={COOKIE_VALUE}"}


def test_original_download_route_returns_binary_headers_and_releases_gate() -> None:
    client, auth, tasks, gate, engine = _client()
    try:
        with client:
            response = client.get(_path(), headers=_cookie_header())

        assert response.status_code == 200
        assert response.content == BODY
        assert response.headers["content-type"] == "image/png"
        assert response.headers["content-disposition"].startswith("attachment; filename=\"task_")
        assert response.headers["content-length"] == str(len(BODY))
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["accept-ranges"] == "none"
        assert response.headers["x-request-id"]
        assert auth.authenticate_calls == 2
        assert tasks.calls == 1
        lease = gate.try_acquire()
        assert lease is not None
        lease.release()
    finally:
        engine.dispose()


def test_original_download_rejects_unknown_query_and_nonempty_body() -> None:
    client, _auth, tasks, _gate, engine = _client()
    try:
        with client:
            query = client.get(_path() + "?object_key=private/file", headers=_cookie_header())
            body = client.request("GET", _path(), headers=_cookie_header(), content=b"unexpected")

        assert query.status_code == 400
        assert query.json()["code"] == "REQUEST_INVALID"
        assert body.status_code == 400
        assert body.json()["code"] == "REQUEST_INVALID"
        assert tasks.calls == 0
    finally:
        engine.dispose()


def test_original_download_requires_auth_and_valid_uuid_before_opening() -> None:
    client, _auth, tasks, _gate, engine = _client()
    try:
        with client:
            anonymous = client.get(_path())
            invalid_uuid = client.get(
                "/api/v1/tasks/not-a-uuid/images/also-invalid/original",
                headers=_cookie_header(),
            )

        assert anonymous.status_code == 401
        assert anonymous.json()["code"] == "AUTH_REQUIRED"
        assert invalid_uuid.status_code == 422
        assert tasks.calls == 0
    finally:
        engine.dispose()


def test_original_download_busy_returns_safe_retry_response_without_service_read() -> None:
    client, _auth, tasks, _gate, engine = _client(gate=_UnavailableGate())
    try:
        with client:
            response = client.get(_path(), headers=_cookie_header())

        assert response.status_code == 503
        assert response.json()["code"] == "DOWNLOAD_BUSY"
        assert response.headers["retry-after"] == "5"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-request-id"] == response.json()["request_id"]
        assert tasks.calls == 0
    finally:
        engine.dispose()


def test_original_download_route_is_absent_when_tasks_gate_is_closed() -> None:
    client, _auth, tasks, _gate, engine = _client(tasks_enabled=False)
    try:
        with client:
            response = client.get(_path(), headers=_cookie_header())

        assert response.status_code == 404
        assert tasks.calls == 0
    finally:
        engine.dispose()
