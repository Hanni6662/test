from __future__ import annotations

from uuid import uuid4

import pytest
from app.schemas.chatflow_generation import (
    ChatflowGenerationEvent,
    ChatflowGenerationSlotEvent,
    chatflow_generation_event_digest,
    generation_snapshot_digest,
)
from app.services.chatflow_generation_actions import (
    ImageGenerationReadError,
    _validate_service_snapshot,
    execute_generation_recovery,
)
from app.workers.action_processor import ActionInput
from pydantic import ValidationError


def _slot(*, status: str = "queued", status_version: int = 1) -> ChatflowGenerationSlotEvent:
    return ChatflowGenerationSlotEvent(
        slot_id="slot_01",
        task_item_id=uuid4(),
        attempt_id=uuid4(),
        position=1,
        image_type="主图",
        status=status,
        status_version=status_version,
        known_retry_count=0,
    )


def _event(
    slot: ChatflowGenerationSlotEvent, *, status: str = "running"
) -> ChatflowGenerationEvent:
    event = ChatflowGenerationEvent.model_validate(
        {
            "contract_version": "chatflow-generation-event-v1",
            "business_session_id": uuid4(),
            "task_id": uuid4(),
            "generation_request_id": uuid4(),
            "dify_run_id": "run-test",
            "batch_id": "batch-test",
            "permit_version": 2,
            "permit_contract_version": "chatflow-generation-permit-v2",
            "package_digest": "a" * 64,
            "slot_mapping_digest": "b" * 64,
            "reference_manifest_digest": "c" * 64,
            "event_id": uuid4(),
            "source": "image_generation",
            "source_sequence": 1,
            "status": status,
            "digest_version": 1,
            "digest": "0" * 64,
            "slots": [slot],
        },
        strict=True,
    )
    return event.model_copy(update={"digest": chatflow_generation_event_digest(event)})


def test_event_aggregate_status_must_match_slots() -> None:
    with pytest.raises(ValidationError):
        _event(_slot(), status="completed")


def test_get_and_event_snapshots_use_the_same_digest_shape() -> None:
    slot = _slot()
    event = _event(slot)
    expected = {
        "generation_request_id": str(event.generation_request_id),
        "task_id": str(event.task_id),
        "dify_run_id": event.dify_run_id,
        "package_digest": event.package_digest,
        "slot_mapping_digest": event.slot_mapping_digest,
        "reference_manifest_digest": event.reference_manifest_digest,
        "slots": [
            {
                "slot_id": slot.slot_id,
                "task_item_id": str(slot.task_item_id),
                "attempt_id": str(slot.attempt_id),
                "position": slot.position,
                "image_type": slot.image_type,
            }
        ],
    }
    snapshot = {
        "contract_version": "chatflow-image-generation-v2",
        "permit_version": 2,
        **{
            key: expected[key]
            for key in (
                "generation_request_id",
                "task_id",
                "dify_run_id",
                "package_digest",
                "slot_mapping_digest",
                "reference_manifest_digest",
            )
        },
        "batch_id": event.batch_id,
        "source_sequence": event.source_sequence,
        "status": "running",
        "slots": [
            {
                **slot.model_dump(mode="json"),
                "artifact_ref": [],
            }
        ],
    }

    normalized = _validate_service_snapshot(snapshot, expected)
    normalized_slot = ChatflowGenerationSlotEvent.model_validate(
        normalized["slots"][0], strict=False
    )
    assert generation_snapshot_digest(
        batch_id=event.batch_id,
        source_sequence=event.source_sequence,
        slots=[normalized_slot],
    ) == generation_snapshot_digest(
        batch_id=event.batch_id,
        source_sequence=event.source_sequence,
        slots=[slot],
    )


def test_service_snapshot_rejects_aggregate_status_mismatch() -> None:
    slot = _slot()
    event = _event(slot)
    expected = {
        "generation_request_id": str(event.generation_request_id),
        "task_id": str(event.task_id),
        "dify_run_id": event.dify_run_id,
        "package_digest": event.package_digest,
        "slot_mapping_digest": event.slot_mapping_digest,
        "reference_manifest_digest": event.reference_manifest_digest,
        "slots": [
            {
                "slot_id": slot.slot_id,
                "task_item_id": str(slot.task_item_id),
                "attempt_id": str(slot.attempt_id),
                "position": slot.position,
                "image_type": slot.image_type,
            }
        ],
    }
    snapshot = {
        "contract_version": "chatflow-image-generation-v2",
        "permit_version": 2,
        **{
            key: expected[key]
            for key in (
                "generation_request_id",
                "task_id",
                "dify_run_id",
                "package_digest",
                "slot_mapping_digest",
                "reference_manifest_digest",
            )
        },
        "batch_id": event.batch_id,
        "source_sequence": 1,
        "status": "completed",
        "slots": [slot.model_dump(mode="json")],
    }
    with pytest.raises(ValueError, match="aggregate status"):
        _validate_service_snapshot(snapshot, expected)


def test_404_read_reconciliation_has_finite_backoff_and_never_posts() -> None:
    class MissingReadPort:
        def __init__(self) -> None:
            self.request_ids = []

        def get_by_request(self, generation_request_id, *, contract_version):
            assert contract_version == "chatflow-image-generation-v2"
            self.request_ids.append(generation_request_id)
            raise ImageGenerationReadError("IMAGE_SERVICE_REQUEST_NOT_FOUND", not_found=True)

        def get_by_batch(self, batch_id, *, contract_version):
            assert contract_version == "chatflow-image-generation-v2"
            raise AssertionError(f"unexpected batch lookup: {batch_id}")

    request_id = uuid4()
    task_id = uuid4()
    session_id = uuid4()
    port = MissingReadPort()
    outcomes = []
    for number in range(1, 6):
        action_input = ActionInput(
            action_id=uuid4(),
            task_id=task_id,
            scope_kind="generation_request",
            scope_id=request_id,
            action_kind="chatflow.generation.recover.v1",
            sequence=number,
            input_digest_version=1,
            input_digest=b"d" * 32,
            payload={
                "generation_request_id": str(request_id),
                "business_session_id": str(session_id),
                "batch_id": None,
                "query_number": number,
                "should_query": True,
            },
            business_session_id=session_id,
            generation_request_id=request_id,
        )
        outcomes.append(execute_generation_recovery(port, action_input))

    assert [result.outcome for result in outcomes] == [
        "safe_retry",
        "safe_retry",
        "safe_retry",
        "safe_retry",
        "unknown",
    ]
    assert [result.retry_after_seconds for result in outcomes[:4]] == [10, 20, 40, 80]
    assert outcomes[-1].safe_reason_code == "IMAGE_SERVICE_REQUEST_NOT_FOUND"
    assert port.request_ids == [request_id] * 5
