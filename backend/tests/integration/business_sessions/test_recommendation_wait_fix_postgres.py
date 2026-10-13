import hashlib
from uuid import UUID, uuid4

import pytest
from app.db.action_models import Action
from app.db.business_session_models import BusinessSession
from app.db.chatflow_reference_files import ChatflowDifyReferenceFile
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.integrations.dify.chatflow import (
    ChatflowOriginalRunEvent,
    ChatflowOriginalRunObservationResult,
    ChatflowStartResult,
)
from app.services.chatflow_actions import (
    CHATFLOW_START_ACTION,
    CHATFLOW_START_OBSERVATION_ACTION,
    build_chatflow_action_registrations,
    chatflow_action_digest,
    chatflow_action_id,
)
from app.workers.action_processor import ActionProcessor, schedule_action_in_transaction
from app.workers.options import QueueOptions
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.integration.business_sessions.test_business_sessions_postgres import (
    TRUSTED_ORIGIN,
    _process_chatflow_action,
)
from tests.integration.business_sessions.test_chatflow_v34_sessions_postgres import (
    _create_session,
)
from tests.integration.business_sessions.test_chatflow_v34_sessions_postgres import (
    v34_pg_engine as v34_pg_engine,
)


class StartPort:
    def __init__(self, outcome, terminal="failed"):
        self.outcome = outcome
        self.terminal = terminal
        self.start_calls = 0
        self.observe_calls = 0

    def start_recommendation(self, command):
        self.start_calls += 1
        return ChatflowStartResult(
            self.outcome,
            dify_run_id="run-" + str(command.business_session_id),
            safe_reason_code="V34_DIFY_WORKFLOW_FAILED"
            if self.outcome == "definitely_rejected"
            else "DIFY_HUMAN_INPUT_NOT_OBSERVED",
        )

    def observe_original_run(self, run, user):
        self.observe_calls += 1
        if self.terminal is None:
            return ChatflowOriginalRunObservationResult("unknown")
        return ChatflowOriginalRunObservationResult(
            "received",
            events=(
                ChatflowOriginalRunEvent(
                    event="workflow_finished", dify_run_id=run, workflow_status=self.terminal
                ),
            ),
        )


def setup_start(engine, outcome, terminal="failed"):
    client, owner, ref, created = _create_session(
        engine, flow_version="visual-v3.4-single-selection", idempotency_key=str(uuid4())
    )
    sid = UUID(created["business_session_id"])
    with Session(engine) as db, db.begin():
        session = db.get(BusinessSession, sid)
        now = db.scalar(select(func.clock_timestamp()))
        for row in db.scalars(
            select(ChatflowDifyReferenceFile).where(
                ChatflowDifyReferenceFile.business_session_id == sid
            )
        ):
            row.status = "uploaded"
            row.dify_file_id = "upload-" + str(row.position)
            row.uploaded_at = now
        for upload in db.scalars(
            select(Action).where(
                Action.business_session_id == sid,
                Action.action_kind == "dify.chatflow.reference.upload.v1",
            )
        ):
            upload.status = "completed"
            upload.completed_at = now
            upload.last_outcome = "accepted"
        action = schedule_action_in_transaction(
            db,
            action_id=chatflow_action_id(
                business_session_id=sid, action_kind=CHATFLOW_START_ACTION
            ),
            task_id=None,
            scope_kind="business_session",
            scope_id=sid,
            business_session_id=sid,
            action_kind=CHATFLOW_START_ACTION,
            sequence=1,
            due_at=now,
            input_digest_version=1,
            input_digest=chatflow_action_digest(
                action_kind=CHATFLOW_START_ACTION,
                business_session_id=sid,
                task_id=None,
                request_digest=hashlib.sha256(
                    session.request_digest + session.reference_asset_digest
                ).digest(),
            ),
        )
        aid = action.id
    port = StartPort(outcome, terminal)
    processor = ActionProcessor(
        engine,
        QueueOptions(broker_url="amqp://test:test@localhost:5672/test"),
        build_chatflow_action_registrations(port),
    )
    _process_chatflow_action(engine, processor=processor, action_id=aid)
    return client, sid, ref, port, processor


@pytest.mark.parametrize("outcome", ["definitely_rejected", "unknown"])
def test_failed_start_releases_capacity_and_allows_same_reference_retry(v34_pg_engine, outcome):
    client, sid, ref, port, processor = setup_start(v34_pg_engine, outcome)
    if outcome == "unknown":
        with Session(v34_pg_engine) as db:
            aid = db.scalar(
                select(Action.id).where(
                    Action.business_session_id == sid,
                    Action.action_kind == CHATFLOW_START_OBSERVATION_ACTION,
                )
            )
        assert aid is not None
        _process_chatflow_action(v34_pg_engine, processor=processor, action_id=aid)
    result = client.get(f"/api/v1/business-sessions/{sid}").json()
    assert result["status"] == "failed"
    assert result["safe_reason_code"] == "V34_DIFY_WORKFLOW_FAILED"
    assert result["selection_ready"] is False
    assert port.start_calls == 1
    replay = client.post(
        "/api/v1/business-sessions",
        json={"reference_set_id": str(ref)},
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": str(uuid4())},
    )
    assert replay.status_code == 202
    assert replay.json()["business_session_id"] != str(sid)


def test_uncertain_start_exhausts_three_observations_without_second_post(v34_pg_engine):
    client, sid, ref, port, processor = setup_start(v34_pg_engine, "unknown", None)
    for seq in range(25, 28):
        with Session(v34_pg_engine) as db, db.begin():
            a = db.scalar(
                select(Action).where(Action.business_session_id == sid, Action.sequence == seq)
            )
            assert a is not None
            a.due_at = db.scalar(select(func.clock_timestamp()))
            event = db.get(OutboxEvent, a.outbox_event_id)
            event.available_at = a.due_at
            receipt = db.get(OutboxReceipt, a.outbox_event_id)
            if receipt is not None:
                receipt.available_at = a.due_at
            aid = a.id
        _process_chatflow_action(v34_pg_engine, processor=processor, action_id=aid)
    result = client.get(f"/api/v1/business-sessions/{sid}").json()
    assert result["status"] == "reconciling"
    assert result["safe_reason_code"] == "V34_START_OBSERVATION_EXHAUSTED"
    assert port.start_calls == 1 and port.observe_calls == 3
    with Session(v34_pg_engine) as db:
        assert (
            db.scalar(
                select(Action.id).where(Action.business_session_id == sid, Action.sequence == 28)
            )
            is None
        )
