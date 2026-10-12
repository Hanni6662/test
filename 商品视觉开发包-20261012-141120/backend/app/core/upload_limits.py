from __future__ import annotations

import json
import tempfile
from uuid import uuid4

import anyio
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_MAX_BYTES = 50 * 1024 * 1024
IMAGE_MAX_BYTES = 10 * 1024 * 1024
REFERENCE_SET_MAX_BYTES = 40 * 1024 * 1024
IMAGE_MAX_PIXELS = 25_000_000
_MEMORY_BODY_BYTES = 1024 * 1024
_REPLAY_CHUNK_BYTES = 64 * 1024


class RequestBodySizeLimitMiddleware:
    """Bound reference-upload requests before multipart parsing creates temp files."""

    def __init__(self, app: ASGIApp, *, max_bytes: int = REQUEST_MAX_BYTES) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._is_reference_upload(scope):
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        request_id = state.get("request_id")
        if not isinstance(request_id, str):
            request_id = str(uuid4())
            state["request_id"] = request_id

        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = 0
            if declared_size > self.max_bytes:
                await self._send_too_large(send, request_id)
                return

        spool = tempfile.SpooledTemporaryFile(max_size=_MEMORY_BODY_BYTES, mode="w+b")
        body_size = 0
        try:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    continue
                chunk = message.get("body", b"")
                body_size += len(chunk)
                if body_size > self.max_bytes:
                    await self._send_too_large(send, request_id)
                    return
                if chunk:
                    await anyio.to_thread.run_sync(spool.write, chunk)
                if not message.get("more_body", False):
                    break

            await anyio.to_thread.run_sync(spool.seek, 0)
            replayed = 0
            empty_body_sent = False

            async def replay_receive() -> Message:
                nonlocal replayed, empty_body_sent
                if replayed < body_size:
                    chunk = await anyio.to_thread.run_sync(
                        spool.read, min(_REPLAY_CHUNK_BYTES, body_size - replayed)
                    )
                    replayed += len(chunk)
                    return {
                        "type": "http.request",
                        "body": chunk,
                        "more_body": replayed < body_size,
                    }
                if body_size == 0 and not empty_body_sent:
                    empty_body_sent = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                return await receive()

            await self.app(scope, replay_receive, send)
        finally:
            await anyio.to_thread.run_sync(spool.close)

    @staticmethod
    def _is_reference_upload(scope: Scope) -> bool:
        return (
            scope.get("method", "").upper() == "POST"
            and scope.get("path", "").rstrip("/") == "/api/v1/reference-sets"
        )

    @staticmethod
    async def _send_too_large(send: Send, request_id: str) -> None:
        payload = json.dumps(
            {
                "code": "UPLOAD_TOO_LARGE",
                "message": "请求内容超过允许大小",
                "request_id": request_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                    (b"x-request-id", request_id.encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
