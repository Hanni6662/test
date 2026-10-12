from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from app.core.domain_errors import DomainError
from app.services.original_downloads import OriginalDownload
from app.services.tasks import TaskOptions, TaskService
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

_DATABASE_NOW = "2026-10-11 13:00:00.000000"
_ORIGINAL_BYTES = b"private original image bytes"


@pytest.fixture
def sqlite_database() -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def register_clock(dbapi_connection: object, _connection_record: object) -> None:
        dbapi_connection.create_function("clock_timestamp", 0, lambda: _DATABASE_NOW)  # type: ignore[attr-defined]

    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE tasks (id CHAR(32), owner_user_id CHAR(32), "
                "status VARCHAR(24), expires_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE task_items (id CHAR(32), task_id CHAR(32), "
                "status VARCHAR(16), image_id CHAR(32))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE generation_results (attempt_id CHAR(32), task_id CHAR(32), "
                "task_item_id CHAR(32), owner_user_id CHAR(32), original_asset_id CHAR(32))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE task_item_attempts (id CHAR(32), task_item_id CHAR(32), "
                "status VARCHAR(16), completed_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE image_assets (id CHAR(32), owner_user_id CHAR(32), "
                "asset_kind VARCHAR(32), object_key VARCHAR(512), mime_type VARCHAR(32), "
                "size_bytes INTEGER, content_sha256 VARCHAR(64))"
            )
        )

    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    try:
        yield engine, sessions
    finally:
        engine.dispose()


@pytest.fixture
def service(sqlite_database: tuple[Engine, sessionmaker[Session]]) -> TaskService:
    engine, _sessions = sqlite_database
    return TaskService(engine, TaskOptions())


class _TrackingReader(BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.closed_by_service = False

    def close(self) -> None:
        self.closed_by_service = True
        super().close()


class _TrackingLease:
    def __init__(self) -> None:
        self.release_calls = 0

    def release(self) -> None:
        if self.release_calls == 0:
            self.release_calls += 1

    def check_deadline(self) -> None:
        if self.release_calls:
            raise RuntimeError("lease already released")


class _Storage:
    def __init__(self, reader: _TrackingReader, on_open: Callable[[], None] | None = None) -> None:
        self.reader = reader
        self.on_open = on_open
        self.opened_keys: list[str] = []

    def open_reader(self, key: str) -> _TrackingReader:
        self.opened_keys.append(key)
        if self.on_open is not None:
            self.on_open()
        return self.reader


class _Rows:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.task_id = uuid4()
        self.item_id = uuid4()
        self.owner_id = uuid4()
        self.image_id = uuid4()
        self.attempt_id = uuid4()
        self.object_key = "generated/private/original.png"

    @staticmethod
    def _db_uuid(value: UUID) -> str:
        return value.hex

    def seed(
        self,
        *,
        parent_status: str = "partially_succeeded",
        expires_at: str | None = None,
        item_status: str = "succeeded",
        item_image_id: UUID | None = None,
        result_task_id: UUID | None = None,
        result_item_id: UUID | None = None,
        result_owner_id: UUID | None = None,
        result_attempt_id: UUID | None = None,
        result_original_asset_id: UUID | None = None,
        attempt_item_id: UUID | None = None,
        attempt_status: str = "completed",
        completed_at: str | None = "2026-10-11 12:59:00.000000",
        asset_id: UUID | None = None,
        asset_owner_id: UUID | None = None,
        asset_kind: str = "generation_original",
        asset_sha256: str | None = None,
    ) -> None:
        sha256 = asset_sha256 or hashlib.sha256(_ORIGINAL_BYTES).hexdigest()
        with self.engine.begin() as connection:
            connection.execute(
                text("INSERT INTO tasks VALUES (:id, :owner, :status, :expires_at)"),
                {
                    "id": self._db_uuid(self.task_id),
                    "owner": self._db_uuid(self.owner_id),
                    "status": parent_status,
                    "expires_at": expires_at,
                },
            )
            connection.execute(
                text("INSERT INTO task_items VALUES (:id, :task, :status, :image)"),
                {
                    "id": self._db_uuid(self.item_id),
                    "task": self._db_uuid(self.task_id),
                    "status": item_status,
                    "image": self._db_uuid(item_image_id or self.image_id),
                },
            )
            connection.execute(
                text("INSERT INTO task_item_attempts VALUES (:id, :item, :status, :completed)"),
                {
                    "id": self._db_uuid(self.attempt_id),
                    "item": self._db_uuid(attempt_item_id or self.item_id),
                    "status": attempt_status,
                    "completed": completed_at,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO generation_results "
                    "VALUES (:attempt, :task, :item, :owner, :asset)"
                ),
                {
                    "attempt": self._db_uuid(result_attempt_id or self.attempt_id),
                    "task": self._db_uuid(result_task_id or self.task_id),
                    "item": self._db_uuid(result_item_id or self.item_id),
                    "owner": self._db_uuid(result_owner_id or self.owner_id),
                    "asset": self._db_uuid(result_original_asset_id or self.image_id),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO image_assets VALUES (:id, :owner, :kind, :key, :mime, :size, :sha)"
                ),
                {
                    "id": self._db_uuid(asset_id or self.image_id),
                    "owner": self._db_uuid(asset_owner_id or self.owner_id),
                    "kind": asset_kind,
                    "key": self.object_key,
                    "mime": "image/png",
                    "size": len(_ORIGINAL_BYTES),
                    "sha": sha256,
                },
            )

    def update(self, statement: str, parameters: dict[str, object]) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(statement), parameters)


def _open(
    service: TaskService,
    rows: _Rows,
    storage: _Storage,
    lease: _TrackingLease,
    *,
    identity_recheck: Callable[[], bool] = lambda: True,
) -> OriginalDownload:
    return service.open_original(
        owner_user_id=rows.owner_id,
        task_id=rows.task_id,
        image_id=rows.image_id,
        storage=storage,  # type: ignore[arg-type]
        lease=lease,  # type: ignore[arg-type]
        identity_recheck=identity_recheck,
    )


def test_open_original_allows_succeeded_image_on_partial_parent_with_null_expiry(
    service: TaskService,
    sqlite_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    engine, _sessions = sqlite_database
    rows = _Rows(engine)
    rows.seed(parent_status="partially_succeeded", expires_at=None)
    reader = _TrackingReader(_ORIGINAL_BYTES)
    storage = _Storage(reader)
    lease = _TrackingLease()

    download = _open(service, rows, storage, lease)

    assert download.content_type == "image/png"
    assert download.size_bytes == len(_ORIGINAL_BYTES)
    assert download.reader is reader
    assert storage.opened_keys == [rows.object_key]
    assert list(download.iter_chunks()) == [_ORIGINAL_BYTES]
    download.close()
    assert reader.closed_by_service
    assert lease.release_calls == 1


@pytest.mark.parametrize(
    ("changes", "expires_at"),
    [
        ({"item_status": "failed"}, None),
        ({"item_image_id": uuid4()}, None),
        ({"result_item_id": uuid4()}, None),
        ({"attempt_item_id": uuid4()}, None),
        ({"attempt_status": "saving"}, None),
        ({"completed_at": None}, None),
        ({"asset_kind": "generation_preview"}, None),
        ({"asset_owner_id": uuid4()}, None),
        ({"asset_id": uuid4()}, None),
        ({"result_owner_id": uuid4()}, None),
        ({"result_task_id": uuid4()}, None),
        ({"result_original_asset_id": uuid4()}, None),
        ({}, "2026-10-11 12:59:59.000000"),
        ({"parent_status": "deleting"}, None),
    ],
)
def test_open_original_rejects_invalid_association_before_storage_read(
    service: TaskService,
    sqlite_database: tuple[Engine, sessionmaker[Session]],
    changes: dict[str, object],
    expires_at: str | None,
) -> None:
    engine, _sessions = sqlite_database
    rows = _Rows(engine)
    rows.seed(expires_at=expires_at, **changes)
    reader = _TrackingReader(_ORIGINAL_BYTES)
    storage = _Storage(reader)
    lease = _TrackingLease()

    with pytest.raises(DomainError) as caught:
        _open(service, rows, storage, lease)

    assert caught.value.status_code == 404
    assert caught.value.code == "ORIGINAL_NOT_FOUND"
    assert storage.opened_keys == []
    assert lease.release_calls == 1
    assert not reader.closed_by_service


def test_open_original_closes_reader_and_releases_lease_when_final_auth_fails(
    service: TaskService,
    sqlite_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    engine, _sessions = sqlite_database
    rows = _Rows(engine)
    rows.seed()
    reader = _TrackingReader(_ORIGINAL_BYTES)
    storage = _Storage(reader)
    lease = _TrackingLease()
    recheck_calls = 0

    def reject_identity() -> bool:
        nonlocal recheck_calls
        recheck_calls += 1
        return False

    with pytest.raises(DomainError) as caught:
        _open(service, rows, storage, lease, identity_recheck=reject_identity)

    assert caught.value.status_code == 401
    assert caught.value.code == "AUTH_REQUIRED"
    assert recheck_calls == 1
    assert reader.closed_by_service
    assert lease.release_calls == 1


@pytest.mark.parametrize("change_kind", ["task_deleting", "result_missing", "snapshot_changed"])
def test_open_original_closes_reader_when_final_resource_check_rejects(
    service: TaskService,
    sqlite_database: tuple[Engine, sessionmaker[Session]],
    change_kind: str,
) -> None:
    engine, _sessions = sqlite_database
    rows = _Rows(engine)
    rows.seed()
    reader = _TrackingReader(_ORIGINAL_BYTES)

    def mutate_after_initial_query() -> None:
        if change_kind == "task_deleting":
            rows.update(
                "UPDATE tasks SET status = 'deleting' WHERE id = :id",
                {"id": rows.task_id.hex},
            )
        elif change_kind == "result_missing":
            rows.update("DELETE FROM generation_results", {})
        else:
            rows.update(
                "UPDATE image_assets SET content_sha256 = :sha WHERE id = :id",
                {"sha": "0" * 64, "id": rows.image_id.hex},
            )

    storage = _Storage(reader, on_open=mutate_after_initial_query)
    lease = _TrackingLease()
    recheck_calls = 0

    def identity_recheck() -> bool:
        nonlocal recheck_calls
        recheck_calls += 1
        return True

    with pytest.raises(DomainError) as caught:
        _open(service, rows, storage, lease, identity_recheck=identity_recheck)

    assert caught.value.status_code == 404
    assert caught.value.code == "ORIGINAL_NOT_FOUND"
    assert recheck_calls == 1
    assert reader.closed_by_service
    assert lease.release_calls == 1


def test_open_original_maps_storage_open_failure_and_releases_lease(
    service: TaskService,
    sqlite_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    engine, _sessions = sqlite_database
    rows = _Rows(engine)
    rows.seed()
    lease = _TrackingLease()

    class BrokenStorage:
        def open_reader(self, _key: str) -> BytesIO:
            raise OSError("private path details must not escape")

    with pytest.raises(DomainError) as caught:
        service.open_original(
            owner_user_id=rows.owner_id,
            task_id=rows.task_id,
            image_id=rows.image_id,
            storage=BrokenStorage(),  # type: ignore[arg-type]
            lease=lease,  # type: ignore[arg-type]
            identity_recheck=lambda: True,
        )

    assert caught.value.status_code == 503
    assert caught.value.code == "ORIGINAL_UNAVAILABLE"
    assert lease.release_calls == 1


def test_open_original_closes_reader_when_content_integrity_fails(
    service: TaskService,
    sqlite_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    engine, _sessions = sqlite_database
    rows = _Rows(engine)
    rows.seed()
    reader = _TrackingReader(b"different bytes")
    storage = _Storage(reader)
    lease = _TrackingLease()

    with pytest.raises(DomainError) as caught:
        _open(service, rows, storage, lease)

    assert caught.value.status_code == 503
    assert caught.value.code == "ORIGINAL_INTEGRITY"
    assert reader.closed_by_service
    assert lease.release_calls == 1


def test_open_original_maps_query_failure_and_releases_lease(
    service: TaskService,
    sqlite_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    engine, _sessions = sqlite_database
    rows = _Rows(engine)
    lease = _TrackingLease()
    reader = _TrackingReader(_ORIGINAL_BYTES)
    storage = _Storage(reader)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE generation_results"))

    with pytest.raises(DomainError) as caught:
        _open(service, rows, storage, lease)

    assert caught.value.status_code == 503
    assert caught.value.code == "DATABASE_UNAVAILABLE"
    assert lease.release_calls == 1
    assert storage.opened_keys == []


@pytest.mark.parametrize("change_kind", ["deleting", "expired", "asset_changed", "result_missing"])
def test_final_resource_admission_observes_changes_during_identity_recheck(
    service: TaskService,
    sqlite_database: tuple[Engine, sessionmaker[Session]],
    change_kind: str,
) -> None:
    engine, _sessions = sqlite_database
    rows = _Rows(engine)
    rows.seed()
    reader = _TrackingReader(_ORIGINAL_BYTES)
    lease = _TrackingLease()

    def recheck_identity() -> bool:
        assert not reader.closed
        if change_kind == "deleting":
            rows.update("UPDATE tasks SET status = 'deleting'", {})
        elif change_kind == "expired":
            rows.update("UPDATE tasks SET expires_at = :now", {"now": _DATABASE_NOW})
        elif change_kind == "asset_changed":
            rows.update("UPDATE image_assets SET content_sha256 = :sha", {"sha": "0" * 64})
        else:
            rows.update("DELETE FROM generation_results", {})
        return True

    with pytest.raises(DomainError) as caught:
        _open(service, rows, _Storage(reader), lease, identity_recheck=recheck_identity)

    assert (caught.value.status_code, caught.value.code) == (404, "ORIGINAL_NOT_FOUND")
    assert reader.closed_by_service
    assert lease.release_calls == 1


def test_invalid_final_identity_takes_precedence_over_late_resource_change(
    service: TaskService,
    sqlite_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    engine, _sessions = sqlite_database
    rows = _Rows(engine)
    rows.seed()
    reader = _TrackingReader(_ORIGINAL_BYTES)
    lease = _TrackingLease()

    def revoked_identity() -> bool:
        rows.update("UPDATE tasks SET status = 'deleting'", {})
        return False

    with pytest.raises(DomainError) as caught:
        _open(service, rows, _Storage(reader), lease, identity_recheck=revoked_identity)

    assert (caught.value.status_code, caught.value.code) == (401, "AUTH_REQUIRED")
    assert reader.closed_by_service
    assert lease.release_calls == 1


@pytest.mark.parametrize("stage", ["identity", "resource"])
def test_final_database_failure_closes_reader_and_releases_lease(
    service: TaskService,
    sqlite_database: tuple[Engine, sessionmaker[Session]],
    stage: str, monkeypatch,
) -> None:
    engine, _sessions = sqlite_database
    rows = _Rows(engine)
    rows.seed()
    reader = _TrackingReader(_ORIGINAL_BYTES)
    lease = _TrackingLease()
    snapshot = service._original_snapshot
    calls = 0

    def final_snapshot_failure(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SQLAlchemyError("private database details")
        return snapshot(**kwargs)

    def identity_recheck() -> bool:
        if stage == "identity":
            raise SQLAlchemyError("private authentication database details")
        return True

    if stage == "resource":
        monkeypatch.setattr(service, "_original_snapshot", final_snapshot_failure)
    with pytest.raises(DomainError) as caught:
        _open(service, rows, _Storage(reader), lease, identity_recheck=identity_recheck)
    assert (caught.value.status_code, caught.value.code) == (503, "DATABASE_UNAVAILABLE")
    assert "private" not in caught.value.message
    assert reader.closed_by_service and lease.release_calls == 1

