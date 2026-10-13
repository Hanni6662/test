from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.retention_models import RetentionPolicy


@dataclass(frozen=True, slots=True)
class RetentionPolicySnapshot:
    task_retention_days: int
    reference_retention_days: int
    recommendation_retention_days: int
    revision: int


class RetentionPolicyUnavailable(RuntimeError):
    """The required singleton policy row has not been installed."""


def load_retention_policy_in_transaction(db: Session) -> RetentionPolicySnapshot:
    """Read the singleton inside the caller's transaction without taking a row lock."""
    policy = db.execute(select(RetentionPolicy).where(RetentionPolicy.id == 1)).scalar_one_or_none()
    if policy is None:
        raise RetentionPolicyUnavailable("retention policy singleton is unavailable")
    return RetentionPolicySnapshot(
        task_retention_days=policy.task_retention_days,
        reference_retention_days=policy.reference_retention_days,
        recommendation_retention_days=policy.recommendation_retention_days,
        revision=policy.revision,
    )
