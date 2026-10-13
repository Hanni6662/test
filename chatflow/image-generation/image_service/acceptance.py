"""Durable acceptance evidence for v3 generation requests.

The acceptance tables live in the same SQLite database as ``batches``.  The
triggers below make a batch and an expiry rejection mutually exclusive even
when separate Engine instances race on the same database file.
"""

import hashlib
import hmac
import re
import sqlite3
import uuid
from datetime import UTC, datetime

from .contracts import canonical_json_bytes

ACCEPTANCE_CONTRACT = "generation-acceptance-v1"
REJECTION_REASON = "GENERATION_PERMIT_EXPIRED_BEFORE_ACCEPTANCE"
RECEIPT_PREFIX = b"generation-acceptance-receipt-v1\0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_REJECTION_COLUMNS = (
    "generation_request_id,task_id,dify_run_id,permit_id,permit_version,"
    "permit_expires_at,generation_contract_version,package_digest,"
    "slot_mapping_digest,reference_manifest_digest,service_request_digest,"
    "reason_code,receipt_version,receipt_id,rejected_at,receipt_digest,"
    "service_authority_id"
)


def initialize_acceptance_storage(db):
    """Create the acceptance schema and return its persisted authority UUID."""
    db.execute(
        """CREATE TABLE IF NOT EXISTS image_service_authority (
               singleton INTEGER PRIMARY KEY CHECK(singleton=1),
               service_authority_id TEXT NOT NULL UNIQUE
           )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS generation_acceptance_rejections (
               generation_request_id TEXT PRIMARY KEY,
               task_id TEXT NOT NULL,
               dify_run_id TEXT NOT NULL,
               permit_id TEXT NOT NULL,
               permit_version INTEGER NOT NULL CHECK(permit_version=2),
               permit_expires_at TEXT NOT NULL,
               generation_contract_version TEXT NOT NULL,
               package_digest TEXT NOT NULL,
               slot_mapping_digest TEXT NOT NULL,
               reference_manifest_digest TEXT NOT NULL,
               service_request_digest TEXT NOT NULL,
               reason_code TEXT NOT NULL
                   CHECK(reason_code='GENERATION_PERMIT_EXPIRED_BEFORE_ACCEPTANCE'),
               receipt_version INTEGER NOT NULL CHECK(receipt_version=1),
               receipt_id TEXT NOT NULL UNIQUE,
               rejected_at TEXT NOT NULL,
               receipt_digest TEXT NOT NULL,
               service_authority_id TEXT NOT NULL
           )"""
    )
    db.execute(
        """CREATE TRIGGER IF NOT EXISTS generation_rejection_requires_no_batch
           BEFORE INSERT ON generation_acceptance_rejections
           WHEN EXISTS (
               SELECT 1 FROM batches WHERE request_id=NEW.generation_request_id
           )
           BEGIN
               SELECT RAISE(ABORT, 'generation_request_already_accepted');
           END"""
    )
    db.execute(
        """CREATE TRIGGER IF NOT EXISTS generation_batch_requires_no_rejection
           BEFORE INSERT ON batches
           WHEN EXISTS (
               SELECT 1 FROM generation_acceptance_rejections
               WHERE generation_request_id=NEW.request_id
           )
           BEGIN
               SELECT RAISE(ABORT, 'generation_request_already_rejected');
           END"""
    )
    db.commit()

    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(
            "SELECT service_authority_id FROM image_service_authority WHERE singleton=1"
        ).fetchone()
        if row is None:
            authority_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO image_service_authority (singleton,service_authority_id) "
                "VALUES (1,?)",
                (authority_id,),
            )
        else:
            authority_id = row[0]
            if not valid_uuid(authority_id):
                raise RuntimeError("persisted image-service authority is invalid")
        db.commit()
    except (sqlite3.Error, RuntimeError):
        db.rollback()
        raise
    return authority_id


def receipt_fields(binding, *, receipt_id, rejected_at):
    """Build the exact v1 receipt digest domain, excluding the digest itself."""
    return {
        "contract_version": ACCEPTANCE_CONTRACT,
        "outcome": "rejected_expired",
        "service_authority_id": binding["service_authority_id"],
        "generation_contract_version": binding["generation_contract_version"],
        "generation_request_id": binding["generation_request_id"],
        "task_id": binding["task_id"],
        "dify_run_id": binding["dify_run_id"],
        "permit_id": binding["permit_id"],
        "permit_version": binding["permit_version"],
        "permit_expires_at": binding["permit_expires_at"],
        "package_digest": binding["package_digest"],
        "slot_mapping_digest": binding["slot_mapping_digest"],
        "reference_manifest_digest": binding["reference_manifest_digest"],
        "service_request_digest": binding["service_request_digest"],
        "batch_id": None,
        "reason_code": REJECTION_REASON,
        "receipt_version": 1,
        "receipt_id": receipt_id,
        "rejected_at": rejected_at,
    }


def make_rejection_receipt(binding, *, receipt_id, rejected_at):
    fields = receipt_fields(binding, receipt_id=receipt_id, rejected_at=rejected_at)
    digest = hashlib.sha256(RECEIPT_PREFIX + canonical_json_bytes(fields)).hexdigest()
    return {**fields, "receipt_digest": digest}


def rejection_row_to_dto(row):
    (
        request_id,
        task_id,
        run_id,
        permit_id,
        permit_version,
        permit_expires_at,
        generation_contract_version,
        package_digest,
        slot_mapping_digest,
        reference_manifest_digest,
        service_request_digest,
        reason_code,
        receipt_version,
        receipt_id,
        rejected_at,
        receipt_digest,
        service_authority_id,
    ) = row
    binding = {
        "service_authority_id": service_authority_id,
        "generation_contract_version": generation_contract_version,
        "generation_request_id": request_id,
        "task_id": task_id,
        "dify_run_id": run_id,
        "permit_id": permit_id,
        "permit_version": permit_version,
        "permit_expires_at": permit_expires_at,
        "package_digest": package_digest,
        "slot_mapping_digest": slot_mapping_digest,
        "reference_manifest_digest": reference_manifest_digest,
        "service_request_digest": service_request_digest,
    }
    if (
        generation_contract_version != "chatflow-image-generation-v3"
        or permit_version != 2
        or not all(
            valid_uuid(value)
            for value in (
                service_authority_id,
                request_id,
                task_id,
                permit_id,
                receipt_id,
            )
        )
        or not isinstance(run_id, str)
        or not 1 <= len(run_id) <= 320
        or run_id != run_id.strip()
        or "\n" in run_id
        or "\r" in run_id
        or not valid_utc_timestamp(permit_expires_at)
        or not valid_utc_timestamp(rejected_at)
        or not all(
            valid_sha256(value)
            for value in (
                package_digest,
                slot_mapping_digest,
                reference_manifest_digest,
                service_request_digest,
            )
        )
    ):
        return None
    expected = make_rejection_receipt(
        binding, receipt_id=receipt_id, rejected_at=rejected_at
    )
    if (
        reason_code != REJECTION_REASON
        or receipt_version != 1
        or not isinstance(receipt_digest, str)
        or _SHA256.fullmatch(receipt_digest) is None
        or not hmac.compare_digest(receipt_digest, expected["receipt_digest"])
    ):
        return None
    return expected


def rejection_binding_matches(row, binding):
    """Compare every immutable request/permit binding field in a receipt row."""
    names = (
        "generation_request_id",
        "task_id",
        "dify_run_id",
        "permit_id",
        "permit_version",
        "permit_expires_at",
        "generation_contract_version",
        "package_digest",
        "slot_mapping_digest",
        "reference_manifest_digest",
        "service_request_digest",
        "service_authority_id",
    )
    row_values = dict(zip(_REJECTION_COLUMNS.split(","), row, strict=True))
    return all(row_values.get(name) == binding.get(name) for name in names)


def read_rejection_row(db, request_id):
    return db.execute(
        f"SELECT {_REJECTION_COLUMNS} FROM generation_acceptance_rejections "
        "WHERE generation_request_id=?",
        (request_id,),
    ).fetchone()


def rejection_insert_values(binding, receipt):
    return (
        binding["generation_request_id"],
        binding["task_id"],
        binding["dify_run_id"],
        binding["permit_id"],
        binding["permit_version"],
        binding["permit_expires_at"],
        binding["generation_contract_version"],
        binding["package_digest"],
        binding["slot_mapping_digest"],
        binding["reference_manifest_digest"],
        binding["service_request_digest"],
        REJECTION_REASON,
        1,
        receipt["receipt_id"],
        receipt["rejected_at"],
        receipt["receipt_digest"],
        binding["service_authority_id"],
    )


def not_observed(service_authority_id, request_id):
    return {
        "contract_version": ACCEPTANCE_CONTRACT,
        "outcome": "not_observed",
        "service_authority_id": service_authority_id,
        "generation_request_id": request_id,
    }


def valid_sha256(value):
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def valid_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def valid_utc_timestamp(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return (
        parsed.tzinfo is not None
        and parsed.utcoffset() == UTC.utcoffset(parsed)
        and parsed.isoformat() == value
    )


def insert_rejection(db, binding, receipt):
    """Insert inside the caller's active ``BEGIN IMMEDIATE`` transaction."""
    db.execute(
        f"INSERT INTO generation_acceptance_rejections ({_REJECTION_COLUMNS}) "
        f"VALUES ({','.join('?' for _ in _REJECTION_COLUMNS.split(','))})",
        rejection_insert_values(binding, receipt),
    )
