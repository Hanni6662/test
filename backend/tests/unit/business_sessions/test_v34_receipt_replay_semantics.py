from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.core.domain_errors import DomainError
from app.services.chatflow_v34_events import _existing_stage_event_response


@pytest.mark.parametrize(
    ("disposition", "safe_reason_code", "processed_at", "expected_status"),
    [
        ("accepted", None, datetime(2026, 10, 10, tzinfo=UTC), "replayed"),
        ("rejected", "DIFY_RUN_ID_MISMATCH", datetime(2026, 10, 10, tzinfo=UTC), "rejected"),
        ("received_pending", "V34_RUN_BINDING_PENDING", None, "received_pending"),
    ],
)
def test_same_content_replay_returns_current_persisted_disposition_without_mutating_receipt(
    disposition: str,
    safe_reason_code: str | None,
    processed_at: datetime | None,
    expected_status: str,
) -> None:
    digest = b"d" * 32
    receipt = SimpleNamespace(
        business_session_id=uuid4(),
        event_key="run-1:node:event-1",
        event_digest=digest,
        disposition=disposition,
        safe_reason_code=safe_reason_code,
        received_at=datetime(2026, 10, 9, tzinfo=UTC),
        processed_at=processed_at,
    )
    before = (
        receipt.event_digest,
        receipt.disposition,
        receipt.safe_reason_code,
        receipt.received_at,
        receipt.processed_at,
    )

    response = _existing_stage_event_response(receipt, digest)

    assert response.status == expected_status
    assert response.business_session_id == receipt.business_session_id
    assert response.event_key == receipt.event_key
    assert (
        receipt.event_digest,
        receipt.disposition,
        receipt.safe_reason_code,
        receipt.received_at,
        receipt.processed_at,
    ) == before


def test_same_key_with_different_content_conflicts_without_mutating_receipt() -> None:
    receipt = SimpleNamespace(
        business_session_id=uuid4(),
        event_key="run-1:node:event-1",
        event_digest=b"o" * 32,
        disposition="rejected",
        safe_reason_code="DIFY_RUN_ID_MISMATCH",
        received_at=datetime(2026, 10, 9, tzinfo=UTC),
        processed_at=datetime(2026, 10, 10, tzinfo=UTC),
    )
    before = (
        receipt.event_digest,
        receipt.disposition,
        receipt.safe_reason_code,
        receipt.received_at,
        receipt.processed_at,
    )

    with pytest.raises(DomainError) as error:
        _existing_stage_event_response(receipt, b"n" * 32)

    assert error.value.status_code == 409
    assert error.value.code == "EVENT_CONTENT_CONFLICT"
    assert (
        receipt.event_digest,
        receipt.disposition,
        receipt.safe_reason_code,
        receipt.received_at,
        receipt.processed_at,
    ) == before
