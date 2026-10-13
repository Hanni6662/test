from __future__ import annotations

import os
import time
from pathlib import Path

from app.workers.run_worker import app as app  # noqa: F401
from celery.signals import task_postrun


@task_postrun.connect(weak=False)
def record_task_postrun(task_id: str | None = None, **_kwargs: object) -> None:
    marker_value = os.environ.get("IMAGE_BACKEND_TEST_POST_COMMIT_MARKER")
    if marker_value is None:
        return

    marker = Path(marker_value)
    marker.parent.mkdir(parents=True, exist_ok=True)
    with marker.open("a", encoding="utf-8") as output:
        output.write(f"{task_id}\n")
        output.flush()
        os.fsync(output.fileno())

    if os.environ.get("IMAGE_BACKEND_TEST_POST_COMMIT_MODE") == "pause":
        # task_postrun runs in the prefork child after EventProcessor committed
        # its receipt/effect, but before the parent process can late-ack delivery.
        while True:
            time.sleep(1)
