from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from app.schemas.generation_acceptance import (
    GENERATION_ACCEPTANCE_CONTRACT,
    GENERATION_IMAGE_CONTRACT_V3,
    GenerationAcceptanceAccepted,
    GenerationAcceptanceNotObserved,
    GenerationAcceptanceRejectedExpired,
    generation_acceptance_receipt_digest,
    parse_generation_acceptance,
)
from pydantic import ValidationError

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_RESPONSE_BYTES = 256_000
_UNKNOWN_UNCONFIGURED = "GENERATION_ACCEPTANCE_EXPECTED_IDENTITY_INCOMPLETE"
_UNKNOWN_NOT_OBSERVED = "GENERATION_ACCEPTANCE_NOT_OBSERVED"
_UNKNOWN_IDENTITY = "GENERATION_ACCEPTANCE_IDENTITY_MISMATCH"
_UNKNOWN_RECEIPT = "GENERATION_ACCEPTANCE_RECEIPT_INVALID"
_UNKNOWN_RESPONSE = "GENERATION_ACCEPTANCE_RESPONSE_INVALID"
_UNKNOWN_HTTP = "GENERATION_ACCEPTANCE_HTTP_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class GenerationAcceptanceExpected:
    """The immutable H4 identity that a service acceptance must match."""

    generation_request_id: UUID
    task_id: UUID
    dify_run_id: str
    permit_id: UUID
    permit_version: Literal[2]
    permit_expires_at: datetime
    generation_contract_version: Literal["chatflow-image-generation-v3"]
    package_digest: str
    slot_mapping_digest: str
    reference_manifest_digest: str
    service_authority_id: UUID | None

    def __post_init__(self) -> None:
        for name in ("generation_request_id", "task_id", "permit_id"):
            if not isinstance(getattr(self, name), UUID):
                raise ValueError(f"{name} must be a UUID")
        if self.service_authority_id is not None and not isinstance(
            self.service_authority_id, UUID
        ):
            raise ValueError("service_authority_id must be a UUID or None")
        if self.permit_version != 2 or isinstance(self.permit_version, bool):
            raise ValueError("permit_version must be 2")
        if self.generation_contract_version != GENERATION_IMAGE_CONTRACT_V3:
            raise ValueError("generation_contract_version must be the v3 contract")
        if (
            not isinstance(self.dify_run_id, str)
            or not self.dify_run_id
            or len(self.dify_run_id) > 320
            or self.dify_run_id.strip() != self.dify_run_id
        ):
            raise ValueError("dify_run_id must be non-empty bounded text")
        if (
            not isinstance(self.permit_expires_at, datetime)
            or self.permit_expires_at.tzinfo is None
            or self.permit_expires_at.utcoffset() != UTC.utcoffset(self.permit_expires_at)
        ):
            raise ValueError("permit_expires_at must be a UTC datetime")
        for name in ("package_digest", "slot_mapping_digest", "reference_manifest_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class GenerationAcceptanceObservation:
    outcome: Literal["accepted", "rejected_expired", "unknown"]
    acceptance: GenerationAcceptanceAccepted | GenerationAcceptanceRejectedExpired | None = None
    unknown_reason: str | None = None

    def __post_init__(self) -> None:
        if self.outcome == "accepted":
            valid = isinstance(self.acceptance, GenerationAcceptanceAccepted)
        elif self.outcome == "rejected_expired":
            valid = isinstance(self.acceptance, GenerationAcceptanceRejectedExpired)
        else:
            valid = self.outcome == "unknown" and self.acceptance is None
        if not valid:
            raise ValueError("observation outcome and evidence do not match")
        if (self.outcome == "unknown") != (self.unknown_reason is not None):
            raise ValueError("only unknown observations carry a safe reason")


class GenerationAcceptancePort(Protocol):
    def get_acceptance(
        self, expected: GenerationAcceptanceExpected
    ) -> GenerationAcceptanceObservation: ...


def validate_generation_acceptance(
    payload: object, expected: GenerationAcceptanceExpected
) -> GenerationAcceptanceObservation:
    """Validate identity and receipt evidence without performing I/O.

    Every missing, malformed, contradictory, or mismatched value is unknown.
    The image-generation `service_request_digest` is format-checked by its DTO
    only; it is deliberately not compared with the backend H4 request digest.
    """

    if not isinstance(expected, GenerationAcceptanceExpected):
        return _unknown(_UNKNOWN_UNCONFIGURED)
    if expected.service_authority_id is None:
        return _unknown(_UNKNOWN_UNCONFIGURED)
    if not isinstance(payload, Mapping):
        return _unknown(_UNKNOWN_RESPONSE)
    try:
        parsed = parse_generation_acceptance(payload)
    except (ValidationError, TypeError, ValueError):
        return _unknown(_UNKNOWN_RESPONSE)

    if isinstance(parsed, GenerationAcceptanceNotObserved):
        if (
            parsed.contract_version != GENERATION_ACCEPTANCE_CONTRACT
            or parsed.service_authority_id != expected.service_authority_id
            or parsed.generation_request_id != expected.generation_request_id
        ):
            return _unknown(_UNKNOWN_IDENTITY)
        return _unknown(_UNKNOWN_NOT_OBSERVED)

    if not _identity_matches(parsed, expected):
        return _unknown(_UNKNOWN_IDENTITY)
    if isinstance(parsed, GenerationAcceptanceAccepted):
        # The strict DTO enforces a real non-empty batch id and forbids receipt
        # fields. A syntactically valid accepted DTO is the only positive path.
        return GenerationAcceptanceObservation("accepted", acceptance=parsed)
    if isinstance(parsed, GenerationAcceptanceRejectedExpired):
        if parsed.rejected_at < parsed.permit_expires_at:
            return _unknown(_UNKNOWN_RECEIPT)
        try:
            calculated = generation_acceptance_receipt_digest(payload)
        except (TypeError, ValueError):
            return _unknown(_UNKNOWN_RECEIPT)
        if not hmac.compare_digest(calculated, parsed.receipt_digest):
            return _unknown(_UNKNOWN_RECEIPT)
        return GenerationAcceptanceObservation("rejected_expired", acceptance=parsed)
    return _unknown(_UNKNOWN_RESPONSE)


def _identity_matches(
    acceptance: GenerationAcceptanceAccepted | GenerationAcceptanceRejectedExpired,
    expected: GenerationAcceptanceExpected,
) -> bool:
    return (
        acceptance.contract_version == GENERATION_ACCEPTANCE_CONTRACT
        and acceptance.service_authority_id == expected.service_authority_id
        and acceptance.generation_contract_version == expected.generation_contract_version
        and acceptance.generation_request_id == expected.generation_request_id
        and acceptance.task_id == expected.task_id
        and acceptance.dify_run_id == expected.dify_run_id
        and acceptance.permit_id == expected.permit_id
        and acceptance.permit_version == expected.permit_version
        and acceptance.permit_expires_at == expected.permit_expires_at.astimezone(UTC)
        and acceptance.package_digest == expected.package_digest
        and acceptance.slot_mapping_digest == expected.slot_mapping_digest
        and acceptance.reference_manifest_digest == expected.reference_manifest_digest
    )


def _unknown(reason: str) -> GenerationAcceptanceObservation:
    return GenerationAcceptanceObservation("unknown", unknown_reason=reason)


class HttpGenerationAcceptancePort:
    """Single-call GET-only acceptance observer pinned to one configured origin."""

    def __init__(
        self,
        service_url: str,
        bearer_token: str,
        *,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        origin = _fixed_origin(service_url)
        if not isinstance(bearer_token, str) or not bearer_token:
            raise ValueError("image-generation acceptance credentials are invalid")
        if any(character.isspace() for character in bearer_token):
            raise ValueError("image-generation acceptance credentials are invalid")
        self._origin = origin
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0)),
            follow_redirects=False,
            trust_env=False,
            headers={"Authorization": f"Bearer {bearer_token}", "Accept": "application/json"},
            transport=transport,
        )

    def get_acceptance(
        self, expected: GenerationAcceptanceExpected
    ) -> GenerationAcceptanceObservation:
        if not isinstance(expected, GenerationAcceptanceExpected):
            return _unknown(_UNKNOWN_UNCONFIGURED)
        if expected.service_authority_id is None:
            return _unknown(_UNKNOWN_UNCONFIGURED)
        request_id = expected.generation_request_id
        url = f"{self._origin}/v3/generation-requests/{request_id}/acceptance"
        try:
            with self._client.stream("GET", url) as response:
                if response.status_code != 200:
                    return _unknown(_UNKNOWN_HTTP)
                body = _read_limited_response(response)
                payload = _strict_json_object(body)
        except (httpx.HTTPError, TypeError, ValueError):
            return _unknown(_UNKNOWN_HTTP)
        return validate_generation_acceptance(payload, expected)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpGenerationAcceptancePort:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _fixed_origin(service_url: str) -> str:
    if not isinstance(service_url, str) or not service_url:
        raise ValueError("image-generation acceptance origin is invalid")
    try:
        parsed = urlsplit(service_url)
        port = parsed.port
    except ValueError:
        raise ValueError("image-generation acceptance origin is invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("image-generation acceptance origin must be a fixed HTTP origin")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    return f"{parsed.scheme}://{netloc}"


def _read_limited_response(response: httpx.Response) -> bytes:
    body = bytearray()
    for chunk in response.iter_bytes():
        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
            raise ValueError("acceptance response exceeds the allowed size")
        body.extend(chunk)
    return bytes(body)


def _strict_json_object(body: bytes) -> dict[str, object]:
    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON value")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("acceptance response is not valid strict JSON") from None
    if not isinstance(value, dict):
        raise ValueError("acceptance response must be a JSON object")
    return value
