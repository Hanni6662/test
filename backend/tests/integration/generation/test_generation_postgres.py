from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from app.db.generation_models import MockGenerationInvocation, MockGenerationJob
from app.integrations.generation.mock import MockGenerationPort, _TestOnlyFaults
from app.integrations.generation.protocol import (
    GenerationAccepted,
    GenerationCommand,
    GenerationDefinitelyRejected,
    GenerationDownloadError,
    GenerationDownloadTemporaryError,
    GenerationIdempotencyConflict,
    GenerationQueryPending,
    GenerationQuerySucceeded,
    GenerationQueryTemporaryError,
    GenerationRecoveryDefinitelyNotFound,
    GenerationRecoveryFound,
    GenerationUnknown,
)
from sqlalchemy import Engine, create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

TEST_DATABASE_URL = os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="dedicated test PostgreSQL is not configured"),
]


def _make_engine() -> Engine:
    assert TEST_DATABASE_URL is not None
    return create_engine(TEST_DATABASE_URL, pool_pre_ping=True)


def _session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def _command(request_key: UUID, *, prompt_text: str = "mock generation input") -> GenerationCommand:
    return GenerationCommand(
        request_key=request_key,
        image_type="front_view",
        prompt_package_version="prompt-v1",
        prompt_text=prompt_text,
        reference_asset_ids=(uuid4(), uuid4()),
    )


def _counts(engine: Engine, request_key: UUID) -> tuple[int, int]:
    with Session(engine) as db:
        jobs = db.scalar(
            select(func.count())
            .select_from(MockGenerationJob)
            .where(MockGenerationJob.request_key == request_key)
        )
        invocations = db.scalar(
            select(func.count())
            .select_from(MockGenerationInvocation)
            .where(MockGenerationInvocation.request_key == request_key)
        )
    return int(invocations or 0), int(jobs or 0)


def _query_count(engine: Engine, request_key: UUID) -> int:
    with Session(engine) as db:
        count = db.scalar(
            select(MockGenerationJob.query_count).where(
                MockGenerationJob.request_key == request_key
            )
        )
    return int(count or 0)


def _cleanup(engine: Engine, request_key: UUID) -> None:
    with engine.begin() as connection:
        connection.execute(
            delete(MockGenerationInvocation).where(
                MockGenerationInvocation.request_key == request_key
            )
        )
        connection.execute(
            delete(MockGenerationJob).where(MockGenerationJob.request_key == request_key)
        )


def test_same_key_returns_the_same_job_and_counts_survive_adapter_restart() -> None:
    request_key = uuid4()
    command = _command(request_key)
    engine_one = _make_engine()
    engine_two: Engine | None = None
    try:
        first_port = MockGenerationPort(_session_factory(engine_one))
        first = first_port.submit(command)
        assert isinstance(first, GenerationAccepted)
        first_query = first_port.query(first.provider_job_id)
        assert isinstance(first_query, GenerationQueryPending)
        assert _query_count(engine_one, request_key) == 1
        engine_one.dispose()

        engine_two = _make_engine()
        second_port = MockGenerationPort(_session_factory(engine_two))
        second = second_port.submit(command)
        assert isinstance(second, GenerationAccepted)
        assert second.provider_job_id == first.provider_job_id
        second_query = second_port.query(second.provider_job_id)
        assert isinstance(second_query, GenerationQuerySucceeded)
        assert _query_count(engine_two, request_key) == 2
        assert second_port.download(second_query.result_ref).startswith(b"\x89PNG\r\n\x1a\n")
        assert _counts(engine_two, request_key) == (2, 1)
    finally:
        target = engine_two or engine_one
        _cleanup(target, request_key)
        engine_one.dispose()
        if engine_two is not None:
            engine_two.dispose()


def test_accepted_then_lost_response_recovers_after_adapter_restart() -> None:
    request_key = uuid4()
    command = _command(request_key)
    engine_one = _make_engine()
    engine_two: Engine | None = None
    try:
        first_port = MockGenerationPort(
            _session_factory(engine_one),
            _test_faults=_TestOnlyFaults(accepted_response_lost_once=True),
        )
        submitted = first_port.submit(command)
        assert isinstance(submitted, GenerationUnknown)
        assert submitted.reason_code == "MOCK_ACCEPTED_RESPONSE_LOST"
        engine_one.dispose()

        engine_two = _make_engine()
        recovered_port = MockGenerationPort(_session_factory(engine_two))
        recovered = recovered_port.recover(request_key)
        assert isinstance(recovered, GenerationRecoveryFound)
        assert recovered.job_status == "pending"
        assert recovered.result_ref is None
        pending = recovered_port.query(recovered.provider_job_id)
        assert isinstance(pending, GenerationQueryPending)
        query = recovered_port.query(recovered.provider_job_id)
        assert isinstance(query, GenerationQuerySucceeded)
        result = recovered_port.download(query.result_ref)
        assert result.startswith(b"\x89PNG\r\n\x1a\n")
        assert _counts(engine_two, request_key) == (1, 1)
    finally:
        target = engine_two or engine_one
        _cleanup(target, request_key)
        engine_one.dispose()
        if engine_two is not None:
            engine_two.dispose()


def test_before_accept_failure_is_definite_and_retry_uses_same_key() -> None:
    request_key = uuid4()
    command = _command(request_key)
    engine = _make_engine()
    try:
        port = MockGenerationPort(
            _session_factory(engine), _test_faults=_TestOnlyFaults(before_accept_once=True)
        )
        rejected = port.submit(command)
        assert isinstance(rejected, GenerationDefinitelyRejected)
        with pytest.raises(GenerationIdempotencyConflict):
            port.submit(_command(request_key, prompt_text="different input"))
        assert isinstance(port.recover(request_key), GenerationRecoveryDefinitelyNotFound)

        retry = MockGenerationPort(_session_factory(engine)).submit(command)
        assert isinstance(retry, GenerationAccepted)
        assert _counts(engine, request_key) == (2, 1)
    finally:
        _cleanup(engine, request_key)
        engine.dispose()


def test_query_and_download_failures_can_be_retried_without_resubmitting() -> None:
    request_key = uuid4()
    command = _command(request_key)
    engine = _make_engine()
    try:
        initial = MockGenerationPort(_session_factory(engine)).submit(command)
        assert isinstance(initial, GenerationAccepted)
        failing_port = MockGenerationPort(
            _session_factory(engine),
            _test_faults=_TestOnlyFaults(query_failure_once=True, download_failure_once=True),
        )
        with pytest.raises(GenerationQueryTemporaryError):
            failing_port.query(initial.provider_job_id)
        assert _query_count(engine, request_key) == 0
        recovery_port = MockGenerationPort(_session_factory(engine))
        pending = recovery_port.query(initial.provider_job_id)
        assert isinstance(pending, GenerationQueryPending)
        assert _query_count(engine, request_key) == 1
        job_uuid = initial.provider_job_id.removeprefix("mock-job-")
        not_ready_ref = f"mock-result:{job_uuid}"
        with pytest.raises(GenerationDownloadError):
            recovery_port.download(not_ready_ref)

        queried = failing_port.query(initial.provider_job_id)
        assert isinstance(queried, GenerationQuerySucceeded)
        assert _query_count(engine, request_key) == 2
        with pytest.raises(GenerationDownloadTemporaryError):
            failing_port.download(queried.result_ref)

        downloaded = MockGenerationPort(_session_factory(engine)).download(queried.result_ref)
        assert downloaded.startswith(b"\x89PNG\r\n\x1a\n")
        assert _counts(engine, request_key) == (1, 1)
    finally:
        _cleanup(engine, request_key)
        engine.dispose()


def test_same_key_different_digest_conflicts_without_creating_another_job() -> None:
    request_key = uuid4()
    engine = _make_engine()
    try:
        port = MockGenerationPort(_session_factory(engine))
        first = port.submit(_command(request_key, prompt_text="first input"))
        assert isinstance(first, GenerationAccepted)
        with pytest.raises(GenerationIdempotencyConflict):
            port.submit(_command(request_key, prompt_text="different input"))
        assert _counts(engine, request_key) == (1, 1)
    finally:
        _cleanup(engine, request_key)
        engine.dispose()
