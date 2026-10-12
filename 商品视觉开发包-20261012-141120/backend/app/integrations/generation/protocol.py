"""Private generation adapter contract and immutable operation DTOs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

_SAFE_REASON = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
REQUEST_DIGEST_VERSION = 1


def _require_reason_code(value: str) -> None:
    if not isinstance(value, str) or not _SAFE_REASON.fullmatch(value):
        raise ValueError("reason_code must be a stable uppercase code")


def _require_outcome(value: str, expected: str) -> None:
    if value != expected:
        raise ValueError(f"outcome must be {expected}")


@dataclass(frozen=True, slots=True)
class GenerationCommand:
    """Private one-attempt input; `request_key` is the stable attempt UUID."""

    request_key: UUID
    image_type: str
    prompt_package_version: str
    prompt_text: str
    reference_asset_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.request_key, UUID):
            raise ValueError("request_key must be a UUID")
        if not isinstance(self.image_type, str) or not self.image_type.strip():
            raise ValueError("image_type must be non-empty text")
        if len(self.image_type) > 80:
            raise ValueError("image_type is too long")
        if (
            not isinstance(self.prompt_package_version, str)
            or not self.prompt_package_version.strip()
            or len(self.prompt_package_version) > 120
        ):
            raise ValueError("prompt_package_version must be non-empty bounded text")
        if not isinstance(self.prompt_text, str) or not self.prompt_text:
            raise ValueError("prompt_text must be non-empty text")
        if len(self.prompt_text.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("prompt_text exceeds the private command limit")
        if not isinstance(self.reference_asset_ids, tuple) or not 1 <= len(
            self.reference_asset_ids
        ) <= 4:
            raise ValueError("reference_asset_ids must contain one to four UUIDs")
        if any(not isinstance(asset_id, UUID) for asset_id in self.reference_asset_ids):
            raise ValueError("reference_asset_ids must contain UUIDs")
        if len(set(self.reference_asset_ids)) != len(self.reference_asset_ids):
            raise ValueError("reference_asset_ids must be unique")

    @property
    def request_digest(self) -> bytes:
        """SHA-256 of the v1 canonical input, excluding the idempotency key itself.

        The object has exactly the fields below. JSON uses UTF-8, recursively
        sorted object keys, compact separators, and unescaped Unicode. The
        `reference_asset_ids` array preserves caller order because that order is
        meaningful to the generation prompt.
        """

        canonical = {
            "image_type": self.image_type,
            "prompt_package_version": self.prompt_package_version,
            "prompt_text": self.prompt_text,
            "reference_asset_ids": [str(asset_id) for asset_id in self.reference_asset_ids],
        }
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).digest()

    @property
    def request_digest_version(self) -> int:
        return REQUEST_DIGEST_VERSION


@dataclass(frozen=True, slots=True)
class GenerationAccepted:
    outcome: Literal["accepted"]
    provider_job_id: str
    poll_after_seconds: int

    def __post_init__(self) -> None:
        _require_outcome(self.outcome, "accepted")
        if not self.provider_job_id or len(self.provider_job_id) > 320:
            raise ValueError("provider_job_id must be non-empty bounded text")
        if (
            isinstance(self.poll_after_seconds, bool)
            or not isinstance(self.poll_after_seconds, int)
            or self.poll_after_seconds < 0
        ):
            raise ValueError("poll_after_seconds must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class GenerationDefinitelyRejected:
    outcome: Literal["definitely_rejected"]
    reason_code: str

    def __post_init__(self) -> None:
        _require_outcome(self.outcome, "definitely_rejected")
        _require_reason_code(self.reason_code)


@dataclass(frozen=True, slots=True)
class GenerationUnknown:
    outcome: Literal["unknown"]
    reason_code: str
    reconcile_after_seconds: int

    def __post_init__(self) -> None:
        _require_outcome(self.outcome, "unknown")
        _require_reason_code(self.reason_code)
        if (
            isinstance(self.reconcile_after_seconds, bool)
            or not isinstance(self.reconcile_after_seconds, int)
            or self.reconcile_after_seconds <= 0
        ):
            raise ValueError("reconcile_after_seconds must be a positive integer")


type GenerationSubmitResult = GenerationAccepted | GenerationDefinitelyRejected | GenerationUnknown


@dataclass(frozen=True, slots=True)
class GenerationQueryPending:
    outcome: Literal["pending"]
    provider_job_id: str
    poll_after_seconds: int

    def __post_init__(self) -> None:
        _require_outcome(self.outcome, "pending")
        if not self.provider_job_id:
            raise ValueError("provider_job_id must be non-empty")
        if (
            isinstance(self.poll_after_seconds, bool)
            or not isinstance(self.poll_after_seconds, int)
            or self.poll_after_seconds <= 0
        ):
            raise ValueError("poll_after_seconds must be a positive integer")


@dataclass(frozen=True, slots=True)
class GenerationQuerySucceeded:
    outcome: Literal["succeeded"]
    provider_job_id: str
    result_ref: str
    result_digest: bytes

    def __post_init__(self) -> None:
        _require_outcome(self.outcome, "succeeded")
        if not self.provider_job_id or not self.result_ref:
            raise ValueError("successful query requires private job and result references")
        if not isinstance(self.result_digest, bytes) or len(self.result_digest) != 32:
            raise ValueError("result_digest must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class GenerationQueryFailed:
    outcome: Literal["definitely_failed"]
    provider_job_id: str
    reason_code: str

    def __post_init__(self) -> None:
        _require_outcome(self.outcome, "definitely_failed")
        _require_reason_code(self.reason_code)
        if not self.provider_job_id:
            raise ValueError("provider_job_id must be non-empty")


@dataclass(frozen=True, slots=True)
class GenerationQueryUnknown:
    outcome: Literal["unknown"]
    provider_job_id: str
    reason_code: str

    def __post_init__(self) -> None:
        _require_outcome(self.outcome, "unknown")
        _require_reason_code(self.reason_code)
        if not self.provider_job_id:
            raise ValueError("provider_job_id must be non-empty")


type GenerationQueryResult = (
    GenerationQueryPending
    | GenerationQuerySucceeded
    | GenerationQueryFailed
    | GenerationQueryUnknown
)


@dataclass(frozen=True, slots=True)
class GenerationRecoveryFound:
    outcome: Literal["found"]
    request_key: UUID
    provider_job_id: str
    job_status: Literal["pending", "succeeded", "failed"]
    result_ref: str | None = None

    def __post_init__(self) -> None:
        _require_outcome(self.outcome, "found")
        if self.job_status not in {"pending", "succeeded", "failed"}:
            raise ValueError("job_status must identify a known recovery state")
        if not self.provider_job_id:
            raise ValueError("provider_job_id must be non-empty")
        if (self.job_status == "succeeded") != (self.result_ref is not None):
            raise ValueError("result_ref must be present only for a succeeded job")


@dataclass(frozen=True, slots=True)
class GenerationRecoveryDefinitelyNotFound:
    outcome: Literal["definitely_not_found"]
    request_key: UUID

    def __post_init__(self) -> None:
        _require_outcome(self.outcome, "definitely_not_found")


@dataclass(frozen=True, slots=True)
class GenerationRecoveryUnknown:
    outcome: Literal["unknown"]
    request_key: UUID
    reason_code: str

    def __post_init__(self) -> None:
        _require_outcome(self.outcome, "unknown")
        _require_reason_code(self.reason_code)


@dataclass(frozen=True, slots=True)
class GenerationRecoveryUnsupported:
    outcome: Literal["unsupported"]
    request_key: UUID
    reason_code: str = "RECOVERY_UNSUPPORTED"

    def __post_init__(self) -> None:
        _require_outcome(self.outcome, "unsupported")
        _require_reason_code(self.reason_code)


type GenerationRecoveryResult = (
    GenerationRecoveryFound
    | GenerationRecoveryDefinitelyNotFound
    | GenerationRecoveryUnknown
    | GenerationRecoveryUnsupported
)


class GenerationPortError(RuntimeError):
    """A safe, stable error code for a single generation operation."""

    def __init__(self, reason_code: str) -> None:
        _require_reason_code(reason_code)
        self.reason_code = reason_code
        super().__init__(reason_code)


class GenerationIdempotencyConflict(GenerationPortError):
    """A stable request key was reused for different canonical input."""


class GenerationQueryTemporaryError(GenerationPortError):
    """A read-only query failed and can be scheduled again as a new action."""


class GenerationDownloadError(GenerationPortError):
    """A controlled download failed with a stable safe reason code."""


class GenerationDownloadTemporaryError(GenerationDownloadError):
    """A controlled result download failed and can be scheduled again."""


class GenerationPort(Protocol):
    """One-call generation boundary; implementations must not poll in a loop."""

    def submit(self, command: GenerationCommand) -> GenerationSubmitResult: ...

    def query(self, provider_job_id: str) -> GenerationQueryResult: ...

    def recover(self, request_key: UUID) -> GenerationRecoveryResult: ...

    def download(self, result_ref: str) -> bytes: ...
