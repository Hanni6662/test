from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.db.action_models import Action
from app.db.generation_models import GenerationAttemptContext
from app.db.task_models import TaskItemAttempt
from app.services.generation_retry_policy import is_resolved_submit_unknown, is_unknown_action


def _chain():
    task_id, item_id, attempt_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    attempt = TaskItemAttempt(
        id=attempt_id,
        task_item_id=item_id,
        status="failed",
        provider_job_id="provider-known-job",
        failure_code="PROVIDER_FAILED",
    )
    context = GenerationAttemptContext(
        attempt_id=attempt_id,
        task_id=task_id,
        task_item_id=item_id,
        save_state="failed",
        request_digest_version=1,
        request_digest=b"r" * 32,
    )
    actions = []
    for sequence, kind, status, outcome, reason in (
        (1, "generation.submit.v1", "unknown", "unknown", "RESPONSE_LOST"),
        (2, "generation.recover.v1", "completed", "accepted", None),
        (3, "generation.query.v1", "rejected", "definitely_rejected", "PROVIDER_FAILED"),
    ):
        actions.append(
            Action(
                id=uuid4(),
                task_id=task_id,
                task_item_id=item_id,
                attempt_id=attempt_id,
                scope_id=attempt_id,
                scope_kind="attempt",
                sequence_namespace="default",
                sequence=sequence,
                action_kind=kind,
                status=status,
                last_outcome=outcome,
                last_error_code=reason,
                completed_at=now,
                input_digest_version=1,
                input_digest=b"x" * 32,
            )
        )
    _bind_query(actions[-1], attempt, context)
    return attempt, context, actions


def _bind_query(query, attempt, context):
    document = {
        "action_kind": "generation.query.v1",
        "attempt_id": str(attempt.id),
        "request_digest": context.request_digest.hex(),
        "sequence": query.sequence,
        "extra": {"provider_job_id": attempt.provider_job_id},
    }
    query.input_digest = hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).digest()


def test_same_attempt_recovery_and_bound_failure_resolve_only_the_submit_observation():
    attempt, context, actions = _chain()
    before = [(a.status, a.last_outcome, a.last_error_code, a.input_digest) for a in actions]
    assert is_resolved_submit_unknown(actions[0], attempt, context, actions)
    assert [
        (a.status, a.last_outcome, a.last_error_code, a.input_digest) for a in actions
    ] == before
    assert is_unknown_action(actions[0])  # Audit history remains unknown.


@pytest.mark.parametrize(
    "target,field,value",
    [
        ("submit", "action_kind", "generation.download.v1"),
        ("submit", "status", "cancelled"),
        ("submit", "last_outcome", "accepted"),
        ("submit", "completed_at", None),
        ("recovery", "last_outcome", "unknown"),
        ("recovery", "status", "unknown"),
        ("recovery", "completed_at", None),
        ("recovery", "attempt_id", "other"),
        ("recovery", "task_id", "other"),
        ("recovery", "task_item_id", "other"),
        ("recovery", "scope_id", "other"),
        ("recovery", "scope_kind", "generation_request"),
        ("recovery", "sequence_namespace", "unrelated"),
        ("recovery", "input_digest_version", 2),
        ("recovery", "sequence", 4),
        ("query", "action_kind", "generation.download.v1"),
        ("query", "status", "scheduled"),
        ("query", "status", "claimed"),
        ("query", "last_outcome", "unknown"),
        ("query", "last_error_code", "UNRELATED_FAILURE"),
        ("query", "completed_at", None),
        ("query", "input_digest", b"wrong" * 6 + b"xx"),
        ("query", "input_digest_version", 2),
        ("attempt", "status", "reconciling"),
        ("attempt", "provider_job_id", None),
        ("attempt", "provider_job_id", "wrong-job"),
        ("attempt", "failure_code", None),
        ("context", "save_state", "awaiting_result"),
        ("context", "request_digest_version", 2),
        ("context", "request_digest", b"different"),
        ("context", "request_digest", b"s" * 32),
        ("context", "task_id", "other"),
        ("context", "task_item_id", "other"),
        ("context", "attempt_id", "other"),
    ],
)
def test_missing_or_mismatched_evidence_stays_unresolved(target, field, value):
    attempt, context, actions = _chain()
    objects = {
        "submit": actions[0],
        "recovery": actions[1],
        "query": actions[2],
        "attempt": attempt,
        "context": context,
    }
    setattr(objects[target], field, uuid4() if value == "other" else value)
    assert not is_resolved_submit_unknown(actions[0], attempt, context, actions)


def test_recovery_before_a_later_unknown_does_not_resolve_it():
    attempt, context, actions = _chain()
    actions[0].sequence, actions[2].sequence = 3, 4
    _bind_query(actions[2], attempt, context)
    assert not is_resolved_submit_unknown(actions[0], attempt, context, actions)


def test_one_resolved_submit_does_not_hide_another_unknown_step():
    attempt, context, actions = _chain()
    other = Action(
        id=uuid4(),
        task_id=context.task_id,
        task_item_id=context.task_item_id,
        attempt_id=attempt.id,
        scope_id=attempt.id,
        scope_kind="attempt",
        sequence_namespace="default",
        sequence=3,
        action_kind="generation.save.v1",
        status="unknown",
        last_outcome="unknown",
        completed_at=datetime.now(UTC),
        input_digest_version=1,
        input_digest=b"x" * 32,
    )
    actions[2].sequence = 4
    _bind_query(actions[2], attempt, context)
    actions.insert(2, other)
    assert any(
        is_unknown_action(a) and not is_resolved_submit_unknown(a, attempt, context, actions)
        for a in actions
    )


def test_missing_recovery_and_duplicate_sequence_are_not_proofs():
    attempt, context, actions = _chain()
    assert not is_resolved_submit_unknown(actions[0], attempt, context, [actions[0], actions[2]])
    actions[1].sequence = 1
    assert not is_resolved_submit_unknown(actions[0], attempt, context, actions)
