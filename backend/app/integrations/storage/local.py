from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from app.integrations.storage.protocol import (
    ImmutableObjectConflict,
    ObjectStat,
    StorageError,
    StorageInventoryEntry,
    StorageInventoryPage,
    UnsafeStorageKey,
)

_KEY_SEGMENT = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}$")
_INCOMING_FILE = re.compile(r"^\.incoming-[A-Za-z0-9_-]+$")
_METADATA_TEMP_FILE = re.compile(r"^\.metadata-[A-Za-z0-9_-]+$")
_SIDECAR_FILE = re.compile(r"^\.([A-Za-z0-9_-][A-Za-z0-9_.-]{0,127})\.metadata\.json$")
_READ_SIZE = 128 * 1024
_INVENTORY_PREFIXES = frozenset({"users/", "generated/"})


class LocalPrivateStorage:
    """Private development storage rooted at one fixed directory."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        configured_root = Path(root).expanduser()
        configured_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._root = configured_root.resolve(strict=True)
        if not self._root.is_dir():
            raise ValueError("storage root must be a directory")

    @property
    def root(self) -> Path:
        """Internal diagnostic path; never include it in public DTOs."""
        return self._root

    def put_immutable(
        self,
        key: str,
        stream: BinaryIO,
        metadata: Mapping[str, str | int],
    ) -> ObjectStat:
        destination = self._path_for_key(key, create_parents=True)
        sidecar = self._metadata_path(destination)
        safe_metadata = self._normalize_metadata(metadata)
        digest = hashlib.sha256()
        byte_count = 0
        temp_path: Path | None = None

        try:
            descriptor, temp_name = tempfile.mkstemp(prefix=".incoming-", dir=destination.parent)
            temp_path = Path(temp_name)
            with os.fdopen(descriptor, "wb") as output:
                while chunk := stream.read(_READ_SIZE):
                    if not isinstance(chunk, bytes):
                        raise StorageError("storage stream must yield bytes")
                    output.write(chunk)
                    digest.update(chunk)
                    byte_count += len(chunk)
                output.flush()
                os.fsync(output.fileno())

            checksum = digest.hexdigest()
            if destination.exists():
                current = self.stat(key)
                if current is None:
                    raise StorageError("stored object disappeared during write")
                if current.sha256 != checksum:
                    raise ImmutableObjectConflict(
                        "immutable object key already has different content"
                    )
                if current.metadata and dict(current.metadata) != safe_metadata:
                    raise ImmutableObjectConflict("immutable object metadata differs")
                self._write_metadata_if_missing(sidecar, safe_metadata)
                return self.stat(key) or ObjectStat.create(
                    key=key,
                    size_bytes=byte_count,
                    sha256=checksum,
                    metadata=safe_metadata,
                )

            try:
                os.link(temp_path, destination)
            except FileExistsError:
                current = self.stat(key)
                if current is None or current.sha256 != checksum:
                    raise ImmutableObjectConflict(
                        "immutable object key already has different content"
                    ) from None
                if current.metadata and dict(current.metadata) != safe_metadata:
                    raise ImmutableObjectConflict("immutable object metadata differs") from None
            self._write_metadata_if_missing(sidecar, safe_metadata)
            return ObjectStat.create(
                key=key,
                size_bytes=byte_count,
                sha256=checksum,
                metadata=safe_metadata,
            )
        except (ImmutableObjectConflict, UnsafeStorageKey):
            raise
        except OSError:
            raise StorageError("local object write failed") from None
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def open_reader(self, key: str) -> BinaryIO:
        path = self._path_for_key(key)
        try:
            if not path.is_file():
                raise FileNotFoundError
            return path.open("rb")
        except UnsafeStorageKey:
            raise
        except OSError:
            raise StorageError("stored object cannot be opened") from None

    def stat(self, key: str) -> ObjectStat | None:
        path = self._path_for_key(key)
        try:
            if not path.exists():
                return None
            if path.is_symlink() or not path.is_file():
                raise UnsafeStorageKey("object path is not a regular file")
            digest = hashlib.sha256()
            size_bytes = 0
            with path.open("rb") as stored:
                while chunk := stored.read(_READ_SIZE):
                    digest.update(chunk)
                    size_bytes += len(chunk)
            metadata = self._read_metadata(self._metadata_path(path))
            return ObjectStat.create(
                key=key,
                size_bytes=size_bytes,
                sha256=digest.hexdigest(),
                metadata=metadata,
            )
        except (UnsafeStorageKey, StorageError):
            raise
        except OSError:
            raise StorageError("stored object metadata cannot be read") from None

    def delete(self, key: str) -> None:
        path = self._path_for_key(key)
        sidecar = self._metadata_path(path)
        try:
            if path.is_symlink() or sidecar.is_symlink():
                raise UnsafeStorageKey("object path is not a regular file")
            path.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
        except (UnsafeStorageKey, StorageError):
            raise
        except OSError:
            raise StorageError("stored object could not be deleted") from None

    def iter_inventory(
        self, *, prefixes: Sequence[str], modified_before: datetime
    ) -> Iterable[StorageInventoryEntry]:
        cutoff = _aware_utc(modified_before)
        selected_prefixes = self._validate_inventory_prefixes(prefixes)
        for relative_locator in self._iter_inventory_locators(selected_prefixes):
            entry = self._inventory_entry_at(relative_locator, cutoff)
            if entry is not None:
                yield entry

    def iter_inventory_page(
        self,
        *,
        prefixes: Sequence[str],
        modified_before: datetime,
        after_locator: str | None = None,
        max_scanned_items: int = 100,
    ) -> StorageInventoryPage:
        cutoff = _aware_utc(modified_before)
        selected_prefixes = self._validate_inventory_prefixes(prefixes)
        self._validate_inventory_cursor(after_locator, selected_prefixes)
        if (
            isinstance(max_scanned_items, bool)
            or not isinstance(max_scanned_items, int)
            or not 1 <= max_scanned_items <= 1000
        ):
            raise ValueError("max_scanned_items must be between 1 and 1000")

        cursor_key = _inventory_sort_key(after_locator) if after_locator is not None else None
        entries: list[StorageInventoryEntry] = []
        next_cursor = after_locator
        scanned_items = 0
        exhausted = True
        for relative_locator in self._iter_inventory_locators(
            selected_prefixes, after_locator=after_locator
        ):
            if cursor_key is not None and _inventory_sort_key(relative_locator) <= cursor_key:
                continue
            if scanned_items == max_scanned_items:
                exhausted = False
                break
            scanned_items += 1
            next_cursor = relative_locator
            entry = self._inventory_entry_at(relative_locator, cutoff)
            if entry is not None:
                entries.append(entry)

        return StorageInventoryPage(
            entries=tuple(entries),
            next_cursor=next_cursor,
            exhausted=exhausted,
            scanned_items=scanned_items,
        )

    @staticmethod
    def _validate_inventory_prefixes(prefixes: Sequence[str]) -> tuple[str, ...]:
        if any(prefix not in _INVENTORY_PREFIXES for prefix in prefixes):
            raise UnsafeStorageKey("inventory prefix is not allowed")
        return tuple(sorted(set(prefixes)))

    @staticmethod
    def _validate_inventory_cursor(after_locator: str | None, prefixes: Sequence[str]) -> None:
        if after_locator is None:
            return
        if not isinstance(after_locator, str) or after_locator.startswith("/"):
            raise UnsafeStorageKey("invalid inventory cursor")
        parts = after_locator.split("/")
        if (
            len(parts) < 2
            or f"{parts[0]}/" not in prefixes
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise UnsafeStorageKey("invalid inventory cursor")

    def _iter_inventory_locators(
        self, prefixes: Sequence[str], *, after_locator: str | None = None
    ) -> Iterable[str]:
        cursor_key = _inventory_sort_key(after_locator) if after_locator is not None else None
        for prefix in prefixes:
            prefix_parts = (prefix[:-1],)
            if cursor_key is not None and not _inventory_subtree_after_cursor(
                prefix_parts, cursor_key
            ):
                continue
            root = self._root / prefix[:-1]
            try:
                root_stat = root.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                raise StorageError("inventory root cannot be inspected") from None
            if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
                continue
            yield from self._iter_directory_locators(root, cursor_key=cursor_key)

    def _iter_directory_locators(
        self, directory: Path, *, cursor_key: tuple[str, ...] | None
    ) -> Iterable[str]:
        try:
            with os.scandir(directory) as scan:
                children = sorted(scan, key=lambda child: child.name)
        except FileNotFoundError:
            return
        except OSError:
            raise StorageError("inventory directory cannot be read") from None
        for child in children:
            path = Path(child.path)
            try:
                is_symlink = child.is_symlink()
            except FileNotFoundError:
                continue
            except OSError:
                raise StorageError("inventory child cannot be inspected") from None
            if is_symlink:
                # A symlink is a scanned-but-skipped item; never traverse it.
                yield path.relative_to(self._root).as_posix()
                continue
            try:
                is_directory = child.is_dir(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                raise StorageError("inventory child cannot be inspected") from None
            if is_directory:
                relative_parts = tuple(path.relative_to(self._root).parts)
                if cursor_key is not None and not _inventory_subtree_after_cursor(
                    relative_parts, cursor_key
                ):
                    continue
                yield from self._iter_directory_locators(path, cursor_key=cursor_key)
            else:
                yield path.relative_to(self._root).as_posix()

    def _inventory_entry_at(
        self, relative_locator: str, cutoff: datetime
    ) -> StorageInventoryEntry | None:
        path = self._root.joinpath(*relative_locator.split("/"))
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            raise StorageError("inventory entry cannot be inspected") from None
        if (
            stat.S_ISLNK(file_stat.st_mode)
            or not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_mtime >= cutoff.timestamp()
        ):
            return None
        return self._inventory_entry(
            relative_locator=relative_locator,
            modified_at=datetime.fromtimestamp(file_stat.st_mtime, UTC),
            size_bytes=file_stat.st_size,
        )

    def delete_inventory_entry(self, entry: StorageInventoryEntry) -> None:
        path = self._path_for_inventory_entry(entry)
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise StorageError("inventory entry cannot be inspected") from None
        if path.is_symlink() or not path.is_file():
            raise UnsafeStorageKey("inventory entry is not a regular file")
        if not stat.S_ISREG(file_stat.st_mode):
            raise UnsafeStorageKey("inventory entry is not a regular file")
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            raise StorageError("inventory entry could not be deleted") from None

    def _inventory_entry(
        self, *, relative_locator: str, modified_at: datetime, size_bytes: int
    ) -> StorageInventoryEntry | None:
        parts = PurePosixPath(relative_locator).parts
        if len(parts) < 2 or f"{parts[0]}/" not in _INVENTORY_PREFIXES:
            return None
        filename = parts[-1]
        if _INCOMING_FILE.fullmatch(filename) or _METADATA_TEMP_FILE.fullmatch(filename):
            return StorageInventoryEntry(
                kind="staging",
                relative_locator=relative_locator,
                object_key=None,
                modified_at=modified_at,
                size_bytes=size_bytes,
            )
        sidecar_match = _SIDECAR_FILE.fullmatch(filename)
        if sidecar_match is not None:
            object_key = "/".join((*parts[:-1], sidecar_match.group(1)))
            return StorageInventoryEntry(
                kind="sidecar",
                relative_locator=relative_locator,
                object_key=object_key,
                modified_at=modified_at,
                size_bytes=size_bytes,
            )
        if not _KEY_SEGMENT.fullmatch(filename):
            return None
        return StorageInventoryEntry(
            kind="object",
            relative_locator=relative_locator,
            object_key=relative_locator,
            modified_at=modified_at,
            size_bytes=size_bytes,
        )

    def _path_for_inventory_entry(self, entry: StorageInventoryEntry) -> Path:
        locator = entry.relative_locator
        if not isinstance(locator, str) or "\\" in locator:
            raise UnsafeStorageKey("invalid inventory locator")
        parts = locator.split("/")
        if (
            len(parts) < 2
            or f"{parts[0]}/" not in _INVENTORY_PREFIXES
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise UnsafeStorageKey("invalid inventory locator")
        if any(not _KEY_SEGMENT.fullmatch(part) for part in parts[1:-1]):
            raise UnsafeStorageKey("invalid inventory locator")

        filename = parts[-1]
        expected_object_key: str | None = None
        if entry.kind == "object" and _KEY_SEGMENT.fullmatch(filename):
            expected_object_key = locator
        elif entry.kind == "staging" and (
            _INCOMING_FILE.fullmatch(filename) or _METADATA_TEMP_FILE.fullmatch(filename)
        ):
            if entry.object_key is not None:
                raise UnsafeStorageKey("staging inventory entry cannot name an object")
        elif entry.kind == "sidecar":
            sidecar_match = _SIDECAR_FILE.fullmatch(filename)
            if sidecar_match is None:
                raise UnsafeStorageKey("invalid sidecar inventory locator")
            expected_object_key = "/".join((*parts[:-1], sidecar_match.group(1)))
        else:
            raise UnsafeStorageKey("invalid inventory entry kind")

        if expected_object_key is not None:
            if entry.object_key != expected_object_key:
                raise UnsafeStorageKey("inventory object key does not match its locator")
            self._path_for_key(expected_object_key)

        path = self._root.joinpath(*parts)
        self._assert_no_symlink_parents(path)
        if not path.resolve(strict=False).is_relative_to(self._root):
            raise UnsafeStorageKey("inventory locator escapes storage root")
        return path

    def _assert_no_symlink_parents(self, path: Path) -> None:
        try:
            relative = path.relative_to(self._root)
        except ValueError:
            raise UnsafeStorageKey("inventory locator escapes storage root") from None
        current = self._root
        for part in relative.parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise UnsafeStorageKey("inventory path traverses a symbolic link")
            if current.exists() and not current.is_dir():
                raise UnsafeStorageKey("inventory parent is not a directory")

    def _path_for_key(self, key: str, *, create_parents: bool = False) -> Path:
        if not isinstance(key, str) or not key or "\\" in key:
            raise UnsafeStorageKey("invalid object key")
        parts = key.split("/")
        if any(part in {"", ".", ".."} or not _KEY_SEGMENT.fullmatch(part) for part in parts):
            raise UnsafeStorageKey("invalid object key")

        current = self._root
        for part in parts[:-1]:
            current = current / part
            try:
                if current.is_symlink():
                    raise UnsafeStorageKey("object key traverses a symbolic link")
                if create_parents:
                    current.mkdir(mode=0o700, exist_ok=True)
                resolved = current.resolve(strict=False)
            except OSError:
                raise StorageError("storage directory is unavailable") from None
            if not resolved.is_relative_to(self._root):
                raise UnsafeStorageKey("object key escapes storage root")
            current = resolved

        destination = current / parts[-1]
        try:
            if destination.is_symlink():
                raise UnsafeStorageKey("object key resolves to a symbolic link")
            resolved_destination = destination.resolve(strict=False)
        except OSError:
            raise StorageError("storage path is unavailable") from None
        if not resolved_destination.is_relative_to(self._root):
            raise UnsafeStorageKey("object key escapes storage root")
        return resolved_destination

    @staticmethod
    def _metadata_path(path: Path) -> Path:
        return path.with_name(f".{path.name}.metadata.json")

    @staticmethod
    def _normalize_metadata(metadata: Mapping[str, str | int]) -> dict[str, str | int]:
        result: dict[str, str | int] = {}
        for key, value in metadata.items():
            if not isinstance(key, str) or not key or any(ord(char) < 32 for char in key):
                raise ValueError("metadata keys must be non-empty safe text")
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ValueError("metadata values must be strings or integers")
            result[key] = value
        return result

    def _write_metadata_if_missing(self, sidecar: Path, metadata: Mapping[str, str | int]) -> None:
        self._assert_safe_sidecar(sidecar)
        if sidecar.exists():
            stored = self._read_metadata(sidecar)
            if stored and dict(stored) != dict(metadata):
                raise ImmutableObjectConflict("immutable object metadata differs")
            return

        payload = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
        descriptor, temp_name = tempfile.mkstemp(prefix=".metadata-", dir=sidecar.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temp_path, sidecar)
            except FileExistsError:
                stored = self._read_metadata(sidecar)
                if dict(stored) != dict(metadata):
                    raise ImmutableObjectConflict("immutable object metadata differs") from None
        finally:
            temp_path.unlink(missing_ok=True)

    def _read_metadata(self, sidecar: Path) -> Mapping[str, str | int]:
        self._assert_safe_sidecar(sidecar)
        try:
            if not sidecar.exists():
                return {}
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError
            return self._normalize_metadata(data)
        except (OSError, UnicodeError, ValueError, TypeError):
            raise StorageError("stored object metadata is invalid") from None

    def _assert_safe_sidecar(self, sidecar: Path) -> None:
        try:
            if sidecar.is_symlink() or not sidecar.resolve(strict=False).is_relative_to(self._root):
                raise UnsafeStorageKey("object metadata path escapes storage root")
        except OSError:
            raise StorageError("storage metadata path is unavailable") from None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("inventory cutoff must be timezone-aware")
    return value.astimezone(UTC)


def _inventory_sort_key(relative_locator: str) -> tuple[str, ...]:
    return tuple(relative_locator.split("/"))


def _inventory_subtree_after_cursor(
    directory_parts: tuple[str, ...], cursor_key: tuple[str, ...]
) -> bool:
    for directory_part, cursor_part in zip(directory_parts, cursor_key, strict=False):
        if directory_part < cursor_part:
            return False
        if directory_part > cursor_part:
            return True
    return True
