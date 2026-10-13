from __future__ import annotations

import io
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.integrations.storage.local import LocalPrivateStorage
from app.integrations.storage.protocol import (
    ImmutableObjectConflict,
    StorageError,
    UnsafeStorageKey,
)


def test_local_storage_immutable_put_read_stat_and_idempotent_delete(tmp_path) -> None:
    storage = LocalPrivateStorage(tmp_path / "private")
    body = b"private image bytes"
    metadata = {"sha256": "example", "size_bytes": len(body), "mime_type": "image/png"}

    first = storage.put_immutable(
        "users/u1/references/s1/PRIMARY/a.png", io.BytesIO(body), metadata
    )
    second = storage.put_immutable(
        "users/u1/references/s1/PRIMARY/a.png", io.BytesIO(body), metadata
    )

    assert first.sha256 == second.sha256
    assert first.size_bytes == len(body)
    assert dict(storage.stat(first.key).metadata) == metadata
    with storage.open_reader(first.key) as reader:
        assert reader.read() == body

    storage.delete(first.key)
    storage.delete(first.key)
    assert storage.stat(first.key) is None


def test_local_storage_refuses_overwrite_with_different_content(tmp_path) -> None:
    storage = LocalPrivateStorage(tmp_path / "private")
    storage.put_immutable("assets/a.webp", io.BytesIO(b"first"), {})

    with pytest.raises(ImmutableObjectConflict):
        storage.put_immutable("assets/a.webp", io.BytesIO(b"second"), {})

    with storage.open_reader("assets/a.webp") as reader:
        assert reader.read() == b"first"


@pytest.mark.parametrize("key", ["../outside", "a/../outside", "/absolute", "a\\b"])
def test_local_storage_rejects_unsafe_keys(tmp_path, key: str) -> None:
    storage = LocalPrivateStorage(tmp_path / "private")

    with pytest.raises(UnsafeStorageKey):
        storage.stat(key)


def test_local_storage_rejects_symlink_escape_when_supported(tmp_path) -> None:
    storage = LocalPrivateStorage(tmp_path / "private")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = storage.root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable in this environment")

    with pytest.raises(UnsafeStorageKey):
        storage.put_immutable("escape/file.bin", io.BytesIO(b"no"), {})
    assert not (outside / "file.bin").exists()


def test_local_inventory_classifies_and_deletes_individual_entries(tmp_path) -> None:
    storage = LocalPrivateStorage(tmp_path / "private")
    object_key = "users/u1/generated/task1/item1.webp"
    storage.put_immutable(object_key, io.BytesIO(b"image"), {"kind": "generated"})
    staging_path = storage.root / "users/u1/.incoming-stale"
    metadata_temp_path = storage.root / "generated/task1/.metadata-stale"
    staging_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_temp_path.parent.mkdir(parents=True)
    staging_path.write_bytes(b"partial")
    metadata_temp_path.write_bytes(b"partial metadata")

    entries = list(
        storage.iter_inventory(
            prefixes=("users/", "generated/"),
            modified_before=datetime.now(UTC) + timedelta(minutes=1),
        )
    )
    by_locator = {entry.relative_locator: entry for entry in entries}

    assert by_locator[object_key].kind == "object"
    assert by_locator[object_key].object_key == object_key
    sidecar_locator = "users/u1/generated/task1/.item1.webp.metadata.json"
    assert by_locator[sidecar_locator].kind == "sidecar"
    assert by_locator[sidecar_locator].object_key == object_key
    assert by_locator["users/u1/.incoming-stale"].kind == "staging"
    assert by_locator["generated/task1/.metadata-stale"].kind == "staging"

    storage.delete_inventory_entry(by_locator[sidecar_locator])
    assert not (storage.root / sidecar_locator).exists()
    assert (storage.root / object_key).exists()

    storage.delete_inventory_entry(by_locator["users/u1/.incoming-stale"])
    assert not staging_path.exists()


def test_local_inventory_rejects_unapproved_prefix_and_unsafe_locator(tmp_path) -> None:
    from app.integrations.storage.protocol import StorageInventoryEntry

    storage = LocalPrivateStorage(tmp_path / "private")
    with pytest.raises(UnsafeStorageKey):
        list(storage.iter_inventory(prefixes=("assets/",), modified_before=datetime.now(UTC)))

    entry = StorageInventoryEntry(
        kind="object",
        relative_locator="users/../outside.png",
        object_key="users/../outside.png",
        modified_at=datetime.now(UTC),
        size_bytes=1,
    )
    with pytest.raises(UnsafeStorageKey):
        storage.delete_inventory_entry(entry)


def test_local_inventory_pages_have_stable_sorted_cursors_and_scan_skips(
    tmp_path,
) -> None:
    storage = LocalPrivateStorage(tmp_path / "private")
    old_cutoff = datetime.now(UTC) - timedelta(minutes=1)
    first = storage.root / "users" / "u1" / "a.png"
    skipped = storage.root / "users" / "u1" / "b.png"
    last = storage.root / "users" / "u1" / "c.png"
    for path in (last, skipped, first):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.name.encode())
    old_timestamp = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    for path in (first, last):
        os.utime(path, (old_timestamp, old_timestamp))

    page_one = storage.iter_inventory_page(
        prefixes=("users/",),
        modified_before=old_cutoff,
        max_scanned_items=1,
    )
    assert [entry.relative_locator for entry in page_one.entries] == ["users/u1/a.png"]
    assert page_one.next_cursor == "users/u1/a.png"
    assert page_one.scanned_items == 1
    assert page_one.exhausted is False

    page_two = storage.iter_inventory_page(
        prefixes=("users/",),
        modified_before=old_cutoff,
        after_locator=page_one.next_cursor,
        max_scanned_items=1,
    )
    assert page_two.entries == ()  # The recent file is scanned but filtered by age.
    assert page_two.next_cursor == "users/u1/b.png"
    assert page_two.scanned_items == 1
    assert page_two.exhausted is False

    page_three = storage.iter_inventory_page(
        prefixes=("users/",),
        modified_before=old_cutoff,
        after_locator=page_two.next_cursor,
        max_scanned_items=1,
    )
    assert [entry.relative_locator for entry in page_three.entries] == ["users/u1/c.png"]
    assert page_three.next_cursor == "users/u1/c.png"
    assert page_three.scanned_items == 1
    assert page_three.exhausted is True


def test_local_inventory_page_advances_cursor_over_unclassified_files_and_symlinks(
    tmp_path,
) -> None:
    storage = LocalPrivateStorage(tmp_path / "private")
    ignored = storage.root / "generated" / "a file.txt"
    final = storage.root / "generated" / "b.webp"
    ignored.parent.mkdir(parents=True)
    ignored.write_text("ignored", encoding="utf-8")
    final.write_bytes(b"valid")
    old_timestamp = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    os.utime(ignored, (old_timestamp, old_timestamp))
    os.utime(final, (old_timestamp, old_timestamp))

    page = storage.iter_inventory_page(
        prefixes=("generated/",),
        modified_before=datetime.now(UTC),
        max_scanned_items=1,
    )
    assert page.entries == ()
    assert page.next_cursor == "generated/a file.txt"
    assert page.scanned_items == 1
    assert page.exhausted is False

    next_page = storage.iter_inventory_page(
        prefixes=("generated/",),
        modified_before=datetime.now(UTC),
        after_locator=page.next_cursor,
        max_scanned_items=1,
    )
    assert [entry.relative_locator for entry in next_page.entries] == ["generated/b.webp"]
    assert next_page.next_cursor == "generated/b.webp"


def test_local_inventory_page_does_not_follow_directory_symlinks(tmp_path) -> None:
    storage = LocalPrivateStorage(tmp_path / "private")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"outside")
    link = storage.root / "generated" / "a-link"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable in this environment")
    valid = storage.root / "generated" / "b.png"
    valid.write_bytes(b"inside")
    old_timestamp = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    os.utime(valid, (old_timestamp, old_timestamp))

    first = storage.iter_inventory_page(
        prefixes=("generated/",),
        modified_before=datetime.now(UTC),
        max_scanned_items=1,
    )
    assert first.entries == ()
    assert first.next_cursor == "generated/a-link"
    assert first.scanned_items == 1
    assert first.exhausted is False

    second = storage.iter_inventory_page(
        prefixes=("generated/",),
        modified_before=datetime.now(UTC),
        after_locator=first.next_cursor,
        max_scanned_items=1,
    )
    assert [entry.relative_locator for entry in second.entries] == ["generated/b.png"]
    assert all("secret.png" not in entry.relative_locator for entry in second.entries)


def test_local_inventory_page_rejects_invalid_cursor_and_scan_limit(tmp_path) -> None:
    storage = LocalPrivateStorage(tmp_path / "private")
    with pytest.raises(UnsafeStorageKey):
        storage.iter_inventory_page(
            prefixes=("users/",),
            modified_before=datetime.now(UTC),
            after_locator="generated/a.png",
        )
    with pytest.raises(ValueError):
        storage.iter_inventory_page(
            prefixes=("users/",),
            modified_before=datetime.now(UTC),
            max_scanned_items=0,
        )


def test_local_inventory_page_prunes_completed_path_subtrees(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalPrivateStorage(tmp_path / "private")
    for locator in (
        "generated/task/a.png",
        "users/a0/file.png",
        "users/z9/file.png",
    ):
        path = storage.root / locator
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"item")

    visited: list[str] = []
    original = storage._iter_directory_locators

    def track_visited(directory, *, cursor_key):
        visited.append(directory.relative_to(storage.root).as_posix())
        yield from original(directory, cursor_key=cursor_key)

    monkeypatch.setattr(storage, "_iter_directory_locators", track_visited)
    page = storage.iter_inventory_page(
        prefixes=("users/", "generated/"),
        modified_before=datetime.now(UTC) + timedelta(minutes=1),
        after_locator="users/m0/previous.png",
        max_scanned_items=10,
    )

    assert "generated" not in visited
    assert "users/a0" not in visited
    assert "users/z9" in visited
    assert [entry.relative_locator for entry in page.entries] == ["users/z9/file.png"]
    assert page.exhausted is True


def test_local_inventory_page_skips_missing_prefix_and_raises_on_scan_io_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalPrivateStorage(tmp_path / "private")
    missing_prefix = storage.iter_inventory_page(
        prefixes=("users/",),
        modified_before=datetime.now(UTC),
    )
    assert missing_prefix.entries == ()
    assert missing_prefix.next_cursor is None
    assert missing_prefix.exhausted is True

    root = storage.root / "users"
    root.mkdir()
    original_scandir = os.scandir

    def fail_root_scan(path):
        if Path(path) == root:
            raise PermissionError("private path")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", fail_root_scan)
    with pytest.raises(StorageError, match="inventory directory cannot be read"):
        storage.iter_inventory_page(
            prefixes=("users/",),
            modified_before=datetime.now(UTC),
        )


def test_local_inventory_page_raises_on_child_metadata_io_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalPrivateStorage(tmp_path / "private")
    root = storage.root / "generated"
    root.mkdir()

    class BrokenEntry:
        name = "child.png"
        path = str(root / name)

        @staticmethod
        def is_symlink() -> bool:
            raise PermissionError("private path")

    class FakeScan:
        def __enter__(self):
            return iter((BrokenEntry(),))

        @staticmethod
        def __exit__(exc_type, exc, traceback) -> bool:
            return False

    monkeypatch.setattr(os, "scandir", lambda _path: FakeScan())
    with pytest.raises(StorageError, match="inventory child cannot be inspected"):
        storage.iter_inventory_page(
            prefixes=("generated/",),
            modified_before=datetime.now(UTC),
        )


def test_local_inventory_page_skips_file_disappearing_during_inspection(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalPrivateStorage(tmp_path / "private")
    target = storage.root / "users" / "missing.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"gone during scan")
    original_lstat = Path.lstat

    def disappear(path: Path):
        if path == target:
            raise FileNotFoundError
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", disappear)
    page = storage.iter_inventory_page(
        prefixes=("users/",),
        modified_before=datetime.now(UTC),
    )
    assert page.entries == ()
    assert page.next_cursor == "users/missing.png"
    assert page.scanned_items == 1
    assert page.exhausted is True
