"""PostgreSQL-persistent mock generation adapter with test-only fault injection."""

from __future__ import annotations

import hashlib
import struct
import threading
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from app.db.generation_models import MockGenerationInvocation, MockGenerationJob
from app.integrations.generation.protocol import (
    GenerationAccepted,
    GenerationCommand,
    GenerationDefinitelyRejected,
    GenerationDownloadError,
    GenerationDownloadTemporaryError,
    GenerationIdempotencyConflict,
    GenerationQueryFailed,
    GenerationQueryPending,
    GenerationQueryResult,
    GenerationQuerySucceeded,
    GenerationQueryTemporaryError,
    GenerationQueryUnknown,
    GenerationRecoveryDefinitelyNotFound,
    GenerationRecoveryFound,
    GenerationRecoveryResult,
    GenerationRecoveryUnknown,
    GenerationRecoveryUnsupported,
    GenerationSubmitResult,
    GenerationUnknown,
)
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

_RESULT_REF_PREFIX = "mock-result:"
_PROVIDER_JOB_PREFIX = "mock-job-"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MOCK_POLL_AFTER_SECONDS = 5
_MOCK_RECONCILE_AFTER_SECONDS = 30
_MOCK_POLLS_UNTIL_SUCCEEDED = 2


@dataclass(slots=True)
class _TestOnlyFaults:
    """Constructor-only fault switches; never read from config or request JSON."""

    before_accept_once: bool = False
    accepted_response_lost_once: bool = False
    query_failure_once: bool = False
    download_failure_once: bool = False
    recovery_unknown_once: bool = False
    recovery_unsupported_once: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def consume(self, name: str) -> bool:
        with self._lock:
            value = getattr(self, name)
            if value:
                setattr(self, name, False)
            return value


class MockGenerationPort:
    """Database-backed local provider mock with durable idempotency and recovery.

    The session factory is private infrastructure for the mock provider store.
    Each method opens its own short transaction; no caller session is accepted.
    Fault switches exist only as an internal constructor argument for tests.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        _test_faults: _TestOnlyFaults | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._test_faults = _test_faults or _TestOnlyFaults()

    def submit(self, command: GenerationCommand) -> GenerationSubmitResult:
        digest = command.request_digest
        rejected_before_accept = False
        job: MockGenerationJob | None = None
        provider_job_id: str | None = None

        try:
            with self._session_factory() as db, db.begin():
                self._lock_request_key(db, command.request_key)
                job = db.execute(
                    select(MockGenerationJob).where(
                        MockGenerationJob.request_key == command.request_key
                    )
                ).scalar_one_or_none()

                if job is not None:
                    self._assert_same_request(
                        job.request_digest_version,
                        job.request_digest,
                        command.request_digest_version,
                        digest,
                    )
                    db.add(self._invocation(command, outcome="accepted", job=job))
                else:
                    prior_invocation = db.execute(
                        select(MockGenerationInvocation)
                        .where(MockGenerationInvocation.request_key == command.request_key)
                        .order_by(MockGenerationInvocation.invoked_at, MockGenerationInvocation.id)
                        .limit(1)
                    ).scalar_one_or_none()
                    if prior_invocation is not None:
                        self._assert_same_request(
                            prior_invocation.request_digest_version,
                            prior_invocation.request_digest,
                            command.request_digest_version,
                            digest,
                        )
                    if self._test_faults.consume("before_accept_once"):
                        db.add(
                            self._invocation(
                                command,
                                outcome="definitely_rejected",
                                job=None,
                            )
                        )
                        rejected_before_accept = True
                    else:
                        job_id = uuid4()
                        provider_job_id = f"{_PROVIDER_JOB_PREFIX}{job_id}"
                        result_ref = f"{_RESULT_REF_PREFIX}{job_id}"
                        result_bytes = _png_for_digest(digest)
                        job = MockGenerationJob(
                            id=job_id,
                            request_key=command.request_key,
                            request_digest_version=command.request_digest_version,
                            request_digest=digest,
                            provider_job_id=provider_job_id,
                            status="pending",
                            query_count=0,
                            result_ref=result_ref,
                            result_digest=hashlib.sha256(result_bytes).digest(),
                        )
                        db.add(job)
                        db.flush()
                        db.add(self._invocation(command, outcome="accepted", job=job))
                if job is not None:
                    provider_job_id = job.provider_job_id
        except GenerationIdempotencyConflict:
            raise
        except SQLAlchemyError:
            # A commit or connection failure cannot prove whether the durable
            # mock provider record was committed; preserve the unknown outcome.
            return GenerationUnknown(
                outcome="unknown",
                reason_code="MOCK_STORE_OUTCOME_UNKNOWN",
                reconcile_after_seconds=_MOCK_RECONCILE_AFTER_SECONDS,
            )

        if rejected_before_accept:
            return GenerationDefinitelyRejected(
                outcome="definitely_rejected",
                reason_code="MOCK_BEFORE_ACCEPT_FAILURE",
            )
        if provider_job_id is None:
            return GenerationUnknown(
                outcome="unknown",
                reason_code="MOCK_JOB_NOT_DURABLE",
                reconcile_after_seconds=_MOCK_RECONCILE_AFTER_SECONDS,
            )
        if self._test_faults.consume("accepted_response_lost_once"):
            return GenerationUnknown(
                outcome="unknown",
                reason_code="MOCK_ACCEPTED_RESPONSE_LOST",
                reconcile_after_seconds=_MOCK_RECONCILE_AFTER_SECONDS,
            )
        return GenerationAccepted(
            outcome="accepted",
            provider_job_id=provider_job_id,
            poll_after_seconds=_MOCK_POLL_AFTER_SECONDS,
        )

    def query(self, provider_job_id: str) -> GenerationQueryResult:
        if self._test_faults.consume("query_failure_once"):
            raise GenerationQueryTemporaryError("MOCK_QUERY_TEMPORARY_FAILURE")
        if _uuid_suffix(provider_job_id, _PROVIDER_JOB_PREFIX) is None:
            return GenerationQueryUnknown(
                outcome="unknown",
                provider_job_id=(
                    provider_job_id
                    if isinstance(provider_job_id, str) and provider_job_id
                    else "invalid"
                ),
                reason_code="MOCK_JOB_REFERENCE_UNKNOWN",
            )
        queried_job: tuple[str, str, str, bytes] | None = None
        try:
            with self._session_factory() as db, db.begin():
                job = db.execute(
                    select(MockGenerationJob).where(
                        MockGenerationJob.provider_job_id == provider_job_id
                    ).with_for_update()
                ).scalar_one_or_none()
                if job is not None:
                    job.status, job.query_count = _advance_successful_mock_query(
                        job.status, job.query_count
                    )
                    queried_job = (
                        job.provider_job_id,
                        job.status,
                        job.result_ref,
                        job.result_digest,
                    )
        except SQLAlchemyError:
            raise GenerationQueryTemporaryError("MOCK_STORE_QUERY_FAILURE") from None

        if queried_job is None:
            return GenerationQueryUnknown(
                outcome="unknown",
                provider_job_id=provider_job_id,
                reason_code="MOCK_JOB_REFERENCE_UNKNOWN",
            )
        if queried_job[1] == "pending":
            return GenerationQueryPending(
                outcome="pending",
                provider_job_id=queried_job[0],
                poll_after_seconds=_MOCK_POLL_AFTER_SECONDS,
            )
        if queried_job[1] == "failed":
            return GenerationQueryFailed(
                outcome="definitely_failed",
                provider_job_id=queried_job[0],
                reason_code="MOCK_GENERATION_FAILED",
            )
        return GenerationQuerySucceeded(
            outcome="succeeded",
            provider_job_id=queried_job[0],
            result_ref=queried_job[2],
            result_digest=queried_job[3],
        )

    def recover(self, request_key: UUID) -> GenerationRecoveryResult:
        if self._test_faults.consume("recovery_unknown_once"):
            return GenerationRecoveryUnknown(
                outcome="unknown",
                request_key=request_key,
                reason_code="MOCK_RECOVERY_TEMPORARY_UNKNOWN",
            )
        if self._test_faults.consume("recovery_unsupported_once"):
            return GenerationRecoveryUnsupported(
                outcome="unsupported",
                request_key=request_key,
            )
        recovered_job: tuple[str, str, str] | None = None
        try:
            with self._session_factory() as db, db.begin():
                self._lock_request_key(db, request_key)
                job = db.execute(
                    select(MockGenerationJob).where(
                        MockGenerationJob.request_key == request_key
                    )
                ).scalar_one_or_none()
                if job is not None:
                    recovered_job = (job.provider_job_id, job.status, job.result_ref)
        except SQLAlchemyError:
            return GenerationRecoveryUnknown(
                outcome="unknown",
                request_key=request_key,
                reason_code="MOCK_STORE_RECOVERY_FAILURE",
            )

        if recovered_job is None:
            # The mock's PostgreSQL index is authoritative for all request keys.
            return GenerationRecoveryDefinitelyNotFound(
                outcome="definitely_not_found",
                request_key=request_key,
            )
        return GenerationRecoveryFound(
            outcome="found",
            request_key=request_key,
            provider_job_id=recovered_job[0],
            job_status=recovered_job[1],
            result_ref=recovered_job[2] if recovered_job[1] == "succeeded" else None,
        )

    def download(self, result_ref: str) -> bytes:
        if self._test_faults.consume("download_failure_once"):
            raise GenerationDownloadTemporaryError("MOCK_DOWNLOAD_TEMPORARY_FAILURE")
        job_id = _uuid_suffix(result_ref, _RESULT_REF_PREFIX)
        if job_id is None:
            raise GenerationDownloadError("MOCK_RESULT_REFERENCE_INVALID")
        try:
            with self._session_factory() as db:
                job = db.execute(
                    select(MockGenerationJob).where(
                        MockGenerationJob.id == job_id,
                        MockGenerationJob.result_ref == result_ref,
                        MockGenerationJob.status == "succeeded",
                    )
                ).scalar_one_or_none()
        except SQLAlchemyError:
            raise GenerationDownloadTemporaryError("MOCK_STORE_DOWNLOAD_FAILURE") from None
        if job is None:
            raise GenerationDownloadError("MOCK_RESULT_UNAVAILABLE")

        result = _png_for_digest(job.request_digest)
        if hashlib.sha256(result).digest() != job.result_digest:
            raise GenerationDownloadError("MOCK_RESULT_INTEGRITY_FAILURE")
        return result

    @staticmethod
    def _assert_same_request(
        stored_version: int,
        stored_digest: bytes,
        requested_version: int,
        requested_digest: bytes,
    ) -> None:
        if stored_version != requested_version or stored_digest != requested_digest:
            raise GenerationIdempotencyConflict("GENERATION_IDEMPOTENCY_CONFLICT")

    @staticmethod
    def _invocation(
        command: GenerationCommand,
        *,
        outcome: str,
        job: MockGenerationJob | None,
    ) -> MockGenerationInvocation:
        return MockGenerationInvocation(
            request_key=command.request_key,
            request_digest_version=command.request_digest_version,
            request_digest=command.request_digest,
            outcome=outcome,
            provider_job_id=job.provider_job_id if job is not None else None,
        )

    @staticmethod
    def _lock_request_key(db: Session, request_key: UUID) -> None:
        """Serialize same-key submit/recover decisions on PostgreSQL."""

        if db.get_bind().dialect.name != "postgresql":
            return
        lock_digest = hashlib.sha256(request_key.bytes).digest()
        first = int.from_bytes(lock_digest[:4], "big", signed=True)
        second = int.from_bytes(lock_digest[4:8], "big", signed=True)
        db.execute(select(func.pg_advisory_xact_lock(first, second)))


def _uuid_suffix(value: str, prefix: str) -> UUID | None:
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    suffix = value[len(prefix) :]
    try:
        parsed = UUID(suffix)
    except (ValueError, AttributeError):
        return None
    if str(parsed) != suffix:
        return None
    return parsed


def _advance_successful_mock_query(status: str, query_count: int) -> tuple[str, int]:
    """Persist one successful query step; failures never call this transition."""

    if status != "pending":
        return status, query_count
    next_count = query_count + 1
    next_status = (
        "succeeded" if next_count >= _MOCK_POLLS_UNTIL_SUCCEEDED else "pending"
    )
    return next_status, next_count


def _png_for_digest(request_digest: bytes) -> bytes:
    """Build a bounded deterministic 8x8 RGB PNG without variable compression."""

    if len(request_digest) != 32:
        raise ValueError("request_digest must be 32 bytes")
    width = height = 8
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # PNG filter type None.
        for x in range(width):
            offset = (x + y * width) % len(request_digest)
            raw.extend(
                (
                    (request_digest[offset] + x * 7) % 256,
                    (request_digest[(offset + 11) % 32] + y * 13) % 256,
                    (request_digest[(offset + 23) % 32] + x + y) % 256,
                )
            )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        _PNG_SIGNATURE
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", _zlib_store(bytes(raw)))
        + _png_chunk(b"IEND", b"")
    )


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def _zlib_store(data: bytes) -> bytes:
    if len(data) > 65_535:
        raise ValueError("mock PNG payload exceeds the deterministic encoder limit")
    length = len(data)
    checksum_a = 1
    checksum_b = 0
    for byte in data:
        checksum_a = (checksum_a + byte) % 65_521
        checksum_b = (checksum_b + checksum_a) % 65_521
    adler32 = (checksum_b << 16) | checksum_a
    stored_block = b"\x01" + struct.pack("<HH", length, 0xFFFF - length) + data
    return b"\x78\x01" + stored_block + struct.pack(">I", adler32)
