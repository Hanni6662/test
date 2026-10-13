"""Read-only proof that a historical submit uncertainty has been resolved.

This is an exception for one submit Action, not general retry authorization.
The caller still validates ownership, item state, prompts, expiry and every
other Action while holding the existing retry transaction locks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Sequence

from app.db.action_models import Action
from app.db.generation_models import GenerationAttemptContext
from app.db.task_models import TaskItemAttempt


def is_unknown_action(action: Action) -> bool:
    return action.status == "unknown" or action.last_outcome == "unknown"


def _same_attempt(
    action: Action, attempt: TaskItemAttempt, context: GenerationAttemptContext
) -> bool:
    return (
        action.task_id == context.task_id
        and action.task_item_id == context.task_item_id == attempt.task_item_id
        and action.attempt_id == action.scope_id == context.attempt_id == attempt.id
        and action.scope_kind == "attempt"
        and action.sequence_namespace == "default"
        and type(action.sequence) is int
        and action.sequence > 0
        and action.input_digest_version == 1
        and isinstance(action.input_digest, (bytes, bytearray, memoryview))
        and len(action.input_digest) == 32
    )


def is_resolved_submit_unknown(
    action: Action,
    attempt: TaskItemAttempt,
    context: GenerationAttemptContext,
    actions: Sequence[Action],
) -> bool:
    """Recognize same-attempt recovered acceptance followed by known job failure.

    Keep the original unknown/outcome untouched. Reject other uncertainty kinds,
    active work, mismatched scopes, unknown digest versions and unbound failures.
    No provider call, query, write or mock-specific table is used here.
    """
    job_id = attempt.provider_job_id
    if (
        not is_unknown_action(action)
        or action.action_kind != "generation.submit.v1"
        or action.status not in {"unknown", "completed"}
        or action.last_outcome != "unknown"
        or action.completed_at is None
        or attempt.status != "failed"
        or context.save_state != "failed"
        or context.request_digest_version != 1
        or not isinstance(context.request_digest, (bytes, bytearray, memoryview))
        or len(context.request_digest) != 32
        or not isinstance(job_id, str)
        or not job_id.strip()
        or len(job_id) > 320
        or not actions
        or action.id not in {candidate.id for candidate in actions}
        or any(candidate.status in {"scheduled", "claimed"} for candidate in actions)
        or not all(_same_attempt(candidate, attempt, context) for candidate in actions)
        or len({candidate.sequence for candidate in actions}) != len(actions)
    ):
        return False

    terminal = max(actions, key=lambda candidate: candidate.sequence)
    if (
        terminal.action_kind != "generation.query.v1"
        or terminal.status != "rejected"
        or terminal.last_outcome != "definitely_rejected"
        or terminal.completed_at is None
        or not attempt.failure_code
        or terminal.last_error_code != attempt.failure_code
    ):
        return False

    # This is the existing generation runtime's v1 canonical query input. Bind
    # the final failure to the current job and the frozen request, not only to
    # an 'accepted' status somewhere in the Action history.
    document = {
        "action_kind": "generation.query.v1",
        "attempt_id": str(attempt.id),
        "request_digest": bytes(context.request_digest).hex(),
        "sequence": terminal.sequence,
        "extra": {"provider_job_id": job_id},
    }
    digest = hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).digest()
    if not hmac.compare_digest(bytes(terminal.input_digest), digest):
        return False

    return any(
        candidate.action_kind == "generation.recover.v1"
        and candidate.status == "completed"
        and candidate.last_outcome == "accepted"
        and candidate.completed_at is not None
        and action.sequence < candidate.sequence < terminal.sequence
        for candidate in actions
    )
