from __future__ import annotations

from app.services.recommendations import RECOMMENDATION_EVENT_TYPE
from app.workers.demo import DEMO_EVENT_TYPE

# task.prompt.v1 and task.action.v1 stay out of this static allowlist. The
# dispatcher enables task.prompt.v1 only when its DB handler and submission
# Action registrations are wired; action wakes remain filtered by exact kind and
# scope. H2 business-session Actions are enabled only behind the Chatflow gate and
# their registered stub adapter. task.generation.start.v1 remains closed until R5.
DEFAULT_PUBLISH_ALLOWLIST = frozenset(
    {
        DEMO_EVENT_TYPE,
        RECOMMENDATION_EVENT_TYPE,
    }
)

UNSUPPORTED_EVENT_REASON = "EVENT_TYPE_NOT_ALLOWLISTED"
