from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import uuid4

from sqlalchemy import Engine, and_, false, func, not_, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.action_models import Action
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.session import create_database_engine
from app.integrations.dify.factory import create_prompt_workflow_adapter
from app.integrations.generation.mock import MockGenerationPort
from app.integrations.generation_acceptance import HttpGenerationAcceptancePort
from app.integrations.storage.local import LocalPrivateStorage
from app.services.chatflow_generation_actions import (
    HttpGenerationStatusPort,
    UnavailableGenerationStatusPort,
    build_generation_recovery_registration,
)
from app.services.chatflow_generation_delivery_actions import (
    HttpImageGenerationDeliveryPort,
    UnavailableImageGenerationDeliveryPort,
    build_generation_delivery_registrations,
)
from app.services.chatflow_generation_expiry import (
    UnavailableGenerationAcceptancePort,
    build_generation_expiry_registrations,
)
from app.services.generation_runtime import (
    GENERATION_ACTION_KINDS,
    GenerationActionRuntime,
    handle_generation_start_event,
)
from app.services.generation_runtime import (
    GENERATION_START_EVENT_TYPE as GENERATION_RUNTIME_START_EVENT_TYPE,
)
from app.services.operations_heartbeat import OperationsHeartbeatWriter
from app.services.prompt_callbacks import PromptCallbackService
from app.services.prompt_workflows import (
    PROMPT_FOLLOWUP_RECOVER_ACTION_KIND,
    PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND,
    PROMPT_INITIAL_RECOVER_ACTION_KIND,
    PROMPT_INITIAL_SUBMIT_ACTION_KIND,
    PROMPT_MOCK_RESULT_QUERY_ACTION_KIND,
    build_prompt_action_registrations,
)
from app.services.tasks import TASK_PROMPT_EVENT_TYPE, task_prompt_handlers
from app.workers.action_processor import ACTION_EVENT_TYPE, ActionRegistration
from app.workers.event_allowlist import (
    DEFAULT_PUBLISH_ALLOWLIST,
    UNSUPPORTED_EVENT_REASON,
)
from app.workers.messages import ClaimedEvent
from app.workers.options import QueueOptions, queue_options_from_settings
from app.workers.processor import EventHandler, HandlerRegistration
from app.workers.publisher import CeleryTaskPublisher, UnroutableMessageError

LOGGER = logging.getLogger(__name__)

_PROMPT_ACTION_KINDS = frozenset(
    {
        PROMPT_INITIAL_SUBMIT_ACTION_KIND,
        PROMPT_INITIAL_RECOVER_ACTION_KIND,
        PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND,
        PROMPT_FOLLOWUP_RECOVER_ACTION_KIND,
        PROMPT_MOCK_RESULT_QUERY_ACTION_KIND,
    }
)


class EventPublisher(Protocol):
    def publish(self, event: ClaimedEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class DispatchReport:
    claimed: int = 0
    published: int = 0
    failed: int = 0
    dead: int = 0
    quarantined: int = 0


def _build_chatflow_generation_action_registrations(
    settings, private_storage: LocalPrivateStorage
) -> dict[str, ActionRegistration]:
    """Build the same H6 and H7 registrations the task workers can execute."""
    if (
        settings.image_generation_service_url is not None
        and settings.image_generation_service_token is not None
    ):
        status_port = HttpGenerationStatusPort(
            str(settings.image_generation_service_url),
            settings.image_generation_service_token.get_secret_value(),
        )
        delivery_port = HttpImageGenerationDeliveryPort(
            str(settings.image_generation_service_url),
            settings.image_generation_service_token.get_secret_value(),
        )
        acceptance_port = HttpGenerationAcceptancePort(
            str(settings.image_generation_service_url),
            settings.image_generation_service_token.get_secret_value(),
        )
    else:
        status_port = UnavailableGenerationStatusPort()
        delivery_port = UnavailableImageGenerationDeliveryPort()
        acceptance_port = UnavailableGenerationAcceptancePort()
    registrations = {
        "chatflow.generation.recover.v1": build_generation_recovery_registration(status_port)
    }
    registrations.update(build_generation_delivery_registrations(delivery_port, private_storage))
    registrations.update(build_generation_expiry_registrations(acceptance_port, status_port))
    return registrations


class OutboxDispatcher:
    """Claim due outbox rows briefly, publish outside PG, then compare claim token."""

    def __init__(
        self,
        engine: Engine,
        options: QueueOptions,
        *,
        publisher: EventPublisher | None = None,
        allowed_event_types: frozenset[str] | None = None,
        action_registrations: Mapping[str, ActionRegistration] | None = None,
        event_handlers: Mapping[str, HandlerRegistration | EventHandler] | None = None,
        generation_runtime_enabled: bool = False,
        tasks_api_enabled: bool = False,
        mock_prompt_result_query_required: bool = False,
    ) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        self._options = options
        self._publisher = publisher or CeleryTaskPublisher(options)
        resolved_action_registrations = action_registrations or {}
        if any(
            key != registration.kind for key, registration in resolved_action_registrations.items()
        ):
            raise ValueError("action registration keys must match their action kinds")
        self._event_handlers = dict(event_handlers or {})
        generation_runtime_ready = generation_runtime_enabled and _has_complete_generation_runtime(
            self._event_handlers, resolved_action_registrations
        )
        publish_action_registrations = dict(resolved_action_registrations)
        if not generation_runtime_ready:
            for kind in GENERATION_ACTION_KINDS:
                publish_action_registrations.pop(kind, None)
        prompt_runtime_ready = _has_complete_prompt_runtime(
            self._event_handlers,
            publish_action_registrations,
            mock_result_query_required=mock_prompt_result_query_required,
        )
        if tasks_api_enabled:
            # Public task creation is only safe when the full prompt-to-generation
            # pipeline is registered. If a downstream handler is absent, leave
            # task intent events in PostgreSQL for recovery instead of publishing.
            prompt_runtime_ready = prompt_runtime_ready and generation_runtime_ready
        if not prompt_runtime_ready:
            for kind in _PROMPT_ACTION_KINDS:
                publish_action_registrations.pop(kind, None)
        self._action_registrations = publish_action_registrations
        self._action_kinds = frozenset(publish_action_registrations)
        if allowed_event_types is None:
            resolved_event_types = DEFAULT_PUBLISH_ALLOWLIST | (
                {ACTION_EVENT_TYPE} if self._action_kinds else set()
            )
        else:
            resolved_event_types = frozenset(allowed_event_types)
        if prompt_runtime_ready:
            if allowed_event_types is None or TASK_PROMPT_EVENT_TYPE in resolved_event_types:
                resolved_event_types = resolved_event_types | {TASK_PROMPT_EVENT_TYPE}
        else:
            resolved_event_types = resolved_event_types - {TASK_PROMPT_EVENT_TYPE}
        if generation_runtime_ready:
            if (
                allowed_event_types is None
                or GENERATION_RUNTIME_START_EVENT_TYPE in resolved_event_types
            ):
                resolved_event_types = resolved_event_types | {GENERATION_RUNTIME_START_EVENT_TYPE}
        else:
            resolved_event_types = resolved_event_types - {GENERATION_RUNTIME_START_EVENT_TYPE}
        self._allowed_event_types = resolved_event_types
        if not self._action_kinds:
            self._allowed_event_types = self._allowed_event_types - {ACTION_EVENT_TYPE}

    def dispatch_once(self) -> DispatchReport:
        quarantined = self._quarantine_unregistered_events()
        claims = self._claim_due_events()
        published = failed = dead = 0
        for event in claims:
            try:
                self._publisher.publish(event)
            except UnroutableMessageError:
                is_dead = self._record_publish_failure(event, "BROKER_UNROUTABLE")
                failed += 1
                dead += int(is_dead)
            except Exception:
                # Never persist or log the broker exception text; it may contain
                # hostnames, virtual hosts, or credentials from the connection URL.
                is_dead = self._record_publish_failure(event, "BROKER_PUBLISH_FAILED")
                failed += 1
                dead += int(is_dead)
            else:
                published += int(self._mark_published(event))
        return DispatchReport(len(claims), published, failed, dead, quarantined)

    def operations_registration_snapshot(self) -> dict[str, object]:
        """Return counts/booleans only; never expose handler configuration payloads."""
        return {
            "event_handler_count": len(self._event_handlers),
            "action_registration_count": len(self._action_registrations),
            "prompt_runtime_registered": TASK_PROMPT_EVENT_TYPE in self._allowed_event_types,
            "generation_runtime_registered": (
                GENERATION_RUNTIME_START_EVENT_TYPE in self._allowed_event_types
            ),
        }

    def _quarantine_unregistered_events(self) -> int:
        """Keep unsupported intent durable and recoverable without publishing it."""
        quarantined = 0
        with self._sessions() as db, db.begin():
            now = func.clock_timestamp()
            db_now = db.scalar(select(func.clock_timestamp()))
            unsupported = not_(self._publishable_event_predicate())
            candidates = db.execute(
                select(OutboxEvent)
                .where(
                    unsupported,
                    or_(
                        and_(OutboxEvent.status == "pending", OutboxEvent.available_at <= now),
                        and_(OutboxEvent.status == "claimed", OutboxEvent.lease_until <= now),
                    ),
                )
                .order_by(OutboxEvent.available_at, OutboxEvent.created_at, OutboxEvent.id)
                .limit(self._options.claim_batch_size)
                .with_for_update(skip_locked=True)
            ).scalars()
            for event in candidates:
                # Preserve the established event -> receipt lock order. This is
                # a recoverable quarantine marker, never a permanent dead letter.
                receipt = _lock_or_create_receipt(db, event.id)
                if receipt.status == "completed":
                    event.status = "published"
                    event.published_at = event.published_at or func.clock_timestamp()
                    event.claim_token = None
                    event.lease_until = None
                    continue
                if receipt.status == "dead":
                    event.status = "dead"
                    event.claim_token = None
                    event.lease_until = None
                    continue
                if (
                    receipt.status == "processing"
                    and receipt.lease_until is not None
                    and receipt.lease_until > db_now
                ):
                    # An already delivered message still has a live consumer
                    # claim; do not steal its receipt lease.
                    continue
                newly_quarantined = (
                    event.status != "pending"
                    or event.last_error_code != UNSUPPORTED_EVENT_REASON
                    or receipt.status != "pending"
                    or receipt.last_error_code != UNSUPPORTED_EVENT_REASON
                )
                event.status = "pending"
                event.claim_token = None
                event.lease_until = None
                event.last_error_code = UNSUPPORTED_EVENT_REASON
                receipt.status = "pending"
                receipt.claim_token = None
                receipt.lease_until = None
                receipt.last_error_code = UNSUPPORTED_EVENT_REASON
                quarantined += int(newly_quarantined)
        return quarantined

    def _claim_due_events(self) -> list[ClaimedEvent]:
        if not self._allowed_event_types:
            return []
        claimed: list[ClaimedEvent] = []
        with self._sessions() as db, db.begin():
            now = func.clock_timestamp()
            candidates = db.execute(
                select(OutboxEvent)
                .where(
                    self._publishable_event_predicate(),
                    or_(
                        and_(OutboxEvent.status == "pending", OutboxEvent.available_at <= now),
                        and_(
                            OutboxEvent.status == "claimed",
                            OutboxEvent.lease_until <= now,
                        ),
                    ),
                )
                .order_by(OutboxEvent.available_at, OutboxEvent.created_at, OutboxEvent.id)
                .limit(self._options.claim_batch_size)
                .with_for_update(skip_locked=True)
            ).scalars()
            for event in candidates:
                receipt = _lock_or_create_receipt(db, event.id)
                if receipt.status == "completed":
                    event.status = "published"
                    event.published_at = func.clock_timestamp()
                    event.claim_token = None
                    event.lease_until = None
                    continue
                if receipt.status == "dead":
                    _mark_dispatch_dead(event, receipt, receipt.last_error_code or "EVENT_DEAD")
                    continue
                if receipt.status == "processing":
                    # A consumer has proved delivery. Leave its receipt lease to
                    # the scheduler and avoid dispatching a concurrent duplicate.
                    event.status = "published"
                    event.published_at = func.clock_timestamp()
                    event.claim_token = None
                    event.lease_until = None
                    continue
                if receipt.attempt_count > 0:
                    # A consumer already observed a prior publication; recovery
                    # is scheduled from this durable published timestamp.
                    event.status = "published"
                    event.published_at = func.clock_timestamp()
                    event.claim_token = None
                    event.lease_until = None
                    continue
                if event.attempt_count >= self._options.max_attempts:
                    _mark_dispatch_dead(event, receipt, "PUBLISH_MAX_ATTEMPTS")
                    continue
                token = uuid4()
                event.status = "claimed"
                event.claim_token = token
                event.lease_until = func.clock_timestamp() + timedelta(
                    seconds=self._options.lease_seconds
                )
                event.attempt_count += 1
                event.last_error_code = None
                claimed.append(
                    ClaimedEvent(
                        event_id=event.id,
                        event_type=event.event_type,
                        aggregate_id=event.aggregate_id,
                        operation_version=event.operation_version,
                        schema_version=event.schema_version,
                        payload=dict(event.payload),
                        claim_token=token,
                        attempt_count=event.attempt_count,
                    )
                )
        return claimed

    def _publishable_event_predicate(self):
        """Allow action wakes only for kinds registered in this worker configuration."""
        event_types = self._allowed_event_types - {ACTION_EVENT_TYPE}
        predicates = []
        if event_types:
            predicates.append(OutboxEvent.event_type.in_(event_types))
        if ACTION_EVENT_TYPE in self._allowed_event_types and self._action_kinds:
            registered_scopes = or_(
                *(
                    and_(
                        Action.action_kind == kind,
                        Action.scope_kind.in_(registration.scope_kinds),
                    )
                    for kind, registration in self._action_registrations.items()
                )
            )
            registered_action = (
                select(Action.id)
                .where(
                    Action.outbox_event_id == OutboxEvent.id,
                    registered_scopes,
                    or_(
                        Action.status.in_({"scheduled", "claimed"}),
                        and_(
                            Action.action_kind == "dify.chatflow.cancel.recover.v1",
                            Action.sequence.between(16, 18),
                            Action.status == "cancelled",
                        ),
                    ),
                )
                .exists()
            )
            predicates.append(and_(OutboxEvent.event_type == ACTION_EVENT_TYPE, registered_action))
        return or_(*predicates) if predicates else false()

    def _mark_published(self, event: ClaimedEvent) -> bool:
        with self._sessions() as db, db.begin():
            result = db.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.id == event.event_id,
                    OutboxEvent.status == "claimed",
                    OutboxEvent.claim_token == event.claim_token,
                )
                .values(
                    status="published",
                    published_at=func.clock_timestamp(),
                    claim_token=None,
                    lease_until=None,
                    last_error_code=None,
                )
            )
            return result.rowcount == 1

    def _record_publish_failure(self, event: ClaimedEvent, error_code: str) -> bool:
        with self._sessions() as db, db.begin():
            row = db.execute(
                select(OutboxEvent)
                .where(
                    OutboxEvent.id == event.event_id,
                    OutboxEvent.status == "claimed",
                    OutboxEvent.claim_token == event.claim_token,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                return False
            receipt = _lock_or_create_receipt(db, row.id)
            if receipt.status in {"completed", "processing"} or receipt.attempt_count > 0:
                # A lost confirm can follow a successful publish. Consumer state
                # is stronger evidence than the publisher's ambiguous exception.
                row.status = "published"
                row.published_at = func.clock_timestamp()
                row.claim_token = None
                row.lease_until = None
                return False
            is_dead = row.attempt_count >= self._options.max_attempts
            row.status = "dead" if is_dead else "pending"
            row.available_at = func.clock_timestamp() + timedelta(
                seconds=self._options.retry_delay_seconds(row.attempt_count)
            )
            row.claim_token = None
            row.lease_until = None
            row.last_error_code = error_code
            if is_dead:
                receipt.status = "dead"
                receipt.claim_token = None
                receipt.lease_until = None
                receipt.last_error_code = error_code
            return is_dead


def _lock_or_create_receipt(db: Session, event_id: object) -> OutboxReceipt:
    receipt = db.execute(
        select(OutboxReceipt).where(OutboxReceipt.event_id == event_id).with_for_update()
    ).scalar_one_or_none()
    if receipt is None:
        receipt = OutboxReceipt(event_id=event_id, status="pending", attempt_count=0)
        db.add(receipt)
        db.flush()
    return receipt


def _mark_dispatch_dead(event: OutboxEvent, receipt: OutboxReceipt, error_code: str) -> None:
    event.status = "dead"
    event.claim_token = None
    event.lease_until = None
    event.last_error_code = error_code
    receipt.status = "dead"
    receipt.claim_token = None
    receipt.lease_until = None
    receipt.last_error_code = error_code


def run_dispatcher() -> None:
    settings = get_settings()
    options = queue_options_from_settings(
        settings,
        queue_name=os.environ.get("IMAGE_BACKEND_QUEUE_NAME", "image-backend-events"),
    )
    engine = create_database_engine(settings)
    prompt_sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    prompt_port = create_prompt_workflow_adapter(
        settings.dify_mode, session_factory=prompt_sessions
    )
    generation_port = MockGenerationPort(prompt_sessions)
    private_storage = LocalPrivateStorage(settings.private_storage_root)
    generation_runtime = GenerationActionRuntime(generation_port, private_storage)
    event_handlers = task_prompt_handlers()
    event_handlers[GENERATION_RUNTIME_START_EVENT_TYPE] = HandlerRegistration(
        handle_generation_start_event, frozenset({1})
    )
    mock_result_callback_service = (
        PromptCallbackService(engine) if settings.dify_mode == "mock" else None
    )
    action_registrations = build_prompt_action_registrations(
        prompt_port,
        mock_result_callback_service=mock_result_callback_service,
    )
    if settings.chatflow_handoff_enabled:
        from app.integrations.dify.chatflow import StubDifyChatflowAdapter
        from app.integrations.dify.reference_files import DifyReferenceFileUploadClient
        from app.services.chatflow_actions import build_chatflow_action_registrations

        reference_upload_port = (
            DifyReferenceFileUploadClient(
                api_base_url=str(settings.dify_reference_upload_api_base_url),
                app_id=str(settings.dify_app_id),
                app_api_token=settings.dify_reference_upload_api_token.get_secret_value(),
            )
            if settings.dify_reference_upload_enabled
            and settings.dify_reference_upload_api_base_url is not None
            and settings.dify_reference_upload_api_token is not None
            and settings.dify_app_id is not None
            else None
        )

        action_registrations.update(
            build_chatflow_action_registrations(
                StubDifyChatflowAdapter(),
                reference_storage=private_storage,
                reference_upload_port=reference_upload_port,
            )
        )
        action_registrations.update(
            _build_chatflow_generation_action_registrations(settings, private_storage)
        )
    action_registrations.update(generation_runtime.registrations())
    dispatcher = OutboxDispatcher(
        engine,
        options,
        action_registrations=action_registrations,
        event_handlers=event_handlers,
        generation_runtime_enabled=settings.generation_runtime_enabled,
        tasks_api_enabled=settings.tasks_api_enabled,
        mock_prompt_result_query_required=settings.dify_mode == "mock",
    )
    heartbeat = OperationsHeartbeatWriter(engine, "dispatcher")
    try:
        while True:
            heartbeat.iteration_started(dispatcher.operations_registration_snapshot())
            try:
                report = dispatcher.dispatch_once()
                heartbeat.dispatcher_completed(report)
                if report.claimed or report.quarantined:
                    LOGGER.info(
                        "outbox batch processed: claimed=%s published=%s failed=%s dead=%s "
                        "quarantined=%s",
                        report.claimed,
                        report.published,
                        report.failed,
                        report.dead,
                        report.quarantined,
                    )
            except Exception:
                heartbeat.iteration_failed("DISPATCHER_ITERATION_FAILED")
                LOGGER.warning("outbox dispatcher iteration failed")
            time.sleep(options.poll_interval_seconds)
    finally:
        engine.dispose()


def _has_complete_prompt_runtime(
    event_handlers: Mapping[str, HandlerRegistration | EventHandler],
    action_registrations: Mapping[str, ActionRegistration],
    *,
    mock_result_query_required: bool = False,
) -> bool:
    if TASK_PROMPT_EVENT_TYPE not in event_handlers:
        return False
    required_actions = {
        PROMPT_INITIAL_SUBMIT_ACTION_KIND,
        PROMPT_INITIAL_RECOVER_ACTION_KIND,
        PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND,
        PROMPT_FOLLOWUP_RECOVER_ACTION_KIND,
    }
    if mock_result_query_required:
        required_actions.add(PROMPT_MOCK_RESULT_QUERY_ACTION_KIND)
    return all(
        kind in action_registrations and "submission" in action_registrations[kind].scope_kinds
        for kind in required_actions
    )


def _has_complete_generation_runtime(
    event_handlers: Mapping[str, HandlerRegistration | EventHandler],
    action_registrations: Mapping[str, ActionRegistration],
) -> bool:
    if GENERATION_RUNTIME_START_EVENT_TYPE not in event_handlers:
        return False
    return all(
        kind in action_registrations
        and action_registrations[kind].scope_kinds == frozenset({"attempt"})
        for kind in GENERATION_ACTION_KINDS
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_dispatcher()
