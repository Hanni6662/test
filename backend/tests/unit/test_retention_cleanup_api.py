from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.api.internal_maintenance import create_internal_maintenance_router
from app.core.domain_errors import DomainError
from app.core.errors import install_exception_handlers
from app.schemas.retention import (
    CleanupPreviewResponse,
    CleanupRunAccepted,
    CleanupRunResponse,
    RetentionPolicyResponse,
    RetentionPolicyUpdateRequest,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

MAINTENANCE_SECRET = "a" * 40
DIFY_SECRET = "b" * 40


class _ServiceStub:
    def __init__(self) -> None:
        self.update_calls = 0

    def get_policy(self) -> RetentionPolicyResponse:
        return RetentionPolicyResponse(
            revision=2,
            task_retention_days=60,
            reference_retention_days=30,
            recommendation_retention_days=45,
        )

    def update_policy(self, request: RetentionPolicyUpdateRequest) -> RetentionPolicyResponse:
        self.update_calls += 1
        return RetentionPolicyResponse(
            revision=request.expected_revision + 1,
            task_retention_days=request.task_retention_days,
            reference_retention_days=request.reference_retention_days,
            recommendation_retention_days=request.recommendation_retention_days,
        )

    def preview(self) -> CleanupPreviewResponse:
        return CleanupPreviewResponse(as_of=datetime.now(UTC), inventory_available=True)

    def create_run(self, _request, *, idempotency_key: str) -> CleanupRunAccepted:
        assert idempotency_key == "test-cleanup-key"
        return CleanupRunAccepted(run_id=uuid4(), status="queued")

    def get_run(self, run_id: str) -> CleanupRunResponse:
        now = datetime.now(UTC)
        return CleanupRunResponse(
            run_id=run_id,
            trigger_kind="manual",
            status="queued",
            policy_revision=2,
            counters={},
            created_at=now,
        )


def _client(service: _ServiceStub) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_internal_maintenance_router(
            service=service,  # type: ignore[arg-type]
            bearer_secret=SecretStr(MAINTENANCE_SECRET),
            schedule_timezone="Asia/Shanghai",
            schedule_daily_time="03:00",
        )
    )
    install_exception_handlers(app)
    return TestClient(app)


def test_policy_route_uses_independent_bearer_and_effective_schedule_config() -> None:
    service = _ServiceStub()
    client = _client(service)

    cookie_only = client.get(
        "/internal/v1/maintenance/retention-policy",
        cookies={"session": "valid-looking-cookie"},
    )
    dify_only = client.get(
        "/internal/v1/maintenance/retention-policy",
        headers={"Authorization": f"Bearer {DIFY_SECRET}"},
    )
    allowed = client.get(
        "/internal/v1/maintenance/retention-policy",
        headers={"Authorization": f"Bearer {MAINTENANCE_SECRET}"},
    )

    assert cookie_only.status_code == 401
    assert dify_only.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["schedule_timezone"] == "Asia/Shanghai"
    assert allowed.json()["schedule_daily_time"] == "03:00"
    assert allowed.headers["cache-control"] == "private, no-store"


def test_schedule_configuration_cannot_be_changed_by_policy_update() -> None:
    service = _ServiceStub()
    client = _client(service)
    headers = {
        "Authorization": f"Bearer {MAINTENANCE_SECRET}",
        "Content-Type": "application/json",
    }

    response = client.put(
        "/internal/v1/maintenance/retention-policy",
        headers=headers,
        json={
            "expected_revision": 2,
            "task_retention_days": 30,
            "reference_retention_days": 30,
            "recommendation_retention_days": 45,
            "apply_to_existing": False,
            "schedule_daily_time": "04:00",
        },
    )

    assert response.status_code == 422
    assert service.update_calls == 0


def test_cleanup_run_requires_idempotency_key() -> None:
    client = _client(_ServiceStub())
    response = client.post(
        "/internal/v1/maintenance/cleanup/runs",
        headers={
            "Authorization": f"Bearer {MAINTENANCE_SECRET}",
            "Content-Type": "application/json",
        },
        json={},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_INVALID"


def test_invalid_bearer_secret_is_rejected_at_router_creation() -> None:
    try:
        create_internal_maintenance_router(
            service=_ServiceStub(),  # type: ignore[arg-type]
            bearer_secret=SecretStr("short"),
            schedule_timezone="Asia/Shanghai",
            schedule_daily_time="03:00",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("short maintenance secrets must not be accepted")


def test_router_domain_error_type_is_safe() -> None:
    error = DomainError(409, "SAFE_CONFLICT", "maintenance request conflicts")
    assert error.code == "SAFE_CONFLICT"
