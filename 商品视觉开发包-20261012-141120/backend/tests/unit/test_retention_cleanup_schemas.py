from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.db.retention_models import CleanupRun
from app.integrations.storage.protocol import (
    StorageError,
    StorageInventoryEntry,
    StorageInventoryPage,
)
from app.schemas.retention import (
    CleanupRunRequest,
    RetentionPolicyResponse,
    RetentionPolicyUpdateRequest,
)
from app.services.retention_cleanup import (
    _run_response,
    _safe_inventory_entry,
    _validate_inventory_page,
)
from pydantic import ValidationError


def test_policy_response_exposes_the_frozen_asia_shanghai_schedule_default() -> None:
    response = RetentionPolicyResponse(
        revision=1,
        task_retention_days=60,
        reference_retention_days=60,
        recommendation_retention_days=60,
    )

    assert response.schedule_timezone == "Asia/Shanghai"
    assert response.schedule_daily_time == "03:00"


def test_policy_update_is_future_only_and_rejects_schedule_changes() -> None:
    request = RetentionPolicyUpdateRequest(
        expected_revision=1,
        task_retention_days=30,
        reference_retention_days=40,
        recommendation_retention_days=50,
    )
    assert request.apply_to_existing is False
    with pytest.raises(ValidationError):
        RetentionPolicyUpdateRequest.model_validate(
            {
                "expected_revision": 1,
                "task_retention_days": 30,
                "reference_retention_days": 40,
                "recommendation_retention_days": 50,
                "schedule_timezone": "UTC",
            }
        )


def test_manual_cleanup_request_has_no_unpersisted_scope_fields() -> None:
    assert CleanupRunRequest().model_dump() == {}
    with pytest.raises(ValidationError):
        CleanupRunRequest.model_validate({"kinds": ["task"]})


@pytest.mark.parametrize(
    "entry",
    [
        StorageInventoryEntry(
            kind="object",
            relative_locator="generated/abc123/result.png",
            object_key="generated/abc123/result.png",
            modified_at=datetime.now(UTC),
            size_bytes=12,
        ),
        StorageInventoryEntry(
            kind="staging",
            relative_locator="users/owner/.incoming-build",
            object_key=None,
            modified_at=datetime.now(UTC),
            size_bytes=0,
        ),
        StorageInventoryEntry(
            kind="sidecar",
            relative_locator="users/owner/.image.png.metadata.json",
            object_key="users/owner/image.png",
            modified_at=datetime.now(UTC),
            size_bytes=30,
        ),
    ],
)
def test_inventory_entries_must_be_root_relative_and_well_formed(
    entry: StorageInventoryEntry,
) -> None:
    assert _safe_inventory_entry(entry)


@pytest.mark.parametrize(
    "entry",
    [
        StorageInventoryEntry(
            kind="object",
            relative_locator="../secrets.txt",
            object_key="../secrets.txt",
            modified_at=datetime.now(UTC),
            size_bytes=12,
        ),
        StorageInventoryEntry(
            kind="staging",
            relative_locator="C:/private/file",
            object_key=None,
            modified_at=datetime.now(UTC),
            size_bytes=12,
        ),
        StorageInventoryEntry(
            kind="object",
            relative_locator="users/owner/object.png",
            object_key="generated/another/object.png",
            modified_at=datetime.now(UTC),
            size_bytes=12,
        ),
    ],
)
def test_inventory_entries_reject_escape_and_key_mismatches(
    entry: StorageInventoryEntry,
) -> None:
    assert not _safe_inventory_entry(entry)


def test_inventory_pages_require_monotonic_bounded_cursor_and_entry_order() -> None:
    first = StorageInventoryEntry(
        kind="object",
        relative_locator="users/u-a/objects/a.png",
        object_key="users/u-a/objects/a.png",
        modified_at=datetime.now(UTC),
        size_bytes=1,
    )
    second = StorageInventoryEntry(
        kind="object",
        relative_locator="users/u-a/objects/b.png",
        object_key="users/u-a/objects/b.png",
        modified_at=datetime.now(UTC),
        size_bytes=1,
    )
    _validate_inventory_page(
        StorageInventoryPage(
            entries=(first, second),
            next_cursor="users/u-a/objects/c.png",
            exhausted=False,
            scanned_items=3,
        ),
        expected_cursor="users/u-a/objects/0.png",
        max_scanned_items=3,
    )

    with pytest.raises(StorageError, match="cursor did not advance"):
        _validate_inventory_page(
            StorageInventoryPage(
                entries=(first,),
                next_cursor="users/u-a/objects/0.png",
                exhausted=False,
                scanned_items=1,
            ),
            expected_cursor="users/u-a/objects/0.png",
            max_scanned_items=1,
        )

    with pytest.raises(StorageError, match="locators are not in cursor order"):
        _validate_inventory_page(
            StorageInventoryPage(
                entries=(second, first),
                next_cursor="users/u-a/objects/c.png",
                exhausted=True,
                scanned_items=2,
            ),
            expected_cursor=None,
            max_scanned_items=2,
        )

    with pytest.raises(StorageError, match="cannot advance its cursor"):
        _validate_inventory_page(
            StorageInventoryPage(
                entries=(),
                next_cursor="users/u-a/objects/a.png",
                exhausted=True,
                scanned_items=0,
            ),
            expected_cursor=None,
            max_scanned_items=2,
        )


def test_run_summary_never_serializes_trigger_key_or_object_locator() -> None:
    run = CleanupRun(
        id=uuid4(),
        trigger_kind="manual",
        trigger_key="private-trigger-key",
        policy_revision=1,
        status="completed",
        counts={
            "tasks_deleted": 1,
            "jobs_completed": 1,
            "inventory_available": 1,
            "reason:INVENTORY_SCAN_FAILED": 2,
            "reason:INVENTORY_UNAVAILABLE": 1,
        },
        orphan_scan_cursor="generated/private/path.png",
        created_at=datetime.now(UTC),
    )

    response = _run_response(run, pending_jobs=0)
    payload = response.model_dump_json()
    reason_counts = {reason.code: reason.count for reason in response.counters.reason_counts}

    assert "private-trigger-key" not in payload
    assert "object_key" not in payload
    assert "orphan_scan_cursor" not in payload
    assert "generated/private/path.png" not in payload
    assert "tasks_deleted" in payload
    assert reason_counts == {"INVENTORY_SCAN_FAILED": 2, "INVENTORY_UNAVAILABLE": 1}
