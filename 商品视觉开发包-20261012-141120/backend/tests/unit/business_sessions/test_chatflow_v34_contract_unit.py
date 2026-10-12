from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest
from app.schemas.chatflow_v34 import (
    V34_FLOW_VERSION,
    V34_STAGE_CONTRACT,
    ChatflowV34StageEvent,
    V34SelectionAppliedPayload,
    chatflow_v34_stage_event_digest,
)
from app.schemas.recommendations import IMAGE_TYPES
from app.schemas.tasks import TaskPreferences
from app.services.business_sessions import BusinessSessionOptions
from pydantic import ValidationError


def _preferences() -> dict[str, object]:
    return {
        "language": {"mode": "A"},
        "platform": "TEMU",
        "usage_scene": {"mode": "B"},
        "visual_style": {"mode": "C"},
        "selling_points": {"mode": "custom", "text": "只使用可核验的商品信息"},
    }


def _event_data(
    event_type: str,
    payload: dict[str, object],
    *,
    node_id: str | None = None,
) -> dict[str, object]:
    default_nodes = {
        "recommendation.ready": "prepare",
        "selection.applied": "parse_0",
        "pre_generation.incomplete": "ready_0",
        "selection.cancelled": "selection_cancelled_callback",
        "selection.expired": "selection_expired_callback",
        "workflow.failed": "workflow_failed_callback",
    }
    data: dict[str, object] = {
        "contract_version": V34_STAGE_CONTRACT,
        "flow_version": V34_FLOW_VERSION,
        "business_session_id": str(uuid4()),
        "recommendation_id": str(uuid4()),
        "recommendation_revision": 1,
        "dify_run_id": "run-v34-1",
        "node_id": node_id or default_nodes[event_type],
        "event_key": "run-v34-1:node:event-1",
        "digest_version": 1,
        "event_type": event_type,
        "payload": payload,
    }
    return _recompute_digest(data)


def _recompute_digest(data: dict[str, object]) -> dict[str, object]:
    canonical = json.dumps(
        {key: value for key, value in data.items() if key != "digest"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    data["digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return data


def _parse_event(data: dict[str, object]) -> ChatflowV34StageEvent:
    return ChatflowV34StageEvent.model_validate_json(json.dumps(data, ensure_ascii=False))


def _valid_selection_payload() -> dict[str, object]:
    return {
        "selection_submission_id": str(uuid4()),
        "parse_state": {
            "preferences": _preferences(),
            "image_types": list(IMAGE_TYPES[:3]),
        },
    }


def test_business_session_options_keep_legacy_default_and_require_fixed_v34_identity() -> None:
    assert BusinessSessionOptions().flow_version == "legacy"
    with pytest.raises(ValueError):
        BusinessSessionOptions(flow_version=V34_FLOW_VERSION)
    with pytest.raises(ValueError):
        BusinessSessionOptions(flow_version=V34_FLOW_VERSION, dify_workflow_id=" ")
    with pytest.raises(ValueError):
        BusinessSessionOptions(flow_version=V34_FLOW_VERSION, dify_workflow_id=" release ")

    options = BusinessSessionOptions(
        flow_version=V34_FLOW_VERSION,
        dify_app_id="v34-app-test",
        dify_workflow_id="release-v34-test",
    )
    assert options.flow_version == V34_FLOW_VERSION
    assert options.dify_workflow_id == "release-v34-test"


def test_selection_applied_requires_typed_preferences_and_canonical_digest() -> None:
    data = _event_data("selection.applied", _valid_selection_payload())
    event = _parse_event(data)
    accepted_payload = V34SelectionAppliedPayload.model_validate_json(
        json.dumps(event.payload, ensure_ascii=False), strict=True
    )
    assert isinstance(accepted_payload.parse_state.preferences, TaskPreferences)
    assert accepted_payload.parse_state.image_types == list(IMAGE_TYPES[:3])
    assert event.digest_version == 1
    assert len(data["digest"]) == 64
    assert data["digest"] == data["digest"].lower()
    assert chatflow_v34_stage_event_digest(event).hex() == data["digest"]

    reordered_keys = json.loads(json.dumps(data, ensure_ascii=False))
    reordered_keys["payload"] = {
        "parse_state": {
            "image_types": list(IMAGE_TYPES[:3]),
            "preferences": _preferences(),
        },
        "selection_submission_id": data["payload"]["selection_submission_id"],
    }
    _recompute_digest(reordered_keys)
    replay = _parse_event(reordered_keys)
    assert chatflow_v34_stage_event_digest(replay) == chatflow_v34_stage_event_digest(event)


@pytest.mark.parametrize(
    "digest",
    ["f" * 63, "G" * 64, "g" * 64],
)
def test_stage_event_rejects_noncanonical_digest_encoding(digest: str) -> None:
    invalid = _event_data("recommendation.ready", {"audit": "{}"})
    invalid["digest"] = digest
    with pytest.raises(ValidationError):
        _parse_event(invalid)


def test_stage_event_rejects_digest_version_other_than_one() -> None:
    invalid = _event_data("recommendation.ready", {"audit": "{}"})
    invalid["digest_version"] = 2
    with pytest.raises(ValidationError):
        _parse_event(_recompute_digest(invalid))


def test_stage_event_service_rejects_well_formed_but_invalid_digest() -> None:
    from app.core.domain_errors import DomainError
    from app.services.chatflow_v34_events import ChatflowV34StageEventService

    invalid = _event_data("recommendation.ready", {"audit": "{}"})
    invalid["digest"] = "0" * 64
    event = _parse_event(invalid)

    # Digest verification runs before opening a database session.
    service = ChatflowV34StageEventService(None)
    with pytest.raises(DomainError) as error:
        service.accept(event)
    assert error.value.code == "CALLBACK_INVALID"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["parse_state"].update({"untrusted": True}),
        lambda payload: payload["parse_state"]["preferences"].update({"untrusted": True}),
        lambda payload: payload["parse_state"].update(
            {"image_types": list(reversed(IMAGE_TYPES[:2]))}
        ),
        lambda payload: payload["parse_state"].update(
            {"image_types": [IMAGE_TYPES[0], IMAGE_TYPES[0]]}
        ),
        lambda payload: payload["parse_state"].update({"image_types": ["free-form"]}),
        lambda payload: payload.update({"recommendation_id": str(uuid4())}),
        lambda payload: payload.update({"submission_id": payload.pop("selection_submission_id")}),
    ],
)
def test_selection_applied_rejects_untyped_extra_or_noncanonical_payload(mutate) -> None:
    payload = _valid_selection_payload()
    mutate(payload)
    with pytest.raises(ValidationError):
        _parse_event(_event_data("selection.applied", payload))


def test_stage_envelope_rejects_extra_fields_wrong_versions_and_untrusted_node_mapping() -> None:
    valid = _event_data("recommendation.ready", {"audit": "{}"})
    with pytest.raises(ValidationError):
        _parse_event(_recompute_digest({**valid, "unexpected": "field"}))

    for key, value in (
        ("contract_version", "chatflow-handoff-v1"),
        ("flow_version", "legacy"),
        ("recommendation_revision", "1"),
    ):
        invalid = _recompute_digest({**valid, key: value})
        with pytest.raises(ValidationError):
            _parse_event(invalid)

    with pytest.raises(ValidationError):
        _parse_event(_recompute_digest({**valid, "node_id": "parse_0"}))


def test_stage_payload_sizes_and_node_reason_pairs_are_bounded() -> None:
    with pytest.raises(ValidationError):
        _parse_event(_event_data("recommendation.ready", {"audit": "汉" * (64 * 1024 + 1)}))
    with pytest.raises(ValidationError):
        _parse_event(_event_data("recommendation.ready", {"audit": "汉" * 33_000}))

    allowed = _parse_event(
        _event_data(
            "pre_generation.incomplete",
            {"reason_code": "PRODUCT_IDENTITY_CONFLICT", "conflicting_field_ids": ["3", "4"]},
        )
    )
    assert allowed.event_type == "pre_generation.incomplete"

    for payload in (
        {"reason_code": "PRODUCT_IDENTITY_CONFLICT", "conflicting_field_ids": ["identity"]},
        {"reason_code": "PRODUCT_IDENTITY_CONFLICT", "conflicting_field_ids": ["3", "3"]},
        {
            "reason_code": "PRODUCT_IDENTITY_CONFLICT",
            "conflicting_field_ids": ["3"],
            "reason": "模型自由文本不得传递",
        },
    ):
        with pytest.raises(ValidationError):
            _parse_event(_event_data("pre_generation.incomplete", payload))

    for event_type, valid_payload, invalid_payload in (
        (
            "selection.cancelled",
            {"reason_code": "FORM_CANCELLED"},
            {"reason_code": "FORM_EXPIRED"},
        ),
        (
            "selection.expired",
            {"reason_code": "FORM_EXPIRED"},
            {"reason_code": "FORM_CANCELLED"},
        ),
        (
            "workflow.failed",
            {"phase": "prepare", "failed_node_id": "prepare"},
            {"phase": "prepare", "failed_node_id": "audit"},
        ),
    ):
        assert _parse_event(_event_data(event_type, valid_payload)).event_type
        with pytest.raises(ValidationError):
            _parse_event(_event_data(event_type, invalid_payload))


def test_s020_is_a_forward_only_additive_revision_from_s019() -> None:
    migration_path = (
        Path(__file__).parents[3] / "migrations" / "versions" / "s020_v34_session_handoff.py"
    )
    spec = importlib.util.spec_from_file_location("v34_s020_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "s020_v34_session_handoff"
    assert migration.down_revision == "s019_h8_dify_reference_files"
    assert 'server_default="legacy"' in migration_path.read_text(encoding="utf-8")

    with pytest.raises(RuntimeError, match="forward-only"):
        migration.downgrade()
