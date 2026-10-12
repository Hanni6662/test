from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.dispatcher import (
    OutboxDispatcher,
    _build_chatflow_generation_action_registrations,
)
from app.integrations.storage.local import LocalPrivateStorage
from app.services.chatflow_actions import CHATFLOW_CANCEL_RECOVERY_ACTION
from app.services.generation_runtime import GENERATION_ACTION_KINDS
from app.services.prompt_workflows import (
    GENERATION_START_EVENT_TYPE,
    PROMPT_FOLLOWUP_RECOVER_ACTION_KIND,
    PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND,
    PROMPT_INITIAL_RECOVER_ACTION_KIND,
    PROMPT_INITIAL_SUBMIT_ACTION_KIND,
    PROMPT_MOCK_RESULT_QUERY_ACTION_KIND,
)
from app.services.tasks import TASK_PROMPT_EVENT_TYPE
from app.workers.action_processor import (
    ACTION_EVENT_TYPE,
    ACTION_TASK_NAME,
    ActionExecutionResult,
    ActionRegistration,
)
from app.workers.celery_app import TASK_NAME, create_celery_base_app
from app.workers.event_allowlist import DEFAULT_PUBLISH_ALLOWLIST, UNSUPPORTED_EVENT_REASON
from app.workers.messages import ClaimedEvent
from app.workers.options import QueueOptions
from app.workers.processor import HandlerRegistration
from app.workers.publisher import CeleryTaskPublisher
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql


def _options(**changes: object) -> QueueOptions:
    values: dict[str, object] = {"broker_url": "amqp://probe:secret@localhost:5672//"}
    values.update(changes)
    return QueueOptions(**values)


def test_default_publish_allowlist_is_explicit_and_leaves_task_intents_pending() -> None:
    assert "demo.persisted.v1" in DEFAULT_PUBLISH_ALLOWLIST
    assert "recommendation.analyze.v1" in DEFAULT_PUBLISH_ALLOWLIST
    assert "task.prompt.v1" not in DEFAULT_PUBLISH_ALLOWLIST
    assert ACTION_EVENT_TYPE not in DEFAULT_PUBLISH_ALLOWLIST
    assert UNSUPPORTED_EVENT_REASON == "EVENT_TYPE_NOT_ALLOWLISTED"


def test_action_event_requires_a_registered_action_kind_in_dispatcher() -> None:
    engine = create_engine("sqlite://")
    try:
        no_handlers = OutboxDispatcher(engine, _options())
        assert ACTION_EVENT_TYPE not in no_handlers._allowed_event_types

        registered = OutboxDispatcher(
            engine,
            _options(),
            allowed_event_types=frozenset({ACTION_EVENT_TYPE}),
            action_registrations={
                "generation.mock": ActionRegistration(
                    "generation.mock",
                    lambda _db, _action: None,
                    lambda _input: ActionExecutionResult("accepted"),
                    lambda _db, _action, _input, _result: None,
                )
            },
        )
        assert ACTION_EVENT_TYPE in registered._allowed_event_types
        sql = str(
            registered._publishable_event_predicate().compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        assert "task_actions.action_kind = 'generation.mock'" in sql
        assert "task_actions.scope_kind IN ('attempt')" in sql
    finally:
        engine.dispose()


def test_chatflow_dispatcher_registers_generation_recovery_for_original_request() -> None:
    engine = create_engine("sqlite://")
    try:
        registrations = _build_chatflow_generation_action_registrations(
            type(
                "Settings",
                (),
                {
                    "image_generation_service_url": None,
                    "image_generation_service_token": None,
                },
            )(),
            LocalPrivateStorage("/tmp/v8-dispatcher-registration-test"),
        )
        assert set(registrations) == {
            "chatflow.generation.recover.v1",
            "chatflow.generation.expiry_assess.v1",
            "chatflow.generation.accepted_batch_recover.v1",
            "chatflow.generation.delivery.download.v1",
            "chatflow.generation.delivery.confirm.v1",
        }
        assert registrations["chatflow.generation.recover.v1"].scope_kinds == frozenset(
            {"generation_request"}
        )
        assert registrations["chatflow.generation.delivery.download.v1"].scope_kinds == frozenset(
            {"generation_delivery"}
        )
        assert registrations["chatflow.generation.expiry_assess.v1"].scope_kinds == frozenset(
            {"generation_request"}
        )
        assert registrations[
            "chatflow.generation.accepted_batch_recover.v1"
        ].scope_kinds == frozenset({"generation_request"})

        dispatcher = OutboxDispatcher(
            engine,
            _options(),
            allowed_event_types=frozenset({ACTION_EVENT_TYPE}),
            action_registrations=registrations,
        )
        assert ACTION_EVENT_TYPE in dispatcher._allowed_event_types
        assert set(registrations).issubset(dispatcher._action_registrations)
    finally:
        engine.dispose()


def _cancel_recovery_dispatcher(engine) -> OutboxDispatcher:
    registration = ActionRegistration(
        CHATFLOW_CANCEL_RECOVERY_ACTION,
        lambda _db, _action: None,
        lambda _input: ActionExecutionResult("accepted"),
        lambda _db, _action, _input, _result: None,
        scope_kinds=frozenset({"business_session"}),
    )
    return OutboxDispatcher(
        engine,
        _options(),
        allowed_event_types=frozenset({ACTION_EVENT_TYPE}),
        action_registrations={CHATFLOW_CANCEL_RECOVERY_ACTION: registration},
    )


def _cancel_recovery_event_is_publishable(
    *, kind: str, scope: str, sequence: int, status: str
) -> bool:
    engine = create_engine("sqlite://")
    dispatcher = _cancel_recovery_dispatcher(engine)
    event_id, action_id = uuid4(), uuid4()
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE outbox_events (id CHAR(32), event_type TEXT)")
            connection.exec_driver_sql(
                "CREATE TABLE task_actions (id CHAR(32), outbox_event_id CHAR(32), "
                "action_kind TEXT, scope_kind TEXT, sequence INTEGER, status TEXT)"
            )
            connection.exec_driver_sql(
                "INSERT INTO outbox_events (id, event_type) VALUES (?, ?)",
                (event_id.hex, ACTION_EVENT_TYPE),
            )
            connection.exec_driver_sql(
                "INSERT INTO task_actions "
                "(id, outbox_event_id, action_kind, scope_kind, sequence, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (action_id.hex, event_id.hex, kind, scope, sequence, status),
            )
            return (
                connection.execute(
                    select(OutboxEvent.id).where(dispatcher._publishable_event_predicate())
                ).scalar_one_or_none()
                is not None
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("sequence", [16, 17, 18])
def test_cancelled_registered_business_session_recovery_action_can_wake_noop(
    sequence: int,
) -> None:
    assert _cancel_recovery_event_is_publishable(
        kind=CHATFLOW_CANCEL_RECOVERY_ACTION,
        scope="business_session",
        sequence=sequence,
        status="cancelled",
    )


@pytest.mark.parametrize(
    ("kind", "scope", "sequence", "status"),
    [
        ("unregistered.action", "business_session", 16, "cancelled"),
        (CHATFLOW_CANCEL_RECOVERY_ACTION, "submission", 16, "cancelled"),
        (CHATFLOW_CANCEL_RECOVERY_ACTION, "business_session", 15, "cancelled"),
        (CHATFLOW_CANCEL_RECOVERY_ACTION, "business_session", 19, "cancelled"),
        (CHATFLOW_CANCEL_RECOVERY_ACTION, "business_session", 16, "completed"),
        (CHATFLOW_CANCEL_RECOVERY_ACTION, "business_session", 16, "rejected"),
        (CHATFLOW_CANCEL_RECOVERY_ACTION, "business_session", 16, "unknown"),
    ],
)
def test_cancelled_recovery_wake_outside_registered_boundary_stays_quarantined(
    kind: str, scope: str, sequence: int, status: str
) -> None:
    assert not _cancel_recovery_event_is_publishable(
        kind=kind, scope=scope, sequence=sequence, status=status
    )


def test_requeued_expired_processing_receipt_with_prior_attempt_is_not_republished() -> None:
    now = datetime.now(UTC)
    event_id, action_id = uuid4(), uuid4()
    event = OutboxEvent(
        id=event_id,
        event_type=ACTION_EVENT_TYPE,
        aggregate_id=action_id,
        operation_version=1,
        schema_version=1,
        payload={"action_id": str(action_id)},
        status="pending",
        available_at=now,
        attempt_count=1,
        created_at=now,
    )
    receipt = OutboxReceipt(
        event_id=event_id,
        status="pending",
        available_at=now,
        attempt_count=1,
    )

    class _Result:
        def __init__(self, value: object) -> None:
            self.value = value

        def scalars(self):
            return iter([self.value])

        def scalar_one_or_none(self):
            return self.value

    class _Session:
        def __init__(self) -> None:
            self._execute_count = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def begin(self):
            return nullcontext()

        def execute(self, _statement):
            self._execute_count += 1
            return _Result(event if self._execute_count == 1 else receipt)

    engine = create_engine("sqlite://")
    dispatcher = _cancel_recovery_dispatcher(engine)
    dispatcher._sessions = _Session  # type: ignore[method-assign]
    try:
        assert dispatcher._claim_due_events() == []
        assert event.status == "published"
        assert event.attempt_count == 1
        assert receipt.status == "pending"
        assert receipt.attempt_count == 1
    finally:
        engine.dispose()


def test_task_prompt_release_requires_handler_and_both_submission_actions() -> None:
    engine = create_engine("sqlite://")
    prompt_handler = HandlerRegistration(lambda _db, _event, _message: None)

    def registration(kind: str, scope: str = "submission") -> ActionRegistration:
        return ActionRegistration(
            kind,
            lambda _db, _action: None,
            lambda _input: ActionExecutionResult("accepted"),
            lambda _db, _action, _input, _result: None,
            scope_kinds=frozenset({scope}),
        )

    complete_actions = {
        PROMPT_INITIAL_SUBMIT_ACTION_KIND: registration(PROMPT_INITIAL_SUBMIT_ACTION_KIND),
        PROMPT_INITIAL_RECOVER_ACTION_KIND: registration(PROMPT_INITIAL_RECOVER_ACTION_KIND),
        PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND: registration(PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND),
        PROMPT_FOLLOWUP_RECOVER_ACTION_KIND: registration(PROMPT_FOLLOWUP_RECOVER_ACTION_KIND),
    }
    try:
        no_handler = OutboxDispatcher(
            engine,
            _options(),
            action_registrations=complete_actions,
        )
        missing_recover = OutboxDispatcher(
            engine,
            _options(),
            action_registrations={
                PROMPT_INITIAL_SUBMIT_ACTION_KIND: complete_actions[
                    PROMPT_INITIAL_SUBMIT_ACTION_KIND
                ]
            },
            event_handlers={TASK_PROMPT_EVENT_TYPE: prompt_handler},
        )
        missing_followup_recover = OutboxDispatcher(
            engine,
            _options(),
            action_registrations={
                kind: registration(kind)
                for kind in complete_actions
                if kind != PROMPT_FOLLOWUP_RECOVER_ACTION_KIND
            },
            event_handlers={TASK_PROMPT_EVENT_TYPE: prompt_handler},
        )
        missing_followup_submit = OutboxDispatcher(
            engine,
            _options(),
            action_registrations={
                kind: registration(kind)
                for kind in complete_actions
                if kind != PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND
            },
            event_handlers={TASK_PROMPT_EVENT_TYPE: prompt_handler},
        )
        wrong_scope = OutboxDispatcher(
            engine,
            _options(),
            action_registrations={
                **complete_actions,
                PROMPT_INITIAL_SUBMIT_ACTION_KIND: registration(
                    PROMPT_INITIAL_SUBMIT_ACTION_KIND, scope="attempt"
                ),
            },
            event_handlers={TASK_PROMPT_EVENT_TYPE: prompt_handler},
        )
        wrong_followup_scope = OutboxDispatcher(
            engine,
            _options(),
            action_registrations={
                **complete_actions,
                PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND: registration(
                    PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND, scope="attempt"
                ),
            },
            event_handlers={TASK_PROMPT_EVENT_TYPE: prompt_handler},
        )
        complete = OutboxDispatcher(
            engine,
            _options(),
            allowed_event_types=frozenset({TASK_PROMPT_EVENT_TYPE, GENERATION_START_EVENT_TYPE}),
            action_registrations=complete_actions,
            event_handlers={TASK_PROMPT_EVENT_TYPE: prompt_handler},
        )
        complete_default = OutboxDispatcher(
            engine,
            _options(),
            action_registrations=complete_actions,
            event_handlers={TASK_PROMPT_EVENT_TYPE: prompt_handler},
        )
        mock_query_required_missing = OutboxDispatcher(
            engine,
            _options(),
            action_registrations=complete_actions,
            event_handlers={TASK_PROMPT_EVENT_TYPE: prompt_handler},
            mock_prompt_result_query_required=True,
        )
        mock_query_registration = registration(PROMPT_MOCK_RESULT_QUERY_ACTION_KIND)
        mock_query_complete = OutboxDispatcher(
            engine,
            _options(),
            action_registrations={
                **complete_actions,
                PROMPT_MOCK_RESULT_QUERY_ACTION_KIND: mock_query_registration,
            },
            event_handlers={TASK_PROMPT_EVENT_TYPE: prompt_handler},
            mock_prompt_result_query_required=True,
        )

        assert TASK_PROMPT_EVENT_TYPE not in no_handler._allowed_event_types
        assert TASK_PROMPT_EVENT_TYPE not in missing_recover._allowed_event_types
        assert TASK_PROMPT_EVENT_TYPE not in missing_followup_recover._allowed_event_types
        assert TASK_PROMPT_EVENT_TYPE not in missing_followup_submit._allowed_event_types
        assert TASK_PROMPT_EVENT_TYPE not in wrong_scope._allowed_event_types
        assert TASK_PROMPT_EVENT_TYPE not in wrong_followup_scope._allowed_event_types
        assert TASK_PROMPT_EVENT_TYPE in complete._allowed_event_types
        assert TASK_PROMPT_EVENT_TYPE in complete_default._allowed_event_types
        assert TASK_PROMPT_EVENT_TYPE not in mock_query_required_missing._allowed_event_types
        assert TASK_PROMPT_EVENT_TYPE in mock_query_complete._allowed_event_types
        assert GENERATION_START_EVENT_TYPE not in complete._allowed_event_types
        compiled = str(
            complete._publishable_event_predicate().compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        assert "outbox_events.event_type IN ('task.prompt.v1')" in compiled
    finally:
        engine.dispose()


def test_generation_release_requires_enabled_complete_five_action_runtime() -> None:
    engine = create_engine("sqlite://")
    generation_handler = HandlerRegistration(lambda _db, _event, _message: None)

    def registration(kind: str, scope: str = "attempt") -> ActionRegistration:
        return ActionRegistration(
            kind,
            lambda _db, _action: None,
            lambda _input: ActionExecutionResult("accepted"),
            lambda _db, _action, _input, _result: None,
            scope_kinds=frozenset({scope}),
        )

    complete = {kind: registration(kind) for kind in GENERATION_ACTION_KINDS}
    try:
        default_closed = OutboxDispatcher(
            engine,
            _options(),
            allowed_event_types=frozenset({GENERATION_START_EVENT_TYPE, ACTION_EVENT_TYPE}),
            action_registrations=complete,
            event_handlers={GENERATION_START_EVENT_TYPE: generation_handler},
        )
        missing_save = OutboxDispatcher(
            engine,
            _options(),
            allowed_event_types=frozenset({GENERATION_START_EVENT_TYPE, ACTION_EVENT_TYPE}),
            action_registrations={
                kind: hook for kind, hook in complete.items() if kind != "generation.save.v1"
            },
            event_handlers={GENERATION_START_EVENT_TYPE: generation_handler},
            generation_runtime_enabled=True,
        )
        wrong_scope = OutboxDispatcher(
            engine,
            _options(),
            allowed_event_types=frozenset({GENERATION_START_EVENT_TYPE, ACTION_EVENT_TYPE}),
            action_registrations={
                **complete,
                "generation.download.v1": registration(
                    "generation.download.v1", scope="submission"
                ),
            },
            event_handlers={GENERATION_START_EVENT_TYPE: generation_handler},
            generation_runtime_enabled=True,
        )
        enabled = OutboxDispatcher(
            engine,
            _options(),
            allowed_event_types=frozenset({GENERATION_START_EVENT_TYPE, ACTION_EVENT_TYPE}),
            action_registrations=complete,
            event_handlers={GENERATION_START_EVENT_TYPE: generation_handler},
            generation_runtime_enabled=True,
        )

        assert GENERATION_START_EVENT_TYPE not in default_closed._allowed_event_types
        assert ACTION_EVENT_TYPE not in default_closed._allowed_event_types
        assert GENERATION_START_EVENT_TYPE not in missing_save._allowed_event_types
        assert GENERATION_START_EVENT_TYPE not in wrong_scope._allowed_event_types
        assert GENERATION_START_EVENT_TYPE in enabled._allowed_event_types
        assert set(GENERATION_ACTION_KINDS).issubset(enabled._action_registrations)
    finally:
        engine.dispose()


def test_task_api_gate_requires_the_complete_prompt_and_generation_pipeline() -> None:
    engine = create_engine("sqlite://")
    prompt_handler = HandlerRegistration(lambda _db, _event, _message: None)
    generation_handler = HandlerRegistration(lambda _db, _event, _message: None)

    def registration(kind: str, scope: str) -> ActionRegistration:
        return ActionRegistration(
            kind,
            lambda _db, _action: None,
            lambda _input: ActionExecutionResult("accepted"),
            lambda _db, _action, _input, _result: None,
            scope_kinds=frozenset({scope}),
        )

    prompt_actions = {
        kind: registration(kind, "submission")
        for kind in (
            PROMPT_INITIAL_SUBMIT_ACTION_KIND,
            PROMPT_INITIAL_RECOVER_ACTION_KIND,
            PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND,
            PROMPT_FOLLOWUP_RECOVER_ACTION_KIND,
        )
    }
    generation_actions = {kind: registration(kind, "attempt") for kind in GENERATION_ACTION_KINDS}
    enabled_events = frozenset(
        {TASK_PROMPT_EVENT_TYPE, GENERATION_START_EVENT_TYPE, ACTION_EVENT_TYPE}
    )
    handlers = {
        TASK_PROMPT_EVENT_TYPE: prompt_handler,
        GENERATION_START_EVENT_TYPE: generation_handler,
    }
    try:
        complete = OutboxDispatcher(
            engine,
            _options(),
            allowed_event_types=enabled_events,
            action_registrations={**prompt_actions, **generation_actions},
            event_handlers=handlers,
            generation_runtime_enabled=True,
            tasks_api_enabled=True,
        )
        missing_generation_save = OutboxDispatcher(
            engine,
            _options(),
            allowed_event_types=enabled_events,
            action_registrations={
                **prompt_actions,
                **{
                    kind: registration(kind, "attempt")
                    for kind in GENERATION_ACTION_KINDS
                    if kind != "generation.save.v1"
                },
            },
            event_handlers=handlers,
            generation_runtime_enabled=True,
            tasks_api_enabled=True,
        )
        gate_off = OutboxDispatcher(
            engine,
            _options(),
            allowed_event_types=enabled_events,
            action_registrations={**prompt_actions, **generation_actions},
            event_handlers=handlers,
            generation_runtime_enabled=False,
            tasks_api_enabled=True,
        )

        assert TASK_PROMPT_EVENT_TYPE in complete._allowed_event_types
        assert GENERATION_START_EVENT_TYPE in complete._allowed_event_types
        assert TASK_PROMPT_EVENT_TYPE not in missing_generation_save._allowed_event_types
        assert GENERATION_START_EVENT_TYPE not in missing_generation_save._allowed_event_types
        assert TASK_PROMPT_EVENT_TYPE not in gate_off._allowed_event_types
        assert GENERATION_START_EVENT_TYPE not in gate_off._allowed_event_types
    finally:
        engine.dispose()


def test_action_task_is_registered_on_the_durable_queue() -> None:
    app = create_celery_base_app(_options())

    assert app.conf.task_routes[TASK_NAME] == {"queue": "image-backend-events"}
    assert app.conf.task_routes[ACTION_TASK_NAME] == {"queue": "image-backend-events"}


def test_action_event_uses_the_separate_action_task_without_private_payload(monkeypatch) -> None:
    import app.workers.publisher as publisher_module

    class FakeChannel:
        def close(self):
            return None

    class FakeConnection:
        def __init__(self, _url, **_kwargs):
            self.fake_channel = FakeChannel()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def channel(self):
            return self.fake_channel

    class FakeProducer:
        def __init__(self, _channel, on_return=None):
            self.on_return = on_return

    class FakeAmqp:
        def __init__(self):
            self.message = None
            self.sent = None

        def create_task_message(self, **kwargs):
            self.message = kwargs
            return kwargs

        def send_task_message(self, _producer, name, envelope, **kwargs):
            self.sent = (name, envelope, kwargs)

    class FakeApp:
        def __init__(self):
            self.amqp = FakeAmqp()

    monkeypatch.setattr(publisher_module, "Producer", FakeProducer)
    app = FakeApp()
    publisher = CeleryTaskPublisher(_options(), app=app, connection_factory=FakeConnection)
    event_id = uuid4()
    action_id = uuid4()
    publisher.publish(
        ClaimedEvent(
            event_id=event_id,
            event_type=ACTION_EVENT_TYPE,
            aggregate_id=action_id,
            operation_version=1,
            schema_version=1,
            payload={"action_id": str(action_id)},
            claim_token=uuid4(),
            attempt_count=1,
        )
    )

    task_name, envelope, _kwargs = app.amqp.sent
    assert task_name == ACTION_TASK_NAME
    assert envelope["kwargs"]["payload"] == {
        "event_id": str(event_id),
        "aggregate_id": str(action_id),
        "operation_version": 1,
        "schema_version": 1,
        "payload": {"action_id": str(action_id)},
    }
    assert "prompt" not in envelope["kwargs"]["payload"]
