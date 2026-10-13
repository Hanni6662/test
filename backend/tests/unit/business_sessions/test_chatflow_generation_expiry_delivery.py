from __future__ import annotations

from datetime import UTC, datetime
from itertools import cycle
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

from app.db.chatflow_generation_models import (
    ChatflowGenerationAcceptanceEvidence,
    ChatflowGenerationRequest,
    ChatflowGenerationTracking,
)
from app.db.task_models import Task
from app.services.chatflow_generation_expiry import mark_accepted_batch_delivery_progress

_REQUEST_ID = UUID("00000000-0000-4000-8000-000000000301")
_TASK_ID = UUID("00000000-0000-4000-8000-000000000302")
_ITEM_ID = UUID("00000000-0000-4000-8000-000000000303")
_NOW = datetime(2026, 10, 10, 13, 0, tzinfo=UTC)
_BATCH_ID = "batch-accepted-301"
_ACCEPTANCE_DIGEST = b"a" * 32


class _Result:
    def __init__(self, value=None, *, rows=None) -> None:
        self._value = value
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._rows


def _delivery_context(*, item_status: str, delivery_status: str | None):
    followup = SimpleNamespace(
        generation_request_id=_REQUEST_ID,
        task_id=_TASK_ID,
        batch_id=_BATCH_ID,
        acceptance_digest=_ACCEPTANCE_DIGEST,
        state="awaiting_delivery",
        resolved_at=None,
        next_action_at=None,
        updated_at=None,
    )
    request = SimpleNamespace(id=_REQUEST_ID, task_id=_TASK_ID)
    task = SimpleNamespace(id=_TASK_ID, status="succeeded", terminal_at=_NOW)
    evidence = SimpleNamespace(
        generation_request_id=_REQUEST_ID,
        outcome="accepted",
        batch_id=_BATCH_ID,
        evidence_digest=_ACCEPTANCE_DIGEST,
    )
    tracking = SimpleNamespace(
        generation_request_id=_REQUEST_ID,
        batch_id=_BATCH_ID,
        reconcile_state="complete",
    )
    item = SimpleNamespace(id=_ITEM_ID, status=item_status)
    deliveries = (
        [SimpleNamespace(task_item_id=_ITEM_ID, status=delivery_status)]
        if delivery_status is not None
        else []
    )
    db = Mock()
    get_rows = {
        ChatflowGenerationRequest: request,
        Task: task,
        ChatflowGenerationAcceptanceEvidence: evidence,
        ChatflowGenerationTracking: tracking,
    }
    db.get.side_effect = lambda model, _identity: get_rows.get(model)
    results = cycle(
        [
            _Result(followup),
            _Result(rows=[item]),
            _Result(rows=deliveries),
        ]
    )
    db.execute.side_effect = lambda _statement: next(results)
    return SimpleNamespace(
        db=db,
        followup=followup,
        task=task,
        evidence=evidence,
        tracking=tracking,
        item=item,
        deliveries=deliveries,
    )


def test_followup_resolves_only_after_terminal_task_and_confirmed_delivery() -> None:
    context = _delivery_context(item_status="succeeded", delivery_status="awaiting_confirmation")

    mark_accepted_batch_delivery_progress(context.db, _REQUEST_ID, now=_NOW)

    assert context.followup.state == "awaiting_delivery"
    assert context.followup.resolved_at is None

    context.deliveries[0].status = "confirmed"
    resolved_at = datetime(2026, 10, 10, 13, 1, tzinfo=UTC)
    mark_accepted_batch_delivery_progress(context.db, _REQUEST_ID, now=resolved_at)

    assert context.followup.state == "complete"
    assert context.followup.resolved_at == resolved_at
    assert context.followup.next_action_at is None

    later = datetime(2026, 10, 10, 13, 2, tzinfo=UTC)
    mark_accepted_batch_delivery_progress(context.db, _REQUEST_ID, now=later)
    assert context.followup.resolved_at == resolved_at


def test_failed_terminal_item_needs_no_success_delivery_to_resolve() -> None:
    context = _delivery_context(item_status="failed", delivery_status=None)
    context.task.status = "failed"

    mark_accepted_batch_delivery_progress(context.db, _REQUEST_ID, now=_NOW)

    assert context.followup.state == "complete"
    assert context.followup.resolved_at == _NOW


def test_conflicting_acceptance_or_tracking_batch_does_not_resolve() -> None:
    context = _delivery_context(item_status="succeeded", delivery_status="confirmed")
    context.evidence.batch_id = "batch-other"

    mark_accepted_batch_delivery_progress(context.db, _REQUEST_ID, now=_NOW)

    assert context.followup.state == "awaiting_delivery"
    assert context.followup.resolved_at is None
