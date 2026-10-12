"""Celery app used only by the isolated R8 HTTP/worker acceptance runner."""

from app.integrations.generation.mock import _TestOnlyFaults
from app.workers.run_worker import _build_worker_app

# The first accepted generation submit loses only its response. The mock job and
# invocation remain durable in PostgreSQL, so the normal recovery Action must
# discover that same job without issuing another submit for its attempt key.
app = _build_worker_app(generation_test_faults=_TestOnlyFaults(accepted_response_lost_once=True))
