from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID

from app.core.config import Settings
from app.core.request_id import RequestIdMiddleware
from app.core.upload_limits import RequestBodySizeLimitMiddleware
from app.main import create_app

TEST_DATABASE_URL = "postgresql+psycopg://test:test@127.0.0.1:1/image_backend"


def test_body_limit_checks_actual_bytes_even_when_length_header_is_small() -> None:
    async def scenario() -> tuple[int, dict[str, object]]:
        sent = []
        messages = iter(
            [
                {"type": "http.request", "body": b"abc", "more_body": True},
                {"type": "http.request", "body": b"def", "more_body": False},
            ]
        )

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        async def application(_scope, inner_receive, _send):
            await inner_receive()
            await inner_receive()

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/reference-sets",
            "headers": [(b"content-length", b"1")],
            "state": {},
        }
        middleware = RequestBodySizeLimitMiddleware(application, max_bytes=5)
        await middleware(scope, receive, send)
        status = sent[0]["status"]
        payload = json.loads(sent[1]["body"])
        return status, payload

    status, payload = asyncio.run(scenario())

    assert status == 413
    assert payload["code"] == "UPLOAD_TOO_LARGE"
    assert payload["message"] == "请求内容超过允许大小"


def test_early_content_length_rejection_reuses_outer_request_id() -> None:
    async def scenario() -> list[dict[str, object]]:
        sent: list[dict[str, object]] = []

        async def receive():
            raise AssertionError("the declared size should be rejected before reading")

        async def send(message):
            sent.append(message)

        async def application(_scope, _receive, _send):
            raise AssertionError("the oversized request must not reach the app")

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/reference-sets",
            "headers": [(b"content-length", b"6")],
            "state": {},
        }
        middleware = RequestIdMiddleware(
            RequestBodySizeLimitMiddleware(application, max_bytes=5)
        )
        await middleware(scope, receive, send)
        return sent

    sent = asyncio.run(scenario())
    payload = json.loads(sent[1]["body"])
    response_headers = dict(sent[0]["headers"])

    assert sent[0]["status"] == 413
    assert len([message for message in sent if message["type"] == "http.response.start"]) == 1
    assert payload["request_id"] == response_headers[b"x-request-id"].decode("ascii")
    UUID(payload["request_id"])


def test_chunked_overflow_is_rejected_before_app_and_spool_is_closed(monkeypatch) -> None:
    import app.core.upload_limits as upload_limits

    spool_instances = []
    spool_factory = upload_limits.tempfile.SpooledTemporaryFile

    def tracked_spool(*args, **kwargs):
        spool = spool_factory(*args, **kwargs)
        spool_instances.append(spool)
        return spool

    monkeypatch.setattr(upload_limits.tempfile, "SpooledTemporaryFile", tracked_spool)

    async def scenario() -> list[dict[str, object]]:
        sent: list[dict[str, object]] = []
        messages = iter(
            [
                {"type": "http.request", "body": b"first", "more_body": True},
                {"type": "http.request", "body": b"second", "more_body": True},
            ]
        )

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        async def application(_scope, _receive, _send):
            raise AssertionError("overflow must be rejected before multipart parsing")

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/reference-sets",
            "headers": [(b"content-length", b"1")],
            "state": {},
        }
        middleware = RequestIdMiddleware(
            RequestBodySizeLimitMiddleware(application, max_bytes=10)
        )
        await middleware(scope, receive, send)
        return sent

    sent = asyncio.run(scenario())
    payload = json.loads(sent[1]["body"])
    response_headers = dict(sent[0]["headers"])

    assert sent[0]["status"] == 413
    assert len([message for message in sent if message["type"] == "http.response.start"]) == 1
    assert payload["request_id"] == response_headers[b"x-request-id"].decode("ascii")
    UUID(payload["request_id"])
    assert len(spool_instances) == 1
    assert spool_instances[0].closed


def test_main_app_rejects_declared_and_chunked_overflow_with_matching_request_id(
    tmp_path: Path,
) -> None:
    class UnusedEngine:
        def dispose(self) -> None:
            pass

    settings = Settings(
        database_url=TEST_DATABASE_URL,
        production=False,
        use_dev_cookie=True,
        trusted_origins=frozenset({"http://testserver"}),
        private_storage_root=tmp_path / "private",
    )
    app = create_app(settings=settings, engine=UnusedEngine())  # type: ignore[arg-type]

    async def invoke(headers: list[tuple[bytes, bytes]], chunks: list[bytes]):
        sent: list[dict[str, object]] = []
        messages = iter(
            [
                {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": index < len(chunks) - 1,
                }
                for index, chunk in enumerate(chunks)
            ]
        )

        async def receive():
            try:
                return next(messages)
            except StopIteration:
                return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/reference-sets",
            "raw_path": b"/api/v1/reference-sets",
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "state": {},
        }
        await app(scope, receive, send)
        start_messages = [message for message in sent if message["type"] == "http.response.start"]
        assert len(start_messages) == 1
        start = start_messages[0]
        body_message = next(message for message in sent if message["type"] == "http.response.body")
        payload = json.loads(body_message["body"])
        response_headers = dict(start["headers"])
        assert start["status"] == 413
        assert payload["request_id"] == response_headers[b"x-request-id"].decode("ascii")
        UUID(payload["request_id"])

    asyncio.run(
        invoke(
            [(b"content-length", str(50 * 1024 * 1024 + 1).encode("ascii"))],
            [],
        )
    )
    chunk = b"x" * (30 * 1024 * 1024)
    asyncio.run(
        invoke(
            [(b"content-type", b"multipart/form-data; boundary=test")],
            [chunk, chunk],
        )
    )
