from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid5

from app.schemas.chatflow_prompt_packages import ChatflowPromptPackageAccepted

_PROMPT_SUBMISSION_NAMESPACE = "chatflow-prompt-package-v1"


def prompt_package_submission_id(selection_submission_id: UUID) -> UUID:
    """Return the one prompt-package identity bound to a v3.4 selection Action."""
    if not isinstance(selection_submission_id, UUID):
        raise TypeError("selection submission identity must be a UUID")
    return uuid5(selection_submission_id, _PROMPT_SUBMISSION_NAMESPACE)


def selection_evidence_matches_action(
    *, evidence_kind: str | None, action_status: str, last_outcome: str | None
) -> bool:
    if evidence_kind == "http_200":
        return action_status == "completed" and last_outcome == "accepted"
    return (
        evidence_kind == "selection_applied"
        and action_status in {"claimed", "unknown", "completed"}
        and last_outcome not in {"definitely_rejected", "rejected"}
    )


def validate_prompt_package_acceptance(
    response: object,
    *,
    http_status: int,
    expected_task_id: UUID,
    expected_submission_id: UUID,
    expected_package_digest: str,
    expected_image_types: Sequence[str],
    now: datetime | None = None,
) -> ChatflowPromptPackageAccepted:
    """Validate the complete H4 response before a v3 request can be prepared."""
    if http_status != 200:
        raise ValueError("prompt package was not accepted")
    if not isinstance(expected_task_id, UUID) or not isinstance(expected_submission_id, UUID):
        raise ValueError("prompt package identity is invalid")
    if (
        not isinstance(expected_package_digest, str)
        or len(expected_package_digest) != 64
        or any(char not in "0123456789abcdef" for char in expected_package_digest)
    ):
        raise ValueError("prompt package digest is invalid")
    if not isinstance(expected_image_types, Sequence) or isinstance(
        expected_image_types, (str, bytes)
    ):
        raise ValueError("prompt package image types are invalid")

    try:
        if isinstance(response, ChatflowPromptPackageAccepted):
            accepted = response
        else:
            if not isinstance(response, Mapping) or any(
                not isinstance(key, str) for key in response
            ):
                raise ValueError("response must be a JSON object")
            response_json = json.dumps(
                response,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            accepted = ChatflowPromptPackageAccepted.model_validate_json(response_json, strict=True)
    except (TypeError, ValueError):
        raise ValueError("prompt package response is invalid") from None

    if (
        accepted.status not in {"accepted", "replayed"}
        or accepted.contract_version != "chatflow-handoff-v1"
        or accepted.task_id != expected_task_id
        or accepted.submission_id != expected_submission_id
        or not hmac.compare_digest(accepted.package_digest, expected_package_digest)
        or accepted.generation_request_id
        != uuid5(
            expected_task_id,
            f"chatflow-generation-request:{expected_submission_id}",
        )
        or len(accepted.slots) != len(expected_image_types)
        or [slot.position for slot in accepted.slots]
        != list(range(1, len(expected_image_types) + 1))
        or [slot.slot_id for slot in accepted.slots]
        != [f"slot_{position:02d}" for position in range(1, len(expected_image_types) + 1)]
        or [slot.image_type for slot in accepted.slots] != list(expected_image_types)
        or len({slot.task_item_id for slot in accepted.slots}) != len(accepted.slots)
        or len({slot.attempt_id for slot in accepted.slots}) != len(accepted.slots)
    ):
        raise ValueError("prompt package response does not match the saved Task")

    permit = accepted.permit
    token_digest = hashlib.sha256(permit.token.encode("ascii")).hexdigest()
    if (
        permit.permit_version != 2
        or permit.contract_version != "chatflow-generation-permit-v2"
        or not hmac.compare_digest(permit.digest, token_digest)
    ):
        raise ValueError("generation permit is invalid")
    try:
        expires_at = datetime.fromisoformat(permit.expires_at.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("generation permit expiry is invalid") from None
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("generation permit expiry is invalid")
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("current time must be timezone aware")
    if expires_at.astimezone(UTC) <= current_time.astimezone(UTC):
        raise ValueError("generation permit has expired")
    return accepted
