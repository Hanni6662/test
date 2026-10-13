from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from app.db.chatflow_generation_models import (
    ChatflowGenerationAcceptedBatchFollowup,
)
from app.schemas.chatflow_generation import ChatflowGenerationSlotEvent, service_generation_status
from app.schemas.chatflow_v34 import V34_FLOW_VERSION
from app.schemas.generation_acceptance import (
    GENERATION_ACCEPTANCE_CONTRACT,
    GENERATION_IMAGE_CONTRACT_V3,
    GenerationAcceptanceAccepted,
)
from app.services import chatflow_generation_expiry as expiry
from app.services.chatflow_generation_actions import MAX_RECONCILE_QUERIES
from app.services.chatflow_generation_contracts import image_generation_contract_for_flow_version
from app.workers.action_processor import ActionExecutionResult, ActionInput

_REQUEST_ID = UUID("00000000-0000-4000-8000-000000000201")
_TASK_ID = UUID("00000000-0000-4000-8000-000000000202")
_SESSION_ID = UUID("00000000-0000-4000-8000-000000000203")
_PERMIT_ID = UUID("00000000-0000-4000-8000-000000000204")
_AUTHORITY_ID = UUID("00000000-0000-4000-8000-000000000205")
_ITEM_ID = UUID("00000000-0000-4000-8000-000000000206")
_ATTEMPT_ID = UUID("00000000-0000-4000-8000-000000000207")
_NOW = datetime(2026, 10, 10, 12, 45, tzinfo=UTC)
_BATCH_ID = "batch-accepted-201"


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


def _accepted(*, batch_id: str = _BATCH_ID, **overrides: object):
    values: dict[str, object] = {
        "contract_version": GENERATION_ACCEPTANCE_CONTRACT,
        "outcome": "accepted",
        "service_authority_id": _AUTHORITY_ID,
        "generation_contract_version": GENERATION_IMAGE_CONTRACT_V3,
        "generation_request_id": _REQUEST_ID,
        "task_id": _TASK_ID,
        "dify_run_id": "run-v34-unit-201",
        "permit_id": _PERMIT_ID,
        "permit_version": 2,
        "permit_expires_at": datetime(2026, 10, 10, 12, 30, tzinfo=UTC),
        "package_digest": "a" * 64,
        "slot_mapping_digest": "b" * 64,
        "reference_manifest_digest": "c" * 64,
        "service_request_digest": "d" * 64,
        "batch_id": batch_id,
    }
    values.update(overrides)
    return GenerationAcceptanceAccepted.model_validate(values, strict=True)


def _context(
    *,
    h6_count: int = MAX_RECONCILE_QUERIES,
    h6_state: str = "unknown",
    batch_id: str | None = None,
    followup_count: int = 0,
    followup_state: str = "scheduled",
) -> SimpleNamespace:
    request = SimpleNamespace(
        id=_REQUEST_ID,
        task_id=_TASK_ID,
        business_session_id=_SESSION_ID,
        dify_run_id="run-v34-unit-201",
        package_digest=bytes.fromhex("a" * 64),
        service_authority_id=_AUTHORITY_ID,
    )
    permit = SimpleNamespace(
        generation_request_id=_REQUEST_ID,
        task_id=_TASK_ID,
        permit_id=_PERMIT_ID,
        permit_version=2,
        expires_at=datetime(2026, 10, 10, 12, 30, tzinfo=UTC),
        service_authority_id=_AUTHORITY_ID,
        slot_mapping_digest=bytes.fromhex("b" * 64),
        reference_manifest_digest=bytes.fromhex("c" * 64),
    )
    session = SimpleNamespace(
        id=_SESSION_ID,
        dify_run_id="run-v34-unit-201",
        flow_version=V34_FLOW_VERSION,
    )
    tracking = SimpleNamespace(
        generation_request_id=_REQUEST_ID,
        reconcile_query_count=h6_count,
        reconcile_state=h6_state,
        status="submission_unknown",
        batch_id=batch_id,
        source_sequence=7,
        status_digest=b"s" * 32,
        updated_at=None,
        next_reconcile_at=None,
    )
    evidence_digest = b"e" * 32
    evidence = SimpleNamespace(
        generation_request_id=_REQUEST_ID,
        task_id=_TASK_ID,
        outcome="accepted",
        batch_id=_BATCH_ID,
        service_authority_id=_AUTHORITY_ID,
        evidence_digest=evidence_digest,
        permit_id=_PERMIT_ID,
    )
    followup = SimpleNamespace(
        generation_request_id=_REQUEST_ID,
        task_id=_TASK_ID,
        batch_id=_BATCH_ID,
        service_authority_id=_AUTHORITY_ID,
        acceptance_digest=evidence_digest,
        state=followup_state,
        query_count=followup_count,
        next_action_at=_NOW,
        last_error_code=None,
        last_checked_at=None,
        resolved_at=None,
        updated_at=None,
    )
    slot = SimpleNamespace(
        slot_id="slot_01",
        task_item_id=_ITEM_ID,
        attempt_id=_ATTEMPT_ID,
        position=1,
        image_type="主图",
    )
    return SimpleNamespace(
        request=request,
        permit=permit,
        session=session,
        tracking=tracking,
        evidence=evidence,
        followup=followup,
        slots=[slot],
        package=SimpleNamespace(id=uuid4()),
    )


def _fake_prepare_db(context: SimpleNamespace) -> Mock:
    db = Mock()
    db.execute.side_effect = [_Result(context.followup), _Result(context.evidence)]
    return db


def _action(context: SimpleNamespace, *, sequence: int = 1, status: str = "scheduled"):
    return SimpleNamespace(
        id=uuid4(),
        task_id=_TASK_ID,
        business_session_id=_SESSION_ID,
        generation_request_id=_REQUEST_ID,
        scope_kind="generation_request",
        scope_id=_REQUEST_ID,
        action_kind=expiry.ACCEPTED_BATCH_RECOVERY_ACTION,
        sequence=sequence,
        sequence_namespace=expiry.ACCEPTED_BATCH_SEQUENCE_NAMESPACE,
        status=status,
        input_digest_version=1,
        input_digest=expiry._accepted_batch_action_digest(
            request_id=_REQUEST_ID,
            task_id=_TASK_ID,
            session_id=_SESSION_ID,
            batch_id=_BATCH_ID,
            sequence=sequence,
        ),
    )


def _status_input(*, query_number: int = 1, batch_id: str = _BATCH_ID) -> ActionInput:
    slot = {
        "slot_id": "slot_01",
        "task_item_id": str(_ITEM_ID),
        "attempt_id": str(_ATTEMPT_ID),
        "position": 1,
        "image_type": "主图",
    }
    return ActionInput(
        action_id=uuid4(),
        task_id=_TASK_ID,
        scope_kind="generation_request",
        scope_id=_REQUEST_ID,
        action_kind=expiry.ACCEPTED_BATCH_RECOVERY_ACTION,
        sequence=query_number,
        input_digest_version=1,
        input_digest=b"i" * 32,
        payload={
            "generation_request_id": str(_REQUEST_ID),
            "task_id": str(_TASK_ID),
            "business_session_id": str(_SESSION_ID),
            "dify_run_id": "run-v34-unit-201",
            "package_digest": "a" * 64,
            "slot_mapping_digest": "b" * 64,
            "reference_manifest_digest": "c" * 64,
            "image_generation_contract_version": image_generation_contract_for_flow_version(
                V34_FLOW_VERSION
            ),
            "batch_id": batch_id,
            "accepted_batch_id": batch_id,
            "slots": [slot],
            "query_number": query_number,
            "should_query": True,
        },
        sequence_namespace=expiry.ACCEPTED_BATCH_SEQUENCE_NAMESPACE,
        business_session_id=_SESSION_ID,
        generation_request_id=_REQUEST_ID,
    )


def _snapshot(*, batch_id: str = _BATCH_ID, terminal: bool = False) -> dict[str, object]:
    slot = ChatflowGenerationSlotEvent(
        slot_id="slot_01",
        task_item_id=_ITEM_ID,
        attempt_id=_ATTEMPT_ID,
        position=1,
        image_type="主图",
        status="completed" if terminal else "queued",
        status_version=1,
        known_retry_count=0,
    )
    return {
        "contract_version": image_generation_contract_for_flow_version(V34_FLOW_VERSION),
        "permit_version": 2,
        "generation_request_id": str(_REQUEST_ID),
        "task_id": str(_TASK_ID),
        "dify_run_id": "run-v34-unit-201",
        "package_digest": "a" * 64,
        "slot_mapping_digest": "b" * 64,
        "reference_manifest_digest": "c" * 64,
        "batch_id": batch_id,
        "source_sequence": 1,
        "status": service_generation_status([slot]),
        "slots": [{**slot.model_dump(mode="json"), "artifact_ref": []}],
    }


class _StatusPort:
    def __init__(self, snapshot: dict[str, object]) -> None:
        self.snapshot = snapshot
        self.batch_calls: list[tuple[str, str]] = []

    def get_by_request(self, *_args, **_kwargs):
        raise AssertionError("accepted continuation must use the accepted batch id")

    def get_by_batch(self, batch_id: str, *, contract_version: str):
        self.batch_calls.append((batch_id, contract_version))
        return self.snapshot


def test_followup_is_scheduled_once_only_after_h6_budget_is_exhausted(monkeypatch) -> None:
    context = _context()
    db = Mock()
    db.execute.side_effect = [
        _Result(context.request),
        _Result(context.tracking),
        _Result(context.evidence),
        _Result(None),
        _Result(context.permit),
    ]
    db.scalar.return_value = _NOW
    scheduled: list[dict[str, object]] = []
    monkeypatch.setattr(
        expiry,
        "_schedule_accepted_batch_action",
        lambda _db, **values: scheduled.append(values),
    )

    expiry.ensure_accepted_batch_followup_from_tracking(db, _REQUEST_ID)

    rows = [call.args[0] for call in db.add.call_args_list]
    assert len(rows) == 1
    followup = rows[0]
    assert isinstance(followup, ChatflowGenerationAcceptedBatchFollowup)
    assert followup.batch_id == _BATCH_ID
    assert followup.acceptance_digest == context.evidence.evidence_digest
    assert followup.query_count == 0
    assert followup.state == "scheduled"
    assert scheduled == [
        {
            "request": context.request,
            "batch_id": _BATCH_ID,
            "task_id": _TASK_ID,
            "session_id": _SESSION_ID,
            "sequence": 1,
            "due_at": _NOW,
        }
    ]

    duplicate_db = Mock()
    duplicate_db.execute.side_effect = [
        _Result(context.request),
        _Result(context.tracking),
        _Result(context.evidence),
        _Result(followup),
    ]
    expiry.ensure_accepted_batch_followup_from_tracking(duplicate_db, _REQUEST_ID)
    assert duplicate_db.add.call_count == 0
    assert len(scheduled) == 1


@pytest.mark.parametrize(
    ("h6_count", "h6_state", "evidence_outcome", "authority_matches"),
    [
        (MAX_RECONCILE_QUERIES - 1, "unknown", "accepted", True),
        (MAX_RECONCILE_QUERIES, "scheduled", "accepted", True),
        (MAX_RECONCILE_QUERIES, "unknown", "rejected_expired", True),
        (MAX_RECONCILE_QUERIES, "unknown", "accepted", False),
    ],
)
def test_followup_is_not_created_for_ineligible_or_conflicting_evidence(
    monkeypatch, h6_count: int, h6_state: str, evidence_outcome: str, authority_matches: bool
) -> None:
    context = _context(h6_count=h6_count, h6_state=h6_state)
    context.evidence.outcome = evidence_outcome
    if not authority_matches:
        context.evidence.service_authority_id = uuid4()
    db = Mock()
    db.execute.side_effect = [
        _Result(context.request),
        _Result(context.tracking),
        _Result(context.evidence),
    ]
    schedule = Mock()
    monkeypatch.setattr(expiry, "_schedule_accepted_batch_action", schedule)

    expiry.ensure_accepted_batch_followup_from_tracking(db, _REQUEST_ID)

    assert db.execute.call_count == 3
    assert db.add.call_count == 0
    schedule.assert_not_called()


@pytest.mark.parametrize("query_number", range(1, MAX_RECONCILE_QUERIES + 1))
def test_prepare_reserves_independent_query_without_mutating_h6(
    monkeypatch, query_number: int
) -> None:
    context = _context(followup_count=query_number - 1)
    action = _action(context, sequence=query_number)
    before_h6 = (
        context.tracking.reconcile_query_count,
        context.tracking.source_sequence,
        context.tracking.status_digest,
        action.input_digest,
        action.sequence,
        action.sequence_namespace,
    )
    monkeypatch.setattr(
        expiry,
        "_load_h4_rows",
        lambda _db, _action: (
            context.request,
            context.permit,
            context.package,
            context.session,
            context.tracking,
            context.slots,
        ),
    )

    action_input = expiry.prepare_accepted_batch_recovery(_fake_prepare_db(context), action)

    assert action_input.payload["should_query"] is True
    assert action_input.payload["query_number"] == query_number
    assert action_input.payload["batch_id"] == _BATCH_ID
    assert action_input.payload["accepted_batch_id"] == _BATCH_ID
    assert action_input.sequence_namespace == expiry.ACCEPTED_BATCH_SEQUENCE_NAMESPACE
    assert context.followup.query_count == query_number
    assert context.followup.state == "active"
    assert (
        context.tracking.reconcile_query_count,
        context.tracking.source_sequence,
        context.tracking.status_digest,
        action.input_digest,
        action.sequence,
        action.sequence_namespace,
    ) == before_h6


def test_prepare_rejects_sixth_accepted_batch_get_without_new_reservation(monkeypatch) -> None:
    context = _context(followup_count=MAX_RECONCILE_QUERIES)
    action = _action(context, sequence=MAX_RECONCILE_QUERIES + 1)
    monkeypatch.setattr(
        expiry,
        "_load_h4_rows",
        lambda _db, _action: (
            context.request,
            context.permit,
            context.package,
            context.session,
            context.tracking,
            context.slots,
        ),
    )

    with pytest.raises(ValueError, match="not eligible"):
        expiry.prepare_accepted_batch_recovery(_fake_prepare_db(context), action)

    assert context.followup.query_count == MAX_RECONCILE_QUERIES
    assert context.followup.state == "scheduled"


def test_claim_recovery_reuses_reservation_and_never_repeats_get(monkeypatch) -> None:
    context = _context(followup_count=1, followup_state="active")
    action = _action(context, status="claimed")
    monkeypatch.setattr(
        expiry,
        "_load_h4_rows",
        lambda _db, _action: (
            context.request,
            context.permit,
            context.package,
            context.session,
            context.tracking,
            context.slots,
        ),
    )

    action_input = expiry.prepare_accepted_batch_recovery(_fake_prepare_db(context), action)
    interrupted = expiry.recover_interrupted_accepted_batch_recovery(action_input)

    assert action_input.payload["should_query"] is False
    assert context.followup.query_count == 1
    assert interrupted.outcome == "unknown"
    assert interrupted.safe_reason_code == "ACCEPTED_BATCH_READ_INTERRUPTED"
    assert interrupted.value == {"queried": False}


def test_prepare_rejects_early_h6_state_and_batch_mismatch(monkeypatch) -> None:
    context = _context(h6_count=MAX_RECONCILE_QUERIES - 1)
    action = _action(context)
    monkeypatch.setattr(
        expiry,
        "_load_h4_rows",
        lambda _db, _action: (
            context.request,
            context.permit,
            context.package,
            context.session,
            context.tracking,
            context.slots,
        ),
    )
    with pytest.raises(ValueError, match="not eligible"):
        expiry.prepare_accepted_batch_recovery(_fake_prepare_db(context), action)

    context = _context(batch_id="batch-conflict")
    action = _action(context)
    with pytest.raises(ValueError, match="not eligible"):
        expiry.prepare_accepted_batch_recovery(_fake_prepare_db(context), action)


def test_accepted_status_get_requires_exact_batch_id_and_never_posts() -> None:
    expected_contract = image_generation_contract_for_flow_version(V34_FLOW_VERSION)
    port = _StatusPort(_snapshot(batch_id="batch-other", terminal=True))

    result = expiry.execute_accepted_batch_recovery(port, _status_input())

    assert port.batch_calls == [(_BATCH_ID, expected_contract)]
    assert result.outcome == "unknown"
    assert result.safe_reason_code == "IMAGE_SERVICE_SNAPSHOT_INVALID"
    assert result.value == {"queried": True, "snapshot": None}


def test_accepted_status_reads_stop_unknown_after_five_nonterminal_gets() -> None:
    port = _StatusPort(_snapshot(terminal=False))
    results = [
        expiry.execute_accepted_batch_recovery(port, _status_input(query_number=number))
        for number in range(1, MAX_RECONCILE_QUERIES + 1)
    ]

    assert [result.outcome for result in results] == [
        "safe_retry",
        "safe_retry",
        "safe_retry",
        "safe_retry",
        "unknown",
    ]
    assert [number for number, _contract in port.batch_calls] == [_BATCH_ID] * 5
    assert results[-1].safe_reason_code == "GENERATION_RECONCILE_LIMIT_REACHED"


def test_terminal_accepted_status_uses_existing_snapshot_shape() -> None:
    port = _StatusPort(_snapshot(terminal=True))

    result = expiry.execute_accepted_batch_recovery(port, _status_input())

    assert result.outcome == "accepted"
    assert result.value["snapshot"]["batch_id"] == _BATCH_ID
    assert result.value["snapshot"]["terminal"] is True


def test_exhausted_followup_finalizer_stays_unknown_and_preserves_h6_fields(
    monkeypatch,
) -> None:
    context = _context(followup_count=MAX_RECONCILE_QUERIES, followup_state="active")
    action = _action(context, sequence=MAX_RECONCILE_QUERIES)
    db = Mock()
    db.execute.side_effect = [_Result(context.tracking), _Result(context.followup)]
    db.scalar.return_value = _NOW
    monkeypatch.setattr(expiry, "_maybe_resolve_accepted_batch_followup", Mock())
    action_input = _status_input(query_number=MAX_RECONCILE_QUERIES)
    result = ActionExecutionResult(
        "unknown",
        safe_reason_code="GENERATION_RECONCILE_LIMIT_REACHED",
        value={"queried": True, "snapshot": None},
    )

    expiry.finalize_accepted_batch_recovery(db, action, action_input, result)

    assert context.followup.query_count == MAX_RECONCILE_QUERIES
    assert context.followup.state == "unknown"
    assert context.followup.next_action_at is None
    assert context.tracking.reconcile_state == "unknown"
    assert context.tracking.reconcile_query_count == MAX_RECONCILE_QUERIES
    assert context.tracking.source_sequence == 7
    assert context.tracking.status_digest == b"s" * 32


def test_accepted_evidence_conflict_detects_other_batch_or_authority(monkeypatch) -> None:
    context = _context()
    action = _action(context)
    acceptance = _accepted()
    monkeypatch.setattr(
        expiry,
        "_load_h4_rows",
        lambda _db, _action: (
            context.request,
            context.permit,
            context.package,
            context.session,
            context.tracking,
            context.slots,
        ),
    )

    db = Mock()
    db.execute.side_effect = [_Result(context.tracking), _Result(context.evidence)]
    assert expiry._accepted_observation_conflicts(db, action, acceptance) is False

    context.tracking.batch_id = "batch-other"
    db = Mock()
    db.execute.side_effect = [_Result(context.tracking), _Result(context.evidence)]
    assert expiry._accepted_observation_conflicts(db, action, acceptance) is True

    context = _context()
    context.evidence.service_authority_id = uuid4()
    db = Mock()
    db.execute.side_effect = [_Result(context.tracking), _Result(context.evidence)]
    assert expiry._accepted_observation_conflicts(db, action, acceptance) is True


def test_duplicate_accepted_evidence_is_idempotent_but_changed_proof_conflicts() -> None:
    original = _accepted()
    action = _action(_context())
    existing = SimpleNamespace(
        task_id=_TASK_ID,
        outcome="accepted",
        batch_id=original.batch_id,
        service_authority_id=original.service_authority_id,
        permit_id=original.permit_id,
        evidence_digest=expiry.canonical_sha256(original.model_dump(mode="json")),
        receipt_digest=None,
        closed_at=None,
    )
    db = Mock()
    db.execute.return_value = _Result(existing)

    assert (
        expiry._evidence_row(
            db,
            action=action,
            acceptance=original,
            outcome="accepted",
            closed_at=None,
        )
        is existing
    )
    db.add.assert_not_called()

    for changed in (
        _accepted(batch_id="batch-other"),
        _accepted(service_authority_id=uuid4()),
        _accepted(service_request_digest="f" * 64),
        _accepted(permit_id=uuid4()),
    ):
        conflict_db = Mock()
        conflict_db.execute.return_value = _Result(existing)
        assert (
            expiry._evidence_row(
                conflict_db,
                action=action,
                acceptance=changed,
                outcome="accepted",
                closed_at=None,
            )
            is None
        )
        conflict_db.add.assert_not_called()


def test_accepted_batch_action_identity_is_stable_and_sequence_scoped() -> None:
    first = expiry._accepted_batch_action_id(_REQUEST_ID, _BATCH_ID, 1)

    assert first == expiry._accepted_batch_action_id(_REQUEST_ID, _BATCH_ID, 1)
    assert first != expiry._accepted_batch_action_id(_REQUEST_ID, _BATCH_ID, 2)
    assert first != expiry._accepted_batch_action_id(_REQUEST_ID, "batch-other", 1)


def test_expiry_assessment_action_is_scheduled_at_exact_permit_expiry(monkeypatch) -> None:
    context = _context()
    permit = SimpleNamespace(
        generation_request_id=_REQUEST_ID,
        task_id=_TASK_ID,
        permit_version=2,
        expires_at=datetime(2026, 10, 10, 12, 30, tzinfo=UTC),
        service_authority_id=_AUTHORITY_ID,
    )
    context.request.service_authority_id = _AUTHORITY_ID
    assessment = None
    db = Mock()
    db.execute.side_effect = [_Result(assessment), _Result(None)]
    scheduled: list[dict[str, object]] = []
    monkeypatch.setattr(
        expiry,
        "_schedule_expiry_action",
        lambda _db, **values: scheduled.append(values),
    )

    expiry.ensure_generation_expiry_assessment_action(
        db,
        request=context.request,
        permit=permit,
        task_id=_TASK_ID,
        session_id=_SESSION_ID,
        tracking=context.tracking,
    )

    rows = [call.args[0] for call in db.add.call_args_list]
    assert len(rows) == 1
    assert rows[0].generation_request_id == _REQUEST_ID
    assert rows[0].state == "scheduled"
    assert rows[0].query_count == 0
    assert scheduled[0]["due_at"] == permit.expires_at
    assert scheduled[0]["sequence"] == 1
