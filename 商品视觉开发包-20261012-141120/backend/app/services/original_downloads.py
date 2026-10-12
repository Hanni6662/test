"""Bounded, integrity-checked original image downloads."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from time import monotonic
from typing import BinaryIO
from uuid import UUID

import anyio
from starlette.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

from app.core.domain_errors import DomainError
from app.services.generation_image_saving import MAX_GENERATION_IMAGE_BYTES

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 128 * 1024
_MIME_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}\Z")
_STREAM_END = object()
_STREAM_FAILURE_CODES = frozenset({
    "DOWNLOAD_BUSY", "ORIGINAL_INTEGRITY", "ORIGINAL_UNAVAILABLE", "CLIENT_DISCONNECT",
    "RESPONSE_START_FAILED", "STREAM_FAILED", "STREAM_SEND_FAILED", "STREAM_CANCELLED",
})


def _busy_error() -> DomainError:
    return DomainError(503, "DOWNLOAD_BUSY", "原图下载暂不可用")


def _integrity_error() -> DomainError:
    return DomainError(503, "ORIGINAL_INTEGRITY", "原图完整性校验失败")


def _unavailable_error() -> DomainError:
    return DomainError(503, "ORIGINAL_UNAVAILABLE", "原图暂时无法读取")


class DownloadGate:
    """An immediate, per-process concurrency gate with a fixed lease deadline."""

    def __init__(self, max_concurrent: int, timeout_seconds: int) -> None:
        if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int):
            raise ValueError("max_concurrent must be a positive integer")
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be a positive integer")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._timeout_seconds = timeout_seconds

    def try_acquire(self) -> DownloadLease | None:
        started_at = monotonic()
        if not self._slots.acquire(blocking=False):
            return None
        return DownloadLease(
            deadline=started_at + self._timeout_seconds,
            release_slot=self._slots.release,
        )


class DownloadLease:
    """One gate slot whose deadline begins when the slot is acquired."""

    def __init__(self, *, deadline: float, release_slot: Callable[[], None]) -> None:
        self._deadline = deadline
        self._release_slot = release_slot
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        callback = None
        with self._lock:
            if not self._released:
                self._released = True
                callback = self._release_slot
        if callback is not None:
            callback()

    def check_deadline(self) -> None:
        self.remaining_seconds()

    def remaining_seconds(self) -> float:
        """Return this lease's remaining budget without resetting its deadline."""
        with self._lock:
            remaining = self._deadline - monotonic()
            if self._released or remaining <= 0:
                raise _busy_error()
            return remaining


class OriginalDownload:
    """A validated reader and the lease that remains held until close."""

    def __init__(
        self,
        *,
        reader: BinaryIO,
        content_type: str,
        size_bytes: int,
        filename: str,
        lease: DownloadLease,
    ) -> None:
        self.reader = reader
        self.content_type = content_type
        self.size_bytes = size_bytes
        self.filename = filename
        self.lease = lease
        self._close_lock = threading.Lock()
        self._reader_lock = threading.Lock()
        self._closed = False

    def iter_chunks(self, chunk_size: int = _CHUNK_SIZE) -> Iterator[bytes]:
        """Yield bounded bytes and reject a changed, truncated, or extended reader."""

        if (
            isinstance(chunk_size, bool)
            or not isinstance(chunk_size, int)
            or chunk_size <= 0
            or chunk_size > _CHUNK_SIZE
        ):
            raise ValueError("chunk_size must be a positive integer")

        sent_bytes = 0
        while sent_bytes < self.size_bytes:
            self.lease.check_deadline()
            requested = min(chunk_size, self.size_bytes - sent_bytes)
            try:
                chunk = self._read(requested)
            except OSError:
                raise _unavailable_error() from None
            except Exception:
                raise _unavailable_error() from None

            if not isinstance(chunk, bytes) or len(chunk) > requested:
                raise _integrity_error()
            if not chunk:
                raise _integrity_error()

            next_size = sent_bytes + len(chunk)
            if next_size == self.size_bytes:
                self.lease.check_deadline()
                try:
                    trailing = self._read(1)
                except OSError:
                    raise _unavailable_error() from None
                except Exception:
                    raise _unavailable_error() from None
                if not isinstance(trailing, bytes):
                    raise _integrity_error()
                if trailing:
                    raise _integrity_error()

            sent_bytes = next_size
            yield chunk

        # Sending the final chunk can itself block on a slow client.
        self.lease.check_deadline()

    def _read(self, size: int) -> bytes:
        with self._reader_lock:
            return self.reader.read(size)

    def close(self) -> None:
        """Close the reader and release the gate exactly once."""

        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            with self._reader_lock:
                self.reader.close()
        finally:
            self.lease.release()


def prepare_original_download(
    *,
    reader: BinaryIO,
    mime_type: str,
    expected_size: int,
    expected_sha256: str,
    task_id: UUID,
    image_id: UUID,
    lease: DownloadLease,
) -> OriginalDownload:
    """Hash a bounded reader, rewind that same object, and transfer its ownership."""

    try:
        lease.check_deadline()
        if (
            mime_type not in _MIME_EXTENSIONS
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size <= 0
            or expected_size > MAX_GENERATION_IMAGE_BYTES
            or not isinstance(expected_sha256, str)
            or _SHA256_HEX.fullmatch(expected_sha256) is None
            or not isinstance(task_id, UUID)
            or not isinstance(image_id, UUID)
        ):
            raise _integrity_error()
        if any(
            not callable(getattr(reader, method_name, None))
            for method_name in ("read", "seek", "tell", "close")
        ):
            raise _integrity_error()

        digest = hashlib.sha256()
        size_bytes = 0
        while True:
            lease.check_deadline()
            remaining_through_limit = MAX_GENERATION_IMAGE_BYTES + 1 - size_bytes
            if remaining_through_limit <= 0:
                raise _integrity_error()
            try:
                chunk = reader.read(min(_CHUNK_SIZE, remaining_through_limit))
            except OSError:
                raise _unavailable_error() from None
            except Exception:
                raise _unavailable_error() from None

            if not isinstance(chunk, bytes) or len(chunk) > min(
                _CHUNK_SIZE, remaining_through_limit
            ):
                raise _integrity_error()
            if not chunk:
                break
            size_bytes += len(chunk)
            if size_bytes > MAX_GENERATION_IMAGE_BYTES:
                raise _integrity_error()
            digest.update(chunk)

        if size_bytes == 0 or size_bytes != expected_size:
            raise _integrity_error()
        if not hmac.compare_digest(digest.hexdigest(), expected_sha256.lower()):
            raise _integrity_error()

        lease.check_deadline()
        try:
            seek_result = reader.seek(0)
            position = reader.tell()
        except Exception:
            raise _integrity_error() from None
        if seek_result != 0 or position != 0:
            raise _integrity_error()
        lease.check_deadline()

        extension = _MIME_EXTENSIONS[mime_type]
        filename = f"task_{task_id}_image_{image_id}.{extension}"
        return OriginalDownload(
            reader=reader,
            content_type=mime_type,
            size_bytes=size_bytes,
            filename=filename,
            lease=lease,
        )
    except DomainError:
        _close_reader_and_release(reader, lease)
        raise
    except Exception:
        _close_reader_and_release(reader, lease)
        raise _integrity_error() from None
    except BaseException:
        _close_reader_and_release(reader, lease)
        raise


def _close_reader_and_release(reader: BinaryIO, lease: DownloadLease) -> None:
    try:
        reader.close()
    except Exception:
        pass
    finally:
        lease.release()


@dataclass
class _StreamProgress:
    start_attempted: bool = False
    sent_bytes: int = 0


class OriginalDownloadResponse(StreamingResponse):
    """ASGI response that owns download cleanup across every send outcome."""

    def __init__(self, download: OriginalDownload, request_id: str) -> None:
        try:
            if (
                not isinstance(request_id, str)
                or not request_id.isascii()
                or any(ord(character) < 32 or ord(character) == 127 for character in request_id)
            ):
                raise ValueError("request_id must be safe ASCII text")
            download.lease.check_deadline()
            headers = {
                "Content-Disposition": f'attachment; filename="{download.filename}"',
                "Content-Length": str(download.size_bytes),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Request-ID": request_id,
                "Accept-Ranges": "none",
            }
            super().__init__(
                iter(()),
                status_code=200,
                headers=headers,
                media_type=download.content_type,
            )
        except BaseException:
            try:
                download.close()
            except Exception:
                pass
            raise
        self.download = download
        self.request_id = request_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._close_download()

    async def _close_download(self) -> None:
        # A read running in AnyIO's worker thread must finish before close/release.
        with anyio.CancelScope(shield=True):
            try:
                await anyio.to_thread.run_sync(self.download.close, abandon_on_cancel=False)
            except Exception:
                logger.info(
                    "original_download_stream request_id=%s outcome=close_failed",
                    self.request_id,
                )

    async def stream_response(self, send: Send) -> None:
        progress = _StreamProgress()
        try:
            remaining = self.download.lease.remaining_seconds()
            # Covers response start, every body send, and the final flush. Reader
            # threads are not abandoned: cleanup waits if a synchronous read is pending.
            with anyio.fail_after(remaining):
                await self._stream_original(send, progress)
        except (DomainError, TimeoutError):
            self._log_failure("DOWNLOAD_BUSY", progress.sent_bytes)
            if not progress.start_attempted:
                await self._send_busy(send)
        except anyio.get_cancelled_exc_class():
            self._log_failure("STREAM_CANCELLED", progress.sent_bytes)
            raise

    async def _send_busy(self, send: Send) -> None:
        # No successful start was attempted. Release image resources before an
        # independently bounded error response; its budget never extends the image lease.
        await self._close_download()
        response = JSONResponse(
            status_code=503,
            content={
                "code": "DOWNLOAD_BUSY",
                "message": "原图下载暂不可用",
                "request_id": self.request_id,
            },
            headers={
                "Cache-Control": "no-store",
                "Retry-After": "5",
                "X-Request-ID": self.request_id,
            },
        )
        try:
            with anyio.move_on_after(1):
                await send({"type": "http.response.start", "status": 503,
                            "headers": response.raw_headers})
                await send({"type": "http.response.body", "body": response.body,
                            "more_body": False})
        except OSError:
            self._log_failure("CLIENT_DISCONNECT", 0)
        except Exception:
            self._log_failure("STREAM_SEND_FAILED", 0)

    async def _stream_original(self, send: Send, progress: _StreamProgress) -> None:
        self.download.lease.check_deadline()
        # A stalled start may already be visible to the transport. Never send a
        # second status/header block once this attempt begins.
        progress.start_attempted = True
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": self.status_code,
                    "headers": self.raw_headers,
                }
            )
        except OSError:
            self._log_failure("CLIENT_DISCONNECT", 0)
            return
        except Exception:
            self._log_failure("RESPONSE_START_FAILED", 0)
            return

        sent_bytes = 0
        iterator = self.download.iter_chunks()
        while True:
            try:
                chunk = await anyio.to_thread.run_sync(
                    _next_or_end,
                    iterator,
                    abandon_on_cancel=False,
                )
            except DomainError as exc:
                self._log_failure(exc.code, sent_bytes)
                return
            except OSError:
                self._log_failure("ORIGINAL_UNAVAILABLE", sent_bytes)
                return
            except Exception:
                self._log_failure("STREAM_FAILED", sent_bytes)
                return

            if chunk is _STREAM_END:
                if sent_bytes != self.download.size_bytes:
                    self._log_failure("ORIGINAL_INTEGRITY", sent_bytes)
                    return
                try:
                    self.download.lease.check_deadline()
                    await send(
                        {
                            "type": "http.response.body",
                            "body": b"",
                            "more_body": False,
                        }
                    )
                except DomainError as exc:
                    self._log_failure(exc.code, sent_bytes)
                    return
                except OSError:
                    self._log_failure("CLIENT_DISCONNECT", sent_bytes)
                    return
                except Exception:
                    self._log_failure("STREAM_SEND_FAILED", sent_bytes)
                    return
                logger.info(
                    "original_download_stream request_id=%s outcome=completed bytes=%d",
                    self.request_id,
                    sent_bytes,
                )
                return

            if not isinstance(chunk, bytes) or not chunk:
                self._log_failure("ORIGINAL_INTEGRITY", sent_bytes)
                return
            if sent_bytes + len(chunk) > self.download.size_bytes:
                self._log_failure("ORIGINAL_INTEGRITY", sent_bytes)
                return
            try:
                self.download.lease.check_deadline()
                await send(
                    {
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": True,
                    }
                )
            except DomainError as exc:
                self._log_failure(exc.code, sent_bytes)
                return
            except OSError:
                self._log_failure("CLIENT_DISCONNECT", sent_bytes)
                return
            except Exception:
                self._log_failure("STREAM_SEND_FAILED", sent_bytes)
                return
            sent_bytes += len(chunk)
            progress.sent_bytes = sent_bytes

    def _log_failure(self, reason: str, sent_bytes: int) -> None:
        safe_reason = reason if reason in _STREAM_FAILURE_CODES else "STREAM_FAILED"
        logger.info(
            "original_download_stream request_id=%s outcome=interrupted reason=%s bytes=%d",
            self.request_id,
            safe_reason,
            sent_bytes,
        )


def _next_or_end(iterator: Iterator[bytes]) -> bytes | object:
    try:
        return next(iterator)
    except StopIteration:
        return _STREAM_END
