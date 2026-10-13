from __future__ import annotations

import hashlib
import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from app.api.internal_dify import create_internal_dify_router
from app.db.action_models import Action
from app.db.business_session_models import BusinessSession
from app.db.chatflow_generation_models import (
    ChatflowGenerationAcceptanceEvidence,
    ChatflowGenerationAcceptedBatchFollowup,
    ChatflowGenerationDelivery,
    ChatflowGenerationExpiryAssessment,
    ChatflowGenerationPermit,
    ChatflowGenerationRequest,
    ChatflowGenerationSlotState,
    ChatflowGenerationTracking,
)
from app.db.chatflow_reference_files import ChatflowDifyReferenceFile
from app.db.chatflow_v34_models import ChatflowV34SessionState
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.task_models import Task, TaskItem, TaskItemAttempt
from app.integrations.dify.chatflow import (
    ChatflowOriginalRunEvent,
    ChatflowOriginalRunObservationResult,
)
from app.integrations.generation_acceptance import GenerationAcceptanceObservation
from app.integrations.storage.local import LocalPrivateStorage
from app.schemas.chatflow_generation import ChatflowGenerationSlotEvent, service_generation_status
from app.schemas.generation_acceptance import (
    GENERATION_ACCEPTANCE_CONTRACT,
    GENERATION_IMAGE_CONTRACT_V3,
    GENERATION_PERMIT_EXPIRED_REASON,
    GenerationAcceptanceAccepted,
    GenerationAcceptanceRejectedExpired,
    generation_acceptance_receipt_digest,
)
from app.services.chatflow_actions import CHATFLOW_REFERENCE_UPLOAD_ACTION
from app.services.chatflow_generation_actions import (
    GENERATION_RECOVERY_ACTION,
    GENERATION_RECOVERY_SEQUENCE_NAMESPACE,
    finalize_generation_recovery,
    generation_action_digest,
    generation_action_id,
    prepare_generation_recovery,
)
from app.services.chatflow_generation_delivery_actions import (
    DELIVERY_CONFIRM_ACTION,
    DELIVERY_DOWNLOAD_ACTION,
    build_generation_delivery_registrations,
)
from app.services.chatflow_generation_expiry import (
    ACCEPTED_BATCH_RECOVERY_ACTION,
    ACCEPTED_BATCH_SEQUENCE_NAMESPACE,
    EXPIRY_ASSESS_ACTION,
    EXPIRY_ASSESS_SEQUENCE_NAMESPACE,
    _action_input,
    _expected_identity,
    build_generation_expiry_registrations,
    finalize_generation_expiry_assessment,
    prepare_generation_expiry_assessment,
)
from app.services.chatflow_prompt_packages import ChatflowPromptPackageService
from app.services.chatflow_v34_prompt_contract import prompt_package_submission_id
from app.services.retention_cleanup import _resolved_generation_requests_for_cleanup
from app.workers.action_processor import (
    ActionExecutionResult,
    ActionProcessor,
    schedule_action_in_transaction,
)
from app.workers.messages import OutboxMessage
from app.workers.options import QueueOptions
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from tests.integration.business_sessions.test_chatflow_prompt_packages_postgres import (
    TEST_GENERATION_PERMIT_SECRET,
    TEST_INTERNAL_CALLBACK_SECRET,
    TEST_SERVICE_AUTHORITY_ID,
    _post_submission,
    _submission_for_task,
)
from tests.integration.business_sessions.test_chatflow_v34_sessions_postgres import (
    _prepare_v34_run_observation_case,
    _process_v34_run_observation,
)

pytest_plugins = ["tests.integration.business_sessions.test_chatflow_prompt_packages_postgres"]

_QUEUE_OPTIONS = QueueOptions(
    broker_url="amqp://integration:integration@localhost:5672/image-backend-test",
    queue_name="permit-expiry-integration",
)


def _h4_context(engine: Engine) -> dict[str, object]:
    (
        client,
        session_id,
        recommendation_id,
        run_id,
        task_id,
        observation_action_id,
    ) = _prepare_v34_run_observation_case(
        engine, idempotency_key=f"permit-expiry-v34-{uuid4().hex}"
    )
    # This fixture starts at the point where a ready recommendation and accepted
    # selection already exist, so complete the deterministic Dify upload records
    # the same way the V3.4 lifecycle tests do before moving into H4.
    with Session(engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        mappings = (
            db.execute(
                select(ChatflowDifyReferenceFile)
                .where(ChatflowDifyReferenceFile.business_session_id == session_id)
                .order_by(ChatflowDifyReferenceFile.position)
            )
            .scalars()
            .all()
        )
        upload_actions = (
            db.execute(
                select(Action).where(
                    Action.business_session_id == session_id,
                    Action.action_kind == CHATFLOW_REFERENCE_UPLOAD_ACTION,
                )
            )
            .scalars()
            .all()
        )
        assert now is not None and mappings and upload_actions
        for mapping in mappings:
            mapping.status = "uploaded"
            mapping.dify_file_id = f"expiry-test-file-{mapping.position}"
            mapping.uploaded_at = now
            mapping.updated_at = now
        for upload_action in upload_actions:
            upload_action.status = "completed"
            upload_action.last_outcome = "accepted"
            upload_action.completed_at = now
    paused = ChatflowOriginalRunObservationResult(
        "received",
        events=(
            ChatflowOriginalRunEvent(
                event="workflow_paused",
                dify_run_id=run_id,
                workflow_status="paused",
            ),
        ),
    )
    assert (
        _process_v34_run_observation(engine, action_id=observation_action_id, observation=paused)[0]
        == "completed"
    )
    with Session(engine) as db:
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        selection_submission_id = state.selection_submission_id
        assert state.safe_reason_code == "V34_RUN_TERMINAL_NOT_OBSERVED"
    assert selection_submission_id is not None
    callback = SimpleNamespace(
        recommendation_id=recommendation_id,
        recommendation_revision=1,
        dify_run_id=run_id,
    )
    submission = _submission_for_task(
        engine,
        task_id=task_id,
        session_id=session_id,
        callback=callback,
    ).model_copy(update={"submission_id": prompt_package_submission_id(selection_submission_id)})
    prompt_service = ChatflowPromptPackageService(
        engine,
        permit_secret=SecretStr(TEST_GENERATION_PERMIT_SECRET),
        service_authority_id=TEST_SERVICE_AUTHORITY_ID,
    )
    client.app.include_router(
        create_internal_dify_router(
            engine=engine,
            callback_credential=SecretStr(TEST_INTERNAL_CALLBACK_SECRET),
            prompt_package_service=prompt_service,
        )
    )
    response = _post_submission(client, submission)
    assert response.status_code == 200, response.text
    h4_body = response.json()
    h4_body["dify_run_id"] = submission.dify_run_id
    return {
        "client": client,
        "task_id": task_id,
        "session_id": session_id,
        "submission": submission,
        "h4": h4_body,
        "request_id": UUID(h4_body["generation_request_id"]),
    }


def _accepted(engine: Engine, context: dict[str, object], *, batch_id: str):
    request_id = context["request_id"]
    with Session(engine) as db:
        request = db.get(ChatflowGenerationRequest, request_id)
        permit = db.execute(
            select(ChatflowGenerationPermit).where(
                ChatflowGenerationPermit.generation_request_id == request_id
            )
        ).scalar_one()
        assert request is not None
        assert request.service_authority_id == TEST_SERVICE_AUTHORITY_ID
        return GenerationAcceptanceAccepted.model_validate(
            {
                "contract_version": GENERATION_ACCEPTANCE_CONTRACT,
                "outcome": "accepted",
                "service_authority_id": request.service_authority_id,
                "generation_contract_version": GENERATION_IMAGE_CONTRACT_V3,
                "generation_request_id": request.id,
                "task_id": request.task_id,
                "dify_run_id": request.dify_run_id,
                "permit_id": permit.permit_id,
                "permit_version": permit.permit_version,
                "permit_expires_at": permit.expires_at.astimezone(UTC),
                "package_digest": request.package_digest.hex(),
                "slot_mapping_digest": permit.slot_mapping_digest.hex(),
                "reference_manifest_digest": permit.reference_manifest_digest.hex(),
                "service_request_digest": "e" * 64,
                "batch_id": batch_id,
            },
            strict=True,
        )


def _rejected(engine: Engine, context: dict[str, object]):
    request_id = context["request_id"]
    with Session(engine) as db:
        request = db.get(ChatflowGenerationRequest, request_id)
        permit = db.execute(
            select(ChatflowGenerationPermit).where(
                ChatflowGenerationPermit.generation_request_id == request_id
            )
        ).scalar_one()
        assert request is not None
        rejected_at = permit.expires_at.astimezone(UTC) + timedelta(seconds=1)
        receipt_id = uuid4()
        fields: dict[str, object] = {
            "contract_version": GENERATION_ACCEPTANCE_CONTRACT,
            "outcome": "rejected_expired",
            "service_authority_id": str(request.service_authority_id),
            "generation_contract_version": GENERATION_IMAGE_CONTRACT_V3,
            "generation_request_id": str(request.id),
            "task_id": str(request.task_id),
            "dify_run_id": request.dify_run_id,
            "permit_id": str(permit.permit_id),
            "permit_version": permit.permit_version,
            "permit_expires_at": permit.expires_at.astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "package_digest": request.package_digest.hex(),
            "slot_mapping_digest": permit.slot_mapping_digest.hex(),
            "reference_manifest_digest": permit.reference_manifest_digest.hex(),
            "service_request_digest": "f" * 64,
            "batch_id": None,
            "reason_code": GENERATION_PERMIT_EXPIRED_REASON,
            "receipt_version": 1,
            "receipt_id": str(receipt_id),
            "rejected_at": rejected_at.isoformat().replace("+00:00", "Z"),
            "receipt_digest": "0" * 64,
        }
        fields["receipt_digest"] = generation_acceptance_receipt_digest(fields)
        return GenerationAcceptanceRejectedExpired.model_validate(fields, strict=True)


class _AcceptancePort:
    def __init__(self, observation: GenerationAcceptanceObservation) -> None:
        self.observation = observation
        self.calls = 0

    def get_acceptance(self, expected):
        self.calls += 1
        assert expected.service_authority_id == TEST_SERVICE_AUTHORITY_ID
        assert expected.generation_contract_version == GENERATION_IMAGE_CONTRACT_V3
        return self.observation


class _StatusPort:
    def __init__(
        self, h4: dict[str, object], *, terminal: bool = False, content: bytes = b""
    ) -> None:
        self.h4 = h4
        self.terminal = terminal
        self.content = content
        self.batch_reads: list[str] = []

    def get_by_request(self, *_args, **_kwargs):
        raise AssertionError("accepted batch recovery must never GET by request")

    def get_by_batch(self, batch_id: str, *, contract_version: str):
        assert contract_version == GENERATION_IMAGE_CONTRACT_V3
        assert batch_id == f"batch-expiry-{self.h4['generation_request_id']}"
        self.batch_reads.append(batch_id)
        source_sequence = len(self.batch_reads)
        status_slots = []
        for raw in self.h4["slots"]:
            status_slots.append(
                ChatflowGenerationSlotEvent(
                    slot_id=raw["slot_id"],
                    task_item_id=UUID(raw["task_item_id"]),
                    attempt_id=UUID(raw["attempt_id"]),
                    position=raw["position"],
                    image_type=raw["image_type"],
                    status="completed" if self.terminal else "queued",
                    status_version=source_sequence,
                    known_retry_count=0,
                )
            )
        refs = {}
        for slot in status_slots:
            refs[slot.slot_id] = (
                [
                    {
                        "kind": "original",
                        "file_id": f"{slot.slot_id}-original-1.png",
                        "sha256": hashlib.sha256(self.content).hexdigest(),
                        "mime_type": "image/png",
                        "size_bytes": len(self.content),
                        "expires_at": None,
                    }
                ]
                if self.terminal
                else []
            )
        return {
            "contract_version": GENERATION_IMAGE_CONTRACT_V3,
            "permit_version": 2,
            "generation_request_id": self.h4["generation_request_id"],
            "task_id": self.h4["task_id"],
            "dify_run_id": self.h4["dify_run_id"],
            "package_digest": self.h4["package_digest"],
            "slot_mapping_digest": self.h4["permit"]["slot_mapping_digest"],
            "reference_manifest_digest": self.h4["permit"]["reference_manifest_digest"],
            "batch_id": batch_id,
            "source_sequence": source_sequence,
            "status": service_generation_status(status_slots),
            "slots": [
                {
                    **slot.model_dump(mode="json"),
                    "artifact_ref": refs[slot.slot_id],
                }
                for slot in status_slots
            ],
        }


class _DeliveryPort:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.downloads: list[tuple[str, str, str]] = []
        self.confirmations: list[tuple[str, str]] = []

    def get_artifact(self, batch_id, slot_id, file_id, *, contract_version):
        assert contract_version == GENERATION_IMAGE_CONTRACT_V3
        self.downloads.append((batch_id, slot_id, file_id))
        return self.content, "image/png"

    def confirm_delivery(self, batch_id, slot_id, _payload, *, contract_version):
        assert contract_version == GENERATION_IMAGE_CONTRACT_V3
        self.confirmations.append((batch_id, slot_id))


def _action_message(engine: Engine, action_id: UUID) -> dict[str, object]:
    with Session(engine) as db:
        action = db.get(Action, action_id)
        assert action is not None
        event = db.get(OutboxEvent, action.outbox_event_id)
        assert event is not None
        return OutboxMessage(
            event_id=event.id,
            aggregate_id=event.aggregate_id,
            operation_version=event.operation_version,
            schema_version=event.schema_version,
            payload={key: UUID(value) for key, value in event.payload.items()},
        ).model_dump(mode="json")


def _force_action_due(engine: Engine, action_id: UUID) -> None:
    with Session(engine) as db, db.begin():
        action = db.get(Action, action_id)
        assert action is not None
        event = db.get(OutboxEvent, action.outbox_event_id)
        receipt = db.get(OutboxReceipt, action.outbox_event_id)
        assert event is not None and receipt is not None
        now = db.scalar(select(func.clock_timestamp()))
        action.due_at = now
        event.available_at = now
        receipt.available_at = now


def _process_action(processor: ActionProcessor, engine: Engine, action_id: UUID):
    message = _action_message(engine, action_id)
    _force_action_due(engine, action_id)
    return processor.process_payload(message)


def _set_h6_exhausted(engine: Engine, request_id: UUID) -> tuple[object, ...]:
    with Session(engine) as db, db.begin():
        tracking = db.execute(
            select(ChatflowGenerationTracking)
            .where(ChatflowGenerationTracking.generation_request_id == request_id)
            .with_for_update()
        ).scalar_one()
        tracking.reconcile_query_count = 5
        tracking.reconcile_state = "unknown"
        tracking.next_reconcile_at = None
        tracking.status = "awaiting_batch"
    return _capture_h6_history(engine, request_id)


def _capture_h6_history(engine: Engine, request_id: UUID) -> tuple[object, ...]:
    with Session(engine) as db:
        tracking = db.execute(
            select(ChatflowGenerationTracking).where(
                ChatflowGenerationTracking.generation_request_id == request_id
            )
        ).scalar_one()
        actions = (
            db.execute(
                select(Action)
                .where(
                    Action.generation_request_id == request_id,
                    Action.action_kind == GENERATION_RECOVERY_ACTION,
                    Action.sequence_namespace == GENERATION_RECOVERY_SEQUENCE_NAMESPACE,
                )
                .order_by(Action.sequence)
            )
            .scalars()
            .all()
        )
        action_history = tuple(
            (row.id, row.sequence, row.input_digest, row.status, row.last_outcome)
            for row in actions
        )
        return tracking.reconcile_query_count, tracking.status_digest, action_history


def _expiry_action(engine: Engine, request_id: UUID) -> Action:
    with Session(engine) as db:
        return db.execute(
            select(Action).where(
                Action.generation_request_id == request_id,
                Action.action_kind == EXPIRY_ASSESS_ACTION,
                Action.sequence_namespace == EXPIRY_ASSESS_SEQUENCE_NAMESPACE,
                Action.sequence == 1,
            )
        ).scalar_one()


def _followup_action(engine: Engine, request_id: UUID, sequence: int) -> Action:
    with Session(engine) as db:
        return db.execute(
            select(Action).where(
                Action.generation_request_id == request_id,
                Action.action_kind == ACCEPTED_BATCH_RECOVERY_ACTION,
                Action.sequence_namespace == ACCEPTED_BATCH_SEQUENCE_NAMESPACE,
                Action.sequence == sequence,
            )
        ).scalar_one()


def _prepare_accepted_context(
    engine: Engine,
    *,
    status_port: _StatusPort,
    acceptance_port: _AcceptancePort,
):
    context = _h4_context(engine)
    request_id = context["request_id"]
    _set_h6_exhausted(engine, request_id)
    registrations = build_generation_expiry_registrations(acceptance_port, status_port)
    processor = ActionProcessor(engine, _QUEUE_OPTIONS, registrations)
    expiry_action = _expiry_action(engine, request_id)
    assert _process_action(processor, engine, expiry_action.id).status == "completed"
    return context, processor


@pytest.mark.parametrize("first_commit", ("h6", "accepted"))
def test_h6_exhaustion_and_accepted_evidence_race_converges_once(
    h4_pg_engine: Engine, first_commit: str
) -> None:
    context = _h4_context(h4_pg_engine)
    request_id = context["request_id"]
    batch_id = f"batch-expiry-{request_id}"
    acceptance = _accepted(h4_pg_engine, context, batch_id=batch_id)
    observation = GenerationAcceptanceObservation("accepted", acceptance=acceptance)
    with Session(h4_pg_engine) as db, db.begin():
        tracking = db.execute(
            select(ChatflowGenerationTracking)
            .where(ChatflowGenerationTracking.generation_request_id == request_id)
            .with_for_update()
        ).scalar_one()
        tracking.reconcile_query_count = 4
        tracking.reconcile_state = "scheduled"
        tracking.status = "awaiting_batch"
        now = db.scalar(select(func.clock_timestamp()))
        h6_action = schedule_action_in_transaction(
            db,
            action_id=generation_action_id(request_id, 5),
            task_id=context["task_id"],
            scope_kind="generation_request",
            scope_id=request_id,
            action_kind=GENERATION_RECOVERY_ACTION,
            sequence=5,
            sequence_namespace=GENERATION_RECOVERY_SEQUENCE_NAMESPACE,
            due_at=now,
            input_digest_version=1,
            input_digest=generation_action_digest(
                generation_request_id=request_id,
                task_id=context["task_id"],
                session_id=context["session_id"],
                sequence=5,
            ),
            business_session_id=context["session_id"],
            generation_request_id=request_id,
        )
        expiry_action = db.execute(
            select(Action).where(
                Action.generation_request_id == request_id,
                Action.action_kind == EXPIRY_ASSESS_ACTION,
            )
        ).scalar_one()
        h6_input = prepare_generation_recovery(db, h6_action)
        expiry_input = prepare_generation_expiry_assessment(db, expiry_action)
        h6_action_id = h6_action.id
        expiry_action_id = expiry_action.id

    lock_held = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    start_barrier = threading.Barrier(2)
    backend_pids: dict[str, int] = {}
    accepted_result = ActionExecutionResult(
        "accepted", value={"queried": True, "observation": observation}
    )
    unknown_result = ActionExecutionResult(
        "unknown",
        safe_reason_code="GENERATION_RECONCILE_LIMIT_REACHED",
        value={"queried": True, "snapshot": None},
    )

    def _apply(role: str, db: Session) -> None:
        if role == "h6":
            action = db.get(Action, h6_action_id)
            assert action is not None
            finalize_generation_recovery(db, action, h6_input, unknown_result)
            return
        action = db.get(Action, expiry_action_id)
        assert action is not None
        finalize_generation_expiry_assessment(db, action, expiry_input, accepted_result)

    def _first_worker() -> None:
        start_barrier.wait(timeout=5)
        with Session(h4_pg_engine) as db, db.begin():
            backend_pids["first"] = db.scalar(text("SELECT pg_backend_pid()"))
            if first_commit == "accepted":
                db.execute(
                    select(ChatflowGenerationExpiryAssessment)
                    .where(ChatflowGenerationExpiryAssessment.generation_request_id == request_id)
                    .with_for_update()
                ).scalar_one()
            db.execute(
                select(ChatflowGenerationTracking)
                .where(ChatflowGenerationTracking.generation_request_id == request_id)
                .with_for_update()
            ).scalar_one()
            lock_held.set()
            assert second_started.wait(5)
            assert release_first.wait(5)
            _apply(first_commit, db)

    def _second_worker() -> None:
        start_barrier.wait(timeout=5)
        assert lock_held.wait(5)
        with Session(h4_pg_engine) as db, db.begin():
            backend_pids["second"] = db.scalar(text("SELECT pg_backend_pid()"))
            second_started.set()
            db.execute(
                select(ChatflowGenerationTracking)
                .where(ChatflowGenerationTracking.generation_request_id == request_id)
                .with_for_update()
            ).scalar_one()
            _apply("h6" if first_commit == "accepted" else "accepted", db)

    first_thread = None
    second_thread = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_thread = pool.submit(_first_worker)
        second_thread = pool.submit(_second_worker)
        try:
            assert second_started.wait(5)
            deadline = time.monotonic() + 5
            blocking_pids: list[int] = []
            while time.monotonic() < deadline:
                with h4_pg_engine.connect() as connection:
                    blocking_pids = connection.scalar(
                        text("SELECT pg_blocking_pids(:pid)"),
                        {"pid": backend_pids.get("second")},
                    )
                if backend_pids.get("first") in blocking_pids:
                    break
                time.sleep(0.02)
            assert backend_pids.get("first") in blocking_pids, (
                "second PostgreSQL connection was not blocked by the first; "
                f"blocking_pids={blocking_pids}"
            )
        finally:
            release_first.set()
        first_thread.result(timeout=10)
        second_thread.result(timeout=10)

    assert backend_pids["first"] != backend_pids["second"]
    with Session(h4_pg_engine) as db:
        evidence = db.get(ChatflowGenerationAcceptanceEvidence, request_id)
        tracking = db.get(ChatflowGenerationTracking, request_id)
        followup = db.get(ChatflowGenerationAcceptedBatchFollowup, request_id)
        actions = (
            db.execute(
                select(Action).where(
                    Action.generation_request_id == request_id,
                    Action.action_kind == ACCEPTED_BATCH_RECOVERY_ACTION,
                    Action.sequence_namespace == ACCEPTED_BATCH_SEQUENCE_NAMESPACE,
                )
            )
            .scalars()
            .all()
        )
        assert evidence is not None and evidence.batch_id == batch_id
        assert tracking is not None and tracking.reconcile_query_count == 5
        assert tracking.reconcile_state == "unknown"
        assert followup is not None and followup.query_count == 0
        assert followup.state == "scheduled"
        assert len(actions) == 1
        assert actions[0].sequence == 1
        assert (
            db.scalar(
                select(
                    func.count(ChatflowGenerationAcceptanceEvidence.generation_request_id)
                ).where(ChatflowGenerationAcceptanceEvidence.generation_request_id == request_id)
            )
            == 1
        )


def test_accepted_followup_is_persistently_bounded_and_preserves_original_h6(
    h4_pg_engine: Engine,
) -> None:
    context = _h4_context(h4_pg_engine)
    request_id = context["request_id"]
    batch_id = f"batch-expiry-{request_id}"
    accepted = _accepted(h4_pg_engine, context, batch_id=batch_id)
    acceptance_port = _AcceptancePort(GenerationAcceptanceObservation("accepted", accepted))
    status_port = _StatusPort(context["h4"])
    h6_before = _set_h6_exhausted(h4_pg_engine, request_id)
    registrations = build_generation_expiry_registrations(acceptance_port, status_port)
    processor = ActionProcessor(h4_pg_engine, _QUEUE_OPTIONS, registrations)
    expiry_action = _expiry_action(h4_pg_engine, request_id)
    assert _process_action(processor, h4_pg_engine, expiry_action.id).status == "completed"
    assert acceptance_port.calls == 1

    # H4 replay and a duplicate accepted finalizer reuse the same evidence and
    # deterministic followup Action rather than resetting its budget.
    replay = _post_submission(context["client"], context["submission"])
    assert replay.status_code == 200, replay.text
    with Session(h4_pg_engine) as db, db.begin():
        action = db.get(Action, expiry_action.id)
        request = db.get(ChatflowGenerationRequest, request_id)
        permit = db.execute(
            select(ChatflowGenerationPermit).where(
                ChatflowGenerationPermit.generation_request_id == request_id
            )
        ).scalar_one()
        session = db.get(BusinessSession, context["session_id"])
        assert action is not None and request is not None and session is not None
        duplicate_input = _action_input(
            action,
            {
                **_expected_identity(request, permit, session),
                "query_number": 1,
                "should_query": True,
            },
        )
        finalize_generation_expiry_assessment(
            db,
            action,
            duplicate_input,
            ActionExecutionResult(
                "accepted",
                value={
                    "queried": True,
                    "observation": GenerationAcceptanceObservation("accepted", accepted),
                },
            ),
        )
    with Session(h4_pg_engine) as db:
        followup = db.get(ChatflowGenerationAcceptedBatchFollowup, request_id)
        first_action = db.execute(
            select(Action).where(
                Action.generation_request_id == request_id,
                Action.action_kind == ACCEPTED_BATCH_RECOVERY_ACTION,
                Action.sequence_namespace == ACCEPTED_BATCH_SEQUENCE_NAMESPACE,
                Action.sequence == 1,
            )
        ).scalar_one()
        assert followup is not None and followup.query_count == 0
        first_identity = (first_action.id, first_action.input_digest)

    for sequence in range(1, 6):
        action = _followup_action(h4_pg_engine, request_id, sequence)
        result = _process_action(processor, h4_pg_engine, action.id)
        if sequence < 5:
            assert result.status == "completed"
        else:
            assert result.status == "unknown"
        with Session(h4_pg_engine) as db:
            followup = db.get(ChatflowGenerationAcceptedBatchFollowup, request_id)
            assert followup is not None and followup.query_count == sequence

    assert status_port.batch_reads == [batch_id] * 5
    with Session(h4_pg_engine) as db:
        followup = db.get(ChatflowGenerationAcceptedBatchFollowup, request_id)
        tracking = db.get(ChatflowGenerationTracking, request_id)
        actions = (
            db.execute(
                select(Action).where(
                    Action.generation_request_id == request_id,
                    Action.action_kind == ACCEPTED_BATCH_RECOVERY_ACTION,
                    Action.sequence_namespace == ACCEPTED_BATCH_SEQUENCE_NAMESPACE,
                )
            )
            .scalars()
            .all()
        )
        assert followup is not None
        assert followup.query_count == 5
        assert followup.state == "unknown"
        assert followup.next_action_at is None
        assert tracking is not None and tracking.reconcile_query_count == 5
        assert tracking.reconcile_state == "unknown"
        assert len(actions) == 5
        assert (actions[0].id, actions[0].input_digest) == first_identity
        assert not any(row.sequence == 6 for row in actions)
    h6_after = _capture_h6_history(h4_pg_engine, request_id)
    assert h6_after[0] == h6_before[0] == 5
    assert h6_after[2] == h6_before[2]
    # tracking.status_digest is the current accepted-batch snapshot digest; it
    # may advance through the existing H6 ingest path. The original H6 Action
    # input digests and history above remain unchanged.


def test_worker_restart_consumes_the_reserved_read_without_reissuing_get(
    h4_pg_engine: Engine,
) -> None:
    context = _h4_context(h4_pg_engine)
    request_id = context["request_id"]
    batch_id = f"batch-expiry-{request_id}"
    accepted = _accepted(h4_pg_engine, context, batch_id=batch_id)
    acceptance_port = _AcceptancePort(GenerationAcceptanceObservation("accepted", accepted))
    status_port = _StatusPort(context["h4"])
    _set_h6_exhausted(h4_pg_engine, request_id)
    registrations = build_generation_expiry_registrations(acceptance_port, status_port)
    processor_before_restart = ActionProcessor(h4_pg_engine, _QUEUE_OPTIONS, registrations)
    expiry_action = _expiry_action(h4_pg_engine, request_id)
    assert _process_action(processor_before_restart, h4_pg_engine, expiry_action.id).status == (
        "completed"
    )
    followup_action = _followup_action(h4_pg_engine, request_id, 1)
    message = _action_message(h4_pg_engine, followup_action.id)
    _force_action_due(h4_pg_engine, followup_action.id)
    claim, claim_result = processor_before_restart._claim(OutboxMessage.model_validate(message))
    assert claim is not None, claim_result
    with Session(h4_pg_engine) as db:
        followup = db.get(ChatflowGenerationAcceptedBatchFollowup, request_id)
        action = db.get(Action, followup_action.id)
        receipt = db.get(OutboxReceipt, followup_action.outbox_event_id)
        assert followup is not None and action is not None and receipt is not None
        assert followup.query_count == 1 and followup.state == "active"
        assert action.status == "claimed" and receipt.status == "processing"

    # A fresh processor instance models a process restart after the request may
    # have reached image-generation, but before its response was committed.
    with Session(h4_pg_engine) as db, db.begin():
        action = db.get(Action, followup_action.id)
        receipt = db.get(OutboxReceipt, followup_action.outbox_event_id)
        assert action is not None and receipt is not None
        expired_lease = db.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
        action.lease_until = expired_lease
        receipt.lease_until = expired_lease
    processor_after_restart = ActionProcessor(h4_pg_engine, _QUEUE_OPTIONS, registrations)
    result = processor_after_restart.process_payload(message)
    assert result.status == "unknown"
    assert status_port.batch_reads == []
    with Session(h4_pg_engine) as db:
        followup = db.get(ChatflowGenerationAcceptedBatchFollowup, request_id)
        assert followup is not None
        assert followup.query_count == 1
        assert followup.state == "unknown"
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.generation_request_id == request_id,
                    Action.action_kind == ACCEPTED_BATCH_RECOVERY_ACTION,
                    Action.sequence_namespace == ACCEPTED_BATCH_SEQUENCE_NAMESPACE,
                )
            )
            == 1
        )


def test_terminal_accepted_followup_reuses_h7_private_delivery_and_ack(
    h4_pg_engine: Engine, tmp_path
) -> None:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (32, 24), (15, 80, 160)).save(output, format="PNG")
    content = output.getvalue()
    context = _h4_context(h4_pg_engine)
    request_id = context["request_id"]
    batch_id = f"batch-expiry-{request_id}"
    accepted = _accepted(h4_pg_engine, context, batch_id=batch_id)
    acceptance_port = _AcceptancePort(GenerationAcceptanceObservation("accepted", accepted))
    status_port = _StatusPort(context["h4"], terminal=True, content=content)
    _set_h6_exhausted(h4_pg_engine, request_id)
    delivery_port = _DeliveryPort(content)
    storage = LocalPrivateStorage(tmp_path / "private-generation-expiry")
    registrations = build_generation_expiry_registrations(acceptance_port, status_port)
    registrations.update(build_generation_delivery_registrations(delivery_port, storage))
    processor = ActionProcessor(h4_pg_engine, _QUEUE_OPTIONS, registrations)
    expiry_action = _expiry_action(h4_pg_engine, request_id)
    assert _process_action(processor, h4_pg_engine, expiry_action.id).status == "completed"
    followup = _followup_action(h4_pg_engine, request_id, 1)
    assert _process_action(processor, h4_pg_engine, followup.id).status == "completed"
    assert status_port.batch_reads == [batch_id]

    for _ in range(32):
        with Session(h4_pg_engine) as db:
            next_action = db.execute(
                select(Action)
                .where(
                    Action.generation_request_id == request_id,
                    Action.action_kind.in_({DELIVERY_DOWNLOAD_ACTION, DELIVERY_CONFIRM_ACTION}),
                    Action.status == "scheduled",
                )
                .order_by(Action.action_kind, Action.sequence, Action.id)
                .limit(1)
            ).scalar_one_or_none()
            if next_action is None:
                break
            action_id = next_action.id
        assert _process_action(processor, h4_pg_engine, action_id).status == "completed"

    with Session(h4_pg_engine) as db:
        followup = db.get(ChatflowGenerationAcceptedBatchFollowup, request_id)
        task = db.get(Task, context["task_id"])
        deliveries = (
            db.execute(
                select(ChatflowGenerationDelivery)
                .where(ChatflowGenerationDelivery.generation_request_id == request_id)
                .order_by(ChatflowGenerationDelivery.slot_id)
            )
            .scalars()
            .all()
        )
        assert followup is not None and followup.state == "complete"
        assert followup.resolved_at is not None and followup.query_count == 1
        assert task is not None and task.status == "succeeded" and task.terminal_at is not None
        assert deliveries and all(row.status == "confirmed" for row in deliveries)
        assert len(delivery_port.downloads) == len(deliveries)
        assert len(delivery_port.confirmations) == len(deliveries)
        for delivery in deliveries:
            with storage.open_reader(delivery.original_object_key) as reader:
                assert reader.read() == content


def test_unknown_acceptance_does_not_fail_task_or_create_followup(h4_pg_engine: Engine) -> None:
    context = _h4_context(h4_pg_engine)
    request_id = context["request_id"]
    port = _AcceptancePort(
        GenerationAcceptanceObservation(
            "unknown", unknown_reason="GENERATION_ACCEPTANCE_NOT_OBSERVED"
        )
    )
    status_port = _StatusPort(context["h4"])
    processor = ActionProcessor(
        h4_pg_engine,
        _QUEUE_OPTIONS,
        build_generation_expiry_registrations(port, status_port),
    )

    result = _process_action(processor, h4_pg_engine, _expiry_action(h4_pg_engine, request_id).id)

    assert result.status == "completed"
    assert port.calls == 1
    assert status_port.batch_reads == []
    with Session(h4_pg_engine) as db:
        task = db.get(Task, context["task_id"])
        evidence = db.get(ChatflowGenerationAcceptanceEvidence, request_id)
        assessment = db.get(ChatflowGenerationExpiryAssessment, request_id)
        followup = db.get(ChatflowGenerationAcceptedBatchFollowup, request_id)
        assert task is not None and task.status == "reconciling"
        assert evidence is None and followup is None
        assert assessment is not None and assessment.state == "scheduled"
        assert assessment.query_count == 1


def test_rejected_expiry_task_close_rolls_back_as_one_transaction(
    h4_pg_engine: Engine,
) -> None:
    context = _h4_context(h4_pg_engine)
    request_id = context["request_id"]
    rejected = _rejected(h4_pg_engine, context)
    observation = GenerationAcceptanceObservation("rejected_expired", rejected)
    action_id = _expiry_action(h4_pg_engine, request_id).id

    with pytest.raises(RuntimeError, match="rollback expiry closure"):
        with Session(h4_pg_engine) as db, db.begin():
            action = db.execute(
                select(Action).where(Action.id == action_id).with_for_update()
            ).scalar_one()
            action_input = prepare_generation_expiry_assessment(db, action)
            finalize_generation_expiry_assessment(
                db,
                action,
                action_input,
                ActionExecutionResult(
                    "accepted",
                    value={"queried": True, "observation": observation},
                ),
            )
            raise RuntimeError("rollback expiry closure")

    with Session(h4_pg_engine) as db:
        task = db.get(Task, context["task_id"])
        tracking = db.get(ChatflowGenerationTracking, request_id)
        evidence = db.get(ChatflowGenerationAcceptanceEvidence, request_id)
        assessment = db.get(ChatflowGenerationExpiryAssessment, request_id)
        items = (
            db.execute(select(TaskItem).where(TaskItem.task_id == context["task_id"]))
            .scalars()
            .all()
        )
        attempts = (
            db.execute(
                select(TaskItemAttempt).where(
                    TaskItemAttempt.task_item_id.in_([item.id for item in items])
                )
            )
            .scalars()
            .all()
        )
        action = db.get(Action, action_id)
        assert task is not None and task.status == "reconciling"
        assert tracking is not None and tracking.status == "awaiting_batch"
        assert evidence is None
        assert assessment is not None
        assert assessment.state == "scheduled" and assessment.query_count == 0
        assert all(item.status == "queued" and item.error_code is None for item in items)
        assert all(
            attempt.status == "prepared" and attempt.failure_code is None for attempt in attempts
        )
        assert action is not None and action.status == "scheduled"


def test_rejected_expiry_closes_only_the_unaccepted_task_and_allows_restart(
    h4_pg_engine: Engine,
) -> None:
    context = _h4_context(h4_pg_engine)
    request_id = context["request_id"]
    rejected = _rejected(h4_pg_engine, context)
    port = _AcceptancePort(GenerationAcceptanceObservation("rejected_expired", rejected))
    status_port = _StatusPort(context["h4"])
    processor = ActionProcessor(
        h4_pg_engine,
        _QUEUE_OPTIONS,
        build_generation_expiry_registrations(port, status_port),
    )
    action = _expiry_action(h4_pg_engine, request_id)
    assert _process_action(processor, h4_pg_engine, action.id).status == "completed"
    assert status_port.batch_reads == []

    with Session(h4_pg_engine) as db, db.begin():
        task = db.get(Task, context["task_id"])
        session = db.get(BusinessSession, context["session_id"])
        tracking = db.get(ChatflowGenerationTracking, request_id)
        items = (
            db.execute(select(TaskItem).where(TaskItem.task_id == context["task_id"]))
            .scalars()
            .all()
        )
        attempts = (
            db.execute(
                select(TaskItemAttempt).where(
                    TaskItemAttempt.task_item_id.in_([item.id for item in items])
                )
            )
            .scalars()
            .all()
        )
        evidence = db.get(ChatflowGenerationAcceptanceEvidence, request_id)
        assert task is not None and task.status == "failed" and task.terminal_at is not None
        assert task.expires_at == task.terminal_at + timedelta(days=task.retention_days)
        assert session is not None and session.status == "failed"
        assert tracking is not None and tracking.status == "rejected_before_acceptance"
        assert evidence is not None and evidence.outcome == "rejected_expired"
        assert all(
            item.status == "failed"
            and item.error_code == GENERATION_PERMIT_EXPIRED_REASON
            and item.image_id is None
            and not item.preview_ready
            for item in items
        )
        assert all(
            attempt.status == "failed"
            and attempt.failure_code == GENERATION_PERMIT_EXPIRED_REASON
            and attempt.completed_at is not None
            for attempt in attempts
        )
        assert (
            db.scalar(
                select(func.count(ChatflowGenerationSlotState.slot_id)).where(
                    ChatflowGenerationSlotState.generation_request_id == request_id
                )
            )
            == 0
        )

    task_owner_and_reference = None
    with Session(h4_pg_engine) as db:
        task = db.get(Task, context["task_id"])
        assert task is not None
        task_owner_and_reference = (task.owner_user_id, task.reference_set_id)
    service = context["client"].app.state.business_session_service
    restarted = service.create_session(
        owner_user_id=task_owner_and_reference[0],
        reference_set_id=task_owner_and_reference[1],
        idempotency_key=f"expiry-explicit-restart-{uuid4().hex}",
    )
    assert restarted.business_session_id != context["session_id"]

    # Exercise the existing cleanup gate using the original historical H6
    # unknown Action; only its exact durable rejection proof resolves it.
    with Session(h4_pg_engine) as db, db.begin():
        task = db.get(Task, context["task_id"])
        assert task is not None
        task.terminal_at = datetime.now(UTC) - timedelta(days=task.retention_days)
        task.expires_at = task.terminal_at + timedelta(days=task.retention_days)
        h6 = db.execute(
            select(Action).where(
                Action.generation_request_id == request_id,
                Action.action_kind == GENERATION_RECOVERY_ACTION,
            )
        ).scalar_one()
        h6.status = "unknown"
        h6.last_outcome = "unknown"
        h6.completed_at = datetime.now(UTC)
        h6.claim_token = None
        h6.lease_until = None
    with Session(h4_pg_engine) as db:
        resolved = _resolved_generation_requests_for_cleanup(db, context["task_id"])
        assert request_id in resolved
