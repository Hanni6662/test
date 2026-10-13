"""Controlled final-auth races in an explicitly opted-in fresh mock project."""
from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from app.core.config import get_settings
from app.core.domain_errors import DomainError
from app.db.models import GuestSession
from app.db.reference_models import ImageAsset
from app.db.task_models import Task
from app.integrations.storage.local import LocalPrivateStorage
from app.main import create_app
from app.services.original_downloads import DownloadGate
from app.services.tasks import TaskOptions, TaskService
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("change", [
    "deleting", "expired", "asset_changed", "revoked", "session_expired", "both",
])
def test_final_admission_observes_changes_committed_during_actual_auth(change: str) -> None:
    if os.getenv("IMAGE_BACKEND_ORIGINAL_ADMISSION_PROBE") != "true":
        pytest.skip("requires an explicitly prepared fresh isolated mock instance")
    project = os.environ["IMAGE_BACKEND_TEST_ISOLATED_PROJECT"]
    assert project.startswith("localimg-original-fix-")
    state = json.loads(Path(os.environ["IMAGE_BACKEND_TEST_ADMISSION_SESSION"]).read_text(
        encoding="utf-8",
    ))
    settings = get_settings()
    assert not settings.production and settings.private_storage_root == Path("/data/private")
    engine = create_engine(settings.database_url.get_secret_value())
    assert engine.url.host == "db" and engine.url.database == "image_backend"
    app = create_app(settings=settings, engine=engine)
    auth = app.state.auth_service
    credential = state["cookies"][auth.cookie_name]
    original_identity = auth.authenticate(credential)
    assert original_identity is not None
    task_id, image_id = UUID(state["task_id"]), UUID(state["image_id"])
    with Session(engine) as db:
        task, asset = db.get(Task, task_id), db.get(ImageAsset, image_id)
        session = db.get(GuestSession, original_identity.session_id)
        saved = (
            task.status, task.expires_at, asset.content_sha256,
            session.revoked_at, session.expires_at,
        )
    readers = []
    gate = DownloadGate(1, 10)
    lease = gate.try_acquire()
    assert lease is not None

    class TrackingStorage:
        def open_reader(self, key):
            reader = LocalPrivateStorage("/data/private").open_reader(key)
            readers.append(reader)
            return reader

    def actual_identity_recheck() -> bool:
        # A separate transaction commits while the last identity lookup is pending.
        with Session(engine) as db, db.begin():
            now = db.scalar(select(func.clock_timestamp()))
            if change in {"deleting", "both"}:
                db.get(Task, task_id).status = "deleting"
            elif change == "expired":
                db.get(Task, task_id).expires_at = now - timedelta(seconds=1)
            elif change == "asset_changed":
                db.get(ImageAsset, image_id).content_sha256 = "0" * 64
            if change in {"revoked", "both"}:
                db.get(GuestSession, original_identity.session_id).revoked_at = now
            elif change == "session_expired":
                db.get(GuestSession, original_identity.session_id).expires_at = now
        current = auth.authenticate(credential)
        return bool(current and current.user_id == original_identity.user_id
                    and current.session_id == original_identity.session_id
                    and current.identity_kind == original_identity.identity_kind)

    try:
        with pytest.raises(DomainError) as error:
            TaskService(engine, TaskOptions()).open_original(
                owner_user_id=original_identity.user_id, task_id=task_id, image_id=image_id,
                storage=TrackingStorage(), lease=lease, identity_recheck=actual_identity_recheck,
            )
        expected = (401, "AUTH_REQUIRED") if change in {"revoked", "session_expired", "both"} else (
            404, "ORIGINAL_NOT_FOUND",
        )
        assert (error.value.status_code, error.value.code) == expected
        assert len(readers) == 1 and readers[0].closed
        recovered = gate.try_acquire()
        assert recovered is not None
        recovered.release()
    finally:
        for reader in readers:
            reader.close()
        lease.release()
        with Session(engine) as db, db.begin():
            task, asset = db.get(Task, task_id), db.get(ImageAsset, image_id)
            session = db.get(GuestSession, original_identity.session_id)
            (task.status, task.expires_at, asset.content_sha256,
             session.revoked_at, session.expires_at) = saved
        engine.dispose()
