from uuid import uuid4

import pytest
from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.business_session_models import BusinessSession
from app.db.chatflow_reference_files import ChatflowDifyReferenceFile
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.integrations.dify.chatflow import DifyReferenceUploadResult, StubDifyChatflowAdapter
from app.schemas.chatflow_v34 import V34_FLOW_VERSION
from app.services.business_sessions import BusinessSessionOptions
from app.services.chatflow_actions import (
    CHATFLOW_REFERENCE_UPLOAD_ACTION,
    CHATFLOW_START_ACTION,
    build_chatflow_action_registrations,
)
from app.workers.action_processor import ActionProcessor
from app.workers.options import QueueOptions
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.integration.business_sessions.test_business_sessions_postgres import (
    TRUSTED_ORIGIN,
    _create_reference_set_with_content,
    _MemoryReferenceStorage,
    _new_guest,
    _process_chatflow_action,
)
from tests.integration.business_sessions.test_chatflow_v34_sessions_postgres import (
    v34_pg_engine as v34_pg_engine,
)


def setup_session(engine, adapter, count=1):
    client, owner = _new_guest(engine)
    reference_id, contents = _create_reference_set_with_content(engine, owner, count=count)
    service = client.app.state.business_session_service
    service.options = BusinessSessionOptions(
        dify_app_id="mock-chatflow-v1",
        flow_version=V34_FLOW_VERSION,
        dify_workflow_id="test-workflow",
    )
    created = service.create_session(
        owner_user_id=owner, reference_set_id=reference_id, idempotency_key=str(uuid4())
    )
    processor = ActionProcessor(
        engine,
        QueueOptions(broker_url="amqp://integration:integration@localhost:5672/h8-test"),
        build_chatflow_action_registrations(
            adapter, reference_storage=_MemoryReferenceStorage(contents, engine=engine)
        ),
    )
    return client, owner, reference_id, service, created, processor


def next_upload(engine, session_id):
    with Session(engine) as db, db.begin():
        row = db.scalars(
            select(Action).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_REFERENCE_UPLOAD_ACTION,
                Action.status == "scheduled",
            )
        ).one()
        row.due_at = db.scalar(select(func.clock_timestamp()))
        db.get(OutboxEvent, row.outbox_event_id).available_at = row.due_at
        receipt = db.get(OutboxReceipt, row.outbox_event_id)
        if receipt is not None:
            receipt.available_at = row.due_at
        return row.id


def test_bounded_upload_retries_are_ordered_and_start_only_once(v34_pg_engine):
    class Reconnecting(StubDifyChatflowAdapter):
        attempts = 0

        def upload_reference(self, command):
            self.attempts += 1
            if self.attempts < 3:
                return DifyReferenceUploadResult("not_sent")
            return super().upload_reference(command)

    adapter = Reconnecting()
    _client, owner, _ref, service, created, processor = setup_session(v34_pg_engine, adapter, 4)
    for _ in range(6):
        action_id = next_upload(v34_pg_engine, created.business_session_id)
        assert (
            _process_chatflow_action(v34_pg_engine, action_id=action_id, processor=processor)
            == "completed"
        )
    with Session(v34_pg_engine) as db:
        rows = db.scalars(
            select(ChatflowDifyReferenceFile)
            .where(ChatflowDifyReferenceFile.business_session_id == created.business_session_id)
            .order_by(ChatflowDifyReferenceFile.position)
        ).all()
        assert [row.status for row in rows] == ["uploaded"] * 4
        starts = db.scalars(
            select(Action).where(
                Action.business_session_id == created.business_session_id,
                Action.action_kind == CHATFLOW_START_ACTION,
            )
        ).all()
        assert len(starts) == 1
        start_id = starts[0].id
    assert (
        _process_chatflow_action(v34_pg_engine, action_id=start_id, processor=processor)
        == "completed"
    )
    assert len(adapter.start_calls) == 1
    with Session(v34_pg_engine) as db:
        assert db.get(BusinessSession, created.business_session_id).dify_run_id is not None


def test_connection_retry_exhaustion_fails_instead_of_infinite_reconciliation(v34_pg_engine):
    class Offline(StubDifyChatflowAdapter):
        def upload_reference(self, command):
            return DifyReferenceUploadResult("not_sent")

    client, owner, reference, service, created, processor = setup_session(v34_pg_engine, Offline())
    for attempt in range(4):
        outcome = _process_chatflow_action(
            v34_pg_engine,
            action_id=next_upload(v34_pg_engine, created.business_session_id),
            processor=processor,
        )
        assert outcome == ("rejected" if attempt == 3 else "completed")
    result = service.get_session(
        owner_user_id=owner, business_session_id=created.business_session_id
    )
    assert result.status == "failed"
    assert result.safe_reason_code == "DIFY_REFERENCE_CONNECTION_UNAVAILABLE"
    assert (
        service.create_session(
            owner_user_id=owner, reference_set_id=reference, idempotency_key=str(uuid4())
        ).status
        == "starting"
    )


def test_unknown_prestart_end_is_owner_scoped_replayable_and_preserves_reference(v34_pg_engine):
    adapter = StubDifyChatflowAdapter(lose_reference_upload_response_at=0)
    client, owner, reference, service, created, processor = setup_session(v34_pg_engine, adapter)
    assert (
        _process_chatflow_action(
            v34_pg_engine,
            action_id=next_upload(v34_pg_engine, created.business_session_id),
            processor=processor,
        )
        == "completed"
    )
    result = service.get_session(
        owner_user_id=owner, business_session_id=created.business_session_id
    )
    assert result.can_end_before_start
    another, another_owner = _new_guest(v34_pg_engine)
    assert service.list_sessions(owner_user_id=another_owner).items == []
    assert (
        another.post(
            f"/api/v1/business-sessions/{created.business_session_id}/end-unstarted",
            headers={"Origin": TRUSTED_ORIGIN},
        ).status_code
        == 404
    )
    entries = client.get("/api/v1/business-sessions?limit=1").json()
    assert entries["items"][0]["reference_set_id"] == str(reference)
    assert "dify_user_id" not in str(entries)
    ended = service.end_unstarted(
        owner_user_id=owner, business_session_id=created.business_session_id
    )
    assert ended.status == "failed"
    assert (
        service.end_unstarted(
            owner_user_id=owner, business_session_id=created.business_session_id
        ).safe_reason_code
        == "PRESTART_UPLOAD_ABANDONED"
    )
    assert (
        service.create_session(
            owner_user_id=owner, reference_set_id=reference, idempotency_key=str(uuid4())
        ).status
        == "starting"
    )
    assert not adapter.start_calls


def test_active_action_or_started_run_cannot_be_ended_and_new_owner_is_unblocked(v34_pg_engine):
    client, owner, _ref, service, created, processor = setup_session(
        v34_pg_engine, StubDifyChatflowAdapter()
    )
    with pytest.raises(DomainError) as failure:
        service.end_unstarted(owner_user_id=owner, business_session_id=created.business_session_id)
    assert failure.value.code == "SESSION_ALREADY_STARTED_OR_ACTIVE"
    assert (
        _process_chatflow_action(
            v34_pg_engine,
            action_id=next_upload(v34_pg_engine, created.business_session_id),
            processor=processor,
        )
        == "completed"
    )
    with Session(v34_pg_engine) as db:
        start_id = db.scalar(
            select(Action.id).where(
                Action.business_session_id == created.business_session_id,
                Action.action_kind == CHATFLOW_START_ACTION,
            )
        )
    assert (
        _process_chatflow_action(v34_pg_engine, action_id=start_id, processor=processor)
        == "completed"
    )
    with pytest.raises(DomainError):
        service.end_unstarted(owner_user_id=owner, business_session_id=created.business_session_id)
    with Session(v34_pg_engine) as db:
        previous_status = db.get(BusinessSession, created.business_session_id).status
        previous_run = db.get(BusinessSession, created.business_session_id).dify_run_id
    new_owner = client.post("/api/v1/auth/guest/reset", headers={"Origin": TRUSTED_ORIGIN}).json()[
        "user_id"
    ]
    from uuid import UUID

    new_ref, _ = _create_reference_set_with_content(v34_pg_engine, UUID(new_owner), count=1)
    assert (
        service.create_session(
            owner_user_id=UUID(new_owner), reference_set_id=new_ref, idempotency_key=str(uuid4())
        ).status
        == "starting"
    )
    with Session(v34_pg_engine) as db:
        original = db.get(BusinessSession, created.business_session_id)
        assert original.status == previous_status
        assert original.dify_run_id == previous_run


def test_session_history_pagination_and_malformed_cursor(v34_pg_engine):
    client, owner, _ref, service, created, _processor = setup_session(
        v34_pg_engine, StubDifyChatflowAdapter()
    )
    service.options = BusinessSessionOptions(
        dify_app_id="mock-chatflow-v1",
        flow_version=V34_FLOW_VERSION,
        dify_workflow_id="test-workflow",
        max_active_per_owner=3,
    )
    reference, _ = _create_reference_set_with_content(v34_pg_engine, owner, count=1)
    second = service.create_session(
        owner_user_id=owner, reference_set_id=reference, idempotency_key=str(uuid4())
    )
    first = service.list_sessions(owner_user_id=owner, limit=1)
    assert first.items[0].business_session_id == second.business_session_id
    page2 = service.list_sessions(owner_user_id=owner, limit=1, cursor=first.next_cursor)
    assert page2.items[0].business_session_id == created.business_session_id
    assert page2.next_cursor is None
    assert client.get("/api/v1/business-sessions?cursor=garbage").status_code == 400
