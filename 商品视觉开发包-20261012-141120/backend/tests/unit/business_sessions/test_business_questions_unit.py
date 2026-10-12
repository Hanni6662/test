from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.integrations.dify.chatflow import (
    ChatflowHumanInputCommand,
    StubDifyChatflowAdapter,
)
from app.schemas.business_questions import (
    BusinessQuestionAnswerRequest,
    HumanInputRequiredPayload,
)
from app.schemas.business_sessions import ChatflowBusinessEvent
from app.services.chatflow_actions import (
    CHATFLOW_ANSWER_ACTION,
    CHATFLOW_ANSWER_RECOVERY_ACTION,
    build_chatflow_action_registrations,
    execute_chatflow_answer,
    recover_chatflow_answer,
)
from app.services.retention_cleanup import (
    _has_blocking_actions,
    _has_blocking_business_questions,
)
from app.workers.action_processor import ActionInput

H3_FIELD_IDS = (
    "choice_1",
    "custom_1",
    "choice_2",
    "choice_3",
    "custom_3",
    "choice_4",
    "custom_4",
    "choice_5",
    "custom_5",
    "image_types",
)


def _h3_fields(field_ids: tuple[str, ...]) -> list[dict[str, object]]:
    return [
        {
            "field_id": field_id,
            "label": field_id,
            "kind": "single_choice" if field_id.startswith("choice_") else "text",
            "required": True,
            "options": ["A", "B"] if field_id.startswith("choice_") else [],
        }
        for field_id in field_ids
    ]


def _answer_action_input() -> tuple[ActionInput, ChatflowHumanInputCommand]:
    command = ChatflowHumanInputCommand(
        business_session_id=uuid4(),
        recommendation_id=uuid4(),
        recommendation_revision=4,
        dify_run_id="same-workflow-run",
        dify_user_id="business-session-user",
        task_id=uuid4(),
        question_id=uuid4(),
        answer_submission_id=uuid4(),
        round_number=1,
        form_token="private-form-token",
        answers={"color": "blue"},
    )
    payload = {
        "business_session_id": str(command.business_session_id),
        "recommendation_id": str(command.recommendation_id),
        "recommendation_revision": command.recommendation_revision,
        "dify_run_id": command.dify_run_id,
        "dify_user_id": command.dify_user_id,
        "task_id": str(command.task_id),
        "question_id": str(command.question_id),
        "answer_submission_id": str(command.answer_submission_id),
        "round_number": command.round_number,
        "form_token": command.form_token,
        "answers": command.answers,
    }
    return (
        ActionInput(
            action_id=uuid4(),
            task_id=command.task_id,
            scope_kind="business_session",
            scope_id=command.business_session_id,
            action_kind=CHATFLOW_ANSWER_ACTION,
            sequence=3,
            input_digest_version=1,
            input_digest=b"x" * 32,
            payload=payload,
            business_session_id=command.business_session_id,
            business_question_id=command.question_id,
            business_answer_submission_id=command.answer_submission_id,
        ),
        command,
    )


def test_unknown_answer_recovers_same_run_and_submission_without_resubmitting() -> None:
    action_input, expected = _answer_action_input()
    adapter = StubDifyChatflowAdapter(lose_answer_response_once=True)

    first = execute_chatflow_answer(adapter, action_input)
    wrong_form = adapter.recover_human_input(replace(expected, form_token="different-form"))
    recovered = recover_chatflow_answer(adapter, action_input)

    assert first.outcome == "unknown"
    assert wrong_form.outcome == "unknown"
    assert recovered.outcome == "accepted"
    assert len(adapter.human_input_calls) == 1
    assert len(adapter.human_input_recovery_calls) == 2
    submitted = adapter.human_input_calls[0]
    mismatched_recovery, recovery = adapter.human_input_recovery_calls
    assert mismatched_recovery.form_token == "different-form"
    assert submitted.dify_run_id == expected.dify_run_id
    assert recovery.dify_run_id == expected.dify_run_id
    assert recovery.form_token == expected.form_token
    assert recovery.answer_submission_id == submitted.answer_submission_id
    assert "private-form-token" not in repr(submitted)
    assert "private-form-token" not in repr(action_input)


def test_answer_and_recovery_actions_are_business_session_scoped() -> None:
    registrations = build_chatflow_action_registrations(StubDifyChatflowAdapter())

    assert CHATFLOW_ANSWER_ACTION in registrations
    assert CHATFLOW_ANSWER_RECOVERY_ACTION in registrations
    assert registrations[CHATFLOW_ANSWER_ACTION].scope_kinds == frozenset({"business_session"})
    assert registrations[CHATFLOW_ANSWER_RECOVERY_ACTION].scope_kinds == frozenset(
        {"business_session"}
    )
    assert registrations[CHATFLOW_ANSWER_ACTION].complete_unknown_step
    assert registrations[CHATFLOW_ANSWER_RECOVERY_ACTION].complete_unknown_step


def test_form_token_is_hidden_from_event_and_question_payload_repr() -> None:
    token = "form-secret-never-log"
    event = ChatflowBusinessEvent(
        contract_version="chatflow-handoff-v1",
        business_session_id=uuid4(),
        recommendation_id=uuid4(),
        recommendation_revision=1,
        dify_run_id="same-workflow-run",
        event_id=uuid4(),
        source="dify",
        source_sequence=2,
        event_type="human_input_required",
        digest_version=1,
        digest="a" * 64,
        payload={"form_token": token},
    )
    payload = HumanInputRequiredPayload(
        task_id=uuid4(),
        round_number=1,
        question_text="选择颜色",
        fields=[
            {
                "field_id": "color",
                "label": "颜色",
                "kind": "single_choice",
                "required": True,
                "options": ["blue", "red"],
            }
        ],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        form_token=token,
    )

    assert token not in repr(event)
    assert token not in repr(payload)
    assert payload.form_token == token


def test_human_input_round_three_fails_closed() -> None:
    with pytest.raises(ValueError):
        HumanInputRequiredPayload(
            task_id=uuid4(),
            round_number=3,
            question_text="不应接受第三轮",
            fields=[
                {
                    "field_id": "color",
                    "label": "颜色",
                    "kind": "text",
                    "required": True,
                    "options": [],
                }
            ],
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )


def test_h3_accepts_all_ten_contract_fields_for_questions_and_answers() -> None:
    payload = HumanInputRequiredPayload(
        task_id=uuid4(),
        round_number=1,
        question_text="补充偏好",
        fields=_h3_fields(H3_FIELD_IDS),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    answers = BusinessQuestionAnswerRequest(
        answers={field_id: "A" for field_id in H3_FIELD_IDS}
    )

    assert tuple(item.field_id for item in payload.fields) == H3_FIELD_IDS
    assert tuple(answers.answers) == H3_FIELD_IDS


def test_h3_rejects_eleven_question_fields_and_answers() -> None:
    eleven_fields = (*H3_FIELD_IDS, "extra_field")
    base_payload = {
        "task_id": uuid4(),
        "round_number": 1,
        "question_text": "补充偏好",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }

    with pytest.raises(ValueError):
        HumanInputRequiredPayload(
            **base_payload,
            fields=_h3_fields(eleven_fields),
        )
    with pytest.raises(ValueError):
        BusinessQuestionAnswerRequest(
            answers={field_id: "A" for field_id in eleven_fields}
        )


def test_cleanup_allows_expired_unanswered_question_but_blocks_unknown_answer() -> None:
    now = datetime.now(UTC)
    expired_question = SimpleNamespace(
        status="awaiting_answer", expires_at=now - timedelta(seconds=1)
    )
    active_question = SimpleNamespace(
        status="awaiting_form_binding", expires_at=now + timedelta(seconds=1)
    )
    confirming_question = SimpleNamespace(
        status="confirming", expires_at=now - timedelta(seconds=1)
    )
    unknown_answer = SimpleNamespace(status="unknown")

    assert not _has_blocking_business_questions([expired_question], [], now=now)
    assert _has_blocking_business_questions([active_question], [], now=now)
    assert _has_blocking_business_questions([confirming_question], [], now=now)
    assert _has_blocking_business_questions([], [unknown_answer], now=now)


def test_cleanup_blocks_pending_and_unknown_answer_actions() -> None:
    pending_action = SimpleNamespace(status="scheduled", scope_kind="business_session")
    unknown_action = SimpleNamespace(status="unknown", scope_kind="business_session")
    empty_domains = {"attempts": [], "submissions": [], "workflows": []}

    assert _has_blocking_actions([pending_action], **empty_domains)
    assert _has_blocking_actions([unknown_action], **empty_domains)


def test_h3_migration_is_a_forward_revision_after_s013() -> None:
    migration_path = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "versions"
        / "s014_business_questions.py"
    )
    namespace: dict[str, object] = {}
    exec(
        compile(migration_path.read_text(encoding="utf-8"), str(migration_path), "exec"),
        namespace,
    )

    assert namespace["revision"] == "s014_business_questions"
    assert namespace["down_revision"] == "s013_chatflow_handoff"
