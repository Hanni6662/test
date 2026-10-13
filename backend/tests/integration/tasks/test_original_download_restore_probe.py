from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from app.db.generation_models import GenerationResult
from app.db.reference_models import ImageAsset
from app.integrations.storage.local import LocalPrivateStorage
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


def test_restored_instance_serves_original_and_preview_to_original_guest() -> None:
    if os.getenv("IMAGE_BACKEND_ORIGINAL_RESTORE_PROBE") != "true":
        pytest.skip("requires a fresh restored mock instance and private cookie evidence")

    base_url = os.getenv("IMAGE_BACKEND_TEST_API_URL", "")
    origin = os.getenv("IMAGE_BACKEND_TEST_API_ORIGIN", "")
    database_url = os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL", "")
    evidence_dir = Path(os.getenv("IMAGE_BACKEND_TEST_PRIVATE_EVIDENCE_DIR", ""))
    assert base_url and origin and database_url
    target = json.loads((evidence_dir / "restore-target.json").read_text(encoding="utf-8"))
    cookie = (evidence_dir / "guest-cookie.txt").read_text(encoding="ascii")
    assert cookie.startswith("guest_session_dev=") and "\n" not in cookie

    task_id = UUID(target["task_id"])
    image_id = UUID(target["image_id"])
    expected_size = int(target["size_bytes"])
    expected_sha256 = str(target["sha256"])
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with Session(engine) as db:
            asset = db.get(ImageAsset, image_id)
            result = db.execute(
                select(GenerationResult).where(
                    GenerationResult.task_id == task_id,
                    GenerationResult.original_asset_id == image_id,
                )
            ).scalar_one()
            assert asset is not None
            assert result.original_asset_id == image_id
            assert result.preview_asset_id != image_id
            assert asset.asset_kind == "generation_original"
            assert asset.size_bytes == expected_size
            assert asset.content_sha256 == expected_sha256
            assert asset.mime_type == target["mime_type"]
            object_key = asset.object_key

        storage = LocalPrivateStorage(
            os.getenv("IMAGE_BACKEND_PRIVATE_STORAGE_ROOT", "/data/private")
        )
        with storage.open_reader(object_key) as reader:
            stored_bytes = reader.read()
        assert len(stored_bytes) == expected_size
        assert hashlib.sha256(stored_bytes).hexdigest() == expected_sha256

        headers = {"Cookie": cookie}
        original_path = f"/api/v1/tasks/{task_id}/images/{image_id}/original"
        preview_path = f"/api/v1/tasks/{task_id}/images/{image_id}/preview"
        with httpx.Client(base_url=base_url, timeout=60) as owner:
            original = owner.get(original_path, headers=headers)
            assert original.status_code == 200, original.text
            assert original.content == stored_bytes
            assert len(original.content) == expected_size
            assert hashlib.sha256(original.content).hexdigest() == expected_sha256
            assert original.headers["content-type"] == target["mime_type"]
            assert original.headers["content-length"] == str(expected_size)
            assert original.headers["content-disposition"].startswith("attachment;")
            assert object_key not in str(original.headers)
            preview = owner.get(preview_path, headers=headers)
            assert preview.status_code == 200, preview.text
            assert preview.headers["content-type"] == "image/webp"
            assert preview.content

        with httpx.Client(base_url=base_url, timeout=30) as other:
            created_guest = other.post("/api/v1/auth/guest", headers={"Origin": origin})
            assert created_guest.status_code == 200, created_guest.text
            cross_owner = other.get(original_path)
            assert cross_owner.status_code == 404
    finally:
        engine.dispose()
