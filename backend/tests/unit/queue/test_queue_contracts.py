from __future__ import annotations

from uuid import uuid4

import pytest
from app.workers.celery_app import TASK_NAME, create_celery_base_app
from app.workers.messages import ClaimedEvent, OutboxMessage
from app.workers.options import QueueOptions
from app.workers.publisher import CeleryTaskPublisher, UnroutableMessageError
from pydantic import ValidationError


def _options(**changes: object) -> QueueOptions:
    values: dict[str, object] = {"broker_url": "amqp://probe:secret@localhost:5672//"}
    values.update(changes)
    return QueueOptions(**values)


def test_queue_options_keep_broker_secret_out_of_repr_and_cap_retry_delay() -> None:
    options = _options(retry_base_seconds=3, retry_max_seconds=20)

    assert "secret" not in repr(options)
    assert [options.retry_delay_seconds(value) for value in (1, 2, 3, 10)] == [3, 6, 12, 20]
    with pytest.raises(ValueError):
        options.retry_delay_seconds(0)


@pytest.mark.parametrize(
    "changes",
    [
        {"broker_url": "https://rabbit.invalid"},
        {"queue_name": "../../private"},
        {"claim_batch_size": 0},
        {"lease_seconds": 1},
        {"poll_interval_seconds": 0},
        {"republish_after_seconds": 1},
    ],
)
def test_queue_options_reject_invalid_transport_and_recovery_bounds(changes) -> None:
    with pytest.raises(ValueError):
        _options(**changes)


def test_message_contains_only_entity_ids_versions_and_internal_id_payload() -> None:
    event_id, aggregate_id, asset_id = uuid4(), uuid4(), uuid4()
    message = OutboxMessage(
        event_id=event_id,
        aggregate_id=aggregate_id,
        operation_version=2,
        schema_version=1,
        payload={"reference_set_id": asset_id},
    )
    serialized = message.model_dump(mode="json")

    assert serialized == {
        "event_id": str(event_id),
        "aggregate_id": str(aggregate_id),
        "operation_version": 2,
        "schema_version": 1,
        "payload": {"reference_set_id": str(asset_id)},
    }
    with pytest.raises(ValidationError):
        OutboxMessage.model_validate({**serialized, "prompt": "must never be queued"})
    with pytest.raises(ValidationError):
        OutboxMessage.model_validate({**serialized, "payload": {"prompt": "not an id"}})


def test_worker_configuration_uses_durable_prefetch_one_late_ack_and_no_eager() -> None:
    app = create_celery_base_app(_options())
    queue = app.conf.task_queues[0]

    assert app.conf.task_always_eager is False
    assert app.conf.result_backend is None
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert queue.durable is True
    assert queue.exchange.durable is True
    assert app.conf.task_routes[TASK_NAME] == {"queue": queue.name}


def test_publisher_requires_mandatory_confirm_and_only_serializes_safe_fields(monkeypatch) -> None:
    import app.workers.publisher as publisher_module

    class FakeChannel:
        closed = False

        def close(self):
            self.closed = True

    class FakeConnection:
        def __init__(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            self.fake_channel = FakeChannel()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def channel(self):
            return self.fake_channel

    class FakeProducer:
        def __init__(self, channel, on_return=None):
            self.channel = channel
            self.on_return = on_return

    class FakeAmqp:
        def __init__(self):
            self.envelope = None
            self.publish_args = None
            self.return_message = False

        def create_task_message(self, **kwargs):
            self.envelope = kwargs
            return kwargs

        def send_task_message(self, producer, name, envelope, **kwargs):
            self.publish_args = (name, envelope, kwargs)
            if self.return_message:
                producer.on_return("returned", "exchange", "routing", "message")

    class FakeApp:
        def __init__(self):
            self.amqp = FakeAmqp()

    captured_connections = []

    def connection_factory(url, **kwargs):
        connection = FakeConnection(url, **kwargs)
        captured_connections.append(connection)
        return connection

    monkeypatch.setattr(publisher_module, "Producer", FakeProducer)
    app = FakeApp()
    event = ClaimedEvent(
        event_id=uuid4(),
        event_type="demo.persisted.v1",
        aggregate_id=uuid4(),
        operation_version=1,
        schema_version=1,
        payload={"reference_set_id": str(uuid4())},
        claim_token=uuid4(),
        attempt_count=1,
    )
    publisher = CeleryTaskPublisher(_options(), app=app, connection_factory=connection_factory)

    publisher.publish(event)

    task_name, envelope, kwargs = app.amqp.publish_args
    assert task_name == TASK_NAME
    assert envelope["task_id"] == str(event.event_id)
    assert set(envelope["kwargs"]["payload"]) == {
        "event_id",
        "aggregate_id",
        "operation_version",
        "schema_version",
        "payload",
    }
    assert kwargs["mandatory"] is True
    assert kwargs["confirm_timeout"] == 10
    assert kwargs["delivery_mode"] == 2
    assert kwargs["retry"] is False
    assert captured_connections[0].kwargs["transport_options"] == {"confirm_publish": True}
    assert captured_connections[0].fake_channel.closed is True

    app.amqp.return_message = True
    with pytest.raises(UnroutableMessageError):
        publisher.publish(event)
