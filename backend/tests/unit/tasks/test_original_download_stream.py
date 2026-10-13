from __future__ import annotations

import hashlib
import json
import threading
from io import BytesIO
from time import monotonic
from uuid import UUID, uuid4

import anyio
import pytest
from app.core.domain_errors import DomainError
from app.services import original_downloads as downloads
from app.services.generation_image_saving import MAX_GENERATION_IMAGE_BYTES
from app.services.original_downloads import (
    DownloadGate,
    OriginalDownload,
    OriginalDownloadResponse,
    prepare_original_download,
)


class TrackedReader(BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.read_sizes: list[int] = []
        self.close_calls = 0
        self.fail_reads = False
        self.entered_read: threading.Event | None = None
        self.allow_read: threading.Event | None = None

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if self.fail_reads:
            raise OSError("private test-only reader error")
        if self.entered_read is not None:
            self.entered_read.set()
        if self.allow_read is not None:
            if not self.allow_read.wait(5):
                raise OSError("test reader timed out")
        return super().read(size)

    def close(self) -> None:
        if not self.closed:
            self.close_calls += 1
        super().close()


class RepeatingReader:
    """A bounded-memory reader for exercising the 32 MiB input ceiling."""

    def __init__(self, size: int) -> None:
        self.remaining = size
        self.position = 0
        self.closed = False
        self.read_sizes: list[int] = []
        self.close_calls = 0

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        count = self.remaining if size < 0 else min(size, self.remaining)
        self.remaining -= count
        self.position += count
        return b"x" * count

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence != 0 or offset != 0:
            raise OSError("unsupported seek")
        self.position = 0
        self.remaining = self.initial_size
        return 0

    def tell(self) -> int:
        return self.position

    def close(self) -> None:
        self.closed = True
        self.close_calls += 1

    @property
    def initial_size(self) -> int:
        return sum(self.read_sizes)


def _valid_download(
    *,
    payload: bytes = b"original bytes",
    mime_type: str = "image/png",
    task_id: UUID | None = None,
    image_id: UUID | None = None,
    gate: DownloadGate | None = None,
) -> tuple[OriginalDownload, TrackedReader, DownloadGate]:
    selected_gate = gate or DownloadGate(1, 30)
    lease = selected_gate.try_acquire()
    assert lease is not None
    reader = TrackedReader(payload)
    download = prepare_original_download(
        reader=reader,
        mime_type=mime_type,
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        task_id=task_id or UUID("11111111-2222-4333-8444-555555555555"),
        image_id=image_id or UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
        lease=lease,
    )
    return download, reader, selected_gate


def _scope(*, range_header: bool = False) -> dict[str, object]:
    headers = [(b"range", b"bytes=1-2")] if range_header else []
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/tasks/task/images/image/original",
        "raw_path": b"/api/v1/tasks/task/images/image/original",
        "query_string": b"",
        "headers": headers,
        "server": ("test", 80),
        "client": ("test", 1234),
        "state": {},
    }


async def _invoke_response(
    response: OriginalDownloadResponse,
    *,
    range_header: bool = False,
    send_hook=None,
) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)
        if send_hook is not None:
            await send_hook(message)

    await response(_scope(range_header=range_header), receive, send)
    return messages


def _run_response(
    response: OriginalDownloadResponse,
    *,
    range_header: bool = False,
    send_hook=None,
) -> list[dict[str, object]]:
    async def run() -> list[dict[str, object]]:
        return await _invoke_response(
            response,
            range_header=range_header,
            send_hook=send_hook,
        )

    return anyio.run(run)


@pytest.mark.parametrize(
    ("mime_type", "extension"),
    [
        ("image/png", "png"),
        ("image/jpeg", "jpg"),
        ("image/webp", "webp"),
    ],
)
def test_o02_mime_filename_and_attachment_headers(mime_type: str, extension: str) -> None:
    task_id = UUID("11111111-2222-4333-8444-555555555555")
    image_id = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    download, _, _ = _valid_download(mime_type=mime_type, task_id=task_id, image_id=image_id)

    response = OriginalDownloadResponse(download, "request-123")

    assert response.status_code == 200
    assert response.headers["content-type"] == mime_type
    assert response.headers["content-disposition"] == (
        f'attachment; filename="task_{task_id}_image_{image_id}.{extension}"'
    )
    assert response.headers["content-length"] == str(download.size_bytes)
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-request-id"] == "request-123"
    assert response.headers["accept-ranges"] == "none"


def test_response_streams_bounded_chunks_and_ignores_range() -> None:
    payload = bytes(range(251)) * 1800
    download, reader, gate = _valid_download(payload=payload)
    response = OriginalDownloadResponse(download, "request-stream")

    messages = _run_response(response, range_header=True)

    starts = [message for message in messages if message["type"] == "http.response.start"]
    chunks = [
        message["body"]
        for message in messages
        if message["type"] == "http.response.body" and message["body"]
    ]
    endings = [
        message
        for message in messages
        if message["type"] == "http.response.body" and not message["more_body"]
    ]
    assert starts[0]["status"] == 200
    assert len(starts) == 1
    assert all(len(chunk) <= 128 * 1024 for chunk in chunks)
    assert b"".join(chunks) == payload
    assert hashlib.sha256(b"".join(chunks)).hexdigest() == hashlib.sha256(payload).hexdigest()
    assert len(endings) == 1
    assert reader.close_calls == 1
    assert gate.try_acquire() is not None


@pytest.mark.parametrize(
    ("mime_type", "expected_size", "expected_sha256", "payload", "expected_code"),
    [
        ("image/gif", 4, hashlib.sha256(b"data").hexdigest(), b"data", "ORIGINAL_INTEGRITY"),
        ("image/png", 5, hashlib.sha256(b"data").hexdigest(), b"data", "ORIGINAL_INTEGRITY"),
        ("image/png", 4, "z" * 64, b"data", "ORIGINAL_INTEGRITY"),
        ("image/png", 4, "not-a-digest", b"data", "ORIGINAL_INTEGRITY"),
        ("image/png", 0, hashlib.sha256(b"").hexdigest(), b"", "ORIGINAL_INTEGRITY"),
    ],
)
def test_prepare_rejects_invalid_metadata_and_empty_files(
    mime_type: str,
    expected_size: int,
    expected_sha256: str,
    payload: bytes,
    expected_code: str,
) -> None:
    gate = DownloadGate(1, 30)
    lease = gate.try_acquire()
    assert lease is not None
    reader = TrackedReader(payload)

    with pytest.raises(DomainError) as error:
        prepare_original_download(
            reader=reader,
            mime_type=mime_type,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            task_id=uuid4(),
            image_id=uuid4(),
            lease=lease,
        )

    assert error.value.status_code == 503
    assert error.value.code == expected_code
    assert reader.close_calls == 1
    assert gate.try_acquire() is not None


def test_prepare_rejects_over_limit_reader_with_bounded_reads() -> None:
    gate = DownloadGate(1, 30)
    lease = gate.try_acquire()
    assert lease is not None
    reader = RepeatingReader(MAX_GENERATION_IMAGE_BYTES + 1)

    with pytest.raises(DomainError) as error:
        prepare_original_download(
            reader=reader,  # type: ignore[arg-type]
            mime_type="image/jpeg",
            expected_size=1,
            expected_sha256=hashlib.sha256(b"x").hexdigest(),
            task_id=uuid4(),
            image_id=uuid4(),
            lease=lease,
        )

    assert error.value.code == "ORIGINAL_INTEGRITY"
    assert max(reader.read_sizes) <= 128 * 1024
    assert reader.close_calls == 1
    assert gate.try_acquire() is not None


def test_prepare_maps_read_io_failure_to_unavailable() -> None:
    gate = DownloadGate(1, 30)
    lease = gate.try_acquire()
    assert lease is not None
    reader = TrackedReader(b"payload")
    reader.fail_reads = True

    with pytest.raises(DomainError) as error:
        prepare_original_download(
            reader=reader,
            mime_type="image/png",
            expected_size=7,
            expected_sha256=hashlib.sha256(b"payload").hexdigest(),
            task_id=uuid4(),
            image_id=uuid4(),
            lease=lease,
        )

    assert error.value.code == "ORIGINAL_UNAVAILABLE"
    assert reader.close_calls == 1
    assert gate.try_acquire() is not None


def test_prepare_scans_and_resets_the_same_reader() -> None:
    payload = b"same reader object" * 100
    gate = DownloadGate(1, 30)
    lease = gate.try_acquire()
    assert lease is not None
    reader = TrackedReader(payload)

    download = prepare_original_download(
        reader=reader,
        mime_type="image/webp",
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        task_id=uuid4(),
        image_id=uuid4(),
        lease=lease,
    )

    assert download.reader is reader
    assert reader.tell() == 0
    assert b"".join(download.iter_chunks()) == payload
    assert max(reader.read_sizes) <= 128 * 1024
    download.close()


def test_prepare_maps_seek_contract_fault_to_integrity() -> None:
    class UnseekableReader(TrackedReader):
        def seek(self, offset: int, whence: int = 0) -> int:
            raise OSError("test-only seek fault")

    gate = DownloadGate(1, 30)
    lease = gate.try_acquire()
    assert lease is not None
    payload = b"payload"
    reader = UnseekableReader(payload)

    with pytest.raises(DomainError) as error:
        prepare_original_download(
            reader=reader,
            mime_type="image/png",
            expected_size=len(payload),
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            task_id=uuid4(),
            image_id=uuid4(),
            lease=lease,
        )

    assert error.value.code == "ORIGINAL_INTEGRITY"
    assert reader.close_calls == 1
    assert gate.try_acquire() is not None


def test_gate_is_immediate_bounded_and_lease_release_is_idempotent(monkeypatch) -> None:
    now = [10.0]
    monkeypatch.setattr(downloads, "monotonic", lambda: now[0])
    gate = DownloadGate(1, 5)
    lease = gate.try_acquire()
    assert lease is not None
    assert gate.try_acquire() is None

    lease.release()
    lease.release()
    fresh_lease = gate.try_acquire()
    assert fresh_lease is not None
    now[0] = 15.0
    with pytest.raises(DomainError) as error:
        fresh_lease.check_deadline()
    assert error.value.code == "DOWNLOAD_BUSY"
    fresh_lease.release()
    assert gate.try_acquire() is not None


@pytest.mark.parametrize(
    ("max_concurrent", "timeout_seconds"),
    [(0, 1), (1, 0), (True, 1), (1, True)],
)
def test_gate_rejects_invalid_limits(max_concurrent: int, timeout_seconds: int) -> None:
    with pytest.raises(ValueError):
        DownloadGate(max_concurrent, timeout_seconds)


def test_download_close_and_response_cleanup_are_idempotent() -> None:
    download, reader, gate = _valid_download()
    lease = download.lease
    response = OriginalDownloadResponse(download, "request-idempotent")

    _run_response(response)
    download.close()
    lease.release()

    assert reader.close_calls == 1
    assert gate.try_acquire() is not None


def test_send_failure_terminates_without_appending_json_and_releases() -> None:
    download, reader, gate = _valid_download(payload=b"payload" * 100)
    response = OriginalDownloadResponse(download, "request-send-failure")

    async def fail_on_first_body(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body":
            raise OSError("client disconnected")

    messages = _run_response(response, send_hook=fail_on_first_body)

    assert len(messages) == 2
    assert messages[0]["type"] == "http.response.start"
    assert messages[1]["type"] == "http.response.body"
    assert messages[1]["body"] == b"payload" * 100
    assert reader.close_calls == 1
    assert gate.try_acquire() is not None


def test_send_cancellation_closes_and_releases_without_final_body() -> None:
    download, reader, gate = _valid_download(payload=b"cancel send" * 100)
    response = OriginalDownloadResponse(download, "request-send-cancel")

    async def run() -> list[dict[str, object]]:
        scope = anyio.CancelScope()
        body_send_entered = anyio.Event()
        messages: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)
            if message["type"] == "http.response.body" and message["body"]:
                body_send_entered.set()
                await anyio.sleep_forever()

        async def invoke() -> None:
            with scope:
                await response(_scope(), receive, send)

        async with anyio.create_task_group() as group:
            group.start_soon(invoke)
            await body_send_entered.wait()
            scope.cancel()

        return messages

    messages = anyio.run(run)

    assert not any(
        message["type"] == "http.response.body" and not message["more_body"] for message in messages
    )
    assert reader.close_calls == 1
    assert gate.try_acquire() is not None


def test_response_start_failure_closes_when_body_is_never_iterated() -> None:
    download, reader, gate = _valid_download()
    reads_before_response = len(reader.read_sizes)
    response = OriginalDownloadResponse(download, "request-no-body")

    async def fail_at_start(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            raise OSError("client disconnected before body")

    messages = _run_response(response, send_hook=fail_at_start)

    assert len(messages) == 1
    assert messages[0]["type"] == "http.response.start"
    assert len(reader.read_sizes) == reads_before_response
    assert reader.close_calls == 1
    assert gate.try_acquire() is not None


def test_cancel_after_start_before_first_read_still_closes_and_releases() -> None:
    download, reader, gate = _valid_download()
    reads_before_response = len(reader.read_sizes)
    response = OriginalDownloadResponse(download, "request-cancel-before-body")

    async def run() -> list[dict[str, object]]:
        scope = anyio.CancelScope()
        messages: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)
            if message["type"] == "http.response.start":
                scope.cancel()

        with scope:
            await response(_scope(), receive, send)
        return messages

    messages = anyio.run(run)

    assert len(messages) == 1
    assert messages[0]["type"] == "http.response.start"
    assert len(reader.read_sizes) == reads_before_response
    assert reader.close_calls == 1
    assert gate.try_acquire() is not None


def test_short_reader_does_not_send_final_success_body() -> None:
    payload = b"z" * (128 * 1024 + 100)
    download, reader, gate = _valid_download(payload=payload)
    reader.seek(0)
    reader.truncate(128 * 1024)
    response = OriginalDownloadResponse(download, "request-short")

    messages = _run_response(response)

    assert not any(
        message["type"] == "http.response.body" and not message["more_body"] for message in messages
    )
    body = b"".join(
        message["body"]
        for message in messages
        if message["type"] == "http.response.body" and message["body"]
    )
    assert len(body) < len(payload)
    assert reader.close_calls == 1
    assert gate.try_acquire() is not None


def test_long_reader_does_not_send_final_content_length_bytes() -> None:
    payload = b"original body"
    download, reader, gate = _valid_download(payload=payload)
    reader.seek(0, 2)
    reader.write(b"extra")
    reader.seek(0)
    response = OriginalDownloadResponse(download, "request-long")

    messages = _run_response(response)

    body = b"".join(
        message["body"]
        for message in messages
        if message["type"] == "http.response.body" and message["body"]
    )
    assert body == b""
    assert not any(
        message["type"] == "http.response.body" and not message["more_body"] for message in messages
    )
    assert reader.close_calls == 1
    assert gate.try_acquire() is not None


def test_transfer_read_failure_after_headers_does_not_append_json() -> None:
    download, reader, gate = _valid_download()
    reader.fail_reads = True
    response = OriginalDownloadResponse(download, "request-read-failure")

    messages = _run_response(response)

    assert messages[0]["type"] == "http.response.start"
    assert len(messages) == 1
    assert reader.close_calls == 1
    assert gate.try_acquire() is not None


def test_transfer_timeout_after_headers_ends_without_final_body(monkeypatch) -> None:
    now = [0.0]
    monkeypatch.setattr(downloads, "monotonic", lambda: now[0])
    gate = DownloadGate(1, 1)
    lease = gate.try_acquire()
    assert lease is not None
    payload = b"a" * (128 * 1024 + 10)
    reader = TrackedReader(payload)
    download = prepare_original_download(
        reader=reader,
        mime_type="image/png",
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        task_id=uuid4(),
        image_id=uuid4(),
        lease=lease,
    )
    response = OriginalDownloadResponse(download, "request-timeout")

    async def advance_clock_after_first_chunk(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body" and message["body"]:
            now[0] = 2.0

    messages = _run_response(response, send_hook=advance_clock_after_first_chunk)

    assert len([message for message in messages if message["type"] == "http.response.start"]) == 1
    assert not any(
        message["type"] == "http.response.body" and not message["more_body"] for message in messages
    )
    assert reader.close_calls == 1
    assert gate.try_acquire() is not None


def test_cancel_waits_for_inflight_reader_before_close_and_release() -> None:
    download, reader, gate = _valid_download(payload=b"cancel me")
    entered = threading.Event()
    allow = threading.Event()
    reader.entered_read = entered
    reader.allow_read = allow
    response = OriginalDownloadResponse(download, "request-cancel")

    async def run() -> None:
        scope = anyio.CancelScope()
        messages: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        async def invoke() -> None:
            with scope:
                await response(_scope(), receive, send)

        async with anyio.create_task_group() as group:
            group.start_soon(invoke)
            entered_on_worker = await anyio.to_thread.run_sync(lambda: entered.wait(3))
            assert entered_on_worker
            scope.cancel()
            await anyio.sleep(0.05)
            assert reader.close_calls == 0
            assert gate.try_acquire() is None
            allow.set()

        assert reader.close_calls == 1
        assert gate.try_acquire() is not None
        assert messages[0]["type"] == "http.response.start"

    anyio.run(run)


def test_response_constructor_timeout_closes_download_and_releases_gate(monkeypatch) -> None:
    now = [0.0]
    monkeypatch.setattr(downloads, "monotonic", lambda: now[0])
    download, reader, gate = _valid_download(gate=DownloadGate(1, 1))
    now[0] = 2.0

    with pytest.raises(DomainError) as error:
        OriginalDownloadResponse(download, "request-expired")

    assert error.value.code == "DOWNLOAD_BUSY"
    assert reader.close_calls == 1
    assert gate.try_acquire() is not None


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
@pytest.mark.parametrize("blocked_phase", ["start", "body", "final_body"])
def test_stalled_transport_enforces_lease_without_external_cancel(
    spec_version: str, blocked_phase: str, caplog,
) -> None:
    download, reader, gate = _valid_download(gate=DownloadGate(1, 1))
    response = OriginalDownloadResponse(download, "request-own-deadline")
    messages: list[dict[str, object]] = []

    async def run() -> None:
        async def receive():
            await anyio.sleep_forever()

        async def send(message):
            messages.append(message)
            is_target = (
                (blocked_phase == "start" and message["type"] == "http.response.start")
                or (blocked_phase == "body" and message.get("body"))
                or (blocked_phase == "final_body" and message.get("more_body") is False)
            )
            if is_target:
                await anyio.sleep_forever()

        scope = _scope()
        scope["asgi"]["spec_version"] = spec_version
        completed = False
        with anyio.move_on_after(1.6) as watchdog:
            await response(scope, receive, send)
            completed = True
        assert completed and not watchdog.cancel_called

    with caplog.at_level("INFO", logger=downloads.__name__):
        anyio.run(run)
    assert reader.close_calls == 1
    recovered = gate.try_acquire()
    assert recovered is not None
    recovered.release()
    assert len([m for m in messages if m["type"] == "http.response.start"]) == 1
    assert not any(b"DOWNLOAD_BUSY" in m.get("body", b"") for m in messages)
    assert "reason=DOWNLOAD_BUSY" in caplog.text
    assert "outcome=completed" not in caplog.text


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
def test_expired_response_returns_safe_503_before_any_success_header(
    monkeypatch, spec_version: str,
) -> None:
    now = [0.0]
    monkeypatch.setattr(downloads, "monotonic", lambda: now[0])
    download, reader, gate = _valid_download(gate=DownloadGate(1, 1))
    response = OriginalDownloadResponse(download, "request-expired-entry")
    now[0] = 2.0
    messages = []

    async def run() -> None:
        async def receive():
            await anyio.sleep_forever()

        async def send(message):
            # The image slot is already released, even while error transport runs.
            assert reader.close_calls == 1
            recovered = gate.try_acquire()
            assert recovered is not None
            recovered.release()
            messages.append(message)

        scope = _scope()
        scope["asgi"]["spec_version"] = spec_version
        await response(scope, receive, send)

    anyio.run(run)
    assert [m["status"] for m in messages if m["type"] == "http.response.start"] == [503]
    headers = dict(messages[0]["headers"])
    assert headers[b"content-type"].startswith(b"application/json")
    assert headers[b"retry-after"] == b"5"
    assert headers[b"x-request-id"] == b"request-expired-entry"
    body = json.loads(messages[1]["body"])
    assert body == {"code": "DOWNLOAD_BUSY", "message": "原图下载暂不可用",
                    "request_id": "request-expired-entry"}
    assert reader.close_calls == 1


def test_deadline_rechecked_immediately_before_start_attempt(monkeypatch) -> None:
    now = [0.0]
    monkeypatch.setattr(downloads, "monotonic", lambda: now[0])
    download, reader, gate = _valid_download(gate=DownloadGate(1, 1))
    response = OriginalDownloadResponse(download, "request-admission-delay")
    original_remaining = download.lease.remaining_seconds

    def remaining_then_expire() -> float:
        remaining = original_remaining()
        now[0] = 2.0
        return remaining

    monkeypatch.setattr(download.lease, "remaining_seconds", remaining_then_expire)
    messages = _run_response(response)
    assert messages[0]["status"] == 503
    assert reader.close_calls == 1
    recovered = gate.try_acquire()
    assert recovered is not None
    recovered.release()


def test_stalled_error_send_is_bounded_and_holds_no_image_slot(monkeypatch) -> None:
    now = [0.0]
    monkeypatch.setattr(downloads, "monotonic", lambda: now[0])
    download, reader, gate = _valid_download(gate=DownloadGate(1, 1))
    response = OriginalDownloadResponse(download, "request-error-stall")
    now[0] = 2.0

    async def run() -> None:
        messages = []

        async def receive():
            await anyio.sleep_forever()

        async def send(message):
            assert reader.closed
            recovered = gate.try_acquire()
            assert recovered is not None
            recovered.release()
            messages.append(message)
            await anyio.sleep_forever()

        with anyio.move_on_after(1.6) as watchdog:
            await response(_scope(), receive, send)
        assert not watchdog.cancel_called
        assert len(messages) == 1 and messages[0]["status"] == 503

    anyio.run(run)
    assert reader.close_calls == 1


def test_chunk_sends_share_absolute_budget_instead_of_resetting_it() -> None:
    download, reader, gate = _valid_download(
        payload=b"x" * (2 * 128 * 1024 + 1), gate=DownloadGate(1, 1),
    )
    response = OriginalDownloadResponse(download, "request-shared-budget")
    messages = []

    async def run() -> None:
        async def receive():
            await anyio.sleep_forever()

        async def send(message):
            messages.append(message)
            if message.get("body"):
                await anyio.sleep(0.6)

        started = monotonic()
        with anyio.move_on_after(1.6) as watchdog:
            await response(_scope(), receive, send)
        assert not watchdog.cancel_called
        assert monotonic() - started < 1.6

    anyio.run(run)
    assert not any(m.get("more_body") is False for m in messages)
    assert reader.close_calls == 1
    recovered = gate.try_acquire()
    assert recovered is not None
    recovered.release()


def test_own_timeout_waits_for_reader_thread_before_closing_and_releasing() -> None:
    download, reader, gate = _valid_download(gate=DownloadGate(1, 1))
    entered, allow = threading.Event(), threading.Event()
    reader.entered_read, reader.allow_read = entered, allow
    response = OriginalDownloadResponse(download, "request-thread-deadline")
    messages = []

    async def run() -> None:
        async def receive():
            await anyio.sleep_forever()

        async def send(message):
            messages.append(message)

        async with anyio.create_task_group() as group:
            group.start_soon(response, _scope(), receive, send)
            try:
                assert await anyio.to_thread.run_sync(lambda: entered.wait(2))
                await anyio.sleep(1.1)
                assert reader.close_calls == 0
                assert gate.try_acquire() is None
            finally:
                allow.set()

    anyio.run(run)
    assert reader.close_calls == 1
    assert not any(m.get("body") for m in messages)
    recovered = gate.try_acquire()
    assert recovered is not None
    recovered.release()
