from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.api.auth import router as auth_router
from app.api.billing import router as billing_router
from app.core.domain_errors import DomainError
from app.services.auth import AuthenticatedIdentity, AuthOptions, GuestSessionResult
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

TRUSTED_ORIGIN = "http://client.example"


class FakeAuthService:
    def __init__(self, options: AuthOptions | None = None) -> None:
        self.options = options or AuthOptions(
            trusted_origins=frozenset({TRUSTED_ORIGIN}), production=False, use_dev_cookie=True
        )
        self.cookie_name = self.options.cookie_name
        self.user_id = uuid4()
        self.session_id = uuid4()
        self.enter_count = 0
        self.refresh_count = 0
        self.reset_count = 0

    def _result(self, *, credential: str) -> GuestSessionResult:
        now = datetime.now(UTC)
        return GuestSessionResult(
            AuthenticatedIdentity(self.user_id, "guest", self.session_id),
            credential,
            now,
            now + timedelta(days=120),
        )

    def enter_guest(self, credential: str | None) -> GuestSessionResult:
        self.enter_count += 1
        return self._result(credential=credential or "test-only-credential")

    def authenticate(self, credential: str | None) -> AuthenticatedIdentity | None:
        if credential in {"test-only-credential", "rotated-test-credential"}:
            return AuthenticatedIdentity(self.user_id, "guest", self.session_id)
        return None

    def refresh_guest(self, credential: str | None) -> GuestSessionResult:
        self.refresh_count += 1
        assert credential == "test-only-credential"
        return self._result(credential=credential)

    def reset_guest(self, credential: str | None) -> GuestSessionResult:
        self.reset_count += 1
        assert credential == "test-only-credential"
        return self._result(credential="rotated-test-credential")


def _make_client(service: FakeAuthService, *, base_url: str = "http://testserver") -> TestClient:
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(billing_router)
    app.state.auth_service = service

    async def handle_domain_error(_request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message},
        )

    app.add_exception_handler(DomainError, handle_domain_error)
    return TestClient(app, base_url=base_url)


def test_guest_cookie_attributes_and_me_does_not_renew() -> None:
    service = FakeAuthService()
    with _make_client(service) as client:
        response = client.post("/api/v1/auth/guest", headers={"Origin": TRUSTED_ORIGIN})

        assert response.status_code == 200
        assert response.json()["identity_kind"] == "guest"
        assert "user_id" in response.json()
        assert "test-only-credential" not in response.text
        cookie_header = response.headers["set-cookie"]
        assert "guest_session_dev=test-only-credential" in cookie_header
        assert "HttpOnly" in cookie_header
        assert "SameSite=lax" in cookie_header
        assert "Path=/" in cookie_header
        assert "Secure" not in cookie_header
        assert "Domain=" not in cookie_header

        before = service.refresh_count
        me = client.get("/api/v1/auth/me")

    assert me.status_code == 200
    assert me.json()["features"] == {
        "account_login": False,
        "billing": False,
        "payments": False,
    }
    assert service.enter_count == 1
    assert service.refresh_count == before == 0


def test_production_cookie_is_host_scoped_secure_and_http_only() -> None:
    options = AuthOptions(frozenset({"https://client.example"}))
    service = FakeAuthService(options)
    with _make_client(service, base_url="https://client.example") as client:
        response = client.post("/api/v1/auth/guest", headers={"Origin": "https://client.example"})

    cookie_header = response.headers["set-cookie"]
    assert "__Host-guest_session=test-only-credential" in cookie_header
    assert "Secure" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=lax" in cookie_header
    assert "Path=/" in cookie_header
    assert "Domain=" not in cookie_header


def test_write_routes_require_trusted_origin() -> None:
    service = FakeAuthService()
    with _make_client(service) as client:
        missing = client.post("/api/v1/auth/guest")
        null_origin = client.post(
            "/api/v1/auth/guest", headers={"Origin": "null", "Referer": f"{TRUSTED_ORIGIN}/"}
        )
        wrong_origin = client.post("/api/v1/auth/guest", headers={"Origin": "http://other.example"})
        referer = client.post(
            "/api/v1/auth/guest", headers={"Referer": f"{TRUSTED_ORIGIN}/app/start?x=1"}
        )
        supplied_identity = client.post(
            "/api/v1/auth/guest",
            headers={"Origin": TRUSTED_ORIGIN},
            json={"user_id": str(uuid4())},
        )

    assert missing.status_code == 403
    assert null_origin.status_code == 403
    assert wrong_origin.status_code == 403
    assert referer.status_code == 200
    assert supplied_identity.status_code == 400


def test_refresh_and_reset_are_explicit_and_billing_is_disabled() -> None:
    service = FakeAuthService()
    with _make_client(service) as client:
        client.cookies.set(service.cookie_name, "test-only-credential")
        refreshed = client.post("/api/v1/auth/guest/refresh", headers={"Origin": TRUSTED_ORIGIN})
        reset = client.post("/api/v1/auth/guest/reset", headers={"Origin": TRUSTED_ORIGIN})
        quota = client.get("/api/v1/quota")
        balance = client.get("/api/v1/billing/balance")
        login = client.post("/api/v1/auth/login", headers={"Origin": TRUSTED_ORIGIN})
        recharge = client.post(
            "/api/v1/billing/recharge-orders",
            headers={"Origin": TRUSTED_ORIGIN},
        )

    assert refreshed.status_code == 204
    assert refreshed.content == b""
    assert reset.status_code == 200
    assert reset.json()["identity_kind"] == "guest"
    assert quota.json() == {"enabled": False}
    assert balance.json() == {"enabled": False}
    assert login.status_code == recharge.status_code == 503
    assert login.json()["code"] == recharge.json()["code"] == "FEATURE_DISABLED"
    assert service.refresh_count == 1
    assert service.reset_count == 1
