from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.db.prompt_models import MockPromptSubmission
from app.integrations.dify.mock import MockPromptWorkflowAdapter
from app.schemas.prompt_workflow import (
    PROMPT_CONTENT_DIGEST_VERSION,
    PromptRecoveryResult,
    PromptSubmissionCommand,
    PromptSubmitResult,
    canonical_sha256,
)
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker


def _command() -> PromptSubmissionCommand:
    references = [
        {"alias": "PRIMARY"},
        {"alias": "AUX01"},
        {"alias": "AUX02"},
        {"alias": "AUX03"},
    ]
    relations = [
        {
            "alias": "AUX01",
            "view": "Synthetic view one",
            "relation": "confirm",
            "scope": "global",
            "evidence": "No image bytes were read.",
        },
        {
            "alias": "AUX02",
            "view": "Synthetic view two",
            "relation": "supplement",
            "scope": "detail_only",
            "evidence": "No image bytes were read.",
        },
        {
            "alias": "AUX03",
            "view": "Synthetic view three",
            "relation": "ignored_conflict",
            "scope": "side_only",
            "evidence": "No image bytes were read.",
        },
    ]
    private_payload: dict[str, object] = {
        "category_snapshot": "synthetic test category",
        "image_types": ["品牌导购主视觉"],
        "resolved_preferences": {
            "language": {"mode": "A", "text": "中文"},
            "platform": {"mode": "platform", "value": "TEMU"},
            "usage_scene": {"mode": "B", "text": "日常使用"},
            "visual_style": {
                "mode": "D",
                "kind": "agent_design",
                "instruction": "设计但不推断未知事实",
            },
            "selling_points": {"mode": "custom", "text": "可核验卖点"},
        },
        "trusted_analysis_snapshot": {
            "references": references,
            "auxiliary_relations": relations,
        },
    }
    phase_payload = {
        "phase": "initial_preferences",
        "followup_round": 0,
        "private_payload": private_payload,
    }
    return PromptSubmissionCommand(
        task_id=uuid4(),
        workflow_run_id=uuid4(),
        workflow_revision=1,
        submission_id=uuid4(),
        phase="initial_preferences",
        followup_round=0,
        content_digest_version=PROMPT_CONTENT_DIGEST_VERSION,
        content_digest=canonical_sha256(phase_payload),
        private_payload=private_payload,
    )


@pytest.fixture
def sqlite_sessions() -> Iterator[Callable[[], Session]]:
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _register_test_sql_functions(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function("octet_length", 1, lambda value: len(value))
        dbapi_connection.create_function(
            "clock_timestamp", 0, lambda: datetime.now(UTC).isoformat()
        )

    MockPromptSubmission.__table__.create(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    try:
        yield sessions
    finally:
        engine.dispose()


class _CountingPort:
    def __init__(self, adapter: MockPromptWorkflowAdapter) -> None:
        self.adapter = adapter
        self.submit_calls = 0
        self.recovery_calls = 0

    def submit_preferences(self, command: PromptSubmissionCommand) -> PromptSubmitResult:
        self.submit_calls += 1
        return self.adapter.submit_preferences(command)

    def recover_submission(self, command: PromptSubmissionCommand) -> PromptRecoveryResult:
        self.recovery_calls += 1
        return self.adapter.recover_submission(command)


def test_accepted_but_lost_submit_response_is_recovered_without_resubmission(
    sqlite_sessions: Callable[[], Session],
) -> None:
    command = _command()
    adapter = MockPromptWorkflowAdapter.for_testing(
        sqlite_sessions, accepted_response_lost_once=True
    )
    port = _CountingPort(adapter)

    unknown = port.submit_preferences(command)
    recovered = port.recover_submission(command)

    assert unknown.outcome == "unknown"
    assert recovered.outcome == "accepted"
    assert port.submit_calls == 1
    assert port.recovery_calls == 1
    with sqlite_sessions() as db:
        assert db.scalar(select(func.count(MockPromptSubmission.submission_id))) == 1
        row = db.get(MockPromptSubmission, command.submission_id)
        assert row is not None and row.outcome == "accepted"
        assert row.result_package["basis"]["auxiliary_relations"] == [
            {
                "id": "AUX01",
                "view": "Synthetic view one",
                "relation": "confirm",
                "scope": "global",
                "evidence": "No image bytes were read.",
            },
            {
                "id": "AUX02",
                "view": "Synthetic view two",
                "relation": "supplement",
                "scope": "detail_only",
                "evidence": "No image bytes were read.",
            },
            {
                "id": "AUX03",
                "view": "Synthetic view three",
                "relation": "ignored_conflict",
                "scope": "side_only",
                "evidence": "No image bytes were read.",
            },
        ]
        assert row.result_package["images"][0]["reference_plan"]["view_reference_ids"] == [
            "AUX01",
            "AUX02",
            "AUX03",
        ]
        assert row.result_package["images"][0]["reference_plan"]["excluded_reference_ids"] == []
        assert (
            "no image bytes were read"
            in row.result_package["basis"]["physical_profile"]["silhouette_proportions"].lower()
        )


def test_unknown_recovery_remains_read_only_until_later_reconciliation(
    sqlite_sessions: Callable[[], Session],
) -> None:
    command = _command()
    adapter = MockPromptWorkflowAdapter.for_testing(
        sqlite_sessions,
        accepted_response_lost_once=True,
        recovery_unknown_once=True,
    )
    port = _CountingPort(adapter)

    initial = port.submit_preferences(command)
    first_recovery = port.recover_submission(command)
    second_recovery = port.recover_submission(command)

    assert initial.outcome == "unknown"
    assert first_recovery.outcome == "unknown"
    assert second_recovery.outcome == "accepted"
    assert port.submit_calls == 1
    assert port.recovery_calls == 2
    with sqlite_sessions() as db:
        assert db.scalar(select(func.count(MockPromptSubmission.submission_id))) == 1


def test_same_submission_and_digest_replays_one_durable_synthetic_acceptance(
    sqlite_sessions: Callable[[], Session],
) -> None:
    command = _command()
    adapter = MockPromptWorkflowAdapter(sqlite_sessions)

    first = adapter.submit_preferences(command)
    replay = adapter.submit_preferences(command)

    assert first.outcome == replay.outcome == "accepted"
    assert first.provider_submission_ref == replay.provider_submission_ref
    with sqlite_sessions() as db:
        assert db.scalar(select(func.count(MockPromptSubmission.submission_id))) == 1
