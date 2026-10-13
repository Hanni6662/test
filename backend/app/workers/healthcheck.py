from __future__ import annotations

import os

from kombu import Connection

from app.core.config import get_settings
from app.workers.options import queue_options_from_settings


def worker_is_consuming() -> bool:
    settings = get_settings()
    options = queue_options_from_settings(
        settings,
        queue_name=os.environ.get("IMAGE_BACKEND_QUEUE_NAME", "image-backend-events"),
    )
    connection = Connection(options.broker_url, connect_timeout=2)
    try:
        with connection:
            channel = connection.channel()
            try:
                state = channel.queue_declare(queue=options.queue_name, passive=True)
                return state.consumer_count > 0
            finally:
                channel.close()
    except Exception:
        # Health checks must not print broker URLs or driver exception details.
        return False


def main() -> None:
    raise SystemExit(0 if worker_is_consuming() else 1)


if __name__ == "__main__":
    main()
