from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from app.db.action_models import Action
from app.db.business_session_models import BusinessSession
from app.db.chatflow_generation_closure_models import ChatflowGenerationRunClosure
from app.db.chatflow_generation_models import ChatflowGenerationTracking
from app.db.chatflow_v34_models import ChatflowV34SessionState
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.reference_models import ImageAsset
from app.db.task_models import Task
from app.dispatcher import OutboxDispatcher
from app.integrations.dify.run_details import normalize_run_detail
from app.integrations.storage.local import LocalPrivateStorage
from app.schemas.chatflow_generation import chatflow_generation_event_digest
from app.schemas.chatflow_generation_results import ChatflowGenerationSourceArtifact
from app.services.business_sessions import BusinessSessionService
from app.services.chatflow_generation_closure import (
    CLOSE_ACTION,
    CLOSE_NAMESPACE,
    build_close_observation_registration,
    ensure_generation_close_action,
    project_generation_completion,
)
from app.services.chatflow_generation_delivery_actions import (
    DELIVERY_CONFIRM_ACTION,
    DELIVERY_DOWNLOAD_ACTION,
    build_generation_delivery_registrations,
)
from app.workers.action_processor import ActionProcessor
from app.workers.event_allowlist import UNSUPPORTED_EVENT_REASON
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.integration.business_sessions.test_chatflow_generation_h7_postgres import (
    _message_for_action,
    _queue_options,
)
from tests.integration.business_sessions.test_v34_run_end_closure_postgres import (
    _V3FakeDeliveryPort,
    _v34_h7_context,
)

pytest_plugins = ["tests.integration.business_sessions.test_v34_run_end_closure_postgres"]


def _complete_source(ctx):
    manifest = ctx["manifest"]
    slots = [
        slot.model_copy(update={"status": "completed", "status_version": 2})
        for slot in manifest.status_event.slots
    ]
    event = manifest.status_event.model_copy(
        update={
            "slots": slots,
            "status": "completed",
            "source_sequence": 2,
            "event_id": uuid4(),
        }
    )
    event = event.model_copy(update={"digest": chatflow_generation_event_digest(event)})
    source = manifest.slots[0].original_artifact
    manifests = [
        slot.model_copy(
            update={
                "status_version": 2,
                "original_artifact": ChatflowGenerationSourceArtifact(
                    file_id=f"{slot.slot_id}-original-1.png",
                    sha256=source.sha256,
                    mime_type=source.mime_type,
                    size_bytes=source.size_bytes,
                    expires_at=None,
                ),
            }
        )
        for slot in manifest.slots
    ]
    result = ctx["result_service"].accept(
        manifest.model_copy(
            update={
                "status_event": event,
                "slots": manifests,
            }
        )
    )
    assert result.status == "accepted"


def _process_deliveries(engine, ctx, storage, kind):
    processor = ActionProcessor(
        engine,
        _queue_options(),
        build_generation_delivery_registrations(_V3FakeDeliveryPort(ctx["bytes"]), storage),
    )
    with Session(engine) as db:
        ids = db.scalars(
            select(Action.id).where(
                Action.generation_request_id == ctx["request_id"],
                Action.action_kind == kind,
                Action.status == "scheduled",
            )
        ).all()
    assert ids
    for action_id in ids:
        result = processor.process_payload(_message_for_action(engine, action_id))
        assert result.status == "completed", result


class _DetailsPort:
    def __init__(self, status):
        self.status = status
        self.calls = 0

    def get_run(self, run_id, app_id, workflow_id):
        self.calls += 1
        return normalize_run_detail(
            {
                "id": run_id,
                "workflow_id": workflow_id,
                "status": self.status,
                "error": "Aborted: Maximum execution steps exceeded: 501 > 500"
                if self.status == "stopped"
                else None,
            },
            run_id,
            workflow_id,
        )


@pytest.mark.parametrize("status", ["succeeded", "partial-succeeded", "stopped", "failed"])
def test_saved_images_wait_for_ack_then_close_with_actual_engine_status(
    run_end_pg_engine,
    tmp_path,
    status,
):
    ctx = _v34_h7_context(run_end_pg_engine)
    _complete_source(ctx)
    storage = LocalPrivateStorage(tmp_path / "private")
    _process_deliveries(run_end_pg_engine, ctx, storage, DELIVERY_DOWNLOAD_ACTION)
    with Session(run_end_pg_engine) as db, db.begin():
        state = db.scalar(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == ctx["session_id"]
            )
        )
        state.safe_reason_code = "V34_RUN_TERMINAL_NOT_OBSERVED"
        action = ensure_generation_close_action(
            db, ctx["request_id"], db.scalar(select(func.clock_timestamp()))
        )
        action_id = action.id
        assert db.get(Task, ctx["task_id"]).status == "succeeded"
    port = _DetailsPort(status)
    processor = ActionProcessor(
        run_end_pg_engine,
        _queue_options(),
        {
            CLOSE_ACTION: build_close_observation_registration(port),
        },
    )
    message = _message_for_action(run_end_pg_engine, action_id)
    assert processor.process_payload(message).status == "completed"
    with Session(run_end_pg_engine) as db:
        row = db.get(ChatflowGenerationRunClosure, ctx["request_id"])
        assert row.workflow_status == status and row.closed_at is None
        assert row.delivery_status == "pending"
    _process_deliveries(run_end_pg_engine, ctx, storage, DELIVERY_CONFIRM_ACTION)
    expected = "completed" if status == "succeeded" else "completed_with_execution_warning"
    with Session(run_end_pg_engine) as db:
        completion = project_generation_completion(db, ctx["task_id"])
        assert completion["task_status"] == "succeeded"
        assert completion["delivery_status"] == "confirmed"
        assert completion["reconciliation_status"] == expected
        assert completion["dify_run_status"] == status
        assert completion["closed_at"] is not None
        session = db.get(BusinessSession, ctx["session_id"])
        projected = BusinessSessionService._project_v34_session(
            db, session, db.scalar(select(func.clock_timestamp()))
        )
        assert projected.completion.reconciliation_status == expected
        assert projected.safe_reason_code != "V34_RUN_TERMINAL_NOT_OBSERVED"
        before_count = db.get(ChatflowGenerationRunClosure, ctx["request_id"]).query_count
    processor.process_payload(message)
    with Session(run_end_pg_engine) as db:
        assert db.get(ChatflowGenerationRunClosure, ctx["request_id"]).query_count == before_count
    assert port.calls == 1


def test_invalid_asset_link_cannot_be_closed_despite_success_strings(run_end_pg_engine, tmp_path):
    ctx = _v34_h7_context(run_end_pg_engine)
    _complete_source(ctx)
    storage = LocalPrivateStorage(tmp_path / "private")
    _process_deliveries(run_end_pg_engine, ctx, storage, DELIVERY_DOWNLOAD_ACTION)
    _process_deliveries(run_end_pg_engine, ctx, storage, DELIVERY_CONFIRM_ACTION)
    with Session(run_end_pg_engine) as db, db.begin():
        row = db.get(ChatflowGenerationRunClosure, ctx["request_id"])
        row.workflow_status = "succeeded"
        from app.db.chatflow_generation_models import ChatflowGenerationDelivery

        delivery = db.scalar(
            select(ChatflowGenerationDelivery).where(
                ChatflowGenerationDelivery.generation_request_id == ctx["request_id"]
            )
        )
        db.get(ImageAsset, delivery.original_asset_id).content_sha256 = "0" * 64
        ensure_generation_close_action(
            db, ctx["request_id"], db.scalar(select(func.clock_timestamp()))
        )
        assert row.delivery_status == "unresolved" and row.closed_at is None


def test_query_budget_is_finite_and_observer_namespace_does_not_collide(run_end_pg_engine):
    ctx = _v34_h7_context(run_end_pg_engine)
    with Session(run_end_pg_engine) as db, db.begin():
        action = ensure_generation_close_action(
            db, ctx["request_id"], db.scalar(select(func.clock_timestamp()))
        )
        assert action.sequence_namespace == CLOSE_NAMESPACE
        row = db.get(ChatflowGenerationRunClosure, ctx["request_id"])
        row.query_count = 32
        row.next_observation_at = db.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
        assert (
            ensure_generation_close_action(
                db, ctx["request_id"], db.scalar(select(func.clock_timestamp()))
            )
            is None
        )
        assert row.state == "unknown" and row.next_observation_at is None
        assert db.get(ChatflowGenerationTracking, ctx["request_id"]).reconcile_query_count == 0


def test_two_schedulers_create_one_live_observation(run_end_pg_engine):
    ctx = _v34_h7_context(run_end_pg_engine)

    def schedule():
        with Session(run_end_pg_engine) as db, db.begin():
            return ensure_generation_close_action(
                db, ctx["request_id"], db.scalar(select(func.clock_timestamp()))
            ).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: schedule(), range(2)))
    assert ids[0] == ids[1]
    with Session(run_end_pg_engine) as db:
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.generation_request_id == ctx["request_id"],
                    Action.sequence_namespace == CLOSE_NAMESPACE,
                )
            )
            == 1
        )


def test_unfinished_generation_is_not_closed_by_engine_success(run_end_pg_engine):
    ctx = _v34_h7_context(run_end_pg_engine)
    with Session(run_end_pg_engine) as db, db.begin():
        action = ensure_generation_close_action(
            db, ctx["request_id"], db.scalar(select(func.clock_timestamp()))
        )
        action_id = action.id
    port = _DetailsPort("succeeded")
    processor = ActionProcessor(
        run_end_pg_engine,
        _queue_options(),
        {
            CLOSE_ACTION: build_close_observation_registration(port),
        },
    )
    assert (
        processor.process_payload(_message_for_action(run_end_pg_engine, action_id)).status
        == "completed"
    )
    with Session(run_end_pg_engine) as db:
        completion = project_generation_completion(db, ctx["task_id"])
        assert completion["dify_run_status"] == "succeeded"
        assert completion["reconciliation_status"] == "pending"
        assert completion["closed_at"] is None


def test_quarantined_observer_is_published_and_consumed_after_registration(run_end_pg_engine):
    ctx = _v34_h7_context(run_end_pg_engine)
    options = replace(_queue_options(), claim_batch_size=1)
    with Session(run_end_pg_engine) as db, db.begin():
        action = ensure_generation_close_action(
            db, ctx["request_id"], db.scalar(select(func.clock_timestamp()))
        )
        action_id, event_id = action.id, action.outbox_event_id
        oldest = db.scalar(select(func.min(OutboxEvent.available_at)))
        db.get(OutboxEvent, event_id).available_at = oldest - timedelta(seconds=1)
    missing = OutboxDispatcher(run_end_pg_engine, options, allowed_event_types=frozenset())
    missing._quarantine_unregistered_events()
    with Session(run_end_pg_engine) as db:
        assert db.get(OutboxEvent, event_id).last_error_code == UNSUPPORTED_EVENT_REASON
    published = []

    class Publisher:
        def publish(self, event):
            published.append(event.event_id)

    registrations = {CLOSE_ACTION: build_close_observation_registration(_DetailsPort("succeeded"))}
    dispatcher = OutboxDispatcher(
        run_end_pg_engine, options, publisher=Publisher(), action_registrations=registrations
    )
    assert dispatcher.dispatch_once().published == 1
    assert published == [event_id]
    worker = ActionProcessor(run_end_pg_engine, options, registrations)
    assert (
        worker.process_payload(_message_for_action(run_end_pg_engine, action_id)).status
        == "completed"
    )
    with Session(run_end_pg_engine) as db:
        assert db.get(OutboxReceipt, event_id).status == "completed"
        assert (
            db.get(ChatflowGenerationRunClosure, ctx["request_id"]).workflow_status == "succeeded"
        )


def test_last_running_observation_exhausts_budget_as_unknown_not_pending(run_end_pg_engine):
    ctx = _v34_h7_context(run_end_pg_engine)
    with Session(run_end_pg_engine) as db, db.begin():
        action = ensure_generation_close_action(
            db, ctx["request_id"], db.scalar(select(func.clock_timestamp()))
        )
        action_id = action.id
        db.get(ChatflowGenerationRunClosure, ctx["request_id"]).query_count = 31
    port = _DetailsPort("running")
    processor = ActionProcessor(
        run_end_pg_engine,
        _queue_options(),
        {
            CLOSE_ACTION: build_close_observation_registration(port),
        },
    )
    result = processor.process_payload(_message_for_action(run_end_pg_engine, action_id))
    assert result.status == "completed" and port.calls == 1
    with Session(run_end_pg_engine) as db:
        row = db.get(ChatflowGenerationRunClosure, ctx["request_id"])
        assert row.query_count == 32 and row.state == "unknown"
        assert row.next_observation_at is None and row.closed_at is None
