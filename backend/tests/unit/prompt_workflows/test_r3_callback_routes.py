from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from app.api.dependencies import Principal, require_principal, require_same_origin
from app.api.internal_dify import CALLBACK_BODY_MAX_BYTES, create_internal_dify_router
from app.api.task_preferences import router as preference_router
from app.core.domain_errors import DomainError
from app.schemas.prompt_preferences import (
    PromptPreferenceAccepted,
    RecommendationResultCallback,
    recommendation_callback_digest,
)
from app.schemas.prompt_workflow import CallbackAcceptance, PromptCallback
from app.schemas.recommendations import IMAGE_TYPES
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.engine import Engine

_CALLBACK_SECRET = "unit-test-independent-callback-secret-0001"


class _PromptCallbackFake:
    def __init__(self) -> None:
        self.callback: PromptCallback | None = None
        self.phase: str | None = None

    def accept(self, callback: PromptCallback, *, phase: str) -> CallbackAcceptance:
        self.callback = callback
        self.phase = phase
        return CallbackAcceptance(
            status="accepted",
            workflow_run_id=callback.workflow_run_id,
            workflow_revision=callback.workflow_revision,
        )


class _RecommendationCallbackFake:
    def __init__(self) -> None:
        self.callback: RecommendationResultCallback | None = None

    def accept(self, callback: RecommendationResultCallback) -> str:
        self.callback = callback
        return "accepted"


def _domain_error_handler(_request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message},
    )


def _internal_app(
    prompt_fake: _PromptCallbackFake | None = None,
    recommendation_fake: _RecommendationCallbackFake | None = None,
) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(DomainError, _domain_error_handler)
    app.include_router(
        create_internal_dify_router(
            engine=cast(Engine, object()),
            callback_credential=SecretStr(_CALLBACK_SECRET),
            prompt_service=prompt_fake,
            recommendation_service=recommendation_fake,
        )
    )
    return app


def _prompt_body() -> dict[str, object]:
    return {
        "task_id": str(uuid4()),
        "workflow_run_id": str(uuid4()),
        "workflow_revision": 1,
        "submission_id": str(uuid4()),
        "phase": "initial_preferences",
        "submission_content_digest_version": 1,
        "submission_content_digest": "01" * 32,
        "result_digest_version": 1,
        "result_digest": "fe" * 32,
        "outcome": "completed",
        "package": {"basis": {"synthetic": True}, "images": []},
        "next_question_round": None,
        "allowed_fields": None,
        "locked_fields": None,
        "valid_until": None,
        "safe_reason_code": None,
    }


def _recommendation_body() -> dict[str, object]:
    callback = RecommendationResultCallback(
        recommendation_id=uuid4(),
        reference_set_id=uuid4(),
        recommendation_revision=1,
        run_ref="mock-run:route-test",
        status="ready",
        original_recommendation={"synthetic": True},
        public_options={"synthetic": True},
        image_types=IMAGE_TYPES,
        audit={"source": "synthetic_mock", "image_analysis_performed": False},
        state={"synthetic": True},
        form_ref="mock-form:route-test",
        valid_until=datetime.now(UTC) + timedelta(hours=1),
        result_digest_version=1,
        result_digest="00" * 32,
    )
    payload = callback.model_dump(mode="json")
    payload["result_digest"] = recommendation_callback_digest(callback)
    return payload


def test_internal_prompt_route_decodes_hex_and_passes_phase() -> None:
    fake = _PromptCallbackFake()
    app = _internal_app(prompt_fake=fake)
    response = TestClient(app).post(
        "/internal/v1/dify/prompt-results",
        json=_prompt_body(),
        headers={"Authorization": f"Bearer {_CALLBACK_SECRET}"},
    )

    assert response.status_code == 200
    assert fake.callback is not None
    assert fake.phase == "initial_preferences"
    assert fake.callback.submission_content_digest == bytes.fromhex("01" * 32)
    assert fake.callback.result_digest == bytes.fromhex("fe" * 32)
    assert len(fake.callback.result_digest) == 32
    assert response.headers["cache-control"] == "no-store"


def test_internal_recommendation_route_accepts_hex_result_digest() -> None:
    fake = _RecommendationCallbackFake()
    response = TestClient(_internal_app(recommendation_fake=fake)).post(
        "/internal/v1/dify/recommendation-results",
        json=_recommendation_body(),
        headers={"Authorization": f"Bearer {_CALLBACK_SECRET}"},
    )

    assert response.status_code == 200
    assert fake.callback is not None
    assert fake.callback.result_digest == recommendation_callback_digest(fake.callback)


def test_internal_route_requires_independent_bearer_credential() -> None:
    fake = _PromptCallbackFake()
    response = TestClient(_internal_app(prompt_fake=fake)).post(
        "/internal/v1/dify/prompt-results",
        json=_prompt_body(),
    )

    assert response.status_code == 401
    assert response.json()["code"] == "CALLBACK_AUTH_REQUIRED"
    assert fake.callback is None


def test_internal_router_rejects_missing_strength_independent_credential() -> None:
    with pytest.raises(ValueError):
        create_internal_dify_router(
            engine=cast(Engine, object()),
            callback_credential=SecretStr("too-short"),
            prompt_service=_PromptCallbackFake(),
        )


def test_wrong_credential_is_rejected_before_chunked_payload_is_read() -> None:
    fake = _PromptCallbackFake()
    app = _internal_app(prompt_fake=fake)
    consumed = False
    body = json.dumps(_prompt_body()).encode("utf-8")

    async def chunks():
        nonlocal consumed
        consumed = True
        yield body

    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(
                "/internal/v1/dify/prompt-results",
                content=chunks(),
                headers={
                    "Authorization": "Bearer incorrect-credential",
                    "Content-Type": "application/json",
                },
            )

    response = asyncio.run(send())

    assert response.status_code == 401
    assert consumed is False
    assert fake.callback is None


def test_chunked_full_prompt_callback_is_parsed_without_content_length() -> None:
    fake = _PromptCallbackFake()
    app = _internal_app(prompt_fake=fake)
    received_headers: list[dict[str, str]] = []

    @app.middleware("http")
    async def record_body_headers(request, call_next):
        received_headers.append(dict(request.headers))
        return await call_next(request)

    body = _prompt_body()
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")

    async def chunks():
        midpoint = len(raw) // 2
        yield raw[:midpoint]
        yield raw[midpoint:]

    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(
                "/internal/v1/dify/prompt-results",
                content=chunks(),
                headers={
                    "Authorization": f"Bearer {_CALLBACK_SECRET}",
                    "Content-Type": "application/json",
                },
            )

    response = asyncio.run(send())

    assert response.status_code == 200
    assert received_headers and "content-length" not in received_headers[0]
    assert received_headers[0].get("transfer-encoding") == "chunked"
    assert fake.callback is not None
    assert fake.callback.task_id == UUID(body["task_id"])


def test_chunked_prompt_callback_actual_stream_over_limit_is_rejected() -> None:
    fake = _PromptCallbackFake()
    app = _internal_app(prompt_fake=fake)
    initial = json.dumps(_prompt_body(), separators=(",", ":")).encode("utf-8")
    excess = CALLBACK_BODY_MAX_BYTES + 1 - len(initial)

    async def chunks():
        yield initial
        block = b" " * 65536
        remaining = excess
        while remaining:
            part = block[:remaining]
            yield part
            remaining -= len(part)

    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(
                "/internal/v1/dify/prompt-results",
                content=chunks(),
                headers={
                    "Authorization": f"Bearer {_CALLBACK_SECRET}",
                    "Content-Type": "application/json",
                },
            )

    response = asyncio.run(send())

    assert response.status_code == 413
    assert response.json()["code"] == "CALLBACK_TOO_LARGE"
    assert fake.callback is None


def test_content_length_over_limit_is_rejected_before_callback_parsing() -> None:
    fake = _PromptCallbackFake()
    raw = json.dumps(_prompt_body(), separators=(",", ":")).encode("utf-8")
    raw += b" " * (CALLBACK_BODY_MAX_BYTES + 1 - len(raw))
    response = TestClient(_internal_app(prompt_fake=fake)).post(
        "/internal/v1/dify/prompt-results",
        content=raw,
        headers={
            "Authorization": f"Bearer {_CALLBACK_SECRET}",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 413
    assert response.json()["code"] == "CALLBACK_TOO_LARGE"
    assert fake.callback is None


def test_exact_two_mib_boundary_accepts_complete_callback_json() -> None:
    fake = _PromptCallbackFake()
    raw = json.dumps(_prompt_body(), separators=(",", ":")).encode("utf-8")
    raw += b" " * (CALLBACK_BODY_MAX_BYTES - len(raw))
    assert len(raw) == CALLBACK_BODY_MAX_BYTES

    response = TestClient(_internal_app(prompt_fake=fake)).post(
        "/internal/v1/dify/prompt-results",
        content=raw,
        headers={
            "Authorization": f"Bearer {_CALLBACK_SECRET}",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    assert fake.callback is not None


def test_internal_route_rejects_duplicate_json_keys_and_hides_payload() -> None:
    fake = _PromptCallbackFake()
    body = b'{"task_id":"private-marker","task_id":"overridden"}'
    response = TestClient(_internal_app(prompt_fake=fake)).post(
        "/internal/v1/dify/prompt-results",
        content=body,
        headers={
            "Authorization": f"Bearer {_CALLBACK_SECRET}",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 400
    assert "private-marker" not in response.text
    assert fake.callback is None


def test_internal_callback_response_does_not_echo_private_package_text() -> None:
    fake = _PromptCallbackFake()
    body = _prompt_body()
    body["package"] = {
        "basis": {"private": "synthetic-private-marker"},
        "images": [],
    }
    response = TestClient(_internal_app(prompt_fake=fake)).post(
        "/internal/v1/dify/prompt-results",
        json=body,
        headers={"Authorization": f"Bearer {_CALLBACK_SECRET}"},
    )

    assert response.status_code == 200
    assert "synthetic-private-marker" not in response.text
    assert fake.callback is not None


def test_public_preference_router_is_independent_and_forwards_allowed_fields() -> None:
    task_id = uuid4()
    owner_id = uuid4()
    session_id = uuid4()
    captured: dict[str, object] = {}

    class _PreferenceServiceFake:
        def submit(self, **kwargs):
            captured.update(kwargs)
            return PromptPreferenceAccepted(
                status="accepted",
                task_id=kwargs["task_id"],
                submission_id=uuid4(),
                workflow_run_id=kwargs["request"].workflow_run_id,
                workflow_revision=kwargs["request"].workflow_revision,
                question_round=kwargs["request"].question_round,
            )

    app = FastAPI()
    app.add_exception_handler(DomainError, _domain_error_handler)
    app.include_router(preference_router)
    app.state.prompt_preference_service = _PreferenceServiceFake()
    app.dependency_overrides[require_principal] = lambda: Principal(
        user_id=owner_id, identity_kind="guest", session_id=session_id
    )
    app.dependency_overrides[require_same_origin] = lambda: None
    response = TestClient(app).post(
        f"/api/v1/tasks/{task_id}/preferences",
        json={
            "workflow_run_id": str(uuid4()),
            "workflow_revision": 1,
            "question_round": 1,
            "answers": {"language": {"mode": "B"}},
        },
        headers={"Idempotency-Key": "preference-answer-0001"},
    )

    assert response.status_code == 202, response.text
    assert captured["owner_user_id"] == owner_id
    assert captured["task_id"] == task_id
    assert captured["idempotency_key"] == "preference-answer-0001"
    assert response.headers["cache-control"] == "no-store"
    routes = set(app.openapi()["paths"])
    assert "/api/v1/tasks/{task_id}/preferences" in routes
    assert "/api/v1/tasks" not in routes
