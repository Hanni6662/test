from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest
from app.db.reference_models import ImageAsset, ReferenceSetImage
from app.services.recommendations import RecommendationService
from app.services.trusted_analysis import (
    TRUSTED_ANALYSIS_STATE_KEY,
    TrustedAnalysisSnapshotError,
    build_trusted_analysis_snapshot,
    load_trusted_analysis_snapshot,
    trusted_analysis_state,
)


def _references(count: int) -> list[dict[str, object]]:
    values = []
    for position in range(count):
        digest = f"{position + 1:064x}"
        values.append(
            {
                "alias": "PRIMARY" if position == 0 else f"AUX{position:02d}",
                "position": position,
                "reference_set_image_id": str(uuid4()),
                "model_asset_id": str(uuid4()),
                "source_asset_id": str(uuid4()),
                "model_sha256": digest,
                "source_sha256": f"{position + 10:064x}",
                "model_mime_type": "image/png",
                "source_mime_type": "image/jpeg",
                "model_size_bytes": 128 + position,
                "source_size_bytes": 256 + position,
                "model_width": 32,
                "model_height": 32,
                "source_width": 64,
                "source_height": 64,
            }
        )
    return values


def _build(count: int, *, overrides: dict[str, str] | None = None):
    return build_trusted_analysis_snapshot(
        reference_set_id=uuid4(),
        recommendation_id=uuid4(),
        recommendation_revision=1,
        category="测试品类",
        references=_references(count),
        created_at=datetime(2026, 10, 7, tzinfo=UTC),
        relation_overrides=overrides,
    )


def test_primary_only_snapshot_is_complete_and_has_no_auxiliary_relations() -> None:
    references = _references(1)
    reference_set_id = uuid4()
    recommendation_id = uuid4()
    snapshot = build_trusted_analysis_snapshot(
        reference_set_id=reference_set_id,
        recommendation_id=recommendation_id,
        recommendation_revision=1,
        category="台灯",
        references=references,
        created_at=datetime(2026, 10, 7, tzinfo=UTC),
    )

    assert snapshot.source_kind == "synthetic_mock"
    assert snapshot.analysis_revision == 1
    assert snapshot.references[0].alias == "PRIMARY"
    assert snapshot.auxiliary_relations == ()
    loaded = load_trusted_analysis_snapshot(
        trusted_analysis_state(snapshot),
        reference_set_id=reference_set_id,
        recommendation_id=recommendation_id,
        recommendation_revision=1,
        category="台灯",
        expected_references=references,
    )
    assert loaded.snapshot_id == snapshot.snapshot_id
    assert loaded.snapshot_digest == snapshot.snapshot_digest


@pytest.mark.parametrize("reference_count", [2, 3, 4])
def test_fixture_covers_one_to_three_auxiliary_slots(reference_count: int) -> None:
    snapshot = _build(reference_count)

    assert [reference.alias for reference in snapshot.references] == [
        "PRIMARY",
        *(f"AUX{index:02d}" for index in range(1, reference_count)),
    ]
    assert [relation.alias for relation in snapshot.auxiliary_relations] == [
        f"AUX{index:02d}" for index in range(1, reference_count)
    ]
    assert all(relation.provenance.synthetic for relation in snapshot.auxiliary_relations)
    if reference_count == 4:
        assert [relation.relation for relation in snapshot.auxiliary_relations] == [
            "confirm",
            "supplement",
            "ignored_conflict",
        ]


def test_test_only_override_can_force_ignored_conflict() -> None:
    snapshot = _build(2, overrides={"AUX01": "ignored_conflict"})

    relation = snapshot.auxiliary_relations[0]
    assert relation.relation == "ignored_conflict"
    assert relation.provenance.synthetic is True
    assert relation.provenance.rule_version == "synthetic-test-override-v1"
    assert "no image bytes were read" in relation.evidence


@pytest.mark.parametrize(
    "mutation",
    [
        lambda values: values.reverse(),
        lambda values: values.__setitem__(1, {**values[1], "alias": "PRIMARY"}),
        lambda values: values.__setitem__(2, {**values[2], "alias": "AUX03"}),
    ],
)
def test_rejects_out_of_order_duplicate_and_sparse_aliases(mutation) -> None:
    references = _references(3)
    mutation(references)

    with pytest.raises(TrustedAnalysisSnapshotError):
        build_trusted_analysis_snapshot(
            reference_set_id=uuid4(),
            recommendation_id=uuid4(),
            recommendation_revision=1,
            category="测试品类",
            references=references,
            created_at=datetime(2026, 10, 7, tzinfo=UTC),
        )


def test_snapshot_is_stable_for_same_run_input_and_bound_to_revision() -> None:
    reference_set_id = uuid4()
    recommendation_id = uuid4()
    references = _references(2)
    args = {
        "reference_set_id": reference_set_id,
        "recommendation_id": recommendation_id,
        "recommendation_revision": 1,
        "category": "测试品类",
        "references": references,
        "created_at": datetime(2026, 10, 7, tzinfo=UTC),
    }

    first = build_trusted_analysis_snapshot(**args)
    second = build_trusted_analysis_snapshot(**args)

    assert first.snapshot_id == second.snapshot_id
    assert first.analysis_run_id == second.analysis_run_id
    assert first.snapshot_digest == second.snapshot_digest
    changed = build_trusted_analysis_snapshot(**{**args, "recommendation_revision": 2})
    assert changed.snapshot_id != first.snapshot_id
    assert changed.analysis_run_id != first.analysis_run_id


def test_loading_fails_closed_for_missing_or_cross_bound_snapshot() -> None:
    references = _references(2)
    snapshot = build_trusted_analysis_snapshot(
        reference_set_id=uuid4(),
        recommendation_id=uuid4(),
        recommendation_revision=1,
        category="测试品类",
        references=references,
        created_at=datetime(2026, 10, 7, tzinfo=UTC),
    )
    binding = {
        "reference_set_id": snapshot.reference_set_id,
        "recommendation_id": snapshot.recommendation_id,
        "recommendation_revision": snapshot.recommendation_revision,
        "category": "测试品类",
        "expected_references": references,
    }

    with pytest.raises(TrustedAnalysisSnapshotError):
        load_trusted_analysis_snapshot(None, **binding)
    with pytest.raises(TrustedAnalysisSnapshotError):
        load_trusted_analysis_snapshot(
            trusted_analysis_state(snapshot), **{**binding, "reference_set_id": uuid4()}
        )


def test_loading_rejects_different_owner_asset_mapping_and_digest_tampering() -> None:
    references = _references(2)
    snapshot = build_trusted_analysis_snapshot(
        reference_set_id=uuid4(),
        recommendation_id=uuid4(),
        recommendation_revision=1,
        category="测试品类",
        references=references,
        created_at=datetime(2026, 10, 7, tzinfo=UTC),
    )
    binding = {
        "reference_set_id": snapshot.reference_set_id,
        "recommendation_id": snapshot.recommendation_id,
        "recommendation_revision": snapshot.recommendation_revision,
        "category": "测试品类",
    }
    other_owner_references = deepcopy(references)
    other_owner_references[1]["model_asset_id"] = str(uuid4())

    with pytest.raises(TrustedAnalysisSnapshotError):
        load_trusted_analysis_snapshot(
            trusted_analysis_state(snapshot),
            **binding,
            expected_references=other_owner_references,
        )

    tampered = trusted_analysis_state(snapshot)
    tampered["snapshot_digest"] = "f" * 64
    with pytest.raises(TrustedAnalysisSnapshotError):
        load_trusted_analysis_snapshot(
            tampered, **binding, expected_references=references
        )


def test_snapshot_state_is_private_and_has_no_storage_locator() -> None:
    snapshot = _build(2)
    persisted_state = {TRUSTED_ANALYSIS_STATE_KEY: trusted_analysis_state(snapshot)}
    references = persisted_state[TRUSTED_ANALYSIS_STATE_KEY]["references"]

    assert all("object_key" not in reference and "url" not in reference for reference in references)


def test_model_input_digest_mismatch_fails_closed_before_snapshot_creation() -> None:
    owner_id = uuid4()
    reference_set_id = uuid4()
    source = ImageAsset(
        id=uuid4(),
        owner_user_id=owner_id,
        asset_kind="reference_source",
        object_key="source-private-key",
        content_sha256="a" * 64,
        mime_type="image/png",
        size_bytes=64,
        width=8,
        height=8,
    )
    model = ImageAsset(
        id=uuid4(),
        owner_user_id=owner_id,
        asset_kind="reference_model_input",
        object_key="model-private-key",
        content_sha256="b" * 64,
        mime_type="image/png",
        size_bytes=32,
        width=4,
        height=4,
    )
    image = ReferenceSetImage(
        id=uuid4(),
        reference_set_id=reference_set_id,
        owner_user_id=owner_id,
        slot="PRIMARY",
        position=0,
        source_asset_id=source.id,
        model_asset_id=model.id,
        content_sha256="c" * 64,
    )
    db = Mock()
    db.execute.return_value.all.return_value = [(image, source, model)]

    summaries = RecommendationService._reference_asset_summary(
        db, owner_user_id=owner_id, reference_set_id=reference_set_id
    )

    assert summaries == ()
