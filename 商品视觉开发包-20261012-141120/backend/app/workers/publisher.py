from __future__ import annotations

from collections.abc import Callable

from celery import Celery
from kombu import Connection, Producer

from app.workers.action_processor import ACTION_EVENT_TYPE, ACTION_TASK_NAME
from app.workers.celery_app import TASK_NAME, create_celery_base_app, durable_queue
from app.workers.messages import ClaimedEvent, OutboxMessage
from app.workers.options import QueueOptions


class UnroutableMessageError(RuntimeError):
    """Broker returned a mandatory publish instead of routing it to the fixed queue."""


ConnectionFactory = Callable[..., Connection]


class CeleryTaskPublisher:
    """Publish one durable Celery envelope and wait for RabbitMQ confirmation."""

    def __init__(
        self,
        options: QueueOptions,
        *,
        app: Celery | None = None,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self._options = options
        self._app = app or create_celery_base_app(options)
        self._connection_factory = connection_factory or Connection
        self._queue = durable_queue(options)

    def publish(self, event: ClaimedEvent) -> None:
        task_name = ACTION_TASK_NAME if event.event_type == ACTION_EVENT_TYPE else TASK_NAME
        message = OutboxMessage(
            event_id=event.event_id,
            aggregate_id=event.aggregate_id,
            operation_version=event.operation_version,
            schema_version=event.schema_version,
            payload=event.payload,
        )
        envelope = self._app.amqp.create_task_message(
            task_id=str(event.event_id),
            name=task_name,
            args=(),
            kwargs={"payload": message.model_dump(mode="json")},
            ignore_result=True,
        )
        returned: list[bool] = []

        def on_return(*_args: object, **_kwargs: object) -> None:
            returned.append(True)

        connection = self._connection_factory(
            self._options.broker_url,
            connect_timeout=self._options.confirm_timeout_seconds,
            heartbeat=30,
            transport_options={"confirm_publish": True},
        )
        with connection:
            channel = connection.channel()
            try:
                producer = Producer(channel, on_return=on_return)
                self._app.amqp.send_task_message(
                    producer,
                    task_name,
                    envelope,
                    queue=self._queue,
                    serializer="json",
                    delivery_mode=2,
                    mandatory=True,
                    confirm_timeout=self._options.confirm_timeout_seconds,
                    retry=False,
                )
                if returned:
                    raise UnroutableMessageError("broker did not route the event")
            finally:
                channel.close()
