from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any

from celery import Celery
from celery.signals import worker_process_shutdown
from kombu import Exchange, Queue
from sqlalchemy import Engine

from app.workers.action_processor import ACTION_TASK_NAME, ActionProcessor, ActionRegistration
from app.workers.options import QueueOptions
from app.workers.processor import EventHandler, EventProcessor, HandlerRegistration

TASK_NAME = "app.workers.process_outbox_event"

EngineFactory = Callable[[], Engine]
RuntimeRegistrationsFactory = Callable[
    [Engine],
    tuple[
        Mapping[str, HandlerRegistration | EventHandler],
        Mapping[str, ActionRegistration],
    ],
]


def create_celery_base_app(options: QueueOptions) -> Celery:
    """Build the durable JSON-only queue configuration without opening connections."""
    exchange = Exchange(options.queue_name, type="direct", durable=True, auto_delete=False)
    queue = Queue(
        options.queue_name,
        exchange=exchange,
        routing_key=options.queue_name,
        durable=True,
        exclusive=False,
        auto_delete=False,
    )
    app = Celery("image_backend", broker=options.broker_url, backend=None)
    app.conf.update(
        accept_content=["json"],
        task_serializer="json",
        result_serializer="json",
        task_ignore_result=True,
        task_store_errors_even_if_ignored=False,
        task_always_eager=False,
        task_create_missing_queues=False,
        task_default_queue=options.queue_name,
        task_default_exchange=options.queue_name,
        task_default_exchange_type="direct",
        task_default_routing_key=options.queue_name,
        task_default_delivery_mode=2,
        task_queues=(queue,),
        task_routes={
            TASK_NAME: {"queue": options.queue_name},
            ACTION_TASK_NAME: {"queue": options.queue_name},
        },
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_acks_on_failure_or_timeout=True,
        worker_prefetch_multiplier=1,
        worker_enable_remote_control=False,
        broker_connection_retry_on_startup=True,
        broker_transport_options={"confirm_publish": True},
        task_send_sent_event=False,
    )
    return app


def create_celery_app(
    options: QueueOptions,
    *,
    engine_factory: EngineFactory,
    handlers: Mapping[str, HandlerRegistration | EventHandler] | None = None,
    action_handlers: Mapping[str, ActionRegistration] | None = None,
    runtime_registrations_factory: RuntimeRegistrationsFactory | None = None,
) -> Celery:
    """Register a worker task whose SQLAlchemy pool is created lazily per worker PID."""
    from app.workers.demo import demo_handlers

    resolved_handlers = dict(demo_handlers())
    if handlers:
        resolved_handlers.update(handlers)

    app = create_celery_base_app(options)
    runtime_lock = Lock()
    resolved_action_handlers = dict(action_handlers or {})
    runtime: dict[str, Any] = {
        "pid": None,
        "engine": None,
        "processor": None,
        "action_processor": None,
    }

    def get_processors() -> tuple[EventProcessor, ActionProcessor]:
        current_pid = os.getpid()
        with runtime_lock:
            if runtime["pid"] != current_pid:
                previous_engine = runtime["engine"]
                if previous_engine is not None:
                    previous_engine.dispose()
                engine = engine_factory()
                runtime_handlers: Mapping[str, HandlerRegistration | EventHandler] = {}
                runtime_action_handlers: Mapping[str, ActionRegistration] = {}
                if runtime_registrations_factory is not None:
                    runtime_handlers, runtime_action_handlers = runtime_registrations_factory(
                        engine
                    )
                process_handlers = dict(resolved_handlers)
                process_handlers.update(runtime_handlers)
                process_action_handlers = dict(resolved_action_handlers)
                process_action_handlers.update(runtime_action_handlers)
                processor = EventProcessor(engine, options, process_handlers)
                runtime.update(
                    pid=current_pid,
                    engine=engine,
                    processor=processor,
                    action_processor=ActionProcessor(
                        engine,
                        options,
                        process_action_handlers,
                        invalid_message_processor=processor,
                    ),
                )
            return runtime["processor"], runtime["action_processor"]

    @app.task(name=TASK_NAME, ignore_result=True, acks_late=True)
    def process_outbox_event(payload: object) -> None:
        get_processors()[0].process_payload(payload)

    @app.task(name=ACTION_TASK_NAME, ignore_result=True, acks_late=True)
    def process_task_action_event(payload: object) -> None:
        get_processors()[1].process_payload(payload)

    def dispose_worker_engine(**_kwargs: object) -> None:
        if runtime["pid"] == os.getpid() and runtime["engine"] is not None:
            runtime["engine"].dispose()
            runtime.update(
                pid=None,
                engine=None,
                processor=None,
                action_processor=None,
            )

    worker_process_shutdown.connect(dispose_worker_engine, weak=False)
    return app


def durable_queue(options: QueueOptions) -> Queue:
    """Return the one statically configured durable queue used by publisher and worker."""
    exchange = Exchange(options.queue_name, type="direct", durable=True, auto_delete=False)
    return Queue(
        options.queue_name,
        exchange=exchange,
        routing_key=options.queue_name,
        durable=True,
        exclusive=False,
        auto_delete=False,
    )
