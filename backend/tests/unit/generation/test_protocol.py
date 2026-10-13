from __future__ import annotations

from io import BytesIO
from uuid import uuid4

import pytest
from app.integrations.generation.mock import (
    _advance_successful_mock_query,
    _png_for_digest,
    _TestOnlyFaults,
)
from app.integrations.generation.protocol import (
    GenerationAccepted,
    GenerationCommand,
    GenerationDefinitelyRejected,
    GenerationDownloadError,
    GenerationDownloadTemporaryError,
    GenerationIdempotencyConflict,
    GenerationQueryFailed,
    GenerationQueryPending,
    GenerationQuerySucceeded,
    GenerationQueryTemporaryError,
    GenerationQueryUnknown,
    GenerationRecoveryDefinitelyNotFound,
    GenerationRecoveryFound,
    GenerationRecoveryUnknown,
    GenerationRecoveryUnsupported,
    GenerationUnknown,
)
from app.services.generation import GenerationService
from PIL import Image


def _command(
    *, request_key=None, reference_asset_ids: tuple | None = None, prompt_text: str = "studio"
) -> GenerationCommand:
    return GenerationCommand(
        request_key=request_key or uuid4(),
        image_type="front_view",
        prompt_package_version="prompt-v2",
        prompt_text=prompt_text,
        reference_asset_ids=reference_asset_ids or (uuid4(), uuid4()),
    )


def test_command_digest_is_canonical_and_reference_order_is_significant() -> None:
    request_key = uuid4()
    first = _command(request_key=request_key)
    same_input_other_key = _command(
        request_key=uuid4(),
        reference_asset_ids=first.reference_asset_ids,
    )
    reversed_references = _command(
        request_key=request_key,
        reference_asset_ids=tuple(reversed(first.reference_asset_ids)),
    )

    assert first.request_digest == same_input_other_key.request_digest
    assert first.request_digest_version == 1
    assert first.request_digest != reversed_references.request_digest


def test_submit_and_query_result_dtos_keep_distinct_outcomes() -> None:
    accepted = GenerationAccepted(
        outcome="accepted", provider_job_id="mock-job-123", poll_after_seconds=5
    )
    rejected = GenerationDefinitelyRejected(
        outcome="definitely_rejected", reason_code="REQUEST_REJECTED"
    )
    unknown = GenerationUnknown(
        outcome="unknown", reason_code="RESPONSE_LOST", reconcile_after_seconds=30
    )

    assert (accepted.outcome, rejected.outcome, unknown.outcome) == (
        "accepted",
        "definitely_rejected",
        "unknown",
    )
    assert GenerationQueryPending(
        outcome="pending", provider_job_id="mock-job-123", poll_after_seconds=5
    ).outcome == "pending"
    assert GenerationQuerySucceeded(
        outcome="succeeded",
        provider_job_id="mock-job-123",
        result_ref="mock-result:00000000-0000-0000-0000-000000000001",
        result_digest=b"x" * 32,
    ).outcome == "succeeded"
    assert GenerationQueryFailed(
        outcome="definitely_failed",
        provider_job_id="mock-job-123",
        reason_code="MOCK_FAILED",
    ).outcome == "definitely_failed"
    assert GenerationQueryUnknown(
        outcome="unknown", provider_job_id="mock-job-123", reason_code="STATE_UNKNOWN"
    ).outcome == "unknown"


def test_recovery_outcomes_do_not_collapse_not_found_and_unknown() -> None:
    request_key = uuid4()

    assert GenerationRecoveryFound(
        outcome="found",
        request_key=request_key,
        provider_job_id="mock-job-123",
        job_status="succeeded",
        result_ref="mock-result:00000000-0000-0000-0000-000000000001",
    ).outcome == "found"
    assert GenerationRecoveryDefinitelyNotFound(
        outcome="definitely_not_found", request_key=request_key
    ).outcome == "definitely_not_found"
    assert GenerationRecoveryUnknown(
        outcome="unknown", request_key=request_key, reason_code="STORE_UNAVAILABLE"
    ).outcome == "unknown"
    assert GenerationRecoveryUnsupported(
        outcome="unsupported", request_key=request_key
    ).outcome == "unsupported"


def test_dto_rejects_invalid_state_shapes_and_error_codes() -> None:
    with pytest.raises(ValueError):
        GenerationAccepted(
            outcome="unknown", provider_job_id="mock-job-123", poll_after_seconds=5
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        GenerationAccepted(
            outcome="accepted", provider_job_id="mock-job-123", poll_after_seconds=-1
        )
    with pytest.raises(ValueError):
        GenerationUnknown(
            outcome="unknown", reason_code="not a code", reconcile_after_seconds=30
        )
    with pytest.raises(ValueError):
        GenerationRecoveryFound(
            outcome="found",
            request_key=uuid4(),
            provider_job_id="mock-job-123",
            job_status="succeeded",
        )
    with pytest.raises(ValueError):
        GenerationQuerySucceeded(
            outcome="succeeded",
            provider_job_id="mock-job-123",
            result_ref="mock-result:ref",
            result_digest=b"short",
        )


def test_same_request_key_with_a_different_digest_is_a_conflict() -> None:
    with pytest.raises(GenerationIdempotencyConflict) as raised:
        from app.integrations.generation.mock import MockGenerationPort

        MockGenerationPort._assert_same_request(1, b"a" * 32, 1, b"b" * 32)

    assert raised.value.reason_code == "GENERATION_IDEMPOTENCY_CONFLICT"
    assert "a" * 32 not in str(raised.value)


def test_temporary_query_and_download_errors_use_safe_codes() -> None:
    query_error = GenerationQueryTemporaryError("MOCK_QUERY_TEMPORARY_FAILURE")
    download_error = GenerationDownloadTemporaryError("MOCK_DOWNLOAD_TEMPORARY_FAILURE")
    invalid_result_error = GenerationDownloadError("MOCK_RESULT_REFERENCE_INVALID")

    assert query_error.reason_code == "MOCK_QUERY_TEMPORARY_FAILURE"
    assert download_error.reason_code == "MOCK_DOWNLOAD_TEMPORARY_FAILURE"
    assert not isinstance(invalid_result_error, GenerationDownloadTemporaryError)
    with pytest.raises(ValueError):
        GenerationQueryTemporaryError("database password is secret")


def test_test_fault_switches_are_one_shot_and_png_is_deterministic() -> None:
    faults = _TestOnlyFaults(query_failure_once=True)
    assert faults.consume("query_failure_once") is True
    assert faults.consume("query_failure_once") is False

    digest = bytes(range(32))
    result = _png_for_digest(digest)
    assert result.startswith(b"\x89PNG\r\n\x1a\n")
    assert result == _png_for_digest(digest)
    assert result != _png_for_digest(bytes(reversed(range(32))))
    assert len(result) < 1024
    with Image.open(BytesIO(result)) as image:
        image.verify()
    with Image.open(BytesIO(result)) as image:
        assert image.size == (8, 8)


def test_mock_query_progression_is_deterministic_and_bounded() -> None:
    assert _advance_successful_mock_query("pending", 0) == ("pending", 1)
    assert _advance_successful_mock_query("pending", 1) == ("succeeded", 2)
    assert _advance_successful_mock_query("succeeded", 2) == ("succeeded", 2)


def test_generation_service_makes_one_direct_call_per_operation() -> None:
    class FakePort:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def submit(self, command):
            self.calls.append(("submit", command))
            return GenerationUnknown(
                outcome="unknown", reason_code="STATE_UNKNOWN", reconcile_after_seconds=30
            )

        def query(self, provider_job_id):
            self.calls.append(("query", provider_job_id))
            return GenerationQueryUnknown(
                outcome="unknown", provider_job_id=provider_job_id, reason_code="STATE_UNKNOWN"
            )

        def recover(self, request_key):
            self.calls.append(("recover", request_key))
            return GenerationRecoveryDefinitelyNotFound(
                outcome="definitely_not_found", request_key=request_key
            )

        def download(self, result_ref):
            self.calls.append(("download", result_ref))
            return b"png"

    port = FakePort()
    service = GenerationService(port)  # type: ignore[arg-type]
    command = _command()

    assert service.submit(command).outcome == "unknown"
    assert service.query("mock-job-123").outcome == "unknown"
    assert service.recover(command.request_key).outcome == "definitely_not_found"
    assert service.download("mock-result:ref") == b"png"
    assert [operation for operation, _ in port.calls] == [
        "submit",
        "query",
        "recover",
        "download",
    ]
