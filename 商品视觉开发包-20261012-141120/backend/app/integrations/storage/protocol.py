from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import BinaryIO, Literal, Protocol, runtime_checkable


class StorageError(RuntimeError):
    """Storage operation failed without exposing a provider path or credential."""


class UnsafeStorageKey(StorageError):
    pass


class ImmutableObjectConflict(StorageError):
    pass


@dataclass(frozen=True, slots=True)
class ObjectStat:
    key: str
    size_bytes: int
    sha256: str
    metadata: Mapping[str, str | int]

    @classmethod
    def create(
        cls,
        *,
        key: str,
        size_bytes: int,
        sha256: str,
        metadata: Mapping[str, str | int],
    ) -> ObjectStat:
        return cls(key, size_bytes, sha256, MappingProxyType(dict(metadata)))


@dataclass(frozen=True, slots=True)
class StorageInventoryEntry:
    """A private, root-relative inventory item safe to persist during cleanup."""

    kind: Literal["object", "staging", "sidecar"]
    relative_locator: str
    object_key: str | None
    modified_at: datetime
    size_bytes: int


@dataclass(frozen=True, slots=True)
class StorageInventoryPage:
    """Bounded entries plus a cursor after every locator scanned.

    `scanned_items` counts candidates even when age, symlink, or name checks omit
    them from `entries`. `next_cursor` therefore advances past skipped items too.
    The page bounds returned entries and caller target insertion, not all filesystem
    enumeration or temporary memory used to sort one directory's direct children.
    """

    entries: tuple[StorageInventoryEntry, ...]
    next_cursor: str | None
    exhausted: bool
    scanned_items: int


@runtime_checkable
class StorageInventory(Protocol):
    """Optional inventory capability; it is deliberately not part of Storage.

    Page order is stable by relative locator path components. Resume with the
    previous page's `next_cursor`; the cursor covers all scanned candidates,
    including entries filtered out by the adapter or caller. Local storage prunes
    completed path subtrees but lists and sorts siblings in each visited directory;
    very high fanout in one directory is not strictly bounded and is not a production
    scale or performance guarantee.
    """

    def iter_inventory(
        self, *, prefixes: Sequence[str], modified_before: datetime
    ) -> Iterable[StorageInventoryEntry]: ...

    def iter_inventory_page(
        self,
        *,
        prefixes: Sequence[str],
        modified_before: datetime,
        after_locator: str | None,
        max_scanned_items: int,
    ) -> StorageInventoryPage: ...

    def delete_inventory_entry(self, entry: StorageInventoryEntry) -> None: ...


class Storage(Protocol):
    """Private, replaceable object storage contract used by synchronous services."""

    def put_immutable(
        self,
        key: str,
        stream: BinaryIO,
        metadata: Mapping[str, str | int],
    ) -> ObjectStat: ...

    def open_reader(self, key: str) -> BinaryIO: ...

    def stat(self, key: str) -> ObjectStat | None: ...

    def delete(self, key: str) -> None: ...
