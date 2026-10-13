from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from app.integrations.dify.chatflow import (
    ChatflowContinuationResult,
    ChatflowStartCommand,
)
from app.schemas.business_sessions import ChatflowReferenceIdentity, chatflow_reference_asset_digest
from app.services.chatflow_actions import (
    CHATFLOW_ANSWER_ACTION,
    CHATFLOW_ANSWER_RECOVERY_ACTION,
    CHATFLOW_CANCEL_ACTION,
    CHATFLOW_CANCEL_RECOVERY_ACTION,
    CHATFLOW_CONTINUE_ACTION,
    CHATFLOW_FORM_BIND_ACTION,
    CHATFLOW_REFERENCE_UPLOAD_ACTION,
    CHATFLOW_RUN_OBSERVATION_ACTION,
    CHATFLOW_START_ACTION,
    CHATFLOW_START_OBSERVATION_ACTION,
    build_chatflow_action_registrations,
    execute_chatflow_continuation,
    execute_chatflow_start,
    recover_chatflow_start,
)
from app.workers.action_processor import ActionInput, ActionRegistration


def _action_input(*, action_kind: str, payload: dict[str, object]) -> ActionInput:
    session_id = uuid4()
    return ActionInput(
        action_id=uuid4(),
        task_id=uuid4() if action_kind == CHATFLOW_CONTINUE_ACTION else None,
        scope_kind="business_session",
        scope_id=session_id,
        action_kind=action_kind,
        sequence=2 if action_kind == CHATFLOW_CONTINUE_ACTION else 1,
        input_digest_version=1,
        input_digest=b"x" * 32,
        payload=payload,
        business_session_id=session_id,
    )


class _LostStartAdapter:
    mode = "mock"

    def __init__(self) -> None:
        self.calls: list[ChatflowStartCommand] = []

    def start_recommendation(self, command: ChatflowStartCommand):
        self.calls.append(command)
        raise TimeoutError("simulated response loss")

    def continue_after_selection(self, _command):
        raise AssertionError("start recovery must not continue the workflow")

    def recover_after_selection(self, _command):
        raise AssertionError("start recovery must not inspect continuation")


class _UnknownContinuationAdapter:
    mode = "mock"

    def __init__(self) -> None:
        self.continue_calls = []
        self.recovery_calls = []

    def start_recommendation(self, _command):
        raise AssertionError("continuation recovery must not start a new workflow")

    def continue_after_selection(self, command):
        self.continue_calls.append(command)
        return ChatflowContinuationResult("unknown", "CHATFLOW_RESUME_OUTCOME_UNKNOWN")

    def recover_after_selection(self, command):
        self.recovery_calls.append(command)
        return ChatflowContinuationResult("accepted")


def test_start_response_loss_recovers_unknown_without_resubmitting_start() -> None:
    adapter = _LostStartAdapter()
    action_input = _action_input(
        action_kind=CHATFLOW_START_ACTION,
        payload={
            "business_session_id": str(uuid4()),
            "recommendation_id": str(uuid4()),
            "recommendation_revision": 1,
            "dify_user_id": "bs-test",
            "reference_set_id": str(uuid4()),
            "reference_asset_digest": "a" * 64,
            "reference_assets": [],
            "reference_files": [
                {
                    "dify_file_id": "test-file-id",
                    "slot": "PRIMARY",
                    "position": 0,
                    "content_sha256": "a" * 64,
                }
            ],
            "category": "测试品类",
        },
    )

    first = execute_chatflow_start(adapter, action_input)
    recovered = recover_chatflow_start(action_input)

    assert first.outcome == "unknown"
    assert recovered.outcome == "unknown"
    assert len(adapter.calls) == 1


def test_unknown_continuation_recovers_against_the_same_dify_run() -> None:
    adapter = _UnknownContinuationAdapter()
    action_input = _action_input(
        action_kind=CHATFLOW_CONTINUE_ACTION,
        payload={
            "business_session_id": str(uuid4()),
            "recommendation_id": str(uuid4()),
            "recommendation_revision": 1,
            "dify_run_id": "same-workflow-run",
            "dify_user_id": "bs-test",
            "task_id": str(uuid4()),
            "selection_snapshot": {"selection": {"image_types": ["hero"]}},
        },
    )

    first = execute_chatflow_continuation(adapter, action_input)
    recovered = build_chatflow_action_registrations(adapter)[CHATFLOW_CONTINUE_ACTION].recover
    assert recovered is not None
    second = recovered(action_input)

    assert first.outcome == "unknown"
    assert second.outcome == "accepted"
    assert len(adapter.continue_calls) == 1
    assert len(adapter.recovery_calls) == 1
    assert adapter.recovery_calls[0].dify_run_id == "same-workflow-run"
    assert (
        adapter.recovery_calls[0].business_session_id
        == adapter.continue_calls[0].business_session_id
    )


def test_chatflow_action_registrations_are_scoped_to_business_session() -> None:
    class NoCallAdapter:
        mode = "mock"

        def start_recommendation(self, _command):
            raise AssertionError

        def continue_after_selection(self, _command):
            raise AssertionError

        def recover_after_selection(self, _command):
            raise AssertionError

    registrations: dict[str, ActionRegistration] = build_chatflow_action_registrations(
        NoCallAdapter()
    )

    assert set(registrations) == {
        CHATFLOW_REFERENCE_UPLOAD_ACTION,
        CHATFLOW_START_ACTION,
        CHATFLOW_START_OBSERVATION_ACTION,
        CHATFLOW_FORM_BIND_ACTION,
        CHATFLOW_CONTINUE_ACTION,
        CHATFLOW_ANSWER_ACTION,
        CHATFLOW_ANSWER_RECOVERY_ACTION,
        CHATFLOW_CANCEL_ACTION,
        CHATFLOW_CANCEL_RECOVERY_ACTION,
        CHATFLOW_RUN_OBSERVATION_ACTION,
    }
    assert all(
        item.scope_kinds == frozenset({"business_session"}) for item in registrations.values()
    )


def test_reference_digest_binds_order_and_each_asset_identity() -> None:
    primary = ChatflowReferenceIdentity(
        reference_set_image_id=uuid4(),
        position=0,
        slot="PRIMARY",
        source_asset_id=uuid4(),
        model_asset_id=uuid4(),
        content_sha256="1" * 64,
        source_sha256="2" * 64,
    )
    auxiliary = ChatflowReferenceIdentity(
        reference_set_image_id=uuid4(),
        position=1,
        slot="AUX01",
        source_asset_id=uuid4(),
        model_asset_id=uuid4(),
        content_sha256="3" * 64,
        source_sha256="4" * 64,
    )

    assert chatflow_reference_asset_digest([primary, auxiliary]) != chatflow_reference_asset_digest(
        [auxiliary, primary]
    )


def test_h2_migration_is_single_forward_revision_after_s012() -> None:
    migration_path = (
        Path(__file__).resolve().parents[3] / "migrations" / "versions" / "s013_chatflow_handoff.py"
    )
    namespace: dict[str, object] = {}
    exec(
        compile(migration_path.read_text(encoding="utf-8"), str(migration_path), "exec"), namespace
    )

    assert namespace["revision"] == "s013_chatflow_handoff"
    assert namespace["down_revision"] == "s012_operations_visibility"
