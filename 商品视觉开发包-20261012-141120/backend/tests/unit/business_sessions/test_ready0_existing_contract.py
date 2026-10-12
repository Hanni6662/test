from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from app.schemas.chatflow_v34 import (
    V34_FLOW_VERSION,
    V34_STAGE_CONTRACT,
    ChatflowV34StageEvent,
    chatflow_v34_stage_event_digest,
)
from pydantic import ValidationError

_REPO_ROOT = Path(__file__).resolve().parents[4]
_V8_PATH = _REPO_ROOT / "chatflow/work-copies/v34-backend-handoff-v8-20261009.yml"
_V8_SHA256 = "f3c5d84dfefcffbfd2695081dd20f7b0e3606521a5679fecb2067280e1132861"


def _yaml_code_main(node_id: str):
    """Compile the unchanged code block belonging to one named V8 Code node."""
    assert hashlib.sha256(_V8_PATH.read_bytes()).hexdigest() == _V8_SHA256
    lines = _V8_PATH.read_text(encoding="utf-8").splitlines()
    node_matches = [
        index for index, line in enumerate(lines) if line.strip() == f"id: {node_id}"
    ]
    assert len(node_matches) == 1, f"expected one V8 node {node_id!r}, got {node_matches}"
    node_index = node_matches[0]
    node_start = max(
        index
        for index in range(node_index)
        if lines[index].startswith("    - data:")
    )
    code_matches = [
        index
        for index in range(node_start, node_index)
        if lines[index].strip() == "code: |"
    ]
    assert len(code_matches) == 1, f"expected one literal code block in {node_id!r}"
    code_index = code_matches[0]

    body: list[str] = []
    for line in lines[code_index + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= 8:
            break
        body.append(line[10:] if line.strip() else "")
    namespace: dict[str, object] = {}
    exec("\n".join(body).rstrip() + "\n", namespace)
    main = namespace.get("main")
    assert callable(main), f"V8 node {node_id!r} did not define main"
    return main


def _valid_context() -> dict[str, object]:
    return {
        "contract_version": V34_STAGE_CONTRACT,
        "flow_version": V34_FLOW_VERSION,
        "business_session_id": str(uuid4()),
        "recommendation_id": str(uuid4()),
        "recommendation_revision": 1,
        "start_operation_id": str(uuid4()),
    }


def _valid_review_state(issues: dict[str, str] | None = None) -> dict[str, object]:
    return {
        "selections": {str(field_id): f"choice-{field_id}" for field_id in range(1, 6)},
        "issues": {"3": "identity mismatch"} if issues is None else issues,
    }


def _event_from_yaml_main() -> dict[str, object]:
    main = _yaml_code_main("v34_identity_conflict_event")
    context = _valid_context()
    result = main(
        json.dumps(context, ensure_ascii=False),
        "run-ready0-contract-1",
        json.dumps(_valid_review_state(), ensure_ascii=False),
        "no",
    )
    assert result["ready"] == "yes"
    return json.loads(result["body"])


def test_v8_work_copy_hash_is_the_frozen_ready0_baseline() -> None:
    assert hashlib.sha256(_V8_PATH.read_bytes()).hexdigest() == _V8_SHA256


def test_original_ready0_event_function_builds_schema_valid_deterministic_event() -> None:
    event_data = _event_from_yaml_main()
    event = ChatflowV34StageEvent.model_validate_json(
        json.dumps(event_data, ensure_ascii=False), strict=True
    )

    assert event.node_id == "ready_0"
    assert event.event_type == "pre_generation.incomplete"
    assert event.payload == {
        "reason_code": "PRODUCT_IDENTITY_CONFLICT",
        "conflicting_field_ids": ["3"],
    }
    assert event.event_key == (
        "pre_generation.incomplete:"
        + hashlib.sha256(event.dify_run_id.encode("utf-8")).hexdigest()
    )
    assert event.digest == chatflow_v34_stage_event_digest(event).hex()


@pytest.mark.parametrize(
    ("issues", "expected_fields"),
    [
        ({"3": "field three"}, ["3"]),
        ({"4": "field four"}, ["4"]),
        ({"5": "field five"}, ["5"]),
        ({"5": "field five", "3": "field three", "4": "field four"}, ["3", "4", "5"]),
    ],
)
def test_original_ready0_event_function_emits_only_identity_conflict_fields(
    issues: dict[str, str], expected_fields: list[str]
) -> None:
    result = _yaml_code_main("v34_identity_conflict_event")(
        json.dumps(_valid_context(), ensure_ascii=False),
        "run-ready0-multi-field",
        json.dumps(_valid_review_state(issues), ensure_ascii=False),
        "no",
    )

    event = ChatflowV34StageEvent.model_validate_json(result["body"], strict=True)
    assert result["ready"] == "yes"
    assert event.payload == {
        "reason_code": "PRODUCT_IDENTITY_CONFLICT",
        "conflicting_field_ids": expected_fields,
    }


@pytest.mark.parametrize(
    ("context_change", "run_id", "state_change", "approved"),
    [
        ({}, "run-ready0-invalid-approval", {}, "yes"),
        ({"contract_version": "wrong-contract"}, "run-ready0-invalid-context", {}, "no"),
        ({"flow_version": "legacy"}, "run-ready0-invalid-context", {}, "no"),
        ({"business_session_id": "not-a-uuid"}, "run-ready0-invalid-context", {}, "no"),
        ({"recommendation_revision": True}, "run-ready0-invalid-context", {}, "no"),
        ({}, "run/ready0/unsafe", {}, "no"),
        ({}, "run-ready0-empty-issues", {"issues": {}}, "no"),
        ({}, "run-ready0-missing-issues", {"drop_issues": True}, "no"),
        ({}, "run-ready0-invalid-field", {"issues": {"1": "not allowed"}}, "no"),
        ({}, "run-ready0-invalid-field", {"issues": {"2": "not allowed"}}, "no"),
        ({}, "run-ready0-blank-issue", {"issues": {"3": "  "}}, "no"),
        ({}, "run-ready0-incomplete-selections", {"drop_selections": True}, "no"),
    ],
)
def test_original_ready0_event_function_fails_closed_for_unconstructible_cases(
    context_change: dict[str, object],
    run_id: str,
    state_change: dict[str, object],
    approved: str,
) -> None:
    context = _valid_context()
    context.update(context_change)
    state = _valid_review_state()
    changes = dict(state_change)
    if changes.pop("drop_issues", False):
        state.pop("issues")
    if changes.pop("drop_selections", False):
        state.pop("selections")
    state.update(changes)

    result = _yaml_code_main("v34_identity_conflict_event")(
        json.dumps(context, ensure_ascii=False),
        run_id,
        json.dumps(state, ensure_ascii=False),
        approved,
    )

    assert result == {
        "ready": "no",
        "body": "{}",
        "business_session_id": "",
        "event_key": "",
    }


def test_original_receipt_main_treats_accepted_and_replayed_as_ok() -> None:
    event_data = _event_from_yaml_main()
    main = _yaml_code_main("v34_identity_conflict_receipt")

    for status in ("accepted", "replayed"):
        response = {
            "status": status,
            "business_session_id": event_data["business_session_id"],
            "event_key": event_data["event_key"],
        }
        assert main(
            json.dumps(response),
            200,
            event_data["business_session_id"],
            event_data["event_key"],
        ) == {"ok": "yes"}

    rejected = {
        "status": "rejected",
        "business_session_id": event_data["business_session_id"],
        "event_key": event_data["event_key"],
    }
    assert main(
        json.dumps(rejected),
        200,
        event_data["business_session_id"],
        event_data["event_key"],
    ) == {"ok": "no"}


def test_stage_schema_rejects_wrong_node_context_even_with_recomputed_digest() -> None:
    invalid = _event_from_yaml_main()
    invalid["node_id"] = "parse_0"
    canonical = json.dumps(
        {key: value for key, value in invalid.items() if key != "digest"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    invalid["digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    with pytest.raises(ValidationError):
        ChatflowV34StageEvent.model_validate_json(
            json.dumps(invalid, ensure_ascii=False), strict=True
        )
