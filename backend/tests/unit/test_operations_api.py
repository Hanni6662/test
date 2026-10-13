from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from app.api.internal_maintenance import create_internal_maintenance_router
from app.core.errors import install_exception_handlers
from app.schemas.operations import (
    ActionBacklog,
    BrokerObservation,
    CapacitySnapshot,
    CleanupStatusSnapshot,
    DispatcherRegistrations,
    LoopHeartbeatResponse,
    OperationsDiagnosticsResponse,
    OutboxBacklog,
    RecoveryOperationResponse,
    WorkerProgressSnapshot,
)
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

MAINTENANCE_SECRET = "m" * 40


class _CleanupStub:
    pass


class _OperationsStub:
    def __init__(self) -> None:
        self.save_calls: list[tuple[object, str]] = []
        self.expire_calls: list[tuple[object, str]] = []

    def snapshot(self) -> OperationsDiagnosticsResponse:
        now = datetime.now(UTC)
        return OperationsDiagnosticsResponse(
            as_of=now,
            heartbeat_stale_seconds=90,
            long_state_seconds=1800,
            broker=BrokerObservation(
                state="connected", checked_at=now, ready_messages=2, consumers=1
            ),
            dispatcher=LoopHeartbeatResponse(
                component="dispatcher",
                state="fresh",
                instance_id=uuid4(),
                started_at=now,
                last_heartbeat_at=now,
                last_iteration_started_at=now,
                last_iteration_completed_at=now,
                last_successful_scan_at=now,
                last_outbox_scan_at=now,
                last_broker_confirmed_at=now,
                iteration_count=3,
                successful_iteration_count=3,
                registrations=DispatcherRegistrations(
                    event_handler_count=2,
                    action_registration_count=8,
                    prompt_runtime_registered=True,
                    generation_runtime_registered=True,
                ),
                last_progress={"claimed": 1, "published": 1},
            ),
            scheduler=LoopHeartbeatResponse(component="scheduler", state="stale"),
            outbox=OutboxBacklog(
                pending_due=1,
                claimed=0,
                expired_claims=0,
                published_waiting=0,
                processing_receipts=0,
                dead=0,
                oldest_due_at=now,
                oldest_due_age_seconds=4,
            ),
            actions=ActionBacklog(
                scheduled_due=1,
                claimed=0,
                expired_claims=0,
                unknown=0,
                rejected=0,
                cancelled=0,
                oldest_due_at=now,
                oldest_due_age_seconds=2,
            ),
            capacity=CapacitySnapshot(active_reservations=1, configured_limit=2),
            cleanup=CleanupStatusSnapshot(failed_runs=0, blocked_jobs=0, failed_targets=0),
            worker_progress=WorkerProgressSnapshot(
                processing_receipts=0, completed_actions_last_hour=1, unknown_actions=0
            ),
        )

    def expire_followup(self, task_id, *, idempotency_key):
        self.expire_calls.append((task_id, idempotency_key))
        return RecoveryOperationResponse(
            operation="expire_followup",
            outcome="expired",
            task_id=task_id,
            action_id=uuid4(),
        )

    def expedite_same_attempt_save(self, action_id, *, idempotency_key):
        self.save_calls.append((action_id, idempotency_key))
        return RecoveryOperationResponse(
            operation="expedite_generation_save",
            outcome="expedited",
            task_id=uuid4(),
            action_id=action_id,
        )


def _app(operations_service: _OperationsStub | None) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_internal_maintenance_router(
            service=_CleanupStub(),  # type: ignore[arg-type]
            operations_service=operations_service,  # type: ignore[arg-type]
            bearer_secret=SecretStr(MAINTENANCE_SECRET),
            schedule_timezone="UTC",
            schedule_daily_time="03:00",
        )
    )
    install_exception_handlers(app)
    return app


def _request(app: FastAPI, method: str, path: str, **kwargs):
    async def send_request():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test.invalid"
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send_request())


def test_diagnostics_use_independent_bearer_and_redacted_database_evidence() -> None:
    app = _app(_OperationsStub())
    path = "/internal/v1/maintenance/operations"

    assert _request(app, "GET", path, headers={"Cookie": "session=valid"}).status_code == 401
    assert (
        _request(app, "GET", path, headers={"Authorization": "Bearer " + "d" * 40}).status_code
        == 401
    )
    response = _request(app, "GET", path, headers={"Authorization": f"Bearer {MAINTENANCE_SECRET}"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_process"] == "responding"
    assert payload["database"] == "connected"
    assert payload["broker"]["ready_messages"] == 2
    assert payload["dispatcher"]["registrations"]["generation_runtime_registered"] is True
    assert payload["scheduler"]["state"] == "stale"
    assert payload["outbox"]["oldest_due_age_seconds"] == 4
    assert payload["worker_progress"]["evidence_scope"] == "aggregate_database_evidence"
    assert response.headers["cache-control"] == "private, no-store"
    for forbidden in ("prompt_text", "provider_ref", "digest", "object_key", "secret", "url"):
        assert forbidden not in response.text.lower()


def test_recovery_routes_only_accept_narrow_expire_and_same_attempt_save_operations() -> None:
    service = _OperationsStub()
    app = _app(service)
    headers = {
        "Authorization": f"Bearer {MAINTENANCE_SECRET}",
        "Content-Type": "application/json",
        "Idempotency-Key": "ops-test-key",
    }
    action_id = uuid4()
    task_id = uuid4()

    assert (
        _request(
            app,
            "POST",
            f"/internal/v1/maintenance/operations/actions/{action_id}/recover",
            headers=headers,
            json={},
        ).status_code
        == 404
    )
    save_response = _request(
        app,
        "POST",
        f"/internal/v1/maintenance/operations/actions/{action_id}/retry-save",
        headers=headers,
        json={},
    )
    assert save_response.status_code == 200
    assert service.save_calls == [(action_id, "ops-test-key")]
    assert service.expire_calls == []

    expire_response = _request(
        app,
        "POST",
        f"/internal/v1/maintenance/operations/tasks/{task_id}/expire-followup",
        headers=headers,
        json={},
    )
    assert expire_response.status_code == 200
    assert service.expire_calls == [(task_id, "ops-test-key")]


def test_operations_routes_are_absent_when_not_configured() -> None:
    app = _app(None)
    response = _request(
        app,
        "GET",
        "/internal/v1/maintenance/operations",
        headers={"Authorization": f"Bearer {MAINTENANCE_SECRET}"},
    )
    assert response.status_code == 404
