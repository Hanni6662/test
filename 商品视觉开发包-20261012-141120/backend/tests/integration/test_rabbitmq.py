from __future__ import annotations

import os

import pytest
from kombu import Connection

QUEUE_NAME = "image_backend_s1_persistence_probe"


@pytest.mark.integration
def test_dedicated_rabbitmq_accepts_connection_and_persists_queue() -> None:
    broker_url = os.environ["IMAGE_BACKEND_TEST_BROKER_URL"]
    with Connection(broker_url, connect_timeout=5) as connection:
        connection.connect()
        assert connection.connected

        channel = connection.channel()
        try:
            persisted_queue_expected = os.getenv(
                "IMAGE_BACKEND_TEST_EXPECT_PERSISTED_RABBITMQ_QUEUE", "false"
            ).lower() == "true"
            channel.queue_declare(
                queue=QUEUE_NAME,
                passive=persisted_queue_expected,
                durable=True,
                exclusive=False,
                auto_delete=False,
            )
        finally:
            channel.close()
