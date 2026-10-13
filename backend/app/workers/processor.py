from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.outbox_models import OutboxEvent, OutboxReceipt, OutboxRejectedMessage
from app.workers.messages import OutboxMessage
from app.workers.options import QueueOptions

EventHandler = Callable[[Session, OutboxEvent, OutboxMessage], None]
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")


@dataclass(frozen=True, slots=True)
class HandlerRegistration:
    handler: EventHandler
    operation_versions: frozenset[int] = frozenset({1})

    def __post_init__(self) -> None:
        if not self.operation_versions or any(version < 1 for version in self.operation_versions):
            raise ValueError("handlers require positive supported operation versions")


@dataclass(frozen=True, slots=True)
class ProcessResult:
    status: str
    error_code: str | None = None


class RetryableEventFailure(Exception):
    def __init__(self, error_code: str = "HANDLER_RETRYABLE") -> None:
        if not _ERROR_CODE.fullmatch(error_code):
            raise ValueError("worker error code must be a safe identifier")
        self.error_code = error_code
        super().__init__(error_code)


class PermanentEventFailure(Exception):
    def __init__(self, error_code: str = "HANDLER_REJECTED") -> None:
        if not _ERROR_CODE.fullmatch(error_code):
            raise ValueError("worker error code must be a safe identifier")
        self.error_code = error_code
        super().__init__(error_code)


class WorkerStateUnavailable(RuntimeError):
    """Sanitized failure raised when PostgreSQL state cannot be read or committed."""


class EventProcessor:
    """Database-backed, at-least-once event processor with token-protected effects."""

    def __init__(
        self,
        engine: Engine,
        options: QueueOptions,
        handlers: Mapping[str, HandlerRegistration | EventHandler],
    ) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        self._options = options
        self._handlers = {
            event_type: value
            if isinstance(value, HandlerRegistration)
            else HandlerRegistration(value)
            for event_type, value in handlers.items()
        }

    def process_payload(self, payload: object) -> ProcessResult:
        try:
            message = OutboxMessage.model_validate(payload)
        except ValidationError:
            event_id = _extract_event_id(payload)
            try:
                self._record_invalid_message(event_id, payload)
            except SQLAlchemyError:
                raise WorkerStateUnavailable("worker state is temporarily unavailable") from None
            return ProcessResult("dead" if event_id else "malformed_recorded", "MESSAGE_INVALID")

        try:
            token, claim_result = self._claim(message)
        except SQLAlchemyError:
            raise WorkerStateUnavailable("worker state is temporarily unavailable") from None
        if token is None:
            return claim_result

        try:
            return self._execute(message, token)
        except PermanentEventFailure as exc:
            return self._record_failure(message.event_id, token, exc.error_code, permanent=True)
        except RetryableEventFailure as exc:
            return self._record_failure(message.event_id, token, exc.error_code, permanent=False)
        except SQLAlchemyError:
            # The handler transaction rolls back. A still-processing receipt is
            # recoverable by the persistent scheduler after its lease expires.
            raise WorkerStateUnavailable("worker state is temporarily unavailable") from None
        except Exception:
            # Store only a fixed code. Driver/handler exception text can contain
            # connection details, SQL, request data, or credentials.
            return self._record_failure(message.event_id, token, "HANDLER_FAILED", permanent=False)

    def _claim(self, message: OutboxMessage) -> tuple[UUID | None, ProcessResult]:
        with self._sessions() as db, db.begin():
            event = db.execute(
                select(OutboxEvent)
                .where(OutboxEvent.id == message.event_id)
                .with_for_update()
            ).scalar_one_or_none()
            if event is None:
                return None, ProcessResult("missing")

            receipt = _lock_or_create_receipt(db, event.id)
            if receipt.status == "completed":
                return None, ProcessResult("completed")
            if event.status == "dead" or receipt.status == "dead":
                return None, ProcessResult("dead", receipt.last_error_code or event.last_error_code)
            error_code = self._message_error(event, receipt, message)
            if error_code is not None:
                _mark_dead(event, receipt, error_code)
                return None, ProcessResult("dead", error_code)
            now = db.scalar(select(func.clock_timestamp()))
            if receipt.status == "processing" and receipt.lease_until is not None:
                if receipt.lease_until > now:
                    return None, ProcessResult("already_processing")
            elif receipt.available_at > now:
                return None, ProcessResult("not_due")
            if receipt.attempt_count >= self._options.max_attempts:
                _mark_dead(event, receipt, "MAX_ATTEMPTS_EXCEEDED")
                return None, ProcessResult("dead", "MAX_ATTEMPTS_EXCEEDED")

            token = uuid4()
            receipt.status = "processing"
            receipt.claim_token = token
            receipt.lease_until = func.clock_timestamp() + timedelta(
                seconds=self._options.lease_seconds
            )
            receipt.attempt_count += 1
            receipt.last_error_code = None
            return token, ProcessResult("claimed")

    def _message_error(
        self, event: OutboxEvent, receipt: OutboxReceipt, message: OutboxMessage
    ) -> str | None:
        if event.status == "dead" or receipt.status == "dead":
            return None
        if (
            event.aggregate_id != message.aggregate_id
            or event.operation_version != message.operation_version
            or event.schema_version != message.schema_version
        ):
            return "MESSAGE_EVENT_MISMATCH"
        if message.schema_version != self._options.schema_version:
            return "MESSAGE_SCHEMA_UNSUPPORTED"
        try:
            stored_payload = {key: UUID(value) for key, value in event.payload.items()}
        except (TypeError, ValueError):
            return "EVENT_PAYLOAD_INVALID"
        if stored_payload != message.payload:
            return "MESSAGE_EVENT_MISMATCH"
        registration = self._handlers.get(event.event_type)
        if registration is None:
            return "EVENT_TYPE_UNSUPPORTED"
        if message.operation_version not in registration.operation_versions:
            return "OPERATION_VERSION_UNSUPPORTED"
        return None

    def _execute(self, message: OutboxMessage, token: UUID) -> ProcessResult:
        with self._sessions() as db, db.begin():
            event = db.execute(
                select(OutboxEvent)
                .where(OutboxEvent.id == message.event_id)
                .with_for_update()
            ).scalar_one_or_none()
            if event is None:
                return ProcessResult("missing")
            receipt = db.execute(
                select(OutboxReceipt)
                .where(OutboxReceipt.event_id == event.id)
                .with_for_update()
            ).scalar_one_or_none()
            if (
                receipt is None
                or receipt.status != "processing"
                or receipt.claim_token != token
            ):
                return ProcessResult("claim_lost")
            error_code = self._message_error(event, receipt, message)
            if error_code is not None:
                _mark_dead(event, receipt, error_code)
                return ProcessResult("dead", error_code)
            registration = self._handlers[event.event_type]
            registration.handler(db, event, message)
            receipt.status = "completed"
            receipt.completed_at = func.clock_timestamp()
            receipt.claim_token = None
            receipt.lease_until = None
            receipt.last_error_code = None
            return ProcessResult("completed")

    def _record_failure(
        self, event_id: UUID, token: UUID, error_code: str, *, permanent: bool
    ) -> ProcessResult:
        try:
            with self._sessions() as db, db.begin():
                event = db.execute(
                    select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
                ).scalar_one_or_none()
                if event is None:
                    return ProcessResult("missing")
                receipt = db.execute(
                    select(OutboxReceipt)
                    .where(OutboxReceipt.event_id == event_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if (
                    receipt is None
                    or receipt.status != "processing"
                    or receipt.claim_token != token
                ):
                    return ProcessResult("claim_lost")
                should_die = permanent or receipt.attempt_count >= self._options.max_attempts
                receipt.claim_token = None
                receipt.lease_until = None
                receipt.last_error_code = error_code
                if should_die:
                    _mark_dead(event, receipt, error_code)
                    return ProcessResult("dead", error_code)
                receipt.status = "pending"
                receipt.available_at = func.clock_timestamp() + timedelta(
                    seconds=self._options.retry_delay_seconds(receipt.attempt_count)
                )
                return ProcessResult("retry_scheduled", error_code)
        except SQLAlchemyError:
            raise WorkerStateUnavailable("worker state is temporarily unavailable") from None

    def _record_invalid_message(self, event_id: UUID | None, payload: object) -> None:
        with self._sessions() as db, db.begin():
            event = None
            if event_id is not None:
                event = db.execute(
                    select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
                ).scalar_one_or_none()
            if event is not None:
                receipt = _lock_or_create_receipt(db, event.id)
                if receipt.status != "completed":
                    _mark_dead(event, receipt, "MESSAGE_INVALID")
            db.execute(
                pg_insert(OutboxRejectedMessage)
                .values(
                    event_id=event.id if event is not None else None,
                    message_fingerprint=_message_shape_fingerprint(payload, event_id=event_id),
                    error_code="MESSAGE_INVALID",
                    occurrence_count=1,
                )
                .on_conflict_do_update(
                    index_elements=[OutboxRejectedMessage.message_fingerprint],
                    set_={
                        "occurrence_count": OutboxRejectedMessage.occurrence_count + 1,
                        "received_at": func.clock_timestamp(),
                    },
                )
            )


def _extract_event_id(payload: object) -> UUID | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("event_id")
    if not isinstance(value, (UUID, str)):
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _message_shape_fingerprint(payload: object, *, event_id: UUID | None = None) -> bytes:
    def shape(value: object, depth: int = 0) -> object:
        if depth >= 8:
            return {"kind": type(value).__name__}
        if isinstance(value, dict):
            children = [
                json.dumps(shape(child, depth + 1), sort_keys=True, separators=(",", ":"))
                for child in value.values()
            ]
            return {"kind": "object", "size": len(children), "values": sorted(children)[:100]}
        if isinstance(value, (list, tuple)):
            children = [
                json.dumps(shape(child, depth + 1), sort_keys=True, separators=(",", ":"))
                for child in value[:100]
            ]
            return {"kind": "array", "size": len(value), "items": children}
        if isinstance(value, str):
            return {"kind": "string", "length": len(value)}
        if isinstance(value, bool):
            return {"kind": "boolean"}
        if isinstance(value, int | float):
            return {"kind": "number"}
        if value is None:
            return {"kind": "null"}
        return {"kind": type(value).__name__}

    canonical_shape = json.dumps(
        {"event_id": str(event_id) if event_id is not None else None, "shape": shape(payload)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_shape.encode("utf-8")).digest()


def _lock_or_create_receipt(db: Session, event_id: UUID) -> OutboxReceipt:
    receipt = db.execute(
        select(OutboxReceipt).where(OutboxReceipt.event_id == event_id).with_for_update()
    ).scalar_one_or_none()
    if receipt is None:
        receipt = OutboxReceipt(event_id=event_id, status="pending", attempt_count=0)
        db.add(receipt)
        db.flush()
    return receipt


def _mark_dead(event: OutboxEvent, receipt: OutboxReceipt, error_code: str) -> None:
    event.status = "dead"
    event.claim_token = None
    event.lease_until = None
    event.last_error_code = error_code
    receipt.status = "dead"
    receipt.claim_token = None
    receipt.lease_until = None
    receipt.last_error_code = error_code
