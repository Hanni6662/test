from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest
from app.scheduler import CleanupScheduler


class _FakeCleanupService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def schedule_due_runs(self, *, now: datetime, timezone: ZoneInfo, daily_time: time) -> int:
        self.calls.append(("schedule", (now, timezone, daily_time)))
        return 2

    def process_due_jobs(self, *, batch_size: int) -> object:
        self.calls.append(("process", batch_size))
        return {"completed": 1}


def test_cleanup_scheduler_schedules_then_processes_with_effective_config() -> None:
    service = _FakeCleanupService()
    timezone = ZoneInfo("Asia/Shanghai")
    daily_time = time(3, 0)
    scheduler = CleanupScheduler(
        service,
        timezone,
        daily_time,
        poll_interval_seconds=10,
        batch_size=7,
    )
    now = datetime(2026, 10, 7, 19, 0, tzinfo=UTC)

    report = scheduler.scan_once(now=now)

    assert report.scheduled_runs == 2
    assert report.work_report == {"completed": 1}
    assert service.calls == [
        ("schedule", (now, timezone, daily_time)),
        ("process", 7),
    ]


def test_cleanup_scheduler_rejects_naive_now() -> None:
    scheduler = CleanupScheduler(_FakeCleanupService(), ZoneInfo("Asia/Shanghai"), time(3, 0), 10)

    with pytest.raises(ValueError, match="timezone-aware"):
        scheduler.scan_once(now=datetime(2026, 10, 7, 3, 0))
