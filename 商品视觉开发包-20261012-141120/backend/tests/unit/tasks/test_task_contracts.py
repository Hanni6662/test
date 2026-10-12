from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.api.tasks import router
from app.core.domain_errors import DomainError
from app.db.task_models import Task, TaskItem
from app.integrations.dify.mock import build_mock_options, build_mock_original
from app.schemas.recommendations import IMAGE_TYPES, PLATFORMS
from app.schemas.tasks import TaskCreateRequest
from app.services.prompt_validation import (
    PROMPT_PACKAGE_LEGACY_VALIDATOR_VERSIONS,
    PROMPT_PACKAGE_VALIDATOR_VERSION,
)
from app.services.tasks import (
    TaskOptions,
    TaskService,
    _load_task_analysis_snapshot,
    _retry_generation_command,
    canonical_request_digest,
    idempotency_key_hash,
    normalize_task_request,
    resolve_preferences,
)
from app.services.trusted_analysis import (
    TRUSTED_ANALYSIS_STATE_KEY,
    build_trusted_analysis_snapshot,
)
from pydantic import ValidationError

_CUSTOM_TEXT = "  材质：引号 ' 与 SQL select；换行\n保持原样  "


def _payload(*, selected_types: list[str] | None = None) -> dict[str, object]:
    return {
        "schema_version": 2,
        "reference_set_id": str(uuid4()),
        "recommendation_id": str(uuid4()),
        "recommendation_revision": 1,
        "preferences": {
            "language": {"mode": "A"},
            "platform": "TEMU",
            "usage_scene": {"mode": "B"},
            "visual_style": {"mode": "D"},
            "selling_points": {"mode": "custom", "text": _CUSTOM_TEXT},
        },
        "image_types": selected_types or list(IMAGE_TYPES[:3]),
    }


def test_request_strictness_enum_values_and_json_schema_contract() -> None:
    contracts_dir = os.environ.get("TASK_CONTRACTS_DIR")
    schema_path = (
        Path(contracts_dir) / "task-create-v2.schema.json"
        if contracts_dir
        else Path(__file__).resolve().parents[4]
        / "docs"
        / "contracts"
        / "task-create-v2.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert list(PLATFORMS) == schema["$defs"]["platform"]["enum"]
    assert list(IMAGE_TYPES) == schema["$defs"]["image_type"]["enum"]
    assert TaskCreateRequest.model_validate(_payload()).preferences.selling_points.text == (
        _CUSTOM_TEXT
    )
    assert router.prefix == "/api/v1/tasks"

    extra = _payload()
    extra["user_id"] = str(uuid4())
    duplicate = _payload(selected_types=[IMAGE_TYPES[0], IMAGE_TYPES[0]])
    invalid_type = _payload(selected_types=["不在合同中的类型"])
    forged_text = _payload()
    forged_text["preferences"]["language"] = {"mode": "A", "text": "由用户伪造"}
    invalid_custom = _payload()
    invalid_custom["preferences"]["selling_points"] = {"mode": "custom", "text": " \n "}
    invalid_platform = _payload()
    invalid_platform["preferences"]["platform"] = "https://example.invalid"
    for value in (extra, duplicate, invalid_type, forged_text, invalid_custom, invalid_platform):
        with pytest.raises(ValidationError):
            TaskCreateRequest.model_validate(value)


def test_image_type_order_is_canonical_and_custom_text_is_not_trimmed() -> None:
    first_payload = _payload(selected_types=[IMAGE_TYPES[4], IMAGE_TYPES[0], IMAGE_TYPES[2]])
    reordered_payload = _payload(selected_types=[IMAGE_TYPES[2], IMAGE_TYPES[4], IMAGE_TYPES[0]])
    reordered_payload["reference_set_id"] = first_payload["reference_set_id"]
    reordered_payload["recommendation_id"] = first_payload["recommendation_id"]
    first_payload["recommendation_id"] = reordered_payload["recommendation_id"]
    first_payload["preferences"] = reordered_payload["preferences"]
    first_payload["recommendation_revision"] = reordered_payload["recommendation_revision"]
    first = TaskCreateRequest.model_validate(first_payload)
    second = TaskCreateRequest.model_validate(reordered_payload)

    assert normalize_task_request(first)["image_types"] == [
        IMAGE_TYPES[0],
        IMAGE_TYPES[2],
        IMAGE_TYPES[4],
    ]
    assert canonical_request_digest(first) == canonical_request_digest(second)
    assert first.preferences.selling_points.text == _CUSTOM_TEXT

    changed = _payload(selected_types=list(first.image_types))
    changed["reference_set_id"] = first_payload["reference_set_id"]
    changed["recommendation_id"] = first_payload["recommendation_id"]
    changed["recommendation_revision"] = first_payload["recommendation_revision"]
    changed["preferences"] = json.loads(
        json.dumps(first_payload["preferences"], ensure_ascii=False)
    )
    changed["preferences"]["selling_points"]["text"] = _CUSTOM_TEXT.strip()
    assert canonical_request_digest(
        TaskCreateRequest.model_validate(changed)
    ) != canonical_request_digest(first)


def test_idempotency_key_is_hashed_and_rejects_empty_whitespace_and_oversize() -> None:
    hashed = idempotency_key_hash("browser-tab:7a0f2c")
    assert len(hashed) == 32
    assert hashed != b"browser-tab:7a0f2c"
    for key in ("", "bad key", "雪", "x" * 201):
        with pytest.raises(DomainError):
            idempotency_key_hash(key)


def test_preferences_resolve_field_specific_abc_and_keep_custom_text() -> None:
    options = build_mock_options()
    original = build_mock_original("台灯", options)
    request_payload = _payload()
    request_payload["preferences"] = {
        "language": {"mode": "C"},
        "platform": "TEMU",
        "usage_scene": {"mode": "B"},
        "visual_style": {"mode": "D"},
        "selling_points": {"mode": "custom", "text": _CUSTOM_TEXT},
    }
    request = TaskCreateRequest.model_validate(request_payload)

    resolved = resolve_preferences(
        request,
        public_options_value=options,
        original_recommendation=original,
        category="台灯",
        recommendation_revision=7,
    )

    assert resolved["language"] == {
        "mode": "C",
        "text": original["field_texts"]["language"][2],
        "source": "recommendation_original",
        "recommendation_revision": 7,
    }
    assert resolved["usage_scene"]["text"] == original["field_texts"]["usage_scene"][1]
    assert resolved["visual_style"]["kind"] == "agent_design"
    assert resolved["selling_points"]["text"] == _CUSTOM_TEXT
    assert resolved["platform"] == {"mode": "platform", "value": "TEMU"}


def test_invalid_or_changed_recommendation_snapshot_is_rejected_safely() -> None:
    options = build_mock_options()
    original = build_mock_original("台灯", options)
    request = TaskCreateRequest.model_validate(_payload())
    mismatch = dict(original)
    mismatch["category_snapshot"] = "另一品类"
    with pytest.raises(DomainError, match="推荐内容与参考组不匹配"):
        resolve_preferences(
            request,
            public_options_value=options,
            original_recommendation=mismatch,
            category="台灯",
            recommendation_revision=1,
        )
    damaged = build_mock_original("台灯", options)
    damaged_fields = dict(damaged["field_texts"])
    damaged_fields["language"] = ["被改过"]
    damaged["field_texts"] = damaged_fields
    with pytest.raises(DomainError, match="推荐内容暂不可用"):
        resolve_preferences(
            request,
            public_options_value=options,
            original_recommendation=damaged,
            category="台灯",
            recommendation_revision=1,
        )


def test_task_analysis_snapshot_binding_checks_identity_digest_and_saved_assets() -> None:
    reference_set_id = uuid4()
    recommendation_id = uuid4()
    reference = {
        "alias": "PRIMARY",
        "position": 0,
        "reference_set_image_id": uuid4(),
        "model_asset_id": uuid4(),
        "source_asset_id": uuid4(),
        "model_sha256": "a" * 64,
        "source_sha256": "b" * 64,
        "model_mime_type": "image/png",
        "source_mime_type": "image/png",
        "model_size_bytes": 128,
        "source_size_bytes": 256,
        "model_width": 32,
        "model_height": 32,
        "source_width": 64,
        "source_height": 64,
    }
    snapshot = build_trusted_analysis_snapshot(
        reference_set_id=reference_set_id,
        recommendation_id=recommendation_id,
        recommendation_revision=3,
        category="台灯",
        references=[reference],
        created_at=datetime.now(UTC),
    )
    recommendation = SimpleNamespace(
        id=recommendation_id,
        revision=3,
        state={TRUSTED_ANALYSIS_STATE_KEY: snapshot.model_dump(mode="json")},
    )

    loaded = _load_task_analysis_snapshot(
        recommendation,
        reference_set_id=reference_set_id,
        category="台灯",
        reference_assets=[{**reference, "slot": "PRIMARY"}],
    )
    assert loaded.snapshot_id == snapshot.snapshot_id
    assert loaded.snapshot_digest == snapshot.snapshot_digest

    with pytest.raises(DomainError) as changed_asset:
        _load_task_analysis_snapshot(
            recommendation,
            reference_set_id=reference_set_id,
            category="台灯",
            reference_assets=[{**reference, "slot": "PRIMARY", "model_sha256": "c" * 64}],
        )
    assert changed_asset.value.code == "RECOMMENDATION_UNAVAILABLE"

    legacy = SimpleNamespace(id=recommendation_id, revision=3, state={"fixture_id": "old"})
    with pytest.raises(DomainError) as missing_snapshot:
        _load_task_analysis_snapshot(
            legacy,
            reference_set_id=reference_set_id,
            category="台灯",
            reference_assets=[{**reference, "slot": "PRIMARY"}],
        )
    assert missing_snapshot.value.code == "RECOMMENDATION_UNAVAILABLE"


def test_public_projection_does_not_serialize_saved_inputs_or_prompts() -> None:
    task = Task(
        id=uuid4(),
        owner_user_id=uuid4(),
        reference_set_id=uuid4(),
        recommendation_id=uuid4(),
        recommendation_revision=1,
        recommendation_snapshot_version="synthetic-recommendation-v1",
        schema_version=2,
        workflow_version="business-task-v1",
        request_digest=b"r" * 32,
        request_digest_version=1,
        request_snapshot={"preferences": {"selling_points": {"text": _CUSTOM_TEXT}}},
        resolved_preferences={"selling_points": {"text": _CUSTOM_TEXT}},
        recommendation_snapshot={"original_recommendation": {"basis": "internal"}},
        reference_assets_snapshot=[{"model_asset_id": str(uuid4())}],
        category_snapshot="台灯",
        image_types=[IMAGE_TYPES[0]],
        planned_count=1,
        billing_mode="disabled",
        billing_policy_version="disabled-v1",
        billing_snapshot={"charged_units": 0},
        status="queued",
        retention_days=60,
        retention_policy_revision=1,
    )
    item = TaskItem(
        id=uuid4(),
        task_id=task.id,
        position=1,
        image_type=IMAGE_TYPES[0],
        status="queued",
        preview_ready=False,
    )

    response = TaskService._to_public_response(task, [item]).model_dump(mode="json")
    assert response["counts"] == {
        "planned": 1,
        "succeeded": 0,
        "failed": 0,
        "pending": 1,
        "unresolved": 0,
    }
    assert set(response) == {"schema_version", "task_id", "status", "counts", "items"}
    serialized = json.dumps(response, ensure_ascii=False)
    assert _CUSTOM_TEXT not in serialized
    assert "original_recommendation" not in serialized
    assert "model_asset_id" not in serialized


def test_task_options_pin_free_versioned_defaults() -> None:
    options = TaskOptions()
    assert options.schema_version == 2
    assert options.workflow_version == "business-task-v1"
    assert options.billing_mode == "disabled"
    assert options.billing_policy_version == "disabled-v1"
    assert options.retention_days == 60
    assert options.event_operation_version == 1
    assert options.event_schema_version == 1
    assert options.max_nonterminal_per_owner == 2
    with pytest.raises(ValueError):
        TaskOptions(billing_mode="enabled")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TaskOptions(max_nonterminal_per_owner=0)


def _retry_generation_fixture(validator_version: str = PROMPT_PACKAGE_VALIDATOR_VERSION):
    task_id, item_id, package_id = uuid4(), uuid4(), uuid4()
    aliases = ("PRIMARY", "AUX01", "AUX02")
    asset_ids = tuple(uuid4() for _ in aliases)
    task = SimpleNamespace(id=task_id)
    item = SimpleNamespace(
        id=item_id,
        task_id=task_id,
        image_type=IMAGE_TYPES[0],
        position=1,
    )
    package = SimpleNamespace(
        id=package_id,
        task_id=task_id,
        validator_version=validator_version,
        package_digest_version=1,
        package_digest=b"p" * 32,
        chatflow_contract_version="v3.3",
    )
    prompt_item = SimpleNamespace(
        package_id=package_id,
        task_id=task_id,
        task_item_id=item_id,
        image_type=IMAGE_TYPES[0],
        position=1,
        design_prompt="trusted design",
        identity_prompt="trusted identity",
        identity_anchor_asset_id=asset_ids[0],
        allowed_reference_aliases=list(aliases),
        excluded_reference_aliases=[],
        allowed_reference_asset_ids=[str(asset_id) for asset_id in asset_ids],
    )
    return task, item, package, prompt_item, dict(zip(aliases, asset_ids, strict=True))


@pytest.mark.parametrize(
    "validator_version",
    [PROMPT_PACKAGE_VALIDATOR_VERSION, *PROMPT_PACKAGE_LEGACY_VALIDATOR_VERSIONS],
)
def test_retry_command_uses_every_task_bound_reference_for_known_versions(
    validator_version: str,
) -> None:
    task, item, package, prompt_item, snapshot_aliases = _retry_generation_fixture(
        validator_version
    )

    command = _retry_generation_command(
        task=task,
        item=item,
        package=package,
        prompt_item=prompt_item,
        request_key=uuid4(),
        snapshot_aliases=snapshot_aliases,
    )

    assert command.reference_asset_ids == tuple(snapshot_aliases.values())


def test_legacy_retry_command_rejects_old_partial_or_excluded_reference_rows() -> None:
    legacy_version = next(iter(PROMPT_PACKAGE_LEGACY_VALIDATOR_VERSIONS))
    task, item, package, prompt_item, snapshot_aliases = _retry_generation_fixture(legacy_version)
    prompt_item.allowed_reference_aliases = ["PRIMARY", "AUX01"]
    prompt_item.allowed_reference_asset_ids = [
        str(snapshot_aliases["PRIMARY"]),
        str(snapshot_aliases["AUX01"]),
    ]
    prompt_item.excluded_reference_aliases = ["AUX02"]

    with pytest.raises(DomainError) as caught:
        _retry_generation_command(
            task=task,
            item=item,
            package=package,
            prompt_item=prompt_item,
            request_key=uuid4(),
            snapshot_aliases=snapshot_aliases,
        )

    assert caught.value.code == "PROMPT_NOT_RETRYABLE"


@pytest.mark.parametrize(
    "mutation",
    ["subset", "excluded", "foreign_asset", "cross_task", "unknown_validator"],
)
def test_retry_command_fails_closed_for_partial_or_unbound_reference_plans(
    mutation: str,
) -> None:
    task, item, package, prompt_item, snapshot_aliases = _retry_generation_fixture()
    if mutation == "subset":
        prompt_item.allowed_reference_aliases = ["PRIMARY", "AUX01"]
        prompt_item.allowed_reference_asset_ids = [
            str(snapshot_aliases["PRIMARY"]),
            str(snapshot_aliases["AUX01"]),
        ]
    elif mutation == "excluded":
        prompt_item.excluded_reference_aliases = ["AUX02"]
    elif mutation == "foreign_asset":
        prompt_item.allowed_reference_asset_ids[-1] = str(uuid4())
    elif mutation == "cross_task":
        prompt_item.task_id = uuid4()
    else:
        package.validator_version = "unknown-validator-v9"

    with pytest.raises(DomainError) as caught:
        _retry_generation_command(
            task=task,
            item=item,
            package=package,
            prompt_item=prompt_item,
            request_key=uuid4(),
            snapshot_aliases=snapshot_aliases,
        )

    assert caught.value.code == "PROMPT_NOT_RETRYABLE"
