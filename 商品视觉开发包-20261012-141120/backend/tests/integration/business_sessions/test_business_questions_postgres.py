from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from app.db.action_models import Action
from app.db.business_question_models import (
    BusinessAnswerSubmission,
    BusinessQuestion,
    BusinessQuestionEvent,
)
from app.db.outbox_models import OutboxEvent
from app.db.task_models import Task, TaskItem
from app.integrations.dify.chatflow import StubDifyChatflowAdapter
from app.schemas.business_sessions import (
    ChatflowBusinessEvent,
    chatflow_business_event_digest,
)
from app.services.business_questions import BusinessQuestionService
from app.services.business_sessions import BusinessSessionService
from app.services.chatflow_actions import (
    CHATFLOW_ANSWER_ACTION,
    CHATFLOW_ANSWER_RECOVERY_ACTION,
    execute_chatflow_answer,
    execute_chatflow_continuation,
    finalize_chatflow_answer,
    finalize_chatflow_continuation,
    prepare_chatflow_answer,
    prepare_chatflow_continuation,
    recover_chatflow_answer,
)
from app.services.tasks import TASK_PROMPT_EVENT_TYPE
from sqlalchemy import create_engine, delete, func, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from tests.integration.business_sessions.test_business_sessions_postgres import (
    TEST_INTERNAL_CALLBACK_SECRET,
    TRUSTED_ORIGIN,
    _make_ready_business_session,
    _selection_request,
)


@pytest.fixture
def h3_pg_engine() -> Iterator[Engine]:
    database_url = os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires the dedicated PostgreSQL test runtime URL")
    pg_engine = create_engine(database_url, pool_pre_ping=True)
    required = {
        "users",
        "sessions",
        "session_policies",
        "retention_policies",
        "reference_sets",
        "image_assets",
        "reference_set_images",
        "recommendations",
        "business_sessions",
        "outbox_events",
        "outbox_receipts",
        "tasks",
        "task_items",
        "task_idempotency_records",
        "task_recommendation_consumptions",
        "task_actions",
        "business_questions",
        "business_question_events",
        "business_answer_submissions",
    }
    if not required.issubset(set(inspect(pg_engine).get_table_names())):
        pg_engine.dispose()
        pytest.skip("requires the isolated PostgreSQL test schema through S014")
    yield pg_engine
    pg_engine.dispose()


def _human_input_event(
    callback: ChatflowBusinessEvent,
    *,
    task_id: UUID,
    source_sequence: int,
    round_number: int = 1,
    form_token: str | None,
    expires_at: datetime,
) -> ChatflowBusinessEvent:
    event = ChatflowBusinessEvent(
        contract_version="chatflow-handoff-v1",
        business_session_id=callback.business_session_id,
        recommendation_id=callback.recommendation_id,
        recommendation_revision=callback.recommendation_revision,
        dify_run_id=callback.dify_run_id,
        event_id=uuid4(),
        source="dify",
        source_sequence=source_sequence,
        event_type="human_input_required",
        digest_version=1,
        digest="0" * 64,
        payload={
            "task_id": str(task_id),
            "round_number": round_number,
            "question_text": "选择商品主色",
            "fields": [
                {
                    "field_id": "color",
                    "label": "颜色",
                    "kind": "single_choice",
                    "required": True,
                    "options": ["blue", "red"],
                }
            ],
            "expires_at": expires_at.isoformat(),
            "form_token": form_token,
        },
    )
    return event.model_copy(update={"digest": chatflow_business_event_digest(event)})


def _select_and_accept_continuation(
    engine: Engine,
    *,
    session_id: UUID,
    owner_id: UUID,
    service,
    callback: ChatflowBusinessEvent,
) -> UUID:
    accepted = service.create_selection(
        owner_user_id=owner_id,
        business_session_id=session_id,
        idempotency_key=f"h3-select-{uuid4().hex}",
        request=_selection_request(callback.recommendation_id),
    )
    with Session(engine) as db, db.begin():
        continuation = db.execute(
            select(Action).where(
                Action.business_session_id == session_id,
                Action.action_kind == "dify.chatflow.continue.v1",
            )
        ).scalar_one()
        action_input = prepare_chatflow_continuation(db, continuation)
        continuation_id = continuation.id

    adapter = StubDifyChatflowAdapter()
    result = execute_chatflow_continuation(adapter, action_input)
    assert result.outcome == "accepted"
    with Session(engine) as db, db.begin():
        continuation = db.get(Action, continuation_id)
        assert continuation is not None
        finalize_chatflow_continuation(db, continuation, action_input, result)
        continuation.status = "completed"
        continuation.last_outcome = "accepted"
        continuation.completed_at = func.clock_timestamp()
    return accepted.task_id


def _ready_for_questions(
    engine: Engine,
) -> tuple[
    object,
    UUID,
    UUID,
    BusinessSessionService,
    BusinessQuestionService,
    ChatflowBusinessEvent,
    UUID,
]:
    client, owner_id, _reference_set_id, session_id, service, callback = (
        _make_ready_business_session(engine)
    )
    client.app.state.business_question_service = service.question_service
    task_id = _select_and_accept_continuation(
        engine,
        session_id=session_id,
        owner_id=owner_id,
        service=service,
        callback=callback,
    )
    return client, owner_id, session_id, service, service.question_service, callback, task_id


def _post_human_input_event(client, event: ChatflowBusinessEvent):
    return client.post(
        "/internal/v1/dify/business-events",
        json=event.model_dump(mode="json"),
        headers={"Authorization": f"Bearer {TEST_INTERNAL_CALLBACK_SECRET}"},
    )


def test_question_answer_response_loss_recovers_once_on_same_form_and_run(
    h3_pg_engine: Engine,
) -> None:
    client, owner_id, session_id, service, _question_service, ready_event, task_id = (
        _ready_for_questions(h3_pg_engine)
    )
    with Session(h3_pg_engine) as db:
        now = db.scalar(select(func.clock_timestamp()))
    assert now is not None
    form_token = "h3-private-form-token"
    event = _human_input_event(
        ready_event,
        task_id=task_id,
        source_sequence=2,
        form_token=form_token,
        expires_at=now + timedelta(hours=1),
    )

    received = _post_human_input_event(client, event)
    assert received.status_code == 200
    assert received.json()["status"] == "accepted"
    replayed_event = _post_human_input_event(client, event)
    assert replayed_event.status_code == 200
    assert replayed_event.json()["status"] == "replayed"

    public = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public.status_code == 200
    question = public.json()["question"]
    assert question["can_answer"] is True
    assert "form_token" not in public.text
    question_id = UUID(question["question_id"])

    other_client, other_owner_id = _new_guest_from_h2_fixture(h3_pg_engine)
    other_client.app.state.business_question_service = (
        other_client.app.state.business_session_service.question_service
    )
    assert other_owner_id != owner_id
    denied = other_client.post(
        f"/api/v1/business-sessions/{session_id}/questions/{question_id}/answers",
        json={"answers": {"color": "blue"}},
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": "foreign-answer"},
    )
    assert denied.status_code == 404

    answers = {"answers": {"color": "blue"}}
    headers = {"Origin": TRUSTED_ORIGIN, "Idempotency-Key": "answer-once"}
    submitted = client.post(
        f"/api/v1/business-sessions/{session_id}/questions/{question_id}/answers",
        json=answers,
        headers=headers,
    )
    replayed = client.post(
        f"/api/v1/business-sessions/{session_id}/questions/{question_id}/answers",
        json=answers,
        headers=headers,
    )
    assert submitted.status_code == 202
    assert submitted.json()["status"] == "confirming"
    assert replayed.status_code == 202
    assert replayed.json()["answer_submission_id"] == submitted.json()["answer_submission_id"]
    conflicting = client.post(
        f"/api/v1/business-sessions/{session_id}/questions/{question_id}/answers",
        json={"answers": {"color": "red"}},
        headers=headers,
    )
    assert conflicting.status_code == 409

    answer_id = UUID(submitted.json()["answer_submission_id"])
    with Session(h3_pg_engine) as db, db.begin():
        answer_action = db.execute(
            select(Action).where(
                Action.business_answer_submission_id == answer_id,
                Action.action_kind == CHATFLOW_ANSWER_ACTION,
            )
        ).scalar_one()
        action_input = prepare_chatflow_answer(db, answer_action)
        answer_action_id = answer_action.id
    adapter = StubDifyChatflowAdapter(lose_answer_response_once=True)
    unknown = execute_chatflow_answer(adapter, action_input)
    assert unknown.outcome == "unknown"
    assert len(adapter.human_input_calls) == 1

    with Session(h3_pg_engine) as db, db.begin():
        answer_action = db.get(Action, answer_action_id)
        assert answer_action is not None
        finalize_chatflow_answer(db, answer_action, action_input, unknown)
        answer_action.status = "completed"
        answer_action.last_outcome = "unknown"
        answer_action.completed_at = func.clock_timestamp()
        recovery_action = db.execute(
            select(Action).where(
                Action.business_answer_submission_id == answer_id,
                Action.action_kind == CHATFLOW_ANSWER_RECOVERY_ACTION,
            )
        ).scalar_one()
        recovery_input = prepare_chatflow_answer(db, recovery_action)
        recovery_action_id = recovery_action.id

    recovered = recover_chatflow_answer(adapter, recovery_input)
    assert recovered.outcome == "accepted"
    assert len(adapter.human_input_calls) == 1
    assert len(adapter.human_input_recovery_calls) == 1
    assert adapter.human_input_recovery_calls[0].form_token == form_token
    assert adapter.human_input_recovery_calls[0].dify_run_id == ready_event.dify_run_id
    assert adapter.human_input_recovery_calls[0].answer_submission_id == answer_id

    with Session(h3_pg_engine) as db, db.begin():
        recovery_action = db.get(Action, recovery_action_id)
        assert recovery_action is not None
        finalize_chatflow_answer(db, recovery_action, recovery_input, recovered)
        recovery_action.status = "completed"
        recovery_action.last_outcome = "accepted"
        recovery_action.completed_at = func.clock_timestamp()
        answer = db.get(BusinessAnswerSubmission, answer_id)
        question_row = db.get(BusinessQuestion, question_id)
        assert answer is not None and answer.status == "accepted"
        assert question_row is not None and question_row.status == "answered"
        assert question_row.form_token == form_token
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.business_answer_submission_id == answer_id,
                    Action.action_kind == CHATFLOW_ANSWER_ACTION,
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.business_answer_submission_id == answer_id,
                    Action.action_kind == CHATFLOW_ANSWER_RECOVERY_ACTION,
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.task_id == task_id,
                    Action.action_kind.like("generation.%"),
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.event_type == TASK_PROMPT_EVENT_TYPE,
                    OutboxEvent.aggregate_id == task_id,
                )
            )
            == 0
        )

    final_replay = client.post(
        f"/api/v1/business-sessions/{session_id}/questions/{question_id}/answers",
        json=answers,
        headers=headers,
    )
    assert final_replay.status_code == 202
    assert final_replay.json()["status"] == "accepted"
    assert service.get_session(owner_user_id=owner_id, business_session_id=session_id).question

    second_question_event = _human_input_event(
        ready_event,
        task_id=task_id,
        source_sequence=3,
        round_number=2,
        form_token="second-round-token",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    second_question = _post_human_input_event(client, second_question_event)
    assert second_question.status_code == 200
    assert second_question.json()["status"] == "accepted"
    second_question_id = UUID(second_question.json()["question_id"])
    assert second_question_id != question_id
    second_public = client.get(f"/api/v1/business-sessions/{session_id}")
    assert second_public.json()["question"]["round_number"] == 2

    third_question_event = _human_input_event(
        ready_event,
        task_id=task_id,
        source_sequence=4,
        round_number=3,
        form_token="third-round-token",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    third_question = _post_human_input_event(client, third_question_event)
    assert third_question.status_code == 422
    assert "third-round-token" not in third_question.text
    assert client.get(f"/api/v1/business-sessions/{session_id}").json()["question"][
        "question_id"
    ] == str(second_question_id)


def test_missing_token_is_nonanswerable_and_can_be_bound_by_later_same_question_event(
    h3_pg_engine: Engine,
) -> None:
    client, _owner_id, session_id, _service, _question_service, callback, task_id = (
        _ready_for_questions(h3_pg_engine)
    )
    with Session(h3_pg_engine) as db:
        now = db.scalar(select(func.clock_timestamp()))
    assert now is not None
    missing = _human_input_event(
        callback,
        task_id=task_id,
        source_sequence=2,
        form_token=None,
        expires_at=now + timedelta(hours=1),
    )
    received = _post_human_input_event(client, missing)
    assert received.status_code == 200
    question_id = UUID(received.json()["question_id"])
    assert _post_human_input_event(client, missing).json()["status"] == "replayed"

    stale = missing.model_copy(
        update={"event_id": uuid4(), "source_sequence": 1, "digest": "0" * 64}
    )
    stale = stale.model_copy(update={"digest": chatflow_business_event_digest(stale)})
    assert _post_human_input_event(client, stale).json()["status"] == "ignored"
    public = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public.json()["question"]["status"] == "awaiting_form_binding"
    assert public.json()["question"]["can_answer"] is False

    answer = client.post(
        f"/api/v1/business-sessions/{session_id}/questions/{question_id}/answers",
        json={"answers": {"color": "blue"}},
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": "missing-token"},
    )
    assert answer.status_code == 409
    with Session(h3_pg_engine) as db:
        assert (
            db.scalar(
                select(func.count(BusinessAnswerSubmission.id)).where(
                    BusinessAnswerSubmission.business_question_id == question_id
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(Action.id)).where(Action.business_question_id == question_id)
            )
            == 0
        )

    bound = _human_input_event(
        callback,
        task_id=task_id,
        source_sequence=3,
        form_token="later-private-form-token",
        expires_at=now + timedelta(hours=1),
    )
    response = _post_human_input_event(client, bound)
    assert response.status_code == 200
    assert response.json()["question_id"] == str(question_id)
    bound_public = client.get(f"/api/v1/business-sessions/{session_id}")
    assert bound_public.json()["question"]["can_answer"] is True
    assert "later-private-form-token" not in bound_public.text
    with Session(h3_pg_engine) as db:
        row = db.get(BusinessQuestion, question_id)
        assert row is not None and row.status == "awaiting_answer"
        assert row.form_token == "later-private-form-token"
        assert (
            db.scalar(
                select(func.count(BusinessQuestionEvent.id)).where(
                    BusinessQuestionEvent.business_session_id == session_id
                )
            )
            == 3
        )


def test_expired_question_event_is_recorded_without_creating_answerable_question(
    h3_pg_engine: Engine,
) -> None:
    client, _owner_id, session_id, _service, _question_service, callback, task_id = (
        _ready_for_questions(h3_pg_engine)
    )
    with Session(h3_pg_engine) as db:
        now = db.scalar(select(func.clock_timestamp()))
    assert now is not None
    expired = _human_input_event(
        callback,
        task_id=task_id,
        source_sequence=2,
        form_token="expired-token",
        expires_at=now - timedelta(seconds=1),
    )

    response = _post_human_input_event(client, expired)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert client.get(f"/api/v1/business-sessions/{session_id}").json()["question"] is None
    with Session(h3_pg_engine) as db:
        assert (
            db.scalar(
                select(func.count(BusinessQuestionEvent.id)).where(
                    BusinessQuestionEvent.business_session_id == session_id,
                    BusinessQuestionEvent.disposition == "expired",
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(BusinessQuestion.id)).where(
                    BusinessQuestion.business_session_id == session_id
                )
            )
            == 0
        )


def test_expired_question_is_projected_expired_and_cannot_be_answered(
    h3_pg_engine: Engine,
) -> None:
    client, _owner_id, session_id, _service, _question_service, callback, task_id = (
        _ready_for_questions(h3_pg_engine)
    )
    with Session(h3_pg_engine) as db:
        now = db.scalar(select(func.clock_timestamp()))
    assert now is not None
    event = _human_input_event(
        callback,
        task_id=task_id,
        source_sequence=2,
        form_token="private-expired-question-token",
        expires_at=now + timedelta(hours=1),
    )
    received = _post_human_input_event(client, event)
    assert received.status_code == 200
    question_id = UUID(received.json()["question_id"])

    with Session(h3_pg_engine) as db, db.begin():
        question = db.get(BusinessQuestion, question_id)
        assert question is not None
        question.expires_at = now - timedelta(seconds=1)

    public = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public.status_code == 200
    assert public.json()["question"]["status"] == "expired"
    assert public.json()["question"]["can_answer"] is False
    assert "private-expired-question-token" not in public.text

    answer = client.post(
        f"/api/v1/business-sessions/{session_id}/questions/{question_id}/answers",
        json={"answers": {"color": "blue"}},
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": "expired-question"},
    )
    assert answer.status_code == 409
    with Session(h3_pg_engine) as db:
        persisted = db.get(BusinessQuestion, question_id)
        assert persisted is not None and persisted.status == "awaiting_answer"
        assert (
            db.scalar(
                select(func.count(BusinessAnswerSubmission.id)).where(
                    BusinessAnswerSubmission.business_question_id == question_id
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(Action.id)).where(Action.business_question_id == question_id)
            )
            == 0
        )


def test_event_for_a_different_dify_run_is_redacted_and_never_creates_question(
    h3_pg_engine: Engine,
) -> None:
    client, _owner_id, session_id, _service, _question_service, callback, task_id = (
        _ready_for_questions(h3_pg_engine)
    )
    with Session(h3_pg_engine) as db:
        now = db.scalar(select(func.clock_timestamp()))
    assert now is not None
    event = _human_input_event(
        callback,
        task_id=task_id,
        source_sequence=2,
        form_token="mismatched-run-token",
        expires_at=now + timedelta(hours=1),
    )
    event = event.model_copy(update={"dify_run_id": "unbound-workflow-run", "digest": "0" * 64})
    event = event.model_copy(update={"digest": chatflow_business_event_digest(event)})

    response = _post_human_input_event(client, event)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert client.get(f"/api/v1/business-sessions/{session_id}").json()["question"] is None
    with Session(h3_pg_engine) as db:
        receipt = db.execute(
            select(BusinessQuestionEvent).where(
                BusinessQuestionEvent.business_session_id == session_id
            )
        ).scalar_one()
        assert receipt.disposition == "reconciling"
        assert receipt.question_id is None
        assert receipt.task_id == task_id
        assert not hasattr(receipt, "payload")
        assert (
            db.scalar(
                select(func.count(BusinessQuestion.id)).where(
                    BusinessQuestion.business_session_id == session_id
                )
            )
            == 0
        )


def test_answer_action_persistence_rolls_back_with_database_failure(
    h3_pg_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, owner_id, session_id, _service, _question_service, callback, task_id = (
        _ready_for_questions(h3_pg_engine)
    )
    with Session(h3_pg_engine) as db:
        now = db.scalar(select(func.clock_timestamp()))
    assert now is not None
    event = _human_input_event(
        callback,
        task_id=task_id,
        source_sequence=2,
        form_token="rollback-form-token",
        expires_at=now + timedelta(hours=1),
    )
    received = _post_human_input_event(client, event)
    assert received.status_code == 200
    question_id = UUID(received.json()["question_id"])

    import app.services.business_questions as business_questions_module

    def fail_action_insert(*_args, **_kwargs):
        raise SQLAlchemyError("injected answer Action persistence failure")

    monkeypatch.setattr(
        business_questions_module, "schedule_action_in_transaction", fail_action_insert
    )
    response = client.post(
        f"/api/v1/business-sessions/{session_id}/questions/{question_id}/answers",
        json={"answers": {"color": "blue"}},
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": "answer-rollback"},
    )
    assert response.status_code == 503
    with Session(h3_pg_engine) as db:
        assert (
            db.scalar(
                select(func.count(BusinessAnswerSubmission.id)).where(
                    BusinessAnswerSubmission.business_question_id == question_id
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.business_question_id == question_id,
                    Action.action_kind == CHATFLOW_ANSWER_ACTION,
                )
            )
            == 0
        )
        question = db.get(BusinessQuestion, question_id)
        assert question is not None and question.status == "awaiting_answer"


def test_terminal_question_children_do_not_block_task_cascade_after_actions_are_removed(
    h3_pg_engine: Engine,
) -> None:
    client, _owner_id, session_id, _service, _question_service, callback, task_id = (
        _ready_for_questions(h3_pg_engine)
    )
    with Session(h3_pg_engine) as db:
        now = db.scalar(select(func.clock_timestamp()))
    assert now is not None
    event = _human_input_event(
        callback,
        task_id=task_id,
        source_sequence=2,
        form_token="terminal-form-token",
        expires_at=now + timedelta(hours=1),
    )
    accepted_event = _post_human_input_event(client, event)
    assert accepted_event.status_code == 200
    question_id = UUID(accepted_event.json()["question_id"])
    answer_response = client.post(
        f"/api/v1/business-sessions/{session_id}/questions/{question_id}/answers",
        json={"answers": {"color": "blue"}},
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": "terminal-answer"},
    )
    assert answer_response.status_code == 202
    answer_id = UUID(answer_response.json()["answer_submission_id"])

    with Session(h3_pg_engine) as db, db.begin():
        persisted_task = db.get(Task, task_id)
        question = db.get(BusinessQuestion, question_id)
        answer = db.get(BusinessAnswerSubmission, answer_id)
        assert persisted_task is not None and question is not None and answer is not None
        persisted_task.status = "failed"
        persisted_task.terminal_at = now - timedelta(days=2)
        persisted_task.expires_at = now - timedelta(days=1)
        for item in db.execute(
            select(TaskItem).where(TaskItem.task_id == task_id).with_for_update()
        ).scalars():
            item.status = "failed"
        question.status = "answered"
        answer.status = "accepted"
        for action in db.execute(
            select(Action).where(Action.task_id == task_id).with_for_update()
        ).scalars():
            action.status = "completed"
            action.last_outcome = "accepted"
            action.completed_at = now
        db.flush()
        db.execute(delete(Action).where(Action.task_id == task_id))
        db.execute(delete(Task).where(Task.id == task_id))

    with Session(h3_pg_engine) as db:
        assert db.get(Task, task_id) is None
        assert db.get(BusinessQuestion, question_id) is None
        assert db.get(BusinessAnswerSubmission, answer_id) is None
        assert (
            db.scalar(
                select(func.count(BusinessQuestionEvent.id)).where(
                    BusinessQuestionEvent.business_session_id == session_id
                )
            )
            == 0
        )


def _new_guest_from_h2_fixture(engine: Engine):
    from tests.integration.business_sessions.test_business_sessions_postgres import _new_guest

    return _new_guest(engine)
