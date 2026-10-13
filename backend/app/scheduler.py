from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as DailyTime
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, and_, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.session import create_database_engine
from app.services.operations_heartbeat import OperationsHeartbeatWriter
from app.workers.action_processor import mark_expired_action_unknown_in_transaction
from app.workers.options import QueueOptions, queue_options_from_settings

LOGGER = logging.getLogger(__name__)


class _RetentionCleanupPort(Protocol):
    def schedule_due_runs(
        self, *, now: datetime, timezone: ZoneInfo, daily_time: DailyTime
    ) -> int: ...

    def process_due_jobs(self, *, batch_size: int) -> object: ...


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    scanned: int = 0
    worker_leases_recovered: int = 0
    published_events_redriven: int = 0
    dead: int = 0
    action_leases_marked_unknown: int = 0


@dataclass(frozen=True, slots=True)
class CleanupSchedulerReport:
    scheduled_runs: int
    work_report: object


class CleanupScheduler:
    """Drive durable cleanup scheduling and jobs from any number of replicas."""

    def __init__(
        self,
        service: _RetentionCleanupPort,
        timezone: ZoneInfo,
        daily_time: DailyTime,
        poll_interval_seconds: int,
        batch_size: int = 25,
    ) -> None:
        if poll_interval_seconds < 1 or batch_size < 1:
            raise ValueError("cleanup scheduler intervals and batch size must be positive")
        self._service = service
        self._timezone = timezone
        self._daily_time = daily_time
        self.poll_interval_seconds = poll_interval_seconds
        self._batch_size = batch_size

    def scan_once(self, *, now: datetime | None = None) -> CleanupSchedulerReport:
        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("cleanup scheduler time must be timezone-aware")
        scheduled = self._service.schedule_due_runs(
            now=current_time,
            timezone=self._timezone,
            daily_time=self._daily_time,
        )
        work = self._service.process_due_jobs(batch_size=self._batch_size)
        return CleanupSchedulerReport(scheduled_runs=scheduled, work_report=work)


class OutboxScheduler:
    """Persistently recover due consumer receipts and lost worker deliveries."""

    def __init__(self, engine: Engine, options: QueueOptions) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        self._options = options

    def scan_once(self) -> RecoveryReport:
        recovered = redriven = dead = scanned = action_unknown = 0
        with self._sessions() as db, db.begin():
            now = func.clock_timestamp()
            oldest_published = now - timedelta(seconds=self._options.republish_after_seconds)
            event_ids = (
                db.execute(
                    select(OutboxEvent.id)
                    .join(OutboxReceipt, OutboxReceipt.event_id == OutboxEvent.id)
                    .where(
                        or_(
                            and_(
                                OutboxReceipt.status == "processing",
                                OutboxReceipt.lease_until <= now,
                            ),
                            and_(
                                OutboxEvent.status == "published",
                                OutboxReceipt.status == "pending",
                                OutboxReceipt.available_at <= now,
                                OutboxEvent.published_at <= oldest_published,
                            ),
                        )
                    )
                    .order_by(OutboxReceipt.available_at, OutboxEvent.id)
                    .limit(self._options.claim_batch_size)
                    .with_for_update(skip_locked=True, of=OutboxEvent)
                )
                .scalars()
                .all()
            )
            for event_id in event_ids:
                scanned += 1
                # Keep the global row-lock order consistent with the dispatcher
                # and consumer: event first, receipt second.
                event = db.execute(
                    select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
                ).scalar_one_or_none()
                if event is None:
                    continue
                receipt = db.execute(
                    select(OutboxReceipt)
                    .where(OutboxReceipt.event_id == event_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if receipt is None or receipt.status in {"completed", "dead"}:
                    continue
                db_now = db.scalar(select(func.clock_timestamp()))
                if receipt.status == "processing":
                    if receipt.lease_until is None or receipt.lease_until > db_now:
                        continue
                    if receipt.attempt_count >= self._options.max_attempts:
                        if mark_expired_action_unknown_in_transaction(
                            db,
                            event,
                            receipt,
                            now=db_now,
                            reason_code="ACTION_RECOVERY_LIMIT",
                        ):
                            action_unknown += 1
                            continue
                        _mark_dead(event, receipt, "WORKER_MAX_ATTEMPTS")
                        dead += 1
                        continue
                    next_at = func.clock_timestamp() + timedelta(
                        seconds=self._options.retry_delay_seconds(receipt.attempt_count)
                    )
                    receipt.status = "pending"
                    receipt.claim_token = None
                    receipt.lease_until = None
                    receipt.available_at = next_at
                    receipt.last_error_code = receipt.last_error_code or "WORKER_LEASE_EXPIRED"
                    event.status = "pending"
                    event.available_at = next_at
                    event.claim_token = None
                    event.lease_until = None
                    event.last_error_code = "WORKER_LEASE_EXPIRED"
                    recovered += 1
                    continue
                if (
                    event.status == "published"
                    and receipt.status == "pending"
                    and receipt.available_at <= db_now
                    and event.published_at is not None
                    and event.published_at
                    <= db_now - timedelta(seconds=self._options.republish_after_seconds)
                ):
                    event.status = "pending"
                    event.available_at = func.clock_timestamp()
                    event.claim_token = None
                    event.lease_until = None
                    redriven += 1
        return RecoveryReport(scanned, recovered, redriven, dead, action_unknown)


def run_scheduler() -> None:
    settings = get_settings()
    options = queue_options_from_settings(
        settings,
        queue_name=os.environ.get("IMAGE_BACKEND_QUEUE_NAME", "image-backend-events"),
    )
    engine = create_database_engine(settings)
    scheduler = OutboxScheduler(engine, options)
    from app.services.chatflow_generation_closure import GenerationClosureScheduler

    closure_scheduler = (
        GenerationClosureScheduler(engine, app_id=settings.dify_app_id)
        if settings.chatflow_handoff_enabled
        and settings.chatflow_dify_mode == "http"
        and settings.chatflow_flow_version == "visual-v3.4-single-selection"
        else None
    )
    from app.integrations.storage.local import LocalPrivateStorage
    from app.services.retention_cleanup import RetentionCleanupService

    cleanup_service = RetentionCleanupService(
        engine,
        LocalPrivateStorage(settings.private_storage_root),
    )
    cleanup_scheduler = CleanupScheduler(
        cleanup_service,
        ZoneInfo(settings.cleanup_schedule_timezone),
        settings.cleanup_schedule_daily_time,
        options.poll_interval_seconds,
        batch_size=settings.cleanup_batch_size,
    )
    heartbeat = OperationsHeartbeatWriter(engine, "scheduler")
    try:
        while True:
            heartbeat.iteration_started()
            try:
                report = scheduler.scan_once()
                if closure_scheduler is not None:
                    closure_scheduler.scan_once(limit=options.claim_batch_size)
                heartbeat.scheduler_outbox_scan_completed(report)
                if (
                    report.worker_leases_recovered
                    or report.published_events_redriven
                    or report.dead
                    or report.action_leases_marked_unknown
                ):
                    LOGGER.info(
                        "queue recovery completed: recovered=%s redriven=%s dead=%s "
                        "action_unknown=%s",
                        report.worker_leases_recovered,
                        report.published_events_redriven,
                        report.dead,
                        report.action_leases_marked_unknown,
                    )
                cleanup_report = cleanup_scheduler.scan_once()
                heartbeat.scheduler_completed(cleanup_report)
                if cleanup_report.scheduled_runs:
                    LOGGER.info(
                        "retention cleanup runs scheduled: count=%s",
                        cleanup_report.scheduled_runs,
                    )
                if getattr(cleanup_report.work_report, "safe_error_code", None):
                    LOGGER.warning(
                        "retention cleanup scheduler iteration failed: code=%s",
                        cleanup_report.work_report.safe_error_code,
                    )
            except Exception:
                heartbeat.iteration_failed("SCHEDULER_ITERATION_FAILED")
                LOGGER.warning("scheduler iteration failed")
            time.sleep(cleanup_scheduler.poll_interval_seconds)
    finally:
        engine.dispose()


def _mark_dead(event: OutboxEvent, receipt: OutboxReceipt, error_code: str) -> None:
    event.status = "dead"
    event.claim_token = None
    event.lease_until = None
    event.last_error_code = error_code
    receipt.status = "dead"
    receipt.claim_token = None
    receipt.lease_until = None
    receipt.last_error_code = error_code


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_scheduler()
