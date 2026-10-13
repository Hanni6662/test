from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from app.api.tasks import router as tasks_router
from app.db.action_models import Action
from app.db.business_session_models import BusinessSession
from app.db.chatflow_generation_models import (
    ChatflowGenerationAcceptanceEvidence,
    ChatflowGenerationAcceptedBatchFollowup,
    ChatflowGenerationDelivery,
    ChatflowGenerationExpiryAssessment,
    ChatflowGenerationPermit,
    ChatflowGenerationRequest,
)
from app.db.chatflow_v34_models import (
    ChatflowV34RunEndEvidence,
    ChatflowV34SessionState,
    ChatflowV34StageEventReceipt,
)
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.recommendation_models import Recommendation
from app.db.task_models import Task, TaskItem, TaskItemAttempt
from app.integrations.dify.chatflow import (
    ChatflowOriginalRunEvent,
    ChatflowOriginalRunObservationResult,
)
from app.integrations.generation_acceptance import GenerationAcceptanceObservation
from app.integrations.storage.local import LocalPrivateStorage
from app.schemas.chatflow_generation import (
    ChatflowGenerationEvent,
    ChatflowGenerationSlotEvent,
    chatflow_generation_event_digest,
    service_generation_status,
)
from app.schemas.chatflow_generation_results import (
    ChatflowGenerationManifestSlot,
    ChatflowGenerationResultManifest,
    ChatflowGenerationSourceArtifact,
)
from app.schemas.generation_acceptance import GENERATION_IMAGE_CONTRACT_V3
from app.services.chatflow_actions import (
    CHATFLOW_RUN_OBSERVATION_ACTION,
    CHATFLOW_RUN_OBSERVATION_SEQUENCE_BASE,
)
from app.services.chatflow_generation import ChatflowGenerationEventService
from app.services.chatflow_generation_delivery_actions import (
    DELIVERY_CONFIRM_ACTION,
    DELIVERY_DOWNLOAD_ACTION,
    build_generation_delivery_registrations,
)
from app.services.chatflow_generation_expiry import (
    EXPIRY_ASSESS_ACTION,
    EXPIRY_ASSESS_SEQUENCE_NAMESPACE,
    MAX_EXPIRY_QUERIES,
    build_generation_expiry_registrations,
)
from app.services.chatflow_generation_results import ChatflowGenerationResultService
from app.services.chatflow_v34_events import ChatflowV34StageEventService
from app.workers.action_processor import ActionProcessor
from app.workers.options import QueueOptions
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session
from tests.integration.business_sessions.test_chatflow_generation_h7_postgres import (
    _delivery_action,
    _message_for_action,
    _png_bytes,
    _queue_options,
)
from tests.integration.business_sessions.test_chatflow_prompt_packages_postgres import (
    _post_submission,
)
from tests.integration.business_sessions.test_chatflow_v34_sessions_postgres import (
    _prepare_v34_observer_reconciling_handoff,
    _prepare_v34_run_observation_case,
    _process_v34_run_observation,
    _v34_handoff_row_counts,
    _v34_incomplete_event,
)
from tests.integration.business_sessions.test_permit_expiry_closure_postgres import (
    _accepted as _accepted_expiry_observation,
)
from tests.integration.business_sessions.test_permit_expiry_closure_postgres import (
    _expiry_action,
)
from tests.integration.business_sessions.test_permit_expiry_closure_postgres import (
    _h4_context as _expiry_h4_context,
)

_NO_GENERATION_CODE = "V34_WORKFLOW_ENDED_WITHOUT_GENERATION"
_NO_GENERATION_MESSAGE = "流程已结束，但未取得生图许可，本次未发起生图。"
_RUN_END_TABLES = {
    "chatflow_v34_run_end_evidence",
    "chatflow_generation_expiry_assessments",
    "chatflow_generation_accepted_batch_followups",
    "chatflow_generation_deliveries",
}


class _V3FakeDeliveryPort:
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


@pytest.fixture
def run_end_pg_engine() -> Iterator[Engine]:
    database_url = os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires the dedicated image-backend-test PostgreSQL runtime URL")
    if make_url(database_url).database != "image_backend_test":
        pytest.fail("refusing to run the run-end integration tests outside image_backend_test")
    engine = create_engine(database_url, pool_pre_ping=True)
    if not _RUN_END_TABLES.issubset(set(inspect(engine).get_table_names())):
        engine.dispose()
        pytest.skip("requires the isolated PostgreSQL schema through S026 run-end evidence")
    yield engine
    engine.dispose()


def _run_end_observation(
    run_id: str,
    *,
    event_name: str = "workflow_finished",
    workflow_status: str | None = "partial-succeeded",
    event_run_id: str | None = None,
    outcome: str = "received",
) -> ChatflowOriginalRunObservationResult:
    return ChatflowOriginalRunObservationResult(
        outcome,  # type: ignore[arg-type]
        events=(
            ChatflowOriginalRunEvent(
                event=event_name,
                dify_run_id=event_run_id or run_id,
                workflow_status=workflow_status,
            ),
        ),
    )


def _evidence_count(engine: Engine, session_id: UUID) -> int:
    with Session(engine) as db:
        return db.scalar(
            select(func.count(ChatflowV34RunEndEvidence.id)).where(
                ChatflowV34RunEndEvidence.business_session_id == session_id
            )
        )


def _closure_snapshot(engine: Engine, session_id: UUID, task_id: UUID) -> dict[str, object]:
    with Session(engine) as db:
        session = db.get(BusinessSession, session_id)
        recommendation = db.execute(
            select(Recommendation).where(Recommendation.business_session_id == session_id)
        ).scalar_one()
        task = db.get(Task, task_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        items = (
            db.execute(
                select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.position)
            )
            .scalars()
            .all()
        )
        evidence = db.execute(
            select(ChatflowV34RunEndEvidence).where(
                ChatflowV34RunEndEvidence.business_session_id == session_id
            )
        ).scalar_one_or_none()
        assert session is not None and task is not None
        return {
            "session_status": session.status,
            "recommendation_status": recommendation.status,
            "recommendation_failure_code": recommendation.failure_code,
            "task_status": task.status,
            "task_terminal_at": task.terminal_at,
            "task_expires_at": task.expires_at,
            "retention_days": task.retention_days,
            "state_reason": state.safe_reason_code,
            "item_statuses": tuple(item.status for item in items),
            "item_codes": tuple(item.error_code for item in items),
            "item_images": tuple(item.image_id for item in items),
            "item_previews": tuple(item.preview_ready for item in items),
            "evidence_id": evidence.id if evidence is not None else None,
            "evidence_run_id": evidence.dify_run_id if evidence is not None else None,
            "evidence_status": evidence.terminal_status if evidence is not None else None,
            "evidence_event": evidence.terminal_event if evidence is not None else None,
            "evidence_digest": evidence.event_digest if evidence is not None else None,
        }


def _assert_no_generation_rows(engine: Engine, session_id: UUID, task_id: UUID) -> None:
    with Session(engine) as db:
        assert (
            db.scalar(
                select(func.count(ChatflowGenerationRequest.id)).where(
                    ChatflowGenerationRequest.business_session_id == session_id
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(ChatflowGenerationPermit.permit_id)).where(
                    ChatflowGenerationPermit.task_id == task_id
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(TaskItemAttempt.id))
                .join(TaskItem, TaskItem.id == TaskItemAttempt.task_item_id)
                .where(TaskItem.task_id == task_id)
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(ChatflowV34StageEventReceipt.id)).where(
                    ChatflowV34StageEventReceipt.business_session_id == session_id,
                    ChatflowV34StageEventReceipt.event_type == "workflow.failed",
                    ChatflowV34StageEventReceipt.disposition == "accepted",
                )
            )
            == 0
        )


def _v34_h7_context(engine: Engine) -> dict[str, object]:
    client, session_id, recommendation_id, run_id, task_id, _observer_action, submission = (
        _prepare_v34_observer_reconciling_handoff(
            engine, idempotency_key=f"v34-run-end-h7-{uuid4().hex}"
        )
    )
    h4_response = _post_submission(client, submission)
    assert h4_response.status_code == 200, h4_response.text
    h4_body = h4_response.json()
    request_id = UUID(h4_body["generation_request_id"])
    image_bytes = _png_bytes()
    source_digest = hashlib.sha256(image_bytes).hexdigest()
    completed_position = 1
    event_slots: list[ChatflowGenerationSlotEvent] = []
    manifest_slots: list[ChatflowGenerationManifestSlot] = []
    for raw in h4_body["slots"]:
        position = raw["position"]
        completed = position == completed_position
        version = 2 if completed else 1
        event_slots.append(
            ChatflowGenerationSlotEvent(
                slot_id=raw["slot_id"],
                task_item_id=UUID(raw["task_item_id"]),
                attempt_id=UUID(raw["attempt_id"]),
                position=position,
                image_type=raw["image_type"],
                status="completed" if completed else "queued",
                status_version=version,
                known_retry_count=0,
            )
        )
        artifact = (
            ChatflowGenerationSourceArtifact(
                file_id=f"slot_{position:02d}-original-1.png",
                sha256=source_digest,
                mime_type="image/png",
                size_bytes=len(image_bytes),
                expires_at=None,
            )
            if completed
            else None
        )
        manifest_slots.append(
            ChatflowGenerationManifestSlot(
                slot_id=raw["slot_id"],
                task_item_id=UUID(raw["task_item_id"]),
                attempt_id=UUID(raw["attempt_id"]),
                position=position,
                image_type=raw["image_type"],
                status_version=version,
                original_artifact=artifact,
            )
        )
    generation_event = ChatflowGenerationEvent.model_validate(
        {
            "contract_version": "chatflow-generation-event-v1",
            "business_session_id": session_id,
            "task_id": task_id,
            "generation_request_id": request_id,
            "dify_run_id": run_id,
            "batch_id": f"batch-v34-run-end-{request_id}",
            "permit_version": 2,
            "permit_contract_version": "chatflow-generation-permit-v2",
            "package_digest": h4_body["package_digest"],
            "slot_mapping_digest": h4_body["permit"]["slot_mapping_digest"],
            "reference_manifest_digest": h4_body["permit"]["reference_manifest_digest"],
            "event_id": uuid4(),
            "source": "image_generation",
            "source_sequence": 1,
            "status": service_generation_status(event_slots),
            "digest_version": 1,
            "digest": "0" * 64,
            "slots": event_slots,
        },
        strict=True,
    )
    generation_event = generation_event.model_copy(
        update={"digest": chatflow_generation_event_digest(generation_event)}
    )
    manifest = ChatflowGenerationResultManifest(
        contract_version="chatflow-generation-result-manifest-v1",
        status_event=generation_event,
        slots=manifest_slots,
    )
    event_service = ChatflowGenerationEventService(engine)
    result_service = ChatflowGenerationResultService(engine, generation_event_service=event_service)
    return {
        "client": client,
        "session_id": session_id,
        "recommendation_id": recommendation_id,
        "run_id": run_id,
        "task_id": task_id,
        "request_id": request_id,
        "submission": submission,
        "h4_body": h4_body,
        "manifest": manifest,
        "result_service": result_service,
        "bytes": image_bytes,
    }


@pytest.mark.parametrize("workflow_status", ["partial-succeeded", "succeeded"])
def test_terminal_success_without_permit_closes_task_once_with_owner_safe_projection(
    run_end_pg_engine: Engine,
    workflow_status: str,
) -> None:
    client, session_id, recommendation_id, run_id, task_id, action_id = (
        _prepare_v34_run_observation_case(
            run_end_pg_engine,
            idempotency_key=f"v34-run-end-allowed-{workflow_status}-{uuid4().hex}",
        )
    )
    observation = _run_end_observation(run_id, workflow_status=workflow_status)

    result, port = _process_v34_run_observation(
        run_end_pg_engine, action_id=action_id, observation=observation
    )

    assert result == "completed"
    assert port.observation_calls == [(run_id, f"bs-{session_id.hex}")]
    snapshot = _closure_snapshot(run_end_pg_engine, session_id, task_id)
    assert snapshot["session_status"] == "failed"
    assert snapshot["recommendation_status"] == "failed"
    assert snapshot["recommendation_failure_code"] == _NO_GENERATION_CODE
    assert snapshot["task_status"] == "failed"
    assert snapshot["state_reason"] == _NO_GENERATION_CODE
    assert snapshot["item_statuses"] and set(snapshot["item_statuses"]) == {"failed"}
    assert set(snapshot["item_codes"]) == {_NO_GENERATION_CODE}
    assert set(snapshot["item_images"]) == {None}
    assert set(snapshot["item_previews"]) == {False}
    assert snapshot["evidence_run_id"] == run_id
    assert snapshot["evidence_status"] == workflow_status
    assert snapshot["evidence_event"] == "workflow_finished"
    assert snapshot["task_terminal_at"] is not None
    assert snapshot["task_expires_at"] == snapshot["task_terminal_at"] + timedelta(
        days=snapshot["retention_days"]
    )
    with Session(run_end_pg_engine) as db:
        evidence = db.get(ChatflowV34RunEndEvidence, snapshot["evidence_id"])
        action = db.get(Action, action_id)
        assert evidence is not None and action is not None
        assert evidence.business_session_id == session_id
        assert evidence.task_id == task_id
        assert evidence.recommendation_id == recommendation_id
        assert evidence.recommendation_revision == 1
        task = db.get(Task, task_id)
        assert task is not None and evidence.owner_user_id == task.owner_user_id
        assert evidence.selection_submission_id is not None
        assert len(evidence.selection_input_digest) == 32
        assert len(evidence.event_digest) == 32
        assert evidence.business_reason_code == _NO_GENERATION_CODE
        assert evidence.closure_outcome == "failed_without_generation"
        assert action.status == "completed"
    _assert_no_generation_rows(run_end_pg_engine, session_id, task_id)

    session_response = client.get(f"/api/v1/business-sessions/{session_id}")
    assert session_response.status_code == 200
    assert session_response.json()["status"] == "failed"
    assert session_response.json()["safe_reason_code"] == _NO_GENERATION_CODE
    client.app.state.task_service = client.app.state.business_session_service.task_service
    client.app.include_router(tasks_router)
    task_response = client.get(f"/api/v1/tasks/{task_id}")
    assert task_response.status_code == 200, task_response.text
    task_body = task_response.json()
    assert task_body["status"] == "failed"
    assert task_body["counts"] == {
        "planned": len(task_body["items"]),
        "succeeded": 0,
        "failed": len(task_body["items"]),
        "pending": 0,
        "unresolved": 0,
    }
    assert all(
        item["error"] == {"code": _NO_GENERATION_CODE, "message": _NO_GENERATION_MESSAGE}
        and item["image_id"] is None
        and item["preview_ready"] is False
        and "preview_url" not in item
        for item in task_body["items"]
    )

    # Re-delivery of the already completed observer Action is an idempotent
    # queue replay: it cannot rewrite terminal time/retention or duplicate proof.
    repeated, repeated_port = _process_v34_run_observation(
        run_end_pg_engine, action_id=action_id, observation=observation
    )
    assert repeated == "completed"
    assert repeated_port.observation_calls == []
    assert _closure_snapshot(run_end_pg_engine, session_id, task_id) == snapshot
    assert _evidence_count(run_end_pg_engine, session_id) == 1


@pytest.mark.parametrize(
    ("event_name", "workflow_status", "outcome", "event_run"),
    [
        ("workflow_finished", "running", "received", None),
        ("workflow_finished", "paused", "received", None),
        ("workflow_finished", "partial-failed", "received", None),
        ("workflow_finished", None, "received", None),
        ("workflow_finished", "partial-succeeded", "received", "wrong-run"),
        ("workflow_finished", "partial-succeeded", "unknown", None),
        ("workflow_failed", "succeeded", "received", None),
        ("node_failed", "failed", "received", None),
    ],
)
def test_untrusted_or_unrecognized_observation_stays_reconciling(
    run_end_pg_engine: Engine,
    event_name: str,
    workflow_status: str | None,
    outcome: str,
    event_run: str | None,
) -> None:
    _client, session_id, _recommendation_id, run_id, task_id, action_id = (
        _prepare_v34_run_observation_case(
            run_end_pg_engine,
            idempotency_key=f"v34-run-end-reject-{uuid4().hex}",
        )
    )
    observation = _run_end_observation(
        run_id,
        event_name=event_name,
        workflow_status=workflow_status,
        event_run_id=event_run,
        outcome=outcome,
    )

    result, port = _process_v34_run_observation(
        run_end_pg_engine, action_id=action_id, observation=observation
    )

    assert result == "completed"
    assert port.observation_calls == [(run_id, f"bs-{session_id.hex}")]
    assert _evidence_count(run_end_pg_engine, session_id) == 0
    with Session(run_end_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        task = db.get(Task, task_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        retry = db.execute(
            select(Action).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_RUN_OBSERVATION_ACTION,
                Action.sequence == CHATFLOW_RUN_OBSERVATION_SEQUENCE_BASE + 1,
            )
        ).scalar_one_or_none()
        assert session is not None and task is not None
        assert session.status == "reconciling"
        assert task.status == "reconciling"
        assert state.safe_reason_code == "V34_RUN_TERMINAL_NOT_OBSERVED"
        assert retry is not None and retry.status == "scheduled"
    _assert_no_generation_rows(run_end_pg_engine, session_id, task_id)


@pytest.mark.parametrize("workflow_status", ["failed"])
def test_explicit_workflow_failure_keeps_its_existing_failure_receipt(
    run_end_pg_engine: Engine,
    workflow_status: str,
) -> None:
    _client, session_id, _recommendation_id, run_id, task_id, action_id = (
        _prepare_v34_run_observation_case(
            run_end_pg_engine,
            idempotency_key=f"v34-run-end-existing-failure-{uuid4().hex}",
        )
    )
    result, _port = _process_v34_run_observation(
        run_end_pg_engine,
        action_id=action_id,
        observation=_run_end_observation(run_id, workflow_status=workflow_status),
    )

    assert result == "completed"
    assert _evidence_count(run_end_pg_engine, session_id) == 0
    with Session(run_end_pg_engine) as db:
        receipt = db.execute(
            select(ChatflowV34StageEventReceipt).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_type == "workflow.failed",
                ChatflowV34StageEventReceipt.disposition == "accepted",
            )
        ).scalar_one()
        task = db.get(Task, task_id)
        assert receipt.safe_reason_code == "V34_DIFY_WORKFLOW_FAILED"
        assert task is not None and task.status == "failed"
        assert all(
            item.error_code == "V34_PRE_GENERATION_FAILED"
            for item in db.execute(select(TaskItem).where(TaskItem.task_id == task_id))
            .scalars()
            .all()
        )


def test_selection_evidence_loss_fails_closed_before_generic_terminalization(
    run_end_pg_engine: Engine,
) -> None:
    _client, session_id, _recommendation_id, run_id, task_id, action_id = (
        _prepare_v34_run_observation_case(
            run_end_pg_engine, idempotency_key=f"v34-run-end-missing-selection-{uuid4().hex}"
        )
    )
    with Session(run_end_pg_engine) as db, db.begin():
        state = db.execute(
            select(ChatflowV34SessionState)
            .where(ChatflowV34SessionState.business_session_id == session_id)
            .with_for_update()
        ).scalar_one()
        # Selection identifiers/digest remain as history, while the durable
        # accepted disposition is withdrawn to model an inconsistent binding.
        state.selection_status = "unknown"
        state.selection_evidence_kind = None
        state.selection_accepted_at = None

    result, port = _process_v34_run_observation(
        run_end_pg_engine,
        action_id=action_id,
        observation=_run_end_observation(run_id),
    )

    assert result == "dead"
    assert port.observation_calls == []
    assert _evidence_count(run_end_pg_engine, session_id) == 0
    with Session(run_end_pg_engine) as db:
        task = db.get(Task, task_id)
        session = db.get(BusinessSession, session_id)
        assert task is not None and task.status == "queued" and task.terminal_at is None
        assert session is not None and session.status == "task_created"
    _assert_no_generation_rows(run_end_pg_engine, session_id, task_id)


def test_r3_product_identity_conflict_is_immutable_when_run_observer_arrives_late(
    run_end_pg_engine: Engine,
) -> None:
    client, session_id, recommendation_id, run_id, task_id, action_id = (
        _prepare_v34_run_observation_case(
            run_end_pg_engine,
            idempotency_key=f"v34-run-end-after-identity-conflict-{uuid4().hex}",
        )
    )
    conflict = _v34_incomplete_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        event_key=f"{run_id}:ready:identity-conflict-{uuid4().hex}",
        conflicting_field_ids=["3", "4"],
    )
    accepted = ChatflowV34StageEventService(run_end_pg_engine).accept(conflict)
    assert accepted.status == "accepted"
    before = _closure_snapshot(run_end_pg_engine, session_id, task_id)
    assert before["state_reason"] == "PRODUCT_IDENTITY_CONFLICT"
    assert before["recommendation_failure_code"] == "PRODUCT_IDENTITY_CONFLICT"
    assert set(before["item_codes"]) == {"V34_PRE_GENERATION_FAILED"}

    result, port = _process_v34_run_observation(
        run_end_pg_engine,
        action_id=action_id,
        observation=_run_end_observation(run_id),
    )

    assert result == "completed"
    assert port.observation_calls == []
    assert _closure_snapshot(run_end_pg_engine, session_id, task_id) == before
    assert _evidence_count(run_end_pg_engine, session_id) == 0
    public = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public.status_code == 200
    assert public.json()["safe_reason_code"] == "PRODUCT_IDENTITY_CONFLICT"


def test_late_identity_callback_cannot_replace_committed_generic_run_end(
    run_end_pg_engine: Engine,
) -> None:
    _client, session_id, recommendation_id, run_id, task_id, action_id = (
        _prepare_v34_run_observation_case(
            run_end_pg_engine,
            idempotency_key=f"v34-run-end-before-identity-conflict-{uuid4().hex}",
        )
    )
    assert (
        _process_v34_run_observation(
            run_end_pg_engine,
            action_id=action_id,
            observation=_run_end_observation(run_id),
        )[0]
        == "completed"
    )
    before = _closure_snapshot(run_end_pg_engine, session_id, task_id)

    late_conflict = _v34_incomplete_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        event_key=f"{run_id}:ready:late-identity-conflict-{uuid4().hex}",
        conflicting_field_ids=["3"],
    )
    late_result = ChatflowV34StageEventService(run_end_pg_engine).accept(late_conflict)

    assert late_result.status == "rejected"
    assert _closure_snapshot(run_end_pg_engine, session_id, task_id) == before
    assert _evidence_count(run_end_pg_engine, session_id) == 1
    with Session(run_end_pg_engine) as db:
        receipts = (
            db.execute(
                select(ChatflowV34StageEventReceipt).where(
                    ChatflowV34StageEventReceipt.business_session_id == session_id,
                    ChatflowV34StageEventReceipt.event_key == late_conflict.event_key,
                )
            )
            .scalars()
            .all()
        )
        assert len(receipts) == 1 and receipts[0].disposition == "rejected"


def test_run_end_transaction_rolls_back_if_database_fails_after_evidence_insert(
    run_end_pg_engine: Engine,
) -> None:
    from app.workers.action_processor import WorkerStateUnavailable

    _client, session_id, _recommendation_id, run_id, task_id, action_id = (
        _prepare_v34_run_observation_case(
            run_end_pg_engine,
            idempotency_key=f"v34-run-end-rollback-{uuid4().hex}",
        )
    )
    seen_evidence_insert: list[bool] = []

    def fail_after_evidence_insert(
        connection, _cursor, statement, _parameters, _context, _many
    ) -> None:
        if (
            not seen_evidence_insert
            and "insert into chatflow_v34_run_end_evidence" in statement.lower()
        ):
            seen_evidence_insert.append(True)
            raise RuntimeError("isolated injected run-end transaction rollback")

    event.listen(run_end_pg_engine, "after_cursor_execute", fail_after_evidence_insert)
    try:
        with pytest.raises(WorkerStateUnavailable):
            _process_v34_run_observation(
                run_end_pg_engine,
                action_id=action_id,
                observation=_run_end_observation(run_id),
            )
    finally:
        event.remove(run_end_pg_engine, "after_cursor_execute", fail_after_evidence_insert)

    assert seen_evidence_insert == [True]
    assert _evidence_count(run_end_pg_engine, session_id) == 0
    with Session(run_end_pg_engine) as db:
        task = db.get(Task, task_id)
        session = db.get(BusinessSession, session_id)
        items = db.execute(select(TaskItem).where(TaskItem.task_id == task_id)).scalars().all()
        action = db.get(Action, action_id)
        assert task is not None and task.status == "queued" and task.terminal_at is None
        assert session is not None and session.status == "task_created"
        assert all(item.status == "queued" and item.error_code is None for item in items)
        # Action claim is a separate committed phase; only finalization changes roll back.
        assert action is not None and action.status == "claimed"
    _assert_no_generation_rows(run_end_pg_engine, session_id, task_id)


class _AcceptancePort:
    def __init__(self, observation: GenerationAcceptanceObservation) -> None:
        self.observation = observation
        self.calls = 0

    def get_acceptance(self, _expected):
        self.calls += 1
        return self.observation


class _NoStatusPort:
    def get_by_request(self, *_args, **_kwargs):
        raise AssertionError("expiry assessment must not query batch status by request")

    def get_by_batch(self, *_args, **_kwargs):
        raise AssertionError("expiry assessment must not query batch status by batch")


def _force_action_due(engine: Engine, action_id: UUID) -> None:
    with Session(engine) as db, db.begin():
        action = db.get(Action, action_id)
        assert action is not None and action.outbox_event_id is not None
        outbox = db.get(OutboxEvent, action.outbox_event_id)
        receipt = db.get(OutboxReceipt, action.outbox_event_id)
        now = db.scalar(select(func.clock_timestamp()))
        assert outbox is not None and receipt is not None and now is not None
        action.due_at = now
        outbox.available_at = now
        receipt.available_at = now


def _expiry_action_at_sequence(engine: Engine, request_id: UUID, sequence: int) -> Action:
    with Session(engine) as db:
        return db.execute(
            select(Action).where(
                Action.generation_request_id == request_id,
                Action.action_kind == EXPIRY_ASSESS_ACTION,
                Action.sequence_namespace == EXPIRY_ASSESS_SEQUENCE_NAMESPACE,
                Action.sequence == sequence,
            )
        ).scalar_one()


def _wait_for_pg_lock_wait(engine: Engine, backend_pid: int, timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    with engine.connect() as connection:
        while time.monotonic() < deadline:
            wait_type = connection.execute(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": backend_pid},
            ).scalar_one_or_none()
            if wait_type == "Lock":
                return
    pytest.fail("competing transaction never entered a PostgreSQL row-lock wait")


@pytest.mark.parametrize("expiry_outcome", ["accepted", "unknown"])
def test_accepted_or_unknown_permit_expiry_evidence_blocks_run_end_closure(
    run_end_pg_engine: Engine,
    expiry_outcome: str,
) -> None:
    context = _expiry_h4_context(run_end_pg_engine)
    client = context["client"]
    task_id = context["task_id"]
    session_id = context["session_id"]
    request_id = context["request_id"]
    h4_body = context["h4"]
    observation_action_id: UUID
    with Session(run_end_pg_engine) as db:
        observation_action_id = db.execute(
            select(Action.id)
            .where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_RUN_OBSERVATION_ACTION,
                Action.status == "scheduled",
            )
            .order_by(Action.sequence.desc())
        ).scalar_one()

    if expiry_outcome == "accepted":
        acceptance = _accepted_expiry_observation(
            run_end_pg_engine,
            {"request_id": request_id},
            batch_id=f"run-end-accepted-batch-{request_id}",
        )
        service_observation = GenerationAcceptanceObservation("accepted", acceptance=acceptance)
    else:
        service_observation = GenerationAcceptanceObservation(
            "unknown", unknown_reason="GENERATION_ACCEPTANCE_HTTP_UNAVAILABLE"
        )
    acceptance_port = _AcceptancePort(service_observation)
    query_count = 1 if expiry_outcome == "accepted" else MAX_EXPIRY_QUERIES
    expiry_processor = ActionProcessor(
        run_end_pg_engine,
        QueueOptions(broker_url=f"amqp://integration:integration/run-end-expiry-{uuid4().hex}"),
        build_generation_expiry_registrations(acceptance_port, _NoStatusPort()),
    )
    for sequence in range(1, query_count + 1):
        expiry_action = (
            _expiry_action(run_end_pg_engine, request_id)
            if sequence == 1
            else _expiry_action_at_sequence(run_end_pg_engine, request_id, sequence)
        )
        _force_action_due(run_end_pg_engine, expiry_action.id)
        expiry_result = expiry_processor.process_payload(
            _message_for_action(run_end_pg_engine, expiry_action.id)
        )
        expected_result_status = (
            "unknown"
            if expiry_outcome == "unknown" and sequence == MAX_EXPIRY_QUERIES
            else "completed"
        )
        assert expiry_result.status == expected_result_status
        assert acceptance_port.calls == sequence

    with Session(run_end_pg_engine) as db:
        assessment = db.get(ChatflowGenerationExpiryAssessment, request_id)
        assert assessment is not None and assessment.state == expiry_outcome
        acceptance_count = db.scalar(
            select(func.count(ChatflowGenerationAcceptanceEvidence.generation_request_id)).where(
                ChatflowGenerationAcceptanceEvidence.generation_request_id == request_id
            )
        )
        followup_count = db.scalar(
            select(func.count(ChatflowGenerationAcceptedBatchFollowup.generation_request_id)).where(
                ChatflowGenerationAcceptedBatchFollowup.generation_request_id == request_id
            )
        )
        expiry_state = (acceptance_count, followup_count)
    if expiry_outcome == "accepted":
        # The accepted evidence protects the generation chain immediately. The
        # separate accepted-batch GET budget is eligible only after H6 is exhausted.
        assert expiry_state == (1, 0)
    else:
        assert expiry_state == (0, 0)

    before = _closure_snapshot(run_end_pg_engine, session_id, task_id)
    _force_action_due(run_end_pg_engine, observation_action_id)
    result, port = _process_v34_run_observation(
        run_end_pg_engine,
        action_id=observation_action_id,
        observation=_run_end_observation(str(h4_body["dify_run_id"])),
    )

    assert result == "completed"
    assert port.observation_calls == []
    assert _closure_snapshot(run_end_pg_engine, session_id, task_id) == before
    assert _evidence_count(run_end_pg_engine, session_id) == 0
    assert before["task_terminal_at"] is None
    with Session(run_end_pg_engine) as db:
        assert db.get(ChatflowGenerationRequest, request_id) is not None
        assert (
            db.scalar(
                select(func.count(ChatflowGenerationPermit.permit_id)).where(
                    ChatflowGenerationPermit.generation_request_id == request_id
                )
            )
            == 1
        )
    assert (
        db.scalar(
            select(func.count(Action.id)).where(
                Action.generation_request_id == request_id,
                Action.action_kind.like("chatflow.generation.%"),
            )
        )
        >= 1
    )
    assert client is not None


def test_existing_h7_delivery_and_ack_survive_late_terminal_observation(
    run_end_pg_engine: Engine,
    tmp_path,
) -> None:
    # This is a real H4 -> accepted generation event -> manifest -> private
    # delivery -> ACK chain using isolated fake ports and local storage. Delivery
    # and ACK rows have required request/slot/attempt parents, so no detached
    # synthetic rows are inserted to test the no-generation guard.
    ctx = _v34_h7_context(run_end_pg_engine)
    accepted = ctx["result_service"].accept(ctx["manifest"])
    assert accepted.status == "accepted"
    request_id = ctx["request_id"]
    task_id = ctx["task_id"]
    session_id = ctx["session_id"]
    storage = LocalPrivateStorage(tmp_path / "private-run-end-h7")
    delivery_port = _V3FakeDeliveryPort(ctx["bytes"])
    delivery_processor = ActionProcessor(
        run_end_pg_engine,
        _queue_options(),
        build_generation_delivery_registrations(delivery_port, storage),
    )
    download_action = _delivery_action(run_end_pg_engine, request_id, DELIVERY_DOWNLOAD_ACTION)
    _force_action_due(run_end_pg_engine, download_action.id)
    download_result = delivery_processor.process_payload(
        _message_for_action(run_end_pg_engine, download_action.id)
    )
    if download_result.status != "completed":
        with Session(run_end_pg_engine) as db:
            action_after_download = db.get(Action, download_action.id)
            delivery_after_download = db.execute(
                select(ChatflowGenerationDelivery).where(
                    ChatflowGenerationDelivery.generation_request_id == request_id
                )
            ).scalar_one_or_none()
            diagnostic = {
                "action": (
                    action_after_download.status,
                    action_after_download.last_outcome,
                    action_after_download.last_error_code,
                ),
                "delivery": (
                    delivery_after_download.status,
                    delivery_after_download.error_code,
                )
                if delivery_after_download is not None
                else None,
            }
    diagnostic_text = diagnostic if download_result.status != "completed" else None
    assert download_result.status == "completed", (
        f"result={download_result!r}; diagnostic={diagnostic_text!r}"
    )
    with Session(run_end_pg_engine) as db:
        delivery = db.execute(
            select(ChatflowGenerationDelivery).where(
                ChatflowGenerationDelivery.generation_request_id == request_id
            )
        ).scalar_one()
        assert delivery.status == "awaiting_confirmation"
        confirmation = (
            db.execute(
                select(Action)
                .where(
                    Action.generation_delivery_id == delivery.id,
                    Action.action_kind == DELIVERY_CONFIRM_ACTION,
                )
                .order_by(Action.sequence)
            )
            .scalars()
            .first()
        )
    assert confirmation is not None
    _force_action_due(run_end_pg_engine, confirmation.id)
    assert (
        delivery_processor.process_payload(
            _message_for_action(run_end_pg_engine, confirmation.id)
        ).status
        == "completed"
    )
    with Session(run_end_pg_engine) as db:
        delivery = db.execute(
            select(ChatflowGenerationDelivery).where(
                ChatflowGenerationDelivery.generation_request_id == request_id
            )
        ).scalar_one()
        task_before = db.get(Task, task_id)
        items_before = (
            db.execute(
                select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.position)
            )
            .scalars()
            .all()
        )
        terminal_action = db.execute(
            select(Action)
            .where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_RUN_OBSERVATION_ACTION,
                Action.status == "scheduled",
            )
            .order_by(Action.sequence.desc())
        ).scalar_one()
        assert delivery.status == "confirmed"
        assert task_before is not None
        task_state_before = task_before.status
        item_state_before = tuple(
            (item.status, item.image_id, item.preview_ready, item.error_code)
            for item in items_before
        )
        terminal_action_id = terminal_action.id
        run_id = ctx["submission"].dify_run_id

    _force_action_due(run_end_pg_engine, terminal_action_id)
    result, observer_port = _process_v34_run_observation(
        run_end_pg_engine,
        action_id=terminal_action_id,
        observation=_run_end_observation(run_id),
    )

    assert result == "completed"
    assert observer_port.observation_calls == []
    assert _evidence_count(run_end_pg_engine, session_id) == 0
    with Session(run_end_pg_engine) as db:
        delivery = db.execute(
            select(ChatflowGenerationDelivery).where(
                ChatflowGenerationDelivery.generation_request_id == request_id
            )
        ).scalar_one()
        task_after = db.get(Task, task_id)
        items_after = (
            db.execute(
                select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.position)
            )
            .scalars()
            .all()
        )
        ack_count = db.scalar(
            select(func.count(Action.id)).where(
                Action.generation_delivery_id == delivery.id,
                Action.action_kind == DELIVERY_CONFIRM_ACTION,
                Action.status == "completed",
            )
        )
        assert delivery.status == "confirmed"
        assert task_after is not None and task_after.status == task_state_before
        assert (
            tuple(
                (item.status, item.image_id, item.preview_ready, item.error_code)
                for item in items_after
            )
            == item_state_before
        )
        assert ack_count == 1


def test_terminal_observer_wins_h4_race_with_independent_postgres_connections(
    run_end_pg_engine: Engine,
) -> None:
    (
        client,
        session_id,
        _recommendation_id,
        run_id,
        task_id,
        terminal_action_id,
        submission,
    ) = _prepare_v34_observer_reconciling_handoff(
        run_end_pg_engine, idempotency_key=f"v34-run-end-terminal-first-{uuid4().hex}"
    )
    observation = _run_end_observation(run_id)
    evidence_written = threading.Event()
    release_terminal_commit = threading.Event()
    h4_lock_attempt = threading.Event()
    observer_thread_id: list[int] = []
    h4_thread_id: list[int] = []
    backend_pids: dict[str, int] = {}
    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}
    h4_request_started = threading.Event()
    observer_engine = create_engine(run_end_pg_engine.url, pool_pre_ping=True)

    def backend_pid(connection) -> int:
        return connection.connection.driver_connection.info.backend_pid

    def hold_terminal_transaction(
        connection, _cursor, statement, _parameters, _context, _many
    ) -> None:
        if (
            observer_thread_id
            and threading.get_ident() == observer_thread_id[0]
            and "insert into chatflow_v34_run_end_evidence" in statement.lower()
        ):
            backend_pids["observer"] = backend_pid(connection)
            evidence_written.set()
            if not release_terminal_commit.wait(20):
                raise TimeoutError("test synchronization did not release terminal observer")

    def record_h4_lock_attempt(
        connection, _cursor, statement, _parameters, _context, _many
    ) -> None:
        if h4_request_started.is_set() and "for update" in statement.lower():
            backend_pids["h4"] = backend_pid(connection)
            h4_lock_attempt.set()

    def terminal_observer() -> None:
        observer_thread_id.append(threading.get_ident())
        try:
            results["observer"] = _process_v34_run_observation(
                observer_engine, action_id=terminal_action_id, observation=observation
            )[0]
        except BaseException as exc:
            errors["observer"] = exc

    def submit_h4() -> None:
        h4_thread_id.append(threading.get_ident())
        try:
            h4_request_started.set()
            results["h4"] = _post_submission(client, submission)
        except BaseException as exc:
            errors["h4"] = exc

    event.listen(observer_engine, "after_cursor_execute", hold_terminal_transaction)
    event.listen(run_end_pg_engine, "before_cursor_execute", record_h4_lock_attempt)
    observer_thread = threading.Thread(
        target=terminal_observer, name="v34-terminal-observer-first", daemon=True
    )
    h4_thread: threading.Thread | None = None
    observer_thread.start()
    try:
        assert evidence_written.wait(15), "terminal observer did not write run-end evidence"
        h4_thread = threading.Thread(target=submit_h4, name="v34-h4-after-terminal", daemon=True)
        h4_thread.start()
        assert h4_lock_attempt.wait(15), "H4 did not contend for the observer's locked parent chain"
        h4_pid = backend_pids.get("h4")
        assert h4_pid is not None
        _wait_for_pg_lock_wait(run_end_pg_engine, h4_pid)
    finally:
        release_terminal_commit.set()
        observer_thread.join(timeout=20)
        if h4_thread is not None:
            h4_thread.join(timeout=20)
        event.remove(observer_engine, "after_cursor_execute", hold_terminal_transaction)
        event.remove(run_end_pg_engine, "before_cursor_execute", record_h4_lock_attempt)
        observer_engine.dispose()

    assert not observer_thread.is_alive(), "terminal observer did not finish after commit release"
    assert h4_thread is not None and not h4_thread.is_alive(), (
        "H4 remained blocked past the test deadline"
    )
    assert errors == {}, repr(errors)
    assert backend_pids.get("observer") is not None and backend_pids.get("h4") is not None
    assert backend_pids["observer"] != backend_pids["h4"]
    assert results["observer"] == "completed"
    response = results["h4"]
    assert response.status_code == 409
    assert response.json()["code"] == "PROMPT_PACKAGE_LINKAGE_INVALID"
    assert _v34_handoff_row_counts(run_end_pg_engine, task_id) == (0, 0, 0, 0, 0)
    snapshot = _closure_snapshot(run_end_pg_engine, session_id, task_id)
    assert snapshot["task_status"] == "failed"
    assert snapshot["state_reason"] == _NO_GENERATION_CODE
    assert snapshot["evidence_status"] == "partial-succeeded"
    assert _evidence_count(run_end_pg_engine, session_id) == 1


def test_h4_wins_terminal_observer_race_with_independent_postgres_connections(
    run_end_pg_engine: Engine,
) -> None:
    (
        _client,
        session_id,
        _recommendation_id,
        run_id,
        task_id,
        terminal_action_id,
        submission,
    ) = _prepare_v34_observer_reconciling_handoff(
        run_end_pg_engine, idempotency_key=f"v34-run-end-h4-first-{uuid4().hex}"
    )
    observation = _run_end_observation(run_id)
    permit_inserted = threading.Event()
    release_h4_commit = threading.Event()
    observer_lock_attempt = threading.Event()
    observer_thread_id: list[int] = []
    backend_pids: dict[str, int] = {}
    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}
    observer_engine = create_engine(run_end_pg_engine.url, pool_pre_ping=True)

    def backend_pid(connection) -> int:
        return connection.connection.driver_connection.info.backend_pid

    def hold_h4_transaction(connection, _cursor, statement, _parameters, _context, _many) -> None:
        if "insert into chatflow_generation_permits" in statement.lower():
            backend_pids["h4"] = backend_pid(connection)
            permit_inserted.set()
            if not release_h4_commit.wait(20):
                raise TimeoutError("test synchronization did not release H4 transaction")

    def record_observer_lock_attempt(
        connection, _cursor, statement, _parameters, _context, _many
    ) -> None:
        if (
            observer_thread_id
            and threading.get_ident() == observer_thread_id[0]
            and "from users" in statement.lower()
            and "for update" in statement.lower()
        ):
            backend_pids["observer"] = backend_pid(connection)
            observer_lock_attempt.set()

    def submit_h4() -> None:
        try:
            results["h4"] = _post_submission(_client, submission)
        except BaseException as exc:
            errors["h4"] = exc

    def terminal_observer() -> None:
        observer_thread_id.append(threading.get_ident())
        try:
            results["observer"] = _process_v34_run_observation(
                observer_engine, action_id=terminal_action_id, observation=observation
            )[0]
        except BaseException as exc:
            errors["observer"] = exc

    event.listen(run_end_pg_engine, "after_cursor_execute", hold_h4_transaction)
    event.listen(observer_engine, "before_cursor_execute", record_observer_lock_attempt)
    h4_thread = threading.Thread(target=submit_h4, name="v34-h4-first", daemon=True)
    observer_thread: threading.Thread | None = None
    h4_thread.start()
    try:
        assert permit_inserted.wait(15), "H4 did not flush its generation permit"
        observer_thread = threading.Thread(
            target=terminal_observer, name="v34-terminal-observer-after-h4", daemon=True
        )
        observer_thread.start()
        assert observer_lock_attempt.wait(15), "observer did not contend for H4's parent locks"
        observer_pid = backend_pids.get("observer")
        assert observer_pid is not None
        _wait_for_pg_lock_wait(run_end_pg_engine, observer_pid)
    finally:
        release_h4_commit.set()
        h4_thread.join(timeout=20)
        if observer_thread is not None:
            observer_thread.join(timeout=20)
        event.remove(run_end_pg_engine, "after_cursor_execute", hold_h4_transaction)
        event.remove(observer_engine, "before_cursor_execute", record_observer_lock_attempt)
        observer_engine.dispose()

    assert not h4_thread.is_alive(), "H4 remained blocked past the test deadline"
    assert observer_thread is not None and not observer_thread.is_alive()
    assert errors == {}, repr(errors)
    assert backend_pids.get("h4") is not None and backend_pids.get("observer") is not None
    assert backend_pids["h4"] != backend_pids["observer"]
    response = results["h4"]
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"
    assert response.json()["permit"]["permit_version"] == 2
    assert results["observer"] == "completed"
    assert _v34_handoff_row_counts(run_end_pg_engine, task_id) == (
        1,
        1,
        1,
        1,
        len(submission.image_types),
    )
    assert _evidence_count(run_end_pg_engine, session_id) == 0
    with Session(run_end_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        task = db.get(Task, task_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        accepted_failures = db.scalar(
            select(func.count(ChatflowV34StageEventReceipt.id)).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_type == "workflow.failed",
                ChatflowV34StageEventReceipt.disposition == "accepted",
            )
        )
        assert session is not None and session.status == "reconciling"
        assert task is not None and task.status == "reconciling" and task.terminal_at is None
        assert state.safe_reason_code == "V34_RUN_TERMINAL_NOT_OBSERVED"
        assert accepted_failures == 0
