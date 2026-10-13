from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

import pytest
from app.schemas.chatflow_prompt_packages import ChatflowPromptPackageAccepted
from app.schemas.recommendations import IMAGE_TYPES
from app.services.chatflow_v34_prompt_contract import (
    prompt_package_submission_id,
    selection_evidence_matches_action,
    validate_prompt_package_acceptance,
)

_NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
_TASK_ID = UUID("d57311c2-2fd3-4ad2-89ac-80241a9cb78b")
_SELECTION_SUBMISSION_ID = UUID("a8b12346-2572-44ef-a9c1-416cbd7bcf18")
_PROMPT_SUBMISSION_ID = prompt_package_submission_id(_SELECTION_SUBMISSION_ID)
_PACKAGE_DIGEST = "a" * 64
_PERMIT_TOKEN = "private-permit-token-v2"
_PERMIT_DIGEST = hashlib.sha256(_PERMIT_TOKEN.encode("ascii")).hexdigest()


def _accepted_response(
    *,
    image_types: tuple[str, ...] | list[str] = IMAGE_TYPES[:3],
    status: str = "accepted",
) -> dict[str, object]:
    slots = [
        {
            "task_item_id": str(uuid4()),
            "attempt_id": str(uuid4()),
            "slot_id": f"slot_{position:02d}",
            "position": position,
            "image_type": image_type,
        }
        for position, image_type in enumerate(image_types, start=1)
    ]
    return {
        "status": status,
        "contract_version": "chatflow-handoff-v1",
        "task_id": str(_TASK_ID),
        "submission_id": str(_PROMPT_SUBMISSION_ID),
        "generation_request_id": str(
            uuid5(_TASK_ID, f"chatflow-generation-request:{_PROMPT_SUBMISSION_ID}")
        ),
        "package_digest": _PACKAGE_DIGEST,
        "slots": slots,
        "permit": {
            "permit_id": str(uuid4()),
            "permit_version": 2,
            "contract_version": "chatflow-generation-permit-v2",
            "slot_mapping_digest": "b" * 64,
            "reference_manifest_digest": "c" * 64,
            "token": _PERMIT_TOKEN,
            "digest": _PERMIT_DIGEST,
            "expires_at": (_NOW + timedelta(minutes=15)).isoformat(),
        },
    }


def _validate(
    response: object,
    *,
    http_status: int = 200,
    expected_task_id: UUID = _TASK_ID,
    expected_submission_id: UUID = _PROMPT_SUBMISSION_ID,
    expected_package_digest: str = _PACKAGE_DIGEST,
    expected_image_types: tuple[str, ...] | list[str] = IMAGE_TYPES[:3],
    now: datetime = _NOW,
) -> ChatflowPromptPackageAccepted:
    return validate_prompt_package_acceptance(
        response,
        http_status=http_status,
        expected_task_id=expected_task_id,
        expected_submission_id=expected_submission_id,
        expected_package_digest=expected_package_digest,
        expected_image_types=expected_image_types,
        now=now,
    )


def test_prompt_package_submission_id_is_stable_and_namespaced_by_selection() -> None:
    expected = uuid5(_SELECTION_SUBMISSION_ID, "chatflow-prompt-package-v1")

    assert prompt_package_submission_id(_SELECTION_SUBMISSION_ID) == expected
    assert prompt_package_submission_id(_SELECTION_SUBMISSION_ID) == expected
    assert prompt_package_submission_id(uuid4()) != expected

    with pytest.raises(TypeError):
        prompt_package_submission_id(str(_SELECTION_SUBMISSION_ID))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("evidence_kind", "action_status", "last_outcome", "expected"),
    [
        ("http_200", "completed", "accepted", True),
        ("http_200", "completed", "unknown", False),
        ("http_200", "unknown", "accepted", False),
        ("selection_applied", "claimed", None, True),
        ("selection_applied", "unknown", "accepted", True),
        ("selection_applied", "completed", "unknown", True),
        ("selection_applied", "completed", "rejected", False),
        ("selection_applied", "unknown", "definitely_rejected", False),
        ("pause_snapshot", "completed", "accepted", False),
        (None, "completed", "accepted", False),
    ],
)
def test_selection_evidence_requires_durable_acceptance_or_applied_event(
    evidence_kind: str | None,
    action_status: str,
    last_outcome: str | None,
    expected: bool,
) -> None:
    assert (
        selection_evidence_matches_action(
            evidence_kind=evidence_kind,
            action_status=action_status,
            last_outcome=last_outcome,
        )
        is expected
    )


@pytest.mark.parametrize("status", ["accepted", "replayed"])
@pytest.mark.parametrize("slot_count", [1, 3, 8])
def test_acceptance_allows_exact_response_and_permit_for_ordered_slots(
    status: str,
    slot_count: int,
) -> None:
    expected_types = IMAGE_TYPES[:slot_count]
    response = _accepted_response(image_types=expected_types, status=status)

    accepted = _validate(response, expected_image_types=expected_types)

    assert isinstance(accepted, ChatflowPromptPackageAccepted)
    assert accepted.status == status
    assert accepted.task_id == _TASK_ID
    assert accepted.submission_id == _PROMPT_SUBMISSION_ID
    assert accepted.package_digest == _PACKAGE_DIGEST
    assert [slot.position for slot in accepted.slots] == list(range(1, slot_count + 1))
    assert [slot.slot_id for slot in accepted.slots] == [
        f"slot_{position:02d}" for position in range(1, slot_count + 1)
    ]
    assert [slot.image_type for slot in accepted.slots] == list(expected_types)
    assert accepted.permit.permit_version == 2
    assert accepted.permit.digest == _PERMIT_DIGEST


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("task_id", uuid4()),
        ("submission_id", uuid4()),
        ("package_digest", "d" * 64),
        ("generation_request_id", uuid4()),
    ],
)
def test_acceptance_rejects_task_submission_package_or_request_identity_mismatch(
    field: str,
    expected: object,
) -> None:
    response = _accepted_response()
    response[field] = str(expected) if isinstance(expected, UUID) else expected

    with pytest.raises(ValueError):
        _validate(response)


@pytest.mark.parametrize("slot_count", [1, 3, 8])
@pytest.mark.parametrize("mutation", ["position", "slot_id", "image_type", "missing_slot"])
def test_acceptance_rejects_slot_position_or_image_type_mismatch(
    slot_count: int,
    mutation: str,
) -> None:
    expected_types = IMAGE_TYPES[:slot_count]
    response = _accepted_response(image_types=expected_types)
    slots = response["slots"]
    assert isinstance(slots, list)

    if mutation == "position":
        slots[0]["position"] = 2
    elif mutation == "slot_id":
        slots[0]["slot_id"] = "slot_02"
    elif mutation == "image_type":
        slots[0]["image_type"] = IMAGE_TYPES[-1]
    else:
        response["slots"] = slots[:-1]

    with pytest.raises(ValueError):
        _validate(response, expected_image_types=expected_types)


@pytest.mark.parametrize("http_status", [201, 302, 412, 500])
def test_acceptance_requires_http_200(http_status: int) -> None:
    with pytest.raises(ValueError):
        _validate(_accepted_response(), http_status=http_status)


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("permit_version", 1),
        ("permit_contract", "chatflow-generation-permit-v1"),
        ("token_digest", "0" * 64),
        ("empty_token", ""),
        ("expired", (_NOW - timedelta(seconds=1)).isoformat()),
        ("naive_expiry", "2030-01-02T03:05:05"),
        ("invalid_expiry", "not-an-iso-timestamp"),
    ],
)
def test_acceptance_rejects_invalid_or_expired_generation_permit(
    mutation: str,
    value: object,
) -> None:
    response = _accepted_response()
    permit = response["permit"]
    assert isinstance(permit, dict)

    if mutation == "permit_version":
        permit["permit_version"] = value
    elif mutation == "permit_contract":
        permit["contract_version"] = value
    elif mutation == "token_digest":
        permit["digest"] = value
    elif mutation == "empty_token":
        permit["token"] = value
    else:
        permit["expires_at"] = value

    with pytest.raises(ValueError):
        _validate(response)


@pytest.mark.parametrize(
    ("kwargs", "response"),
    [
        ({"expected_package_digest": "A" * 64}, _accepted_response()),
        ({"expected_image_types": "portrait"}, _accepted_response()),
        ({"now": datetime(2030, 1, 2, 3, 4, 5)}, _accepted_response()),
        ({"http_status": 200}, {"status": "accepted"}),
    ],
)
def test_acceptance_rejects_invalid_expectations_and_malformed_response(
    kwargs: dict[str, object],
    response: object,
) -> None:
    with pytest.raises(ValueError):
        validate_prompt_package_acceptance(
            response,
            http_status=int(kwargs.get("http_status", 200)),
            expected_task_id=_TASK_ID,
            expected_submission_id=_PROMPT_SUBMISSION_ID,
            expected_package_digest=str(kwargs.get("expected_package_digest", _PACKAGE_DIGEST)),
            expected_image_types=kwargs.get("expected_image_types", IMAGE_TYPES[:3]),  # type: ignore[arg-type]
            now=kwargs.get("now", _NOW),  # type: ignore[arg-type]
        )


def test_acceptance_rejects_duplicate_slot_identities() -> None:
    response = _accepted_response()
    slots = response["slots"]
    assert isinstance(slots, list)
    slots[1]["task_item_id"] = slots[0]["task_item_id"]

    with pytest.raises(ValueError):
        _validate(response)

    response = _accepted_response()
    slots = response["slots"]
    assert isinstance(slots, list)
    slots[1]["attempt_id"] = slots[0]["attempt_id"]
    with pytest.raises(ValueError):
        _validate(response)
