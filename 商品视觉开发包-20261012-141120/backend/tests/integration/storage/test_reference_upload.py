from __future__ import annotations

import io
import os
from uuid import uuid4

import pytest
from app.core.domain_errors import DomainError
from app.db.models import User
from app.db.reference_models import (
    ImageAsset,
    ReferenceSet,
    ReferenceSetImage,
    ReferenceUploadObject,
)
from app.integrations.storage.local import LocalPrivateStorage
from app.integrations.storage.protocol import ObjectStat, StorageError
from app.services.references import (
    ReferenceOptions,
    ReferenceService,
    ReferenceUploadFile,
)
from PIL import Image
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session


def _image_bytes(image_format: str, color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (12, 8), color).save(output, format=image_format)
    return output.getvalue()


def _upload(body: bytes, mime_type: str) -> ReferenceUploadFile:
    return ReferenceUploadFile(io.BytesIO(body), mime_type)


def _engine():
    return create_engine(os.environ["IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL"], pool_pre_ping=True)


def _cleanup(engine, owner_user_id) -> None:
    with engine.begin() as connection:
        connection.execute(
            delete(ReferenceSetImage).where(ReferenceSetImage.owner_user_id == owner_user_id)
        )
        connection.execute(
            delete(ReferenceUploadObject).where(
                ReferenceUploadObject.owner_user_id == owner_user_id
            )
        )
        connection.execute(delete(ImageAsset).where(ImageAsset.owner_user_id == owner_user_id))
        connection.execute(delete(ReferenceSet).where(ReferenceSet.owner_user_id == owner_user_id))
        connection.execute(delete(User).where(User.id == owner_user_id))


@pytest.mark.integration
def test_complete_group_is_ready_and_reads_only_for_its_owner(tmp_path) -> None:
    engine = _engine()
    owner_user_id = uuid4()
    other_user_id = uuid4()
    storage = LocalPrivateStorage(tmp_path / "private")
    service = ReferenceService(engine, storage, ReferenceOptions())
    primary_bytes = _image_bytes("PNG", (240, 10, 20))
    auxiliary_bytes = _image_bytes("JPEG", (20, 240, 10))
    try:
        with Session(engine) as session, session.begin():
            session.add_all(
                [
                    User(id=owner_user_id, identity_kind="guest", status="active"),
                    User(id=other_user_id, identity_kind="guest", status="active"),
                ]
            )

        response = service.upload_reference_set(
            owner_user_id=owner_user_id,
            product_category="手提包",
            primary_file=_upload(primary_bytes, "image/png"),
            supplementary_files=[_upload(auxiliary_bytes, "image/jpeg")],
        )

        assert response.status == "ready"
        assert response.primary_image.slot == "PRIMARY"
        assert [image.slot for image in response.auxiliary_images] == ["AUX01"]
        assert response.model_dump_json().find(str(tmp_path)) == -1

        persisted = service.get_reference_set(
            owner_user_id=owner_user_id, reference_set_id=response.reference_set_id
        )
        assert persisted == response
        model_inputs = service.read_model_inputs(
            owner_user_id=owner_user_id, reference_set_id=response.reference_set_id
        )
        assert [image.slot for image in model_inputs] == ["PRIMARY", "AUX01"]
        assert len(model_inputs[0].content) <= 10 * 1024 * 1024

        with pytest.raises(DomainError) as error:
            service.get_reference_set(
                owner_user_id=other_user_id, reference_set_id=response.reference_set_id
            )
        assert error.value.status_code == 404

        with Session(engine) as session:
            reference_set = session.get(ReferenceSet, response.reference_set_id)
            assert reference_set is not None and reference_set.status == "ready"
            asset_keys = session.scalars(
                select(ImageAsset.object_key).where(ImageAsset.owner_user_id == owner_user_id)
            ).all()
            attached = session.scalars(
                select(ReferenceUploadObject.status).where(
                    ReferenceUploadObject.owner_user_id == owner_user_id
                )
            ).all()
        assert len(asset_keys) == 4
        assert all(storage.stat(key) is not None for key in asset_keys)
        assert attached == ["attached"] * 4
    finally:
        _cleanup(engine, owner_user_id)
        _cleanup(engine, other_user_id)
        engine.dispose()


class _FailOnPut:
    def __init__(self, storage: LocalPrivateStorage, fail_at: int) -> None:
        self._storage = storage
        self._fail_at = fail_at
        self._put_count = 0

    def put_immutable(self, key, stream, metadata) -> ObjectStat:
        self._put_count += 1
        if self._put_count == self._fail_at:
            raise StorageError("injected failure")
        return self._storage.put_immutable(key, stream, metadata)

    def open_reader(self, key):
        return self._storage.open_reader(key)

    def stat(self, key):
        return self._storage.stat(key)

    def delete(self, key):
        return self._storage.delete(key)


@pytest.mark.integration
def test_partial_object_failure_never_returns_ready_and_records_cleanup(tmp_path) -> None:
    engine = _engine()
    owner_user_id = uuid4()
    local_storage = LocalPrivateStorage(tmp_path / "private")
    storage = _FailOnPut(local_storage, fail_at=5)
    service = ReferenceService(engine, storage, ReferenceOptions())
    uploads = [
        _upload(_image_bytes("PNG", (240, 10, 20)), "image/png"),
        _upload(_image_bytes("JPEG", (20, 240, 10)), "image/jpeg"),
        _upload(_image_bytes("WEBP", (10, 20, 240)), "image/webp"),
    ]
    try:
        with Session(engine) as session, session.begin():
            session.add(User(id=owner_user_id, identity_kind="guest", status="active"))

        with pytest.raises(DomainError) as error:
            service.upload_reference_set(
                owner_user_id=owner_user_id,
                product_category="鞋履",
                primary_file=uploads[0],
                supplementary_files=uploads[1:],
            )
        assert error.value.status_code == 503

        with Session(engine) as session:
            reference_set = session.scalar(
                select(ReferenceSet).where(ReferenceSet.owner_user_id == owner_user_id)
            )
            assert reference_set is not None
            assert reference_set.status == "failed"
            object_rows = session.scalars(
                select(ReferenceUploadObject).where(
                    ReferenceUploadObject.reference_set_id == reference_set.id
                )
            ).all()
        assert len(object_rows) == 6
        assert all(row.status == "deleted" for row in object_rows)
        assert all(local_storage.stat(row.object_key) is None for row in object_rows)
        with pytest.raises(DomainError) as read_error:
            service.get_reference_set(
                owner_user_id=owner_user_id, reference_set_id=reference_set.id
            )
        assert read_error.value.status_code == 404
    finally:
        _cleanup(engine, owner_user_id)
        engine.dispose()
