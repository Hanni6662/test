from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app.db.retention_models import CleanupJob, CleanupRun, CleanupTarget, RetentionPolicy
from app.services.retention_policy import (
    RetentionPolicyUnavailable,
    load_retention_policy_in_transaction,
)


def test_policy_loader_returns_a_frozen_snapshot_without_locking() -> None:
    policy = SimpleNamespace(
        task_retention_days=45,
        reference_retention_days=75,
        recommendation_retention_days=90,
        revision=8,
    )
    result = Mock()
    result.scalar_one_or_none.return_value = policy
    session = Mock()
    session.execute.return_value = result

    snapshot = load_retention_policy_in_transaction(session)

    assert snapshot.task_retention_days == 45
    assert snapshot.reference_retention_days == 75
    assert snapshot.recommendation_retention_days == 90
    assert snapshot.revision == 8
    statement = session.execute.call_args.args[0]
    assert statement._for_update_arg is None


def test_policy_loader_fails_when_the_singleton_is_missing() -> None:
    result = Mock()
    result.scalar_one_or_none.return_value = None
    session = Mock()
    session.execute.return_value = result

    with pytest.raises(RetentionPolicyUnavailable):
        load_retention_policy_in_transaction(session)


def test_cleanup_models_expose_frozen_status_and_integrity_constraints() -> None:
    assert RetentionPolicy.__table__.primary_key.columns.keys() == ["id"]
    assert "singleton_check" in {
        constraint.name for constraint in RetentionPolicy.__table__.constraints
    }
    assert "uq_cleanup_runs_trigger_key" in {
        constraint.name for constraint in CleanupRun.__table__.constraints
    }
    assert "uq_cleanup_runs_idempotency_hash" in {
        constraint.name for constraint in CleanupRun.__table__.constraints
    }
    assert {
        "orphan_scan_cursor",
        "orphan_scan_lease_token",
        "orphan_scan_lease_until",
        "orphan_scan_completed_at",
    } <= set(CleanupRun.__table__.columns.keys())
    assert {
        "orphan_scan_cursor_check",
        "orphan_scan_lease_consistency_check",
        "orphan_scan_completion_lease_check",
    } <= {constraint.name for constraint in CleanupRun.__table__.constraints}
    assert "ix_cleanup_runs_orphan_scan_claim" in {
        index.name for index in CleanupRun.__table__.indexes
    }
    assert "claim_lease_consistency_check" in {
        constraint.name for constraint in CleanupJob.__table__.constraints
    }
    assert "uq_cleanup_targets_locator" in {
        constraint.name for constraint in CleanupTarget.__table__.constraints
    }
