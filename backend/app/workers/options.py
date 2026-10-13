from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from pydantic import SecretStr

from app.core.config import Settings

_QUEUE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


@dataclass(frozen=True, slots=True)
class QueueOptions:
    broker_url: str = field(repr=False)
    queue_name: str = "image-backend-events"
    claim_batch_size: int = 10
    lease_seconds: int = 300
    max_attempts: int = 8
    retry_base_seconds: int = 2
    retry_max_seconds: int = 300
    confirm_timeout_seconds: int = 10
    poll_interval_seconds: float = 2.0
    republish_after_seconds: int = 600
    schema_version: int = 1

    def __post_init__(self) -> None:
        parsed = urlsplit(self.broker_url)
        if parsed.scheme not in {"amqp", "amqps"} or not parsed.hostname:
            raise ValueError("broker_url must be an AMQP URL")
        if not _QUEUE_NAME.fullmatch(self.queue_name):
            raise ValueError("queue_name must be a safe AMQP queue identifier")
        if self.claim_batch_size < 1:
            raise ValueError("claim_batch_size must be positive")
        if self.lease_seconds < 1 or self.max_attempts < 1:
            raise ValueError("lease_seconds and max_attempts must be positive")
        minimum_claim_time = self.claim_batch_size * self.confirm_timeout_seconds * 2
        if self.lease_seconds < minimum_claim_time:
            raise ValueError("lease_seconds must cover batch connects and publisher confirms")
        if self.retry_base_seconds < 1 or self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("retry delay limits are invalid")
        if (
            self.confirm_timeout_seconds < 1
            or not math.isfinite(self.poll_interval_seconds)
            or self.poll_interval_seconds <= 0
        ):
            raise ValueError("queue timeouts must be positive")
        if self.republish_after_seconds < self.lease_seconds:
            raise ValueError("republish_after_seconds must not be shorter than the claim lease")
        if self.schema_version < 1:
            raise ValueError("schema_version must be positive")

    def retry_delay_seconds(self, attempt: int) -> int:
        if attempt < 1:
            raise ValueError("attempt must be positive")
        power = min(attempt - 1, 30)
        return min(self.retry_max_seconds, self.retry_base_seconds * (2**power))


def queue_options_from_settings(
    settings: Settings, *, queue_name: str = "image-backend-events"
) -> QueueOptions:
    """Adapt the shared, already-validated settings without adding a DSN system."""
    broker_url: SecretStr | None = settings.broker_url
    if broker_url is None:
        raise ValueError("IMAGE_BACKEND_BROKER_URL is required for queue processes")
    return QueueOptions(broker_url=broker_url.get_secret_value(), queue_name=queue_name)
