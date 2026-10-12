from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError

from app.schemas.trusted_analysis import (
    AuxiliaryRelationKind,
    TrustedAnalysisReference,
    TrustedAnalysisSnapshot,
    TrustedAuxiliaryRelation,
    TrustedRelationProvenance,
)

if TYPE_CHECKING:
    from app.integrations.dify.contracts import ReferenceAssetSummary

TRUSTED_ANALYSIS_STATE_KEY: Final[str] = "trusted_analysis_snapshot"
TRUSTED_ANALYSIS_SOURCE_VERSION: Final[str] = "synthetic-analysis-v1"
TRUSTED_ANALYSIS_SCHEMA_VERSION: Final[int] = 1
TRUSTED_ANALYSIS_INPUT_DIGEST_VERSION: Final[int] = 1
TRUSTED_ANALYSIS_SNAPSHOT_DIGEST_VERSION: Final[int] = 1
TRUSTED_AUXILIARY_RULE_VERSION: Final[str] = "synthetic-aux-slot-fixture-v1"
TRUSTED_AUXILIARY_TEST_OVERRIDE_VERSION: Final[str] = "synthetic-test-override-v1"

_FIXTURE_RELATIONS: Final[dict[str, AuxiliaryRelationKind]] = {
    "AUX01": "confirm",
    "AUX02": "supplement",
    "AUX03": "ignored_conflict",
}
_ALLOWED_RELATIONS: Final[frozenset[str]] = frozenset(
    {"confirm", "supplement", "ignored_conflict"}
)


def reference_asset_payload(asset: ReferenceAssetSummary) -> dict[str, object]:
    """Return the persisted, path-free reference metadata used by R1 snapshots."""
    return {
        "alias": asset.slot,
        "position": asset.position,
        "reference_set_image_id": str(asset.reference_set_image_id),
        "model_asset_id": str(asset.model_asset_id),
        "source_asset_id": str(asset.source_asset_id),
        "model_sha256": asset.model_sha256,
        "source_sha256": asset.source_sha256,
        "model_mime_type": asset.model_mime_type,
        "source_mime_type": asset.source_mime_type,
        "model_size_bytes": asset.model_size_bytes,
        "source_size_bytes": asset.source_size_bytes,
        "model_width": asset.model_width,
        "model_height": asset.model_height,
        "source_width": asset.source_width,
        "source_height": asset.source_height,
    }


class TrustedAnalysisSnapshotError(ValueError):
    """Safe fail-closed error; it never includes snapshot or prompt content."""

    code = "TRUSTED_ANALYSIS_UNAVAILABLE"
    message = "可信参考图分析快照不可用"

    def __init__(self) -> None:
        super().__init__(self.message)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ordered_references(
    values: Sequence[Mapping[str, object]],
) -> list[TrustedAnalysisReference]:
    try:
        references = [
            TrustedAnalysisReference.model_validate(value, strict=False) for value in values
        ]
    except (ValidationError, TypeError, ValueError):
        raise TrustedAnalysisSnapshotError from None
    aliases = [reference.alias for reference in references]
    expected = ["PRIMARY", *(f"AUX{index:02d}" for index in range(1, len(references)))]
    if not 1 <= len(references) <= 4 or aliases != expected:
        raise TrustedAnalysisSnapshotError
    return references


def _input_digest(
    *, reference_set_id: UUID, category: str, references: Sequence[TrustedAnalysisReference]
) -> str:
    if not isinstance(category, str) or not category.strip():
        raise TrustedAnalysisSnapshotError
    content = {
        "reference_set_id": str(reference_set_id),
        "category": category,
        "references": [reference.model_dump(mode="json") for reference in references],
    }
    return _canonical_sha256(content)


def _snapshot_digest_content(snapshot: TrustedAnalysisSnapshot) -> dict[str, object]:
    return {
        "snapshot_schema_version": snapshot.snapshot_schema_version,
        "source_kind": snapshot.source_kind,
        "source_version": snapshot.source_version,
        "analysis_run_id": str(snapshot.analysis_run_id),
        "analysis_revision": snapshot.analysis_revision,
        "snapshot_digest_version": snapshot.snapshot_digest_version,
        "reference_set_id": str(snapshot.reference_set_id),
        "recommendation_id": str(snapshot.recommendation_id),
        "recommendation_revision": snapshot.recommendation_revision,
        "input_digest_version": snapshot.input_digest_version,
        "input_digest": snapshot.input_digest,
        "references": [reference.model_dump(mode="json") for reference in snapshot.references],
        "auxiliary_relations": [
            relation.model_dump(mode="json") for relation in snapshot.auxiliary_relations
        ],
    }


def _scope_for(relation: AuxiliaryRelationKind) -> str:
    return "global" if relation == "confirm" else "detail_only"


def _evidence_for(alias: str, relation: AuxiliaryRelationKind, *, overridden: bool) -> str:
    if overridden:
        return (
            f"Synthetic test override marks {alias} as {relation}; "
            "no image bytes were read or analyzed."
        )
    return (
        f"Synthetic slot fixture marks {alias} as {relation}; this is fixed test data, "
        "not a claim about the uploaded image."
    )


def build_trusted_analysis_snapshot(
    *,
    reference_set_id: UUID,
    recommendation_id: UUID,
    recommendation_revision: int,
    category: str,
    references: Sequence[Mapping[str, object]],
    created_at: datetime,
    relation_overrides: Mapping[str, str] | None = None,
) -> TrustedAnalysisSnapshot:
    """Build the deterministic synthetic snapshot from saved asset metadata only."""
    try:
        ordered = _ordered_references(references)
        if recommendation_revision < 1:
            raise TrustedAnalysisSnapshotError
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise TrustedAnalysisSnapshotError
        created_at_utc = created_at.astimezone(UTC)

        overrides = dict(relation_overrides or {})
        auxiliary_aliases = [reference.alias for reference in ordered[1:]]
        if set(overrides) - set(auxiliary_aliases):
            raise TrustedAnalysisSnapshotError
        if any(value not in _ALLOWED_RELATIONS for value in overrides.values()):
            raise TrustedAnalysisSnapshotError

        relations: list[TrustedAuxiliaryRelation] = []
        for alias in auxiliary_aliases:
            relation_value = overrides.get(alias, _FIXTURE_RELATIONS[alias])
            relation = relation_value
            is_overridden = alias in overrides
            relations.append(
                TrustedAuxiliaryRelation(
                    alias=alias,  # type: ignore[arg-type]
                    relation=relation,  # type: ignore[arg-type]
                    scope=_scope_for(relation),  # type: ignore[arg-type]
                    view="unknown",
                    evidence=_evidence_for(alias, relation, overridden=is_overridden),
                    provenance=TrustedRelationProvenance(
                        synthetic=True,
                        rule_version=(
                            TRUSTED_AUXILIARY_TEST_OVERRIDE_VERSION
                            if is_overridden
                            else TRUSTED_AUXILIARY_RULE_VERSION
                        ),
                    ),
                )
            )

        analysis_revision = 1
        analysis_run_id = uuid5(
            NAMESPACE_URL,
            f"image-backend:synthetic-analysis:{recommendation_id}:{recommendation_revision}:{analysis_revision}",
        )
        snapshot_id = uuid5(
            NAMESPACE_URL,
            f"image-backend:trusted-analysis-snapshot:{recommendation_id}:{recommendation_revision}:{analysis_revision}",
        )
        input_digest = _input_digest(
            reference_set_id=reference_set_id, category=category, references=ordered
        )
        draft = TrustedAnalysisSnapshot.model_validate(
            {
                "snapshot_id": snapshot_id,
                "reference_set_id": reference_set_id,
                "recommendation_id": recommendation_id,
                "recommendation_revision": recommendation_revision,
                "source_kind": "synthetic_mock",
                "source_version": TRUSTED_ANALYSIS_SOURCE_VERSION,
                "analysis_run_id": analysis_run_id,
                "analysis_revision": analysis_revision,
                "snapshot_schema_version": TRUSTED_ANALYSIS_SCHEMA_VERSION,
                "snapshot_digest_version": TRUSTED_ANALYSIS_SNAPSHOT_DIGEST_VERSION,
                "snapshot_digest": "0" * 64,
                "input_digest_version": TRUSTED_ANALYSIS_INPUT_DIGEST_VERSION,
                "input_digest": input_digest,
                "references": ordered,
                "auxiliary_relations": relations,
                "created_at": created_at_utc,
            },
            strict=False,
        )
        digest = _canonical_sha256(_snapshot_digest_content(draft))
        return draft.model_copy(update={"snapshot_digest": digest})
    except TrustedAnalysisSnapshotError:
        raise
    except (ValidationError, TypeError, ValueError, OverflowError):
        raise TrustedAnalysisSnapshotError from None


def load_trusted_analysis_snapshot(
    raw: object,
    *,
    reference_set_id: UUID,
    recommendation_id: UUID,
    recommendation_revision: int,
    category: str,
    expected_references: Sequence[Mapping[str, object]],
) -> TrustedAnalysisSnapshot:
    """Validate a persisted snapshot against the exact task/recommendation inputs.

    `expected_references` accepts the saved S6 asset-snapshot mappings (`slot`,
    source/model asset UUIDs and source/model SHA-256 values). When present, MIME,
    size, dimensions, position, and reference-set-image UUID are also compared.
    """
    try:
        snapshot = TrustedAnalysisSnapshot.model_validate(raw, strict=False)
        if (
            snapshot.reference_set_id != reference_set_id
            or snapshot.recommendation_id != recommendation_id
            or snapshot.recommendation_revision != recommendation_revision
            or snapshot.source_version != TRUSTED_ANALYSIS_SOURCE_VERSION
            or snapshot.snapshot_schema_version != TRUSTED_ANALYSIS_SCHEMA_VERSION
            or snapshot.snapshot_digest_version != TRUSTED_ANALYSIS_SNAPSHOT_DIGEST_VERSION
            or snapshot.input_digest_version != TRUSTED_ANALYSIS_INPUT_DIGEST_VERSION
        ):
            raise TrustedAnalysisSnapshotError
        if snapshot.input_digest != _input_digest(
            reference_set_id=snapshot.reference_set_id,
            category=category,
            references=snapshot.references,
        ):
            raise TrustedAnalysisSnapshotError
        if snapshot.snapshot_digest != _canonical_sha256(_snapshot_digest_content(snapshot)):
            raise TrustedAnalysisSnapshotError

        expected = _ordered_expected_references(expected_references)
        if len(expected) != len(snapshot.references):
            raise TrustedAnalysisSnapshotError
        required_fields = {
            "source_asset_id": "source_asset_id",
            "model_asset_id": "model_asset_id",
            "source_sha256": "source_sha256",
            "model_sha256": "model_sha256",
        }
        optional_fields = {
            "reference_set_image_id": "reference_set_image_id",
            "position": "position",
            "source_mime_type": "source_mime_type",
            "model_mime_type": "model_mime_type",
            "source_size_bytes": "source_size_bytes",
            "model_size_bytes": "model_size_bytes",
            "source_width": "source_width",
            "source_height": "source_height",
            "model_width": "model_width",
            "model_height": "model_height",
        }
        for saved, persisted in zip(expected, snapshot.references, strict=True):
            if saved.get("alias") != persisted.alias:
                raise TrustedAnalysisSnapshotError
            for key, attribute in required_fields.items():
                if key not in saved or _normalized_value(saved[key]) != _normalized_value(
                    getattr(persisted, attribute)
                ):
                    raise TrustedAnalysisSnapshotError
            for key, attribute in optional_fields.items():
                if key in saved and _normalized_value(saved[key]) != _normalized_value(
                    getattr(persisted, attribute)
                ):
                    raise TrustedAnalysisSnapshotError
        return snapshot
    except TrustedAnalysisSnapshotError:
        raise
    except (ValidationError, TypeError, ValueError, KeyError, AttributeError, OverflowError):
        raise TrustedAnalysisSnapshotError from None


def _ordered_expected_references(
    values: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    try:
        expected = [dict(value) for value in values]
    except (TypeError, ValueError):
        raise TrustedAnalysisSnapshotError from None
    aliases = [value.get("alias", value.get("slot")) for value in expected]
    canonical = ["PRIMARY", *(f"AUX{index:02d}" for index in range(1, len(expected)))]
    if not 1 <= len(expected) <= 4 or aliases != canonical:
        raise TrustedAnalysisSnapshotError
    for value, alias in zip(expected, canonical, strict=True):
        value["alias"] = alias
    return expected


def _normalized_value(value: object) -> str:
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def trusted_analysis_state(snapshot: TrustedAnalysisSnapshot) -> dict[str, object]:
    """Return a detached JSON-safe snapshot object for private JSONB state."""
    return snapshot.model_dump(mode="json")
