from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from app.db.retention_models import CleanupRun, RetentionPolicy
from app.integrations.storage.local import LocalPrivateStorage
from app.services.retention_cleanup import RetentionCleanupService
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session


def _isolated_scheduler_engines(
    *, migration_url: str, runtime_url: str
) -> tuple[Engine, Engine, str]:
    schema = f"r7_scheduler_{uuid4().hex[:16]}"
    migration_engine = create_engine(
        migration_url,
        pool_pre_ping=True,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    runtime_engine = create_engine(
        runtime_url,
        pool_pre_ping=True,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    role_name = make_url(runtime_url).username
    if role_name is None:
        migration_engine.dispose()
        runtime_engine.dispose()
        raise ValueError("runtime test URL must identify a database role")
    quote_identifier = migration_engine.dialect.identifier_preparer.quote_identifier
    quoted_schema = quote_identifier(schema)
    quoted_role = quote_identifier(role_name)
    try:
        with migration_engine.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA {quoted_schema}"))
        RetentionPolicy.__table__.create(migration_engine)
        CleanupRun.__table__.create(migration_engine)
        with migration_engine.begin() as connection:
            connection.execute(text(f"GRANT USAGE ON SCHEMA {quoted_schema} TO {quoted_role}"))
            connection.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
                    f"{quoted_schema}.retention_policies, {quoted_schema}.cleanup_runs "
                    f"TO {quoted_role}"
                )
            )
        with Session(migration_engine) as db, db.begin():
            db.add(
                RetentionPolicy(
                    id=1,
                    task_retention_days=60,
                    reference_retention_days=60,
                    recommendation_retention_days=60,
                    revision=1,
                )
            )
        return migration_engine, runtime_engine, schema
    except Exception:
        runtime_engine.dispose()
        try:
            with migration_engine.begin() as connection:
                connection.execute(text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"))
        finally:
            migration_engine.dispose()
        raise


@pytest.fixture
def isolated_scheduler_engine() -> Engine:
    runtime_url = os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL")
    migration_url = os.getenv("IMAGE_BACKEND_TEST_MIGRATION_DATABASE_URL")
    if runtime_url is None or migration_url is None:
        pytest.skip("requires dedicated PostgreSQL runtime and migration test URLs")
    migrator = create_engine(migration_url, pool_pre_ping=True)
    required_tables = {"retention_policies", "cleanup_runs"}
    if not required_tables.issubset(set(inspect(migrator).get_table_names())):
        migrator.dispose()
        pytest.skip("requires the s011 retention migration")
    migrator.dispose()
    migration_engine, runtime_engine, schema = _isolated_scheduler_engines(
        migration_url=migration_url,
        runtime_url=runtime_url,
    )
    try:
        yield runtime_engine
    finally:
        runtime_engine.dispose()
        with migration_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        migration_engine.dispose()


@pytest.mark.integration
def test_first_startup_catchup_before_daily_run_and_repeated_polls_are_idempotent(
    isolated_scheduler_engine: Engine, tmp_path
) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    local_day = date(2026, 10, 7)
    service = RetentionCleanupService(
        isolated_scheduler_engine, LocalPrivateStorage(tmp_path / "private")
    )

    startup = datetime.combine(local_day, time(2, 0), timezone)
    assert service.schedule_due_runs(now=startup, timezone=timezone, daily_time=time(3, 0)) == 1
    assert service.schedule_due_runs(now=startup, timezone=timezone, daily_time=time(3, 0)) == 0

    with Session(isolated_scheduler_engine) as db:
        runs = db.scalars(select(CleanupRun).order_by(CleanupRun.created_at)).all()
        assert len(runs) == 1
        startup_run = runs[0]
        assert startup_run.trigger_kind == "catchup"
        assert startup_run.trigger_key == "catchup:startup:2026-10-07:Asia/Shanghai"
        assert startup_run.catchup_from is None
        assert startup_run.catchup_to is None
        assert startup_run.scheduled_for is None

    daily = datetime.combine(local_day, time(3, 0), timezone)
    assert service.schedule_due_runs(now=daily, timezone=timezone, daily_time=time(3, 0)) == 1
    assert service.schedule_due_runs(now=daily, timezone=timezone, daily_time=time(3, 0)) == 0

    with Session(isolated_scheduler_engine) as db:
        runs = db.scalars(select(CleanupRun).order_by(CleanupRun.created_at)).all()
        assert len(runs) == 2
        assert {run.trigger_kind for run in runs} == {"catchup", "scheduled"}
        scheduled = next(run for run in runs if run.trigger_kind == "scheduled")
        assert scheduled.scheduled_for == daily.astimezone(UTC)


@pytest.mark.integration
def test_backlog_catchup_before_daily_time_does_not_suppress_today_scheduled_run(
    isolated_scheduler_engine: Engine, tmp_path
) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    today = date(2026, 10, 7)
    previous = today - timedelta(days=3)
    with Session(isolated_scheduler_engine) as db, db.begin():
        db.add(
            CleanupRun(
                trigger_kind="scheduled",
                trigger_key=f"test-seed:{previous.isoformat()}:{uuid4()}",
                policy_revision=1,
                status="completed",
                counts={},
                scheduled_for=datetime.combine(previous, time(3, 0), timezone).astimezone(UTC),
            )
        )
    service = RetentionCleanupService(
        isolated_scheduler_engine, LocalPrivateStorage(tmp_path / "private")
    )

    two_am = datetime.combine(today, time(2, 0), timezone)
    assert service.schedule_due_runs(now=two_am, timezone=timezone, daily_time=time(3, 0)) == 1
    with Session(isolated_scheduler_engine) as db:
        catchup = db.scalar(select(CleanupRun).where(CleanupRun.trigger_kind == "catchup"))
        assert catchup is not None
        assert catchup.catchup_from == datetime.combine(
            previous + timedelta(days=1), time.min, timezone
        ).astimezone(UTC)
        assert catchup.catchup_to == datetime.combine(today, time.min, timezone).astimezone(UTC)
        assert (
            db.scalar(
                select(CleanupRun.id).where(
                    CleanupRun.scheduled_for
                    == datetime.combine(previous, time(3, 0), timezone).astimezone(UTC)
                )
            )
            is not None
        )
        assert (
            db.scalar(
                select(CleanupRun.id).where(
                    CleanupRun.scheduled_for
                    == datetime.combine(today, time(3, 0), timezone).astimezone(UTC)
                )
            )
            is None
        )

    three_am = datetime.combine(today, time(3, 0), timezone)
    assert service.schedule_due_runs(now=three_am, timezone=timezone, daily_time=time(3, 0)) == 1
    assert service.schedule_due_runs(now=three_am, timezone=timezone, daily_time=time(3, 0)) == 0

    with Session(isolated_scheduler_engine) as db:
        scheduled_today = db.scalar(
            select(CleanupRun).where(
                CleanupRun.scheduled_for == three_am.astimezone(UTC)
            )
        )
        assert scheduled_today is not None


@pytest.mark.integration
def test_parallel_schedulers_insert_each_daily_and_catchup_trigger_once(
    isolated_scheduler_engine: Engine, tmp_path
) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    local_day = date(2026, 10, 7)
    storage = LocalPrivateStorage(tmp_path / "parallel-private")
    with isolated_scheduler_engine.connect() as connection:
        schema = connection.execute(text("SELECT current_schema()")).scalar_one()
    peer_engine = create_engine(
        isolated_scheduler_engine.url,
        pool_pre_ping=True,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    services = [
        RetentionCleanupService(isolated_scheduler_engine, storage),
        RetentionCleanupService(peer_engine, storage),
    ]

    def poll_pair(now: datetime) -> list[int]:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    service.schedule_due_runs,
                    now=now,
                    timezone=timezone,
                    daily_time=time(3, 0),
                )
                for service in services
            ]
            return [future.result(timeout=20) for future in futures]

    try:
        two_am = datetime.combine(local_day, time(2, 0), timezone)
        assert sum(poll_pair(two_am)) == 1
        with Session(isolated_scheduler_engine) as db:
            assert (
                db.scalar(
                    select(CleanupRun.id).where(
                        CleanupRun.trigger_key
                        == "catchup:startup:2026-10-07:Asia/Shanghai"
                    )
                )
                is not None
            )
            assert db.scalar(select(func.count(CleanupRun.id))) == 1

        three_am = datetime.combine(local_day, time(3, 0), timezone)
        assert sum(poll_pair(three_am)) == 1
        assert sum(poll_pair(three_am)) == 0
        with Session(isolated_scheduler_engine) as db:
            runs = db.scalars(select(CleanupRun).order_by(CleanupRun.created_at)).all()
            assert len(runs) == 2
            assert {run.trigger_kind for run in runs} == {"catchup", "scheduled"}
            assert (
                sum(
                    run.trigger_key == "scheduled:2026-10-07:0300:Asia/Shanghai"
                    for run in runs
                )
                == 1
            )
    finally:
        peer_engine.dispose()
