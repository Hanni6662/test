from __future__ import annotations

import hashlib
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from app.api.auth import router as auth_router
from app.api.billing import router as billing_router
from app.core.domain_errors import DomainError
from app.db.models import GuestSession, SessionPolicy, User
from app.services.auth import AuthOptions, AuthService
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, select, text, update
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

pytestmark = pytest.mark.integration
TRUSTED_ORIGIN = "http://testserver"


def _make_app(service: AuthService) -> FastAPI:
    app = FastAPI()
    app.state.auth_service = service
    app.include_router(auth_router)
    app.include_router(billing_router)

    async def handle_domain_error(_request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message},
        )

    app.add_exception_handler(DomainError, handle_domain_error)
    return app


def test_guest_session_lifecycle_uses_postgres_policy_and_database_time() -> None:
    database_url = os.environ["IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL"]
    engine = create_engine(database_url, pool_pre_ping=True)
    options = AuthOptions(
        trusted_origins=frozenset({TRUSTED_ORIGIN}), production=False, use_dev_cookie=True
    )
    service = AuthService(engine, options)
    user_ids: set[UUID] = set()
    try:
        with TestClient(_make_app(service)) as client:
            member_id = uuid4()
            member_session_id = uuid4()
            user_ids.add(member_id)
            member_token = secrets.token_urlsafe(32)
            with Session(engine) as db, db.begin():
                database_now = db.execute(select(func.clock_timestamp())).scalar_one()
                db.add(User(id=member_id, identity_kind="member", status="active"))
                db.flush()
                db.add(
                    GuestSession(
                        id=member_session_id,
                        user_id=member_id,
                        token_hash=hashlib.sha256(member_token.encode()).digest(),
                        created_at=database_now,
                        last_renewed_at=database_now,
                        expires_at=database_now + timedelta(days=240),
                        ttl_days=240,
                        policy_revision=1,
                    )
                )
            client.cookies.set(service.cookie_name, member_token)
            member_response = client.post("/api/v1/auth/guest", headers={"Origin": TRUSTED_ORIGIN})
            assert member_response.status_code == 200
            assert member_response.json()["identity_kind"] == "member"
            assert UUID(member_response.json()["user_id"]) == member_id
            assert "set-cookie" not in member_response.headers
            assert (
                client.post(
                    "/api/v1/auth/guest/reset", headers={"Origin": TRUSTED_ORIGIN}
                ).status_code
                == 403
            )
            with Session(engine) as db:
                unchanged_member_session = db.execute(
                    select(GuestSession).where(GuestSession.user_id == member_id)
                ).scalar_one()
                assert unchanged_member_session.ttl_days == 240
                assert unchanged_member_session.expires_at == database_now + timedelta(days=240)
            client.cookies.clear()

            created = client.post("/api/v1/auth/guest", headers={"Origin": TRUSTED_ORIGIN})
            assert created.status_code == 200
            first_user_id = UUID(created.json()["user_id"])
            user_ids.add(first_user_id)
            original_token = client.cookies.get(service.cookie_name)
            assert original_token and original_token not in created.text

            with Session(engine) as db:
                first_session = db.execute(
                    select(GuestSession).where(GuestSession.user_id == first_user_id)
                ).scalar_one()
                assert len(first_session.token_hash) == 32
                assert first_session.token_hash == hashlib.sha256(original_token.encode()).digest()
                assert first_session.ttl_days == 120
                assert first_session.expires_at - first_session.created_at == timedelta(days=120)

            me = client.get("/api/v1/auth/me")
            assert me.status_code == 200
            assert UUID(me.json()["user_id"]) == first_user_id

            refreshed = client.post(
                "/api/v1/auth/guest/refresh", headers={"Origin": TRUSTED_ORIGIN}
            )
            assert refreshed.status_code == 204
            assert client.cookies.get(service.cookie_name) == original_token

            with ThreadPoolExecutor(max_workers=8) as pool:
                concurrent_results = list(
                    pool.map(lambda _index: service.refresh_guest(original_token), range(8))
                )
            assert all(result.credential == original_token for result in concurrent_results)
            with Session(engine) as db:
                after_concurrent = db.get(GuestSession, concurrent_results[0].identity.session_id)
                assert after_concurrent is not None
                assert after_concurrent.expires_at >= max(
                    result.expires_at for result in concurrent_results
                )
                assert after_concurrent.policy_revision == 1

            with Session(engine) as db:
                original_policy = db.get(SessionPolicy, 1)
                assert original_policy is not None
                old_ttl_days, old_revision = original_policy.ttl_days, original_policy.revision
            try:
                with engine.begin() as connection:
                    connection.execute(
                        update(SessionPolicy)
                        .where(SessionPolicy.id == 1)
                        .values(ttl_days=14, revision=old_revision + 1)
                    )
                revised = service.refresh_guest(original_token)
                assert revised.expires_at == revised.database_now + timedelta(days=14)
                with Session(engine) as db:
                    changed = db.get(GuestSession, revised.identity.session_id)
                    assert changed is not None
                    assert (changed.ttl_days, changed.policy_revision) == (14, old_revision + 1)
            finally:
                with engine.begin() as connection:
                    connection.execute(
                        update(SessionPolicy)
                        .where(SessionPolicy.id == 1)
                        .values(ttl_days=old_ttl_days, revision=old_revision)
                    )

            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE sessions SET created_at = clock_timestamp() - "
                        "interval '2 seconds', expires_at = clock_timestamp() - "
                        "interval '1 second' WHERE user_id = :user_id"
                    ),
                    {"user_id": first_user_id},
                )
            assert service.authenticate(original_token) is None
            expired_refresh = client.post(
                "/api/v1/auth/guest/refresh", headers={"Origin": TRUSTED_ORIGIN}
            )
            assert expired_refresh.status_code == 401

            replaced = client.post("/api/v1/auth/guest", headers={"Origin": TRUSTED_ORIGIN})
            assert replaced.status_code == 200
            second_user_id = UUID(replaced.json()["user_id"])
            user_ids.add(second_user_id)
            assert second_user_id != first_user_id
            second_token = client.cookies.get(service.cookie_name)
            assert second_token and service.authenticate(second_token) is not None

            reset = client.post("/api/v1/auth/guest/reset", headers={"Origin": TRUSTED_ORIGIN})
            assert reset.status_code == 200
            third_user_id = UUID(reset.json()["user_id"])
            user_ids.add(third_user_id)
            assert third_user_id != second_user_id
            assert service.authenticate(second_token) is None
            third_token = client.cookies.get(service.cookie_name)
            assert third_token and service.authenticate(third_token) is not None

            assert client.get("/api/v1/quota").json() == {"enabled": False}
            assert client.get("/api/v1/billing/balance").json() == {"enabled": False}
            assert (
                client.post("/api/v1/auth/login", headers={"Origin": TRUSTED_ORIGIN}).json()["code"]
                == "FEATURE_DISABLED"
            )

            with engine.begin() as connection:
                connection.execute(
                    update(User).where(User.id == third_user_id).values(status="disabled")
                )
            assert service.authenticate(third_token) is None
            assert client.get("/api/v1/auth/me").status_code == 401
    finally:
        with engine.begin() as connection:
            connection.execute(delete(GuestSession).where(GuestSession.user_id.in_(user_ids)))
            connection.execute(delete(User).where(User.id.in_(user_ids)))
        engine.dispose()
