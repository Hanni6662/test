from __future__ import annotations

import io
import os
from collections.abc import Iterable
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from app.core.config import Settings
from app.db.models import GuestSession, User
from app.db.reference_models import (
    ImageAsset,
    ReferenceSet,
    ReferenceSetImage,
    ReferenceUploadObject,
)
from app.integrations.storage.local import LocalPrivateStorage
from app.integrations.storage.protocol import ObjectStat, StorageError
from app.main import create_app
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration
TRUSTED_ORIGIN = "http://testserver"
DEV_COOKIE_NAME = "guest_session_dev"


def _image_bytes(image_format: str, color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (24, 16), color).save(output, format=image_format)
    return output.getvalue()


def _settings(database_url: str, storage_root) -> Settings:
    return Settings(
        database_url=database_url,
        production=False,
        use_dev_cookie=True,
        trusted_origins=frozenset({TRUSTED_ORIGIN}),
        storage_driver="local",
        private_storage_root=storage_root,
    )


def _engine() -> Engine:
    return create_engine(os.environ["IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL"], pool_pre_ping=True)


def _post_upload(client: TestClient, primary: bytes, auxiliary: Iterable[bytes] = (), *,
                 primary_mime: str = "image/png", category: str = "手提包"):
    files = [
        ("product_category", (None, category)),
        ("primary_product_image", ("primary.png", primary, primary_mime)),
    ]
    for index, image in enumerate(auxiliary, start=1):
        files.append(
            ("supplementary_product_images", (f"aux-{index}.png", image, "image/png"))
        )
    return client.post(
        "/api/v1/reference-sets",
        files=files,
        headers={"Origin": TRUSTED_ORIGIN},
    )


def _cleanup(engine: Engine, storage: LocalPrivateStorage, user_ids: set[UUID]) -> None:
    if not user_ids:
        return
    with engine.begin() as connection:
        object_keys = connection.execute(
            select(ImageAsset.object_key).where(ImageAsset.owner_user_id.in_(user_ids))
        ).scalars().all()
        connection.execute(
            delete(ReferenceSetImage).where(ReferenceSetImage.owner_user_id.in_(user_ids))
        )
        connection.execute(
            delete(ReferenceUploadObject).where(ReferenceUploadObject.owner_user_id.in_(user_ids))
        )
        connection.execute(delete(ImageAsset).where(ImageAsset.owner_user_id.in_(user_ids)))
        connection.execute(delete(ReferenceSet).where(ReferenceSet.owner_user_id.in_(user_ids)))
        connection.execute(delete(GuestSession).where(GuestSession.user_id.in_(user_ids)))
        connection.execute(delete(User).where(User.id.in_(user_ids)))
    for key in object_keys:
        storage.delete(key)


class _FailOnPut:
    def __init__(self, storage: LocalPrivateStorage, fail_at: int) -> None:
        self._storage = storage
        self._fail_at = fail_at
        self._put_count = 0

    def put_immutable(self, key, stream, metadata) -> ObjectStat:
        self._put_count += 1
        if self._put_count == self._fail_at:
            raise StorageError("injected storage fault")
        return self._storage.put_immutable(key, stream, metadata)

    def open_reader(self, key):
        return self._storage.open_reader(key)

    def stat(self, key):
        return self._storage.stat(key)

    def delete(self, key):
        return self._storage.delete(key)


def test_http_guest_upload_ownership_failure_and_app_restart(tmp_path) -> None:
    database_url = os.environ["IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL"]
    engine = _engine()
    storage = LocalPrivateStorage(tmp_path / "private")
    settings = _settings(database_url, storage.root)
    app = create_app(settings=settings, engine=engine, storage=storage)
    user_ids: set[UUID] = set()
    ready_set_id: UUID | None = None
    try:
        with TestClient(app) as owner:
            no_cookie = owner.get(f"/api/v1/reference-sets/{uuid4()}")
            assert no_cookie.status_code == 401
            assert set(no_cookie.json()) == {"code", "message", "request_id"}
            assert no_cookie.json()["request_id"] == no_cookie.headers["x-request-id"]

            entered = owner.post("/api/v1/auth/guest", headers={"Origin": TRUSTED_ORIGIN})
            assert entered.status_code == 200
            owner_id = UUID(entered.json()["user_id"])
            user_ids.add(owner_id)
            assert entered.json() == {
                "user_id": str(owner_id),
                "identity_kind": "guest",
                "authenticated": True,
                "features": {"account_login": False, "billing": False, "payments": False},
            }
            token = owner.cookies.get(DEV_COOKIE_NAME)
            assert token and token not in entered.text

            with Session(engine) as session:
                guest_session = session.scalar(
                    select(GuestSession).where(GuestSession.user_id == owner_id)
                )
                assert guest_session is not None
                assert guest_session.ttl_days == 120
                assert guest_session.expires_at - guest_session.created_at == timedelta(days=120)
                initial_expiration = guest_session.expires_at
                initial_renewal = guest_session.last_renewed_at

            me = owner.get("/api/v1/auth/me")
            assert UUID(me.json()["user_id"]) == owner_id
            with Session(engine) as session:
                polled_session = session.scalar(
                    select(GuestSession).where(GuestSession.user_id == owner_id)
                )
                assert polled_session is not None
                assert polled_session.expires_at == initial_expiration
                assert polled_session.last_renewed_at == initial_renewal

            before_refresh_token = owner.cookies.get(DEV_COOKIE_NAME)
            refreshed = owner.post(
                "/api/v1/auth/guest/refresh", headers={"Origin": TRUSTED_ORIGIN}
            )
            assert refreshed.status_code == 204
            assert owner.cookies.get(DEV_COOKIE_NAME) == before_refresh_token == token
            with Session(engine) as session:
                renewed_session = session.scalar(
                    select(GuestSession).where(GuestSession.user_id == owner_id)
                )
                assert renewed_session is not None
                assert renewed_session.last_renewed_at > initial_renewal
                assert renewed_session.expires_at >= initial_expiration

            primary = _image_bytes("PNG", (242, 17, 41))
            auxiliary = _image_bytes("PNG", (12, 232, 91))
            uploaded = _post_upload(owner, primary, [auxiliary])
            assert uploaded.status_code == 201, uploaded.json()
            result = uploaded.json()
            ready_set_id = UUID(result["reference_set_id"])
            assert result["status"] == "ready"
            assert result["primary_image"]["slot"] == "PRIMARY"
            assert [item["slot"] for item in result["auxiliary_images"]] == ["AUX01"]
            assert {"path", "object_key", "credential"}.isdisjoint(result)
            assert owner.get(f"/api/v1/reference-sets/{ready_set_id}").json() == result

            primary_only = _post_upload(
                owner, _image_bytes("PNG", (198, 55, 75))
            )
            assert primary_only.status_code == 201
            assert primary_only.json()["auxiliary_images"] == []

            with Session(engine) as session:
                ready = session.get(ReferenceSet, ready_set_id)
                assert ready is not None and ready.status == "ready"
                assert ready.ready_at is not None and ready.expires_at is not None
                assert (ready.expires_at - ready.ready_at).days == 60
                assert session.scalar(
                    select(ReferenceSetImage.id).where(
                        ReferenceSetImage.reference_set_id == ready_set_id
                    )
                ) is not None

            other_app = create_app(settings=settings, engine=engine, storage=storage)
            with TestClient(other_app) as other:
                assert other.get(f"/api/v1/reference-sets/{ready_set_id}").status_code == 401
                second_identity = other.post(
                    "/api/v1/auth/guest", headers={"Origin": TRUSTED_ORIGIN}
                )
                other_id = UUID(second_identity.json()["user_id"])
                user_ids.add(other_id)
                forbidden_set = other.get(f"/api/v1/reference-sets/{ready_set_id}")
                assert forbidden_set.status_code == 404

                missing_primary = other.post(
                    "/api/v1/reference-sets",
                    files=[("product_category", (None, "鞋履"))],
                    headers={"Origin": TRUSTED_ORIGIN},
                )
                assert missing_primary.status_code == 422

                too_many_auxiliaries = _post_upload(
                    other,
                    _image_bytes("PNG", (1, 2, 3)),
                    [
                        _image_bytes("PNG", (x, 120, 40))
                        for x in (21, 61, 101, 141)
                    ],
                )
                assert too_many_auxiliaries.status_code == 422

                mime_mismatch = _post_upload(
                    other, _image_bytes("JPEG", (81, 42, 20)), primary_mime="image/png"
                )
                assert mime_mismatch.status_code == 422
                assert mime_mismatch.json()["code"] == "REFERENCE_IMAGE_INVALID"

                repeated_image = _image_bytes("PNG", (91, 102, 113))
                duplicate = _post_upload(other, repeated_image, [repeated_image])
                assert duplicate.status_code == 422
                assert duplicate.json()["code"] == "DUPLICATE_REFERENCE_IMAGE"

                oversize_image = _post_upload(
                    other, b"x" * (10 * 1024 * 1024 + 1)
                )
                assert oversize_image.status_code == 413

                assert other.get("/api/v1/quota").json() == {"enabled": False}
                assert other.get("/api/v1/billing/balance").json() == {"enabled": False}
                disabled_login = other.post(
                    "/api/v1/auth/login", headers={"Origin": TRUSTED_ORIGIN}
                )
                assert disabled_login.status_code == 503
                assert disabled_login.json()["code"] == "FEATURE_DISABLED"

                failing_storage = _FailOnPut(
                    LocalPrivateStorage(tmp_path / "failing-private"), fail_at=3
                )
                failing_app = create_app(
                    settings=_settings(database_url, tmp_path / "failing-private"),
                    engine=engine,
                    storage=failing_storage,
                )
                with TestClient(failing_app) as failing:
                    failing.cookies.set(DEV_COOKIE_NAME, other.cookies.get(DEV_COOKIE_NAME))
                    failed = _post_upload(
                        failing,
                        _image_bytes("PNG", (14, 24, 34)),
                        [_image_bytes("PNG", (44, 54, 64))],
                    )
                assert failed.status_code == 503
                assert failed.json()["request_id"] == failed.headers["x-request-id"]
                with Session(engine) as session:
                    failed_set = session.scalar(
                        select(ReferenceSet)
                        .where(ReferenceSet.owner_user_id == other_id)
                        .order_by(ReferenceSet.created_at.desc())
                    )
                    assert failed_set is not None and failed_set.status == "failed"
                    assert failed_set.id != ready_set_id
                    failed_set_id = failed_set.id
                    object_rows = session.scalars(
                        select(ReferenceUploadObject).where(
                            ReferenceUploadObject.reference_set_id == failed_set_id
                        )
                    ).all()
                    assert len(object_rows) == 4
                    assert all(row.status == "deleted" for row in object_rows)
                assert other.get(f"/api/v1/reference-sets/{failed_set_id}").status_code == 404

                migrator = create_engine(
                    os.environ["IMAGE_BACKEND_TEST_MIGRATION_DATABASE_URL"], pool_pre_ping=True
                )
                try:
                    with migrator.begin() as connection:
                        connection.execute(
                            text(
                                "REVOKE INSERT ON TABLE public.reference_sets "
                                "FROM image_backend_runtime"
                            )
                        )
                    database_failure = _post_upload(
                        other, _image_bytes("PNG", (74, 84, 94))
                    )
                    assert database_failure.status_code == 503
                    assert database_failure.json()["code"] == "DATABASE_UNAVAILABLE"
                    with Session(engine) as session:
                        assert session.scalar(
                            select(ReferenceSet.id).where(ReferenceSet.owner_user_id == other_id)
                        ) is not None
                        assert session.scalar(
                            select(ReferenceSet.id).where(
                                ReferenceSet.owner_user_id == other_id,
                                ReferenceSet.status == "uploading",
                            )
                        ) is None
                finally:
                    with migrator.begin() as connection:
                        connection.execute(
                            text(
                                "GRANT INSERT ON TABLE public.reference_sets "
                                "TO image_backend_runtime"
                            )
                        )
                    migrator.dispose()

        restarted_app = create_app(settings=settings, engine=engine, storage=storage)
        with TestClient(restarted_app) as restarted:
            restarted.cookies.set(DEV_COOKIE_NAME, token)
            restored_identity = restarted.get("/api/v1/auth/me")
            restored_group = restarted.get(f"/api/v1/reference-sets/{ready_set_id}")
            assert restored_identity.status_code == 200
            assert UUID(restored_identity.json()["user_id"]) == owner_id
            assert restored_group.status_code == 200
            assert UUID(restored_group.json()["reference_set_id"]) == ready_set_id
    finally:
        _cleanup(engine, storage, user_ids)
        engine.dispose()
