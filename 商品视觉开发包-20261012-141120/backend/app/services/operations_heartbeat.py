from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from uuid import UUID, uuid4

from sqlalchemy import Engine, case, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.operations_models import OperationsLoopHeartbeat

LOGGER = logging.getLogger(__name__)


class OperationsHeartbeatWriter:
    """Best-effort, payload-free heartbeat writes for dispatcher/scheduler loops."""

    def __init__(self, engine: Engine, component: str, *, instance_id: UUID | None = None) -> None:
        if component not in {"dispatcher", "scheduler"}:
            raise ValueError("unsupported operations loop component")
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        self._component = component
        self._instance_id = instance_id or uuid4()

    def iteration_started(self, registrations: Mapping[str, object] | None = None) -> None:
        self._write(
            {
                "last_heartbeat_at": func.clock_timestamp(),
                "last_iteration_started_at": func.clock_timestamp(),
                "registration_state": dict(registrations or {}),
            }
        )

    def dispatcher_completed(self, report: object) -> None:
        claimed = _count(report, "claimed")
        published = _count(report, "published")
        failed = _count(report, "failed")
        dead = _count(report, "dead")
        quarantined = _count(report, "quarantined")
        values: dict[str, object] = {
            "last_heartbeat_at": func.clock_timestamp(),
            "last_iteration_completed_at": func.clock_timestamp(),
            "last_successful_scan_at": func.clock_timestamp(),
            "last_outbox_scan_at": func.clock_timestamp(),
            "iteration_count": OperationsLoopHeartbeat.iteration_count + 1,
            "successful_iteration_count": OperationsLoopHeartbeat.successful_iteration_count + 1,
            "last_safe_error_code": "BROKER_PUBLISH_FAILED" if failed else None,
            "last_progress": {
                "claimed": claimed,
                "published": published,
                "failed": failed,
                "dead": dead,
                "quarantined": quarantined,
            },
        }
        if published:
            values["last_broker_confirmed_at"] = func.clock_timestamp()
        if failed:
            values["last_broker_error_at"] = func.clock_timestamp()
        self._write(
            values,
            first_write_counters={"iteration_count": 1, "successful_iteration_count": 1},
        )

    def scheduler_outbox_scan_completed(self, report: object) -> None:
        self._write(
            {
                "last_heartbeat_at": func.clock_timestamp(),
                "last_outbox_scan_at": func.clock_timestamp(),
                "last_progress": {
                    "outbox_scanned": _count(report, "scanned"),
                    "worker_leases_recovered": _count(report, "worker_leases_recovered"),
                    "published_events_redriven": _count(report, "published_events_redriven"),
                    "dead": _count(report, "dead"),
                    "action_leases_marked_unknown": _count(report, "action_leases_marked_unknown"),
                },
            }
        )

    def scheduler_completed(self, report: object) -> None:
        work_report = getattr(report, "work_report", None)
        cleanup_error = getattr(work_report, "safe_error_code", None)
        progress = {
            "cleanup_runs_scheduled": _count(report, "scheduled_runs"),
            "cleanup_jobs_scanned": _count(work_report, "scanned"),
            "cleanup_jobs_completed": _count(work_report, "completed"),
            "cleanup_jobs_retried": _count(work_report, "retried"),
            "cleanup_jobs_blocked": _count(work_report, "blocked"),
        }
        successful = cleanup_error is None
        self._write(
            {
                "last_heartbeat_at": func.clock_timestamp(),
                "last_iteration_completed_at": func.clock_timestamp(),
                "iteration_count": OperationsLoopHeartbeat.iteration_count + 1,
                "successful_iteration_count": (
                    OperationsLoopHeartbeat.successful_iteration_count + 1
                    if successful
                    else OperationsLoopHeartbeat.successful_iteration_count
                ),
                "failed_iteration_count": (
                    OperationsLoopHeartbeat.failed_iteration_count
                    if successful
                    else OperationsLoopHeartbeat.failed_iteration_count + 1
                ),
                "last_successful_scan_at": (
                    func.clock_timestamp()
                    if successful
                    else OperationsLoopHeartbeat.last_successful_scan_at
                ),
                "last_cleanup_scan_at": (
                    func.clock_timestamp()
                    if successful
                    else OperationsLoopHeartbeat.last_cleanup_scan_at
                ),
                "last_safe_error_code": cleanup_error,
                "last_progress": progress,
            },
            first_write_counters={
                "iteration_count": 1,
                "successful_iteration_count": 1 if successful else 0,
                "failed_iteration_count": 0 if successful else 1,
            },
        )

    def iteration_failed(self, safe_error_code: str) -> None:
        if not _is_safe_code(safe_error_code):
            safe_error_code = "LOOP_ITERATION_FAILED"
        self._write(
            {
                "last_heartbeat_at": func.clock_timestamp(),
                "last_iteration_completed_at": func.clock_timestamp(),
                "iteration_count": OperationsLoopHeartbeat.iteration_count + 1,
                "failed_iteration_count": OperationsLoopHeartbeat.failed_iteration_count + 1,
                "last_safe_error_code": safe_error_code,
            },
            first_write_counters={
                "iteration_count": 1,
                "failed_iteration_count": 1,
            },
        )

    def _write(
        self,
        values: dict[str, object],
        *,
        first_write_counters: dict[str, int] | None = None,
    ) -> None:
        statement = _build_upsert_statement(
            self._component,
            self._instance_id,
            values,
            first_write_counters=first_write_counters,
        )
        try:
            with self._sessions() as db, db.begin():
                db.execute(statement)
        except SQLAlchemyError as error:
            # Heartbeat storage must never stop business dispatch/recovery loops.
            original = getattr(error, "orig", None)
            sqlstate = getattr(original, "sqlstate", None)
            if not isinstance(sqlstate, str) or len(sqlstate) != 5 or not sqlstate.isalnum():
                sqlstate = "unknown"
            error_class = type(original).__name__ if original is not None else type(error).__name__
            primary = getattr(getattr(original, "diag", None), "message_primary", None)
            relation_match = (
                re.search(r'"([A-Za-z0-9_]+)"', primary)
                if sqlstate == "42P01" and isinstance(primary, str)
                else None
            )
            safe_relation = relation_match.group(1) if relation_match is not None else None
            LOGGER.warning(
                "operations heartbeat write failed: component=%s error_class=%s "
                "sqlstate=%s relation=%s",
                self._component,
                error_class,
                sqlstate,
                safe_relation or "unavailable",
            )


def _build_upsert_statement(
    component: str,
    instance_id: UUID,
    values: dict[str, object],
    *,
    first_write_counters: dict[str, int] | None = None,
):
    now = func.clock_timestamp()
    counter_columns = {
        "iteration_count",
        "successful_iteration_count",
        "failed_iteration_count",
    }
    insert_values = {
        "component": component,
        "instance_id": instance_id,
        "started_at": now,
        "last_heartbeat_at": now,
        "iteration_count": 0,
        "successful_iteration_count": 0,
        "failed_iteration_count": 0,
        "registration_state": {},
        "last_progress": {},
        **{key: value for key, value in values.items() if key not in counter_columns},
        **(first_write_counters or {}),
    }
    statement = insert(OperationsLoopHeartbeat).values(**insert_values)
    same_instance = OperationsLoopHeartbeat.instance_id == instance_id
    updates = dict(values)
    updates["instance_id"] = instance_id
    updates["started_at"] = case((same_instance, OperationsLoopHeartbeat.started_at), else_=now)
    return statement.on_conflict_do_update(
        index_elements=[OperationsLoopHeartbeat.component], set_=updates
    )


def _count(value: object, field: str) -> int:
    count = getattr(value, field, 0)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return 0
    return count


def _is_safe_code(value: str) -> bool:
    return (
        bool(value) and len(value) <= 80 and value[0].isalpha() and value.replace("_", "").isalnum()
    )
