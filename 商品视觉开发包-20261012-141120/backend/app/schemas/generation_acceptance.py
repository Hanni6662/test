from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictStr,
    TypeAdapter,
)

GENERATION_ACCEPTANCE_CONTRACT = "generation-acceptance-v1"
GENERATION_IMAGE_CONTRACT_V3 = "chatflow-image-generation-v3"
GENERATION_PERMIT_CONTRACT_V2 = "chatflow-generation-permit-v2"
GENERATION_PERMIT_EXPIRED_REASON = "GENERATION_PERMIT_EXPIRED_BEFORE_ACCEPTANCE"
RECEIPT_DIGEST_DOMAIN = b"generation-acceptance-receipt-v1\0"

_SHA256 = r"^[0-9a-f]{64}$"
_UUID_TEXT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_UTC_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$")
_BATCH_ID = r"^[A-Za-z0-9._:-]{1,128}$"
_DIFY_RUN_ID = r"^\S(?:.*\S)?$"

_RECEIPT_FIELDS = frozenset(
    {
        "contract_version",
        "outcome",
        "service_authority_id",
        "generation_contract_version",
        "generation_request_id",
        "task_id",
        "dify_run_id",
        "permit_id",
        "permit_version",
        "permit_expires_at",
        "package_digest",
        "slot_mapping_digest",
        "reference_manifest_digest",
        "service_request_digest",
        "batch_id",
        "reason_code",
        "receipt_version",
        "receipt_id",
        "rejected_at",
    }
)


def _strict_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or _UUID_TEXT.fullmatch(value) is None:
        raise ValueError("UUID must use canonical lowercase hyphenated text")
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError("UUID must use canonical lowercase hyphenated text")
    return parsed


def _strict_utc_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("datetime must be UTC")
        return value.astimezone(UTC)
    if not isinstance(value, str) or _UTC_TEXT.fullmatch(value) is None:
        raise ValueError("datetime must be an ISO-8601 UTC timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError("datetime must be an ISO-8601 UTC timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("datetime must be UTC")
    return parsed.astimezone(UTC)


StrictUUID = Annotated[UUID, BeforeValidator(_strict_uuid)]
StrictUTCDatetime = Annotated[datetime, BeforeValidator(_strict_utc_datetime)]


class _AcceptanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _AcceptanceIdentity(_AcceptanceModel):
    contract_version: Literal["generation-acceptance-v1"]
    service_authority_id: StrictUUID
    generation_contract_version: Literal["chatflow-image-generation-v3"]
    generation_request_id: StrictUUID
    task_id: StrictUUID
    dify_run_id: Annotated[StrictStr, Field(min_length=1, max_length=320, pattern=_DIFY_RUN_ID)]
    permit_id: StrictUUID
    permit_version: Literal[2]
    permit_expires_at: StrictUTCDatetime
    package_digest: Annotated[StrictStr, Field(pattern=_SHA256)]
    slot_mapping_digest: Annotated[StrictStr, Field(pattern=_SHA256)]
    reference_manifest_digest: Annotated[StrictStr, Field(pattern=_SHA256)]
    # This digest belongs to image-generation's request domain. It is not the
    # backend H4 request_digest and must not be compared with it.
    service_request_digest: Annotated[StrictStr, Field(pattern=_SHA256)]


class GenerationAcceptanceAccepted(_AcceptanceIdentity):
    outcome: Literal["accepted"]
    batch_id: Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=_BATCH_ID)]


class GenerationAcceptanceRejectedExpired(_AcceptanceIdentity):
    outcome: Literal["rejected_expired"]
    batch_id: None
    reason_code: Literal["GENERATION_PERMIT_EXPIRED_BEFORE_ACCEPTANCE"]
    receipt_version: Literal[1]
    receipt_id: StrictUUID
    rejected_at: StrictUTCDatetime
    receipt_digest: Annotated[StrictStr, Field(pattern=_SHA256)]


class GenerationAcceptanceNotObserved(_AcceptanceModel):
    contract_version: Literal["generation-acceptance-v1"]
    outcome: Literal["not_observed"]
    service_authority_id: StrictUUID
    generation_request_id: StrictUUID


type GenerationAcceptance = Annotated[
    GenerationAcceptanceAccepted
    | GenerationAcceptanceRejectedExpired
    | GenerationAcceptanceNotObserved,
    Field(discriminator="outcome"),
]

_GENERATION_ACCEPTANCE_ADAPTER = TypeAdapter(GenerationAcceptance)


def parse_generation_acceptance(value: object) -> GenerationAcceptance:
    """Parse an exact generation-acceptance-v1 response DTO."""

    return _GENERATION_ACCEPTANCE_ADAPTER.validate_python(value)


def generation_acceptance_receipt_digest(fields: Mapping[str, object]) -> str:
    """Return the receipt digest for its exact frozen field set.

    `receipt_digest` is excluded. Values are serialized as supplied so the
    service's canonical timestamp spelling remains part of the signed bytes.
    """

    if not isinstance(fields, Mapping):
        raise TypeError("receipt fields must be a mapping")
    canonical_fields = {key: value for key, value in fields.items() if key != "receipt_digest"}
    if set(canonical_fields) != _RECEIPT_FIELDS:
        raise ValueError("receipt fields do not match generation-acceptance-v1")
    try:
        canonical = json.dumps(
            canonical_fields,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("receipt fields are not canonical JSON values") from None
    return hashlib.sha256(RECEIPT_DIGEST_DOMAIN + canonical).hexdigest()


def generation_acceptance_receipt_digest_for_model(
    receipt: GenerationAcceptanceRejectedExpired,
) -> str:
    """Compute a rejected receipt digest from a validated DTO."""

    if not isinstance(receipt, GenerationAcceptanceRejectedExpired):
        raise TypeError("receipt must be rejected_expired")
    fields = receipt.model_dump(mode="json", exclude={"receipt_digest"})
    return generation_acceptance_receipt_digest(fields)
