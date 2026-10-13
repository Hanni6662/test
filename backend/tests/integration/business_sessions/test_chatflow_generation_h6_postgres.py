from __future__ import annotations

from uuid import UUID, uuid4

from app.api.internal_dify import create_internal_dify_router
from app.db.action_models import Action
from app.db.chatflow_generation_models import ChatflowGenerationTracking
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.schemas.chatflow_generation import (
    ChatflowGenerationEvent,
    ChatflowGenerationSlotEvent,
    chatflow_generation_event_digest,
    generation_snapshot_digest,
    service_generation_status,
)
from app.services.chatflow_generation import ChatflowGenerationEventService
from app.services.chatflow_generation_actions import (
    GENERATION_RECOVERY_ACTION,
    ImageGenerationReadError,
    _apply_service_snapshot,
    _validate_service_snapshot,
    build_generation_recovery_registration,
)
from app.workers.action_processor import ActionInput, ActionProcessor
from app.workers.messages import OutboxMessage
from app.workers.options import QueueOptions
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from tests.integration.business_sessions.test_chatflow_prompt_packages_postgres import (
    TEST_INTERNAL_CALLBACK_SECRET,
    _post_submission,
    _ready_h4_submission,
)

pytest_plugins = ["tests.integration.business_sessions.test_chatflow_prompt_packages_postgres"]


def _generation_event(
    h4_body: dict[str, object],
    *,
    session_id: UUID,
    task_id: UUID,
    dify_run_id: str,
    source_sequence: int,
    slot_status: str,
    status_version: int,
    event_id: UUID | None = None,
) -> ChatflowGenerationEvent:
    slots = [
        ChatflowGenerationSlotEvent(
            slot_id=slot["slot_id"],
            task_item_id=UUID(slot["task_item_id"]),
            attempt_id=UUID(slot["attempt_id"]),
            position=slot["position"],
            image_type=slot["image_type"],
            status=slot_status,
            status_version=status_version,
            known_retry_count=(None if slot_status == "submission_unknown" else 0),
        )
        for slot in h4_body["slots"]
    ]
    event = ChatflowGenerationEvent.model_validate(
        {
            "contract_version": "chatflow-generation-event-v1",
            "business_session_id": session_id,
            "task_id": task_id,
            "generation_request_id": UUID(h4_body["generation_request_id"]),
            "dify_run_id": dify_run_id,
            "batch_id": f"batch-h6-{h4_body['generation_request_id']}",
            "permit_version": 2,
            "permit_contract_version": "chatflow-generation-permit-v2",
            "package_digest": h4_body["package_digest"],
            "slot_mapping_digest": h4_body["permit"]["slot_mapping_digest"],
            "reference_manifest_digest": h4_body["permit"]["reference_manifest_digest"],
            "event_id": event_id or uuid4(),
            "source": "image_generation",
            "source_sequence": source_sequence,
            "status": service_generation_status(slots),
            "digest_version": 1,
            "digest": "0" * 64,
            "slots": slots,
        },
        strict=True,
    )
    return event.model_copy(update={"digest": chatflow_generation_event_digest(event)})


def _h5_snapshot(event: ChatflowGenerationEvent) -> dict[str, object]:
    return {
        "contract_version": "chatflow-image-generation-v2",
        "permit_version": 2,
        "generation_request_id": str(event.generation_request_id),
        "task_id": str(event.task_id),
        "dify_run_id": event.dify_run_id,
        "package_digest": event.package_digest,
        "slot_mapping_digest": event.slot_mapping_digest,
        "reference_manifest_digest": event.reference_manifest_digest,
        "batch_id": event.batch_id,
        "source_sequence": event.source_sequence,
        "status": event.status,
        "slots": [{**slot.model_dump(mode="json"), "artifact_ref": []} for slot in event.slots],
    }


def _action_input(event: ChatflowGenerationEvent) -> ActionInput:
    return ActionInput(
        action_id=uuid4(),
        task_id=event.task_id,
        scope_kind="generation_request",
        scope_id=event.generation_request_id,
        action_kind="chatflow.generation.recover.v1",
        sequence=1,
        input_digest_version=1,
        input_digest=b"i" * 32,
        payload={
            "generation_request_id": str(event.generation_request_id),
            "task_id": str(event.task_id),
            "business_session_id": str(event.business_session_id),
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
                for slot in event.slots
            ],
        },
        business_session_id=event.business_session_id,
        generation_request_id=event.generation_request_id,
    )


def test_generation_event_auth_replay_and_same_sequence_snapshot_fence(
    h4_pg_engine: Engine,
) -> None:
    client, _h4_service, task_id, session_id, submission = _ready_h4_submission(h4_pg_engine)
    accepted_h4 = _post_submission(client, submission)
    assert accepted_h4.status_code == 200, accepted_h4.text
    h4_body = accepted_h4.json()
    event_service = ChatflowGenerationEventService(h4_pg_engine)
    client.app.include_router(
        create_internal_dify_router(
            engine=h4_pg_engine,
            callback_credential=SecretStr(TEST_INTERNAL_CALLBACK_SECRET),
            generation_event_service=event_service,
        )
    )
    event = _generation_event(
        h4_body,
        session_id=session_id,
        task_id=task_id,
        dify_run_id=submission.dify_run_id,
        source_sequence=1,
        slot_status="queued",
        status_version=1,
    )
    url = "/internal/v1/dify/generation-events"
    body = event.model_dump(mode="json")
    assert client.post(url, json=body).status_code == 401

    headers = {"Authorization": f"Bearer {TEST_INTERNAL_CALLBACK_SECRET}"}
    first = client.post(url, json=body, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "accepted"
    replay = client.post(url, json=body, headers=headers)
    assert replay.status_code == 200
    assert replay.json()["status"] == "replayed"

    same_snapshot_new_delivery = _generation_event(
        h4_body,
        session_id=session_id,
        task_id=task_id,
        dify_run_id=submission.dify_run_id,
        source_sequence=1,
        slot_status="queued",
        status_version=1,
    )
    replayed = client.post(
        url,
        json=same_snapshot_new_delivery.model_dump(mode="json"),
        headers=headers,
    )
    assert replayed.status_code == 200
    assert replayed.json()["status"] == "replayed"

    conflicting_snapshot = _generation_event(
        h4_body,
        session_id=session_id,
        task_id=task_id,
        dify_run_id=submission.dify_run_id,
        source_sequence=1,
        slot_status="processing",
        status_version=2,
    )
    conflict = client.post(
        url,
        json=conflicting_snapshot.model_dump(mode="json"),
        headers=headers,
    )
    assert conflict.status_code == 409

    wrong_aggregate = body.copy()
    wrong_aggregate["status"] = "completed"
    invalid = client.post(url, json=wrong_aggregate, headers=headers)
    assert invalid.status_code == 422

    normalized = _validate_service_snapshot(_h5_snapshot(event), _action_input(event).payload)
    normalized_slots = [
        ChatflowGenerationSlotEvent.model_validate(slot, strict=False)
        for slot in normalized["slots"]
    ]
    assert generation_snapshot_digest(
        batch_id=event.batch_id,
        source_sequence=event.source_sequence,
        slots=normalized_slots,
    ) == generation_snapshot_digest(
        batch_id=event.batch_id,
        source_sequence=event.source_sequence,
        slots=event.slots,
    )
    with Session(h4_pg_engine) as db:
        tracking = db.execute(
            select(ChatflowGenerationTracking).where(
                ChatflowGenerationTracking.generation_request_id == event.generation_request_id
            )
        ).scalar_one()
        assert tracking.source_sequence == 1
        assert tracking.status_digest == generation_snapshot_digest(
            batch_id=event.batch_id,
            source_sequence=event.source_sequence,
            slots=event.slots,
        )


def test_out_of_order_and_late_terminal_statuses_never_regress_completion(
    h4_pg_engine: Engine,
) -> None:
    client, _service, task_id, session_id, submission = _ready_h4_submission(h4_pg_engine)
    accepted_h4 = _post_submission(client, submission)
    assert accepted_h4.status_code == 200, accepted_h4.text
    h4_body = accepted_h4.json()
    service = ChatflowGenerationEventService(h4_pg_engine)

    initial = _generation_event(
        h4_body,
        session_id=session_id,
        task_id=task_id,
        dify_run_id=submission.dify_run_id,
        source_sequence=1,
        slot_status="queued",
        status_version=1,
    )
    assert service.accept(initial).status == "accepted"
    completed = _generation_event(
        h4_body,
        session_id=session_id,
        task_id=task_id,
        dify_run_id=submission.dify_run_id,
        source_sequence=3,
        slot_status="completed",
        status_version=2,
    )
    assert service.accept(completed).status == "accepted"

    unseen_old = _generation_event(
        h4_body,
        session_id=session_id,
        task_id=task_id,
        dify_run_id=submission.dify_run_id,
        source_sequence=2,
        slot_status="processing",
        status_version=2,
    )
    assert service.accept(unseen_old).status == "ignored"

    late_conflicting_terminal = _generation_event(
        h4_body,
        session_id=session_id,
        task_id=task_id,
        dify_run_id=submission.dify_run_id,
        source_sequence=4,
        slot_status="failed",
        status_version=3,
    )
    assert service.accept(late_conflicting_terminal).status == "accepted"

    late_get = _generation_event(
        h4_body,
        session_id=session_id,
        task_id=task_id,
        dify_run_id=submission.dify_run_id,
        source_sequence=5,
        slot_status="failed",
        status_version=4,
    )
    expected = _action_input(late_get).payload
    normalized = _validate_service_snapshot(_h5_snapshot(late_get), expected)
    with Session(h4_pg_engine) as db, db.begin():
        tracking = db.execute(
            select(ChatflowGenerationTracking)
            .where(
                ChatflowGenerationTracking.generation_request_id == late_get.generation_request_id
            )
            .with_for_update()
        ).scalar_one()
        _apply_service_snapshot(
            db,
            tracking,
            _action_input(late_get),
            normalized,
            now=db.scalar(select(func.clock_timestamp())),
        )
        assert tracking.source_sequence == 5
        assert tracking.status == "completed"


def test_safe_retry_persists_one_backoff_action_and_outbox(h4_pg_engine: Engine) -> None:
    client, _service, _task_id, _session_id, submission = _ready_h4_submission(h4_pg_engine)
    accepted = _post_submission(client, submission)
    assert accepted.status_code == 200, accepted.text
    request_id = UUID(accepted.json()["generation_request_id"])

    with Session(h4_pg_engine) as db, db.begin():
        first_action = db.execute(
            select(Action).where(
                Action.generation_request_id == request_id,
                Action.sequence == 1,
            )
        ).scalar_one()
        event = db.get(OutboxEvent, first_action.outbox_event_id)
        receipt = db.get(OutboxReceipt, first_action.outbox_event_id)
        assert event is not None and receipt is not None
        now = db.scalar(select(func.clock_timestamp()))
        first_action.due_at = now
        event.available_at = now
        receipt.available_at = now
        message = OutboxMessage(
            event_id=event.id,
            aggregate_id=event.aggregate_id,
            operation_version=event.operation_version,
            schema_version=event.schema_version,
            payload={key: UUID(value) for key, value in event.payload.items()},
        ).model_dump(mode="json")

    class MissingStatusPort:
        def get_by_request(self, generation_request_id: UUID, *, contract_version: str):
            assert contract_version == "chatflow-image-generation-v2"
            assert generation_request_id == request_id
            raise ImageGenerationReadError("IMAGE_SERVICE_REQUEST_NOT_FOUND", not_found=True)

        def get_by_batch(self, batch_id: str, *, contract_version: str):
            assert contract_version == "chatflow-image-generation-v2"
            raise AssertionError(f"unexpected batch lookup: {batch_id}")

    processor = ActionProcessor(
        h4_pg_engine,
        QueueOptions(
            broker_url="amqp://integration:integration@localhost:5672/image-backend-test",
            queue_name="generation-recovery-integration",
        ),
        {GENERATION_RECOVERY_ACTION: build_generation_recovery_registration(MissingStatusPort())},
    )
    claim, claim_result = processor._claim(OutboxMessage.model_validate(message))
    assert claim is not None, claim_result
    execution = processor._run_io(claim.registration.execute, claim.action_input)
    assert processor._finalize(claim, execution).status == "completed"

    with Session(h4_pg_engine) as db:
        tracking = db.execute(
            select(ChatflowGenerationTracking).where(
                ChatflowGenerationTracking.generation_request_id == request_id
            )
        ).scalar_one()
        first_action = db.execute(
            select(Action).where(
                Action.generation_request_id == request_id,
                Action.sequence == 1,
            )
        ).scalar_one()
        retry_action = db.execute(
            select(Action).where(
                Action.generation_request_id == request_id,
                Action.sequence == 2,
            )
        ).scalar_one()
        retry_event = db.get(OutboxEvent, retry_action.outbox_event_id)
        retry_receipt = db.get(OutboxReceipt, retry_action.outbox_event_id)
        assert retry_event is not None and retry_receipt is not None
        assert tracking.reconcile_query_count == 1
        assert tracking.next_reconcile_at == retry_action.due_at
        assert 9.9 <= (retry_action.due_at - tracking.last_reconcile_at).total_seconds() <= 10.1
        assert first_action.status == "completed"
        assert retry_action.status == "scheduled"
        assert retry_event.aggregate_id == retry_action.id
        assert retry_event.status == "pending"
        assert retry_receipt.status == "pending"
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.generation_request_id == request_id,
                    Action.sequence == 2,
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.aggregate_id == retry_action.id,
                    OutboxEvent.event_type == "task.action.v1",
                )
            )
            == 1
        )

    assert processor.process_payload(message).status == "completed"
    replayed_h4 = _post_submission(client, submission)
    assert replayed_h4.status_code == 200, replayed_h4.text
    with Session(h4_pg_engine) as db:
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.generation_request_id == request_id,
                    Action.sequence == 2,
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.aggregate_id == retry_action.id,
                    OutboxEvent.event_type == "task.action.v1",
                )
            )
            == 1
        )
