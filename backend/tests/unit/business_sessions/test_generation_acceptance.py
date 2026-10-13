from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from app.integrations.generation_acceptance import (
    GenerationAcceptanceExpected,
    HttpGenerationAcceptancePort,
    validate_generation_acceptance,
)
from app.schemas.generation_acceptance import (
    GENERATION_ACCEPTANCE_CONTRACT,
    GENERATION_IMAGE_CONTRACT_V3,
    GENERATION_PERMIT_EXPIRED_REASON,
    generation_acceptance_receipt_digest,
    parse_generation_acceptance,
)

BASE_ORIGIN = "https://image-generation.test"
BEARER_TOKEN = "test-acceptance-bearer-token"
GENERATION_REQUEST_ID = UUID("00000000-0000-4000-8000-000000000101")
TASK_ID = UUID("00000000-0000-4000-8000-000000000102")
PERMIT_ID = UUID("00000000-0000-4000-8000-000000000103")
AUTHORITY_ID = UUID("00000000-0000-4000-8000-000000000104")
RECEIPT_ID = UUID("00000000-0000-4000-8000-000000000105")
PERMIT_EXPIRY_TEXT = "2026-10-10T12:30:00Z"
PERMIT_EXPIRY = datetime(2026, 10, 10, 12, 30, tzinfo=UTC)


def _expected(**overrides: object) -> GenerationAcceptanceExpected:
    values: dict[str, object] = {
        "generation_request_id": GENERATION_REQUEST_ID,
        "task_id": TASK_ID,
        "dify_run_id": "run-v34-acceptance-17",
        "permit_id": PERMIT_ID,
        "permit_version": 2,
        "permit_expires_at": PERMIT_EXPIRY,
        "generation_contract_version": GENERATION_IMAGE_CONTRACT_V3,
        "package_digest": "a" * 64,
        "slot_mapping_digest": "b" * 64,
        "reference_manifest_digest": "c" * 64,
        "service_authority_id": AUTHORITY_ID,
    }
    values.update(overrides)
    return GenerationAcceptanceExpected(**values)  # type: ignore[arg-type]


def _accepted(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "contract_version": GENERATION_ACCEPTANCE_CONTRACT,
        "outcome": "accepted",
        "service_authority_id": str(AUTHORITY_ID),
        "generation_contract_version": GENERATION_IMAGE_CONTRACT_V3,
        "generation_request_id": str(GENERATION_REQUEST_ID),
        "task_id": str(TASK_ID),
        "dify_run_id": "run-v34-acceptance-17",
        "permit_id": str(PERMIT_ID),
        "permit_version": 2,
        "permit_expires_at": PERMIT_EXPIRY_TEXT,
        "package_digest": "a" * 64,
        "slot_mapping_digest": "b" * 64,
        "reference_manifest_digest": "c" * 64,
        # Intentionally unrelated to any backend H4 request digest.
        "service_request_digest": "d" * 64,
        "batch_id": "batch-accepted-17",
    }
    value.update(overrides)
    return value


def _rejected(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "contract_version": GENERATION_ACCEPTANCE_CONTRACT,
        "outcome": "rejected_expired",
        "service_authority_id": str(AUTHORITY_ID),
        "generation_contract_version": GENERATION_IMAGE_CONTRACT_V3,
        "generation_request_id": str(GENERATION_REQUEST_ID),
        "task_id": str(TASK_ID),
        "dify_run_id": "run-v34-acceptance-17",
        "permit_id": str(PERMIT_ID),
        "permit_version": 2,
        "permit_expires_at": PERMIT_EXPIRY_TEXT,
        "package_digest": "a" * 64,
        "slot_mapping_digest": "b" * 64,
        "reference_manifest_digest": "c" * 64,
        "service_request_digest": "e" * 64,
        "batch_id": None,
        "reason_code": GENERATION_PERMIT_EXPIRED_REASON,
        "receipt_version": 1,
        "receipt_id": str(RECEIPT_ID),
        "rejected_at": "2026-10-10T12:30:00.000001Z",
        "receipt_digest": "0" * 64,
    }
    value.update(overrides)
    if "receipt_digest" not in overrides:
        try:
            value["receipt_digest"] = generation_acceptance_receipt_digest(value)
        except ValueError:
            # Invalid extra fields are exercised as a strict-DTO failure below.
            pass
    return value


def _not_observed(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "contract_version": GENERATION_ACCEPTANCE_CONTRACT,
        "outcome": "not_observed",
        "service_authority_id": str(AUTHORITY_ID),
        "generation_request_id": str(GENERATION_REQUEST_ID),
    }
    value.update(overrides)
    return value


def _mock_port(handler: Callable[[httpx.Request], httpx.Response]) -> HttpGenerationAcceptancePort:
    return HttpGenerationAcceptancePort(
        BASE_ORIGIN,
        BEARER_TOKEN,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize(
    ("factory", "expected_outcome"),
    [
        (_accepted, "accepted"),
        (_rejected, "rejected_expired"),
    ],
)
def test_strict_evidence_is_accepted_only_after_full_identity_and_receipt_validation(
    factory: Callable[..., dict[str, object]], expected_outcome: str
) -> None:
    payload = factory()
    result = validate_generation_acceptance(payload, _expected())

    assert result.outcome == expected_outcome
    assert result.acceptance is not None
    assert result.unknown_reason is None


def test_service_request_digest_is_format_checked_but_not_compared_to_h4_digest() -> None:
    payload = _accepted(service_request_digest="f" * 64)

    result = validate_generation_acceptance(payload, _expected())

    assert result.outcome == "accepted"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generation_request_id", str(UUID("00000000-0000-4000-8000-000000000201"))),
        ("task_id", str(UUID("00000000-0000-4000-8000-000000000202"))),
        ("dify_run_id", "another-run"),
        ("permit_id", str(UUID("00000000-0000-4000-8000-000000000203"))),
        ("permit_version", 1),
        ("permit_expires_at", "2026-10-10T12:31:00Z"),
        ("generation_contract_version", "chatflow-image-generation-v2"),
        ("package_digest", "1" * 64),
        ("slot_mapping_digest", "2" * 64),
        ("reference_manifest_digest", "3" * 64),
        ("service_authority_id", str(UUID("00000000-0000-4000-8000-000000000204"))),
    ],
)
def test_any_identity_mismatch_is_unknown(field: str, value: object) -> None:
    result = validate_generation_acceptance(_accepted(**{field: value}), _expected())

    assert result.outcome == "unknown"
    assert result.acceptance is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contract_version", "generation-acceptance-v2"),
        ("service_request_digest", "A" * 64),
        ("service_request_digest", "not-a-digest"),
        ("batch_id", ""),
        ("batch_id", None),
        ("extra", "unexpected"),
        ("permit_version", True),
        ("generation_request_id", "00000000000040008000000000000101"),
        ("permit_expires_at", "2026-10-10T20:30:00+08:00"),
    ],
)
def test_accepted_dto_rejects_noncanonical_or_incomplete_fields(field: str, value: object) -> None:
    result = validate_generation_acceptance(_accepted(**{field: value}), _expected())

    assert result.outcome == "unknown"
    assert result.acceptance is None


def test_not_observed_is_never_negative_acceptance_evidence() -> None:
    result = validate_generation_acceptance(_not_observed(), _expected())

    assert result.outcome == "unknown"
    assert result.unknown_reason == "GENERATION_ACCEPTANCE_NOT_OBSERVED"


def test_missing_expected_authority_fails_closed() -> None:
    result = validate_generation_acceptance(_accepted(), _expected(service_authority_id=None))

    assert result.outcome == "unknown"
    assert result.unknown_reason == "GENERATION_ACCEPTANCE_EXPECTED_IDENTITY_INCOMPLETE"


def test_incomplete_expected_object_fails_closed() -> None:
    result = validate_generation_acceptance(_accepted(), None)  # type: ignore[arg-type]

    assert result.outcome == "unknown"
    assert result.unknown_reason == "GENERATION_ACCEPTANCE_EXPECTED_IDENTITY_INCOMPLETE"


def test_rejected_receipt_digest_uses_domain_and_exact_canonical_field_set() -> None:
    payload = _rejected()
    parsed = parse_generation_acceptance(payload)
    assert parsed.outcome == "rejected_expired"
    assert generation_acceptance_receipt_digest(payload) == payload["receipt_digest"]

    changed = dict(payload)
    changed["rejected_at"] = "2026-10-10T12:30:00.000002Z"
    assert validate_generation_acceptance(changed, _expected()).outcome == "unknown"


@pytest.mark.parametrize(
    "overrides",
    [
        {"batch_id": "batch-must-be-null"},
        {"reason_code": "OTHER_REASON"},
        {"receipt_version": 2},
        {"extra": "unexpected"},
        {"rejected_at": "2026-10-10T12:29:59Z"},
        {"service_request_digest": "invalid"},
    ],
)
def test_inconsistent_rejected_receipt_is_unknown(overrides: dict[str, object]) -> None:
    payload = _rejected(**overrides)
    result = validate_generation_acceptance(payload, _expected())

    assert result.outcome == "unknown"
    assert result.acceptance is None


def test_wrong_rejected_receipt_digest_is_unknown() -> None:
    payload = _rejected()
    payload["receipt_digest"] = "f" * 64

    result = validate_generation_acceptance(payload, _expected())

    assert result.outcome == "unknown"
    assert result.acceptance is None


def test_receipt_digest_requires_exact_fields_and_excludes_only_itself() -> None:
    payload = _rejected()
    payload["extra"] = "not-part-of-the-receipt"

    with pytest.raises(ValueError):
        generation_acceptance_receipt_digest(payload)


def test_http_port_uses_one_fixed_get_url_and_authorization_without_logging_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_accepted())

    port = _mock_port(handle)
    try:
        result = port.get_acceptance(_expected())
    finally:
        port.close()

    assert result.outcome == "accepted"
    assert [(request.method, request.url) for request in requests] == [
        (
            "GET",
            httpx.URL(f"{BASE_ORIGIN}/v3/generation-requests/{GENERATION_REQUEST_ID}/acceptance"),
        )
    ]
    assert requests[0].headers["Authorization"] == f"Bearer {BEARER_TOKEN}"
    assert BEARER_TOKEN not in caplog.text
    assert BEARER_TOKEN not in repr(result)


@pytest.mark.parametrize("status_code", [302, 307, 401, 403, 404, 410, 500, 503])
def test_http_statuses_redirects_and_auth_failures_are_unknown(status_code: int) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, headers={"location": "/v2/fallback"})

    port = _mock_port(handle)
    try:
        result = port.get_acceptance(_expected())
    finally:
        port.close()

    assert result.outcome == "unknown"
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == (f"/v3/generation-requests/{GENERATION_REQUEST_ID}/acceptance")


@pytest.mark.parametrize(
    "body",
    [
        b"{bad json",
        b"[]",
        b'{"outcome":"accepted","outcome":"rejected_expired"}',
        b'{"contract_version":"generation-acceptance-v1","outcome":"accepted"}',
        json.dumps(_not_observed()).encode("utf-8"),
    ],
)
def test_malformed_or_incomplete_http_json_is_unknown(body: bytes) -> None:
    port = _mock_port(lambda _request: httpx.Response(200, content=body))
    try:
        result = port.get_acceptance(_expected())
    finally:
        port.close()

    assert result.outcome == "unknown"


def test_timeout_is_unknown_and_is_not_retried() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("timeout")

    port = _mock_port(handle)
    try:
        result = port.get_acceptance(_expected())
    finally:
        port.close()

    assert result.outcome == "unknown"
    assert len(requests) == 1
    assert requests[0].method == "GET"


@pytest.mark.parametrize(
    "service_url",
    [
        "https://image-generation.test/v3",
        "https://user:secret@image-generation.test",
        "https://image-generation.test?redirect=elsewhere",
        "https://image-generation.test/#fragment",
    ],
)
def test_service_origin_cannot_select_a_path_or_credentials(service_url: str) -> None:
    with pytest.raises(ValueError):
        HttpGenerationAcceptancePort(service_url, BEARER_TOKEN)


def test_no_post_or_second_version_probe_occurs() -> None:
    requests: list[httpx.Request] = []
    port = _mock_port(lambda request: requests.append(request) or httpx.Response(404, json={}))
    try:
        result = port.get_acceptance(_expected())
    finally:
        port.close()

    assert result.outcome == "unknown"
    assert [request.method for request in requests] == ["GET"]
    assert requests[0].url.path.startswith("/v3/")
