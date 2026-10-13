from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

import pytest
from app.db.action_models import Action
from app.workers.action_processor import (
    ACTION_EVENT_TYPE,
    ACTION_TASK_NAME,
    ActionExecutionResult,
    ActionInput,
    ActionProcessor,
    ActionRegistration,
)
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint


def _action_input() -> ActionInput:
    attempt_id = uuid4()
    return ActionInput(
        action_id=uuid4(),
        task_id=uuid4(),
        task_item_id=uuid4(),
        attempt_id=attempt_id,
        scope_kind="attempt",
        scope_id=attempt_id,
        action_kind="generation.poll",
        sequence=2,
        input_digest_version=1,
        input_digest=b"x" * 32,
        payload=("private-context",),
    )


def test_action_event_and_action_task_have_stable_names() -> None:
    assert ACTION_EVENT_TYPE == "task.action.v1"
    assert ACTION_TASK_NAME == "app.workers.process_task_action_event"


def test_action_migration_is_a_single_forward_revision_from_s006() -> None:
    migration_path = (
        Path(__file__).resolve().parents[3] / "migrations" / "versions" / "s007_task_actions.py"
    )
    spec = spec_from_file_location("s007_task_actions_unit_test", migration_path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "s007_task_actions"
    assert migration.down_revision == "s006_tasks"


def test_action_table_has_namespaced_scope_sequence_and_unique_outbox_identity() -> None:
    uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in Action.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("scope_kind", "scope_id", "sequence_namespace", "sequence") in uniques
    assert ("outbox_event_id",) in uniques
    submission_foreign_keys = {
        (
            tuple(constraint.columns.keys()),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in Action.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert (
        ("submission_id", "task_id"),
        ("task_prompt_submissions.submission_id", "task_prompt_submissions.task_id"),
    ) in submission_foreign_keys


def test_prompt_migration_is_the_single_successor_to_s008() -> None:
    migration_path = (
        Path(__file__).resolve().parents[3] / "migrations" / "versions" / "s009_prompt_workflows.py"
    )
    spec = spec_from_file_location("s009_prompt_workflows_unit_test", migration_path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "s009_prompt_workflows"
    assert migration.down_revision == "s008_mock_generation"


def test_generation_migration_is_the_single_successor_to_s009_and_models_are_active() -> None:
    migration_path = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "versions"
        / "s010_generation_runtime.py"
    )
    spec = spec_from_file_location("s010_generation_runtime_unit_test", migration_path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "s010_generation_runtime"
    assert migration.down_revision == "s009_prompt_workflows"

    import app.main  # noqa: F401
    from app.db.base import Base

    assert {
        "generation_capacity_control",
        "generation_capacity_reservations",
        "generation_attempt_contexts",
        "generation_results",
    }.issubset(Base.metadata.tables)
    assert "generation_models" in Path(__file__).resolve().parents[3].joinpath(
        "migrations", "env.py"
    ).read_text(encoding="utf-8")


def test_action_result_keeps_unknown_separate_from_safe_retry() -> None:
    unknown = ActionExecutionResult("unknown", safe_reason_code="TRANSPORT_OUTCOME_UNKNOWN")
    retry = ActionExecutionResult(
        "safe_retry", safe_reason_code="POLL_NOT_STARTED", retry_after_seconds=5
    )

    assert unknown.outcome == "unknown"
    assert retry.outcome == "safe_retry"
    assert retry.retry_after_seconds == 5
    with pytest.raises(ValueError):
        ActionExecutionResult("definitely_rejected")
    with pytest.raises(ValueError):
        ActionExecutionResult("safe_retry", safe_reason_code="POLL_NOT_STARTED")


def test_action_registration_rejects_invalid_kind_and_execute_has_no_session_argument() -> None:
    received: list[ActionInput] = []

    def prepare(_db, action):
        return _action_input()

    def execute(action_input: ActionInput) -> ActionExecutionResult:
        received.append(action_input)
        return ActionExecutionResult("accepted")

    def finalize(_db, _action, _action_input, _result):
        return None

    registration = ActionRegistration("generation.poll", prepare, execute, finalize)
    action_input = _action_input()
    assert ActionProcessor._run_io(registration.execute, action_input).outcome == "accepted"
    assert received == [action_input]
    with pytest.raises(ValueError):
        ActionRegistration("Not A Kind", prepare, execute, finalize)
    prompt_registration = ActionRegistration(
        "prompt.submit",
        prepare,
        execute,
        finalize,
        scope_kinds=frozenset({"submission"}),
    )
    assert prompt_registration.scope_kinds == frozenset({"submission"})
    with pytest.raises(ValueError):
        ActionRegistration(
            "prompt.empty-scope",
            prepare,
            execute,
            finalize,
            scope_kinds=frozenset(),
        )


def test_action_input_is_a_frozen_transfer_object() -> None:
    action_input = _action_input()

    with pytest.raises(FrozenInstanceError):
        action_input.sequence = 3


def test_action_input_copies_and_freezes_nested_payload_containers() -> None:
    source = {"items": [{"reference_ids": ["synthetic-id"]}]}
    action_input = replace(_action_input(), payload=source)
    source["items"][0]["reference_ids"].append("later-change")

    assert action_input.payload["items"][0]["reference_ids"] == ("synthetic-id",)
    with pytest.raises(TypeError):
        action_input.payload["items"][0]["new"] = "blocked"


def test_action_io_exception_is_unknown_not_retry() -> None:
    def ambiguous_io(_action_input: ActionInput) -> ActionExecutionResult:
        raise TimeoutError("transport detail must not be surfaced")

    result = ActionProcessor._run_io(ambiguous_io, _action_input())

    assert result.outcome == "unknown"
    assert result.safe_reason_code == "ACTION_IO_OUTCOME_UNKNOWN"
