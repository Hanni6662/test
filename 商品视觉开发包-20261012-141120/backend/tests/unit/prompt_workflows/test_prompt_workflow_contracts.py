from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from app.schemas.prompt_workflow import (
    PROMPT_CONTENT_DIGEST_VERSION,
    PROMPT_RESULT_DIGEST_VERSION,
    PromptCallback,
    PromptSubmissionCommand,
    canonical_sha256,
    prompt_callback_digest_payload,
)


def _command() -> PromptSubmissionCommand:
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
            "references": [{"alias": "PRIMARY"}],
            "auxiliary_relations": [],
        },
    }
    step = {
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
        content_digest=canonical_sha256(step),
        private_payload=private_payload,
    )


def _callback_payload() -> dict[str, object]:
    return {
        "task_id": uuid4(),
        "workflow_run_id": uuid4(),
        "workflow_revision": 1,
        "submission_id": uuid4(),
        "submission_content_digest_version": 1,
        "submission_content_digest": b"s" * 32,
        "result_digest_version": PROMPT_RESULT_DIGEST_VERSION,
        "result_digest": b"r" * 32,
        "outcome": "needs_input",
        "package": None,
        "next_question_round": 1,
        "allowed_fields": ("3",),
        "locked_fields": ("1", "2", "4", "5"),
        "valid_until": datetime(2026, 10, 8, tzinfo=UTC),
        "safe_reason_code": None,
    }


def test_submission_command_digest_binds_exact_private_content() -> None:
    command = _command()

    assert command.content_digest == canonical_sha256(
        {
            "phase": command.phase,
            "followup_round": command.followup_round,
            "private_payload": command.private_payload,
        }
    )
    with pytest.raises(ValueError, match="digest"):
        PromptSubmissionCommand.model_validate(
            {
                **command.model_dump(mode="python"),
                "private_payload": {**command.private_payload, "category_snapshot": "changed"},
            }
        )


def test_followup_submission_round_is_limited_to_one_or_two() -> None:
    command = _command()
    payload = command.model_dump(mode="python")
    step = {
        "phase": "followup_answer",
        "followup_round": 3,
        "private_payload": command.private_payload,
    }

    with pytest.raises(ValueError):
        PromptSubmissionCommand.model_validate(
            {
                **payload,
                "phase": "followup_answer",
                "followup_round": 3,
                "content_digest": canonical_sha256(step),
            }
        )


def test_callback_needs_input_is_bound_to_utc_and_has_no_package() -> None:
    callback_fields = _callback_payload()
    callback = PromptCallback.model_validate(callback_fields)
    result_digest = canonical_sha256(prompt_callback_digest_payload(callback))
    callback = PromptCallback.model_validate({**callback_fields, "result_digest": result_digest})

    assert callback.result_digest == result_digest
    with pytest.raises(ValueError, match="UTC"):
        PromptCallback.model_validate(
            {
                **callback_fields,
                "valid_until": datetime(2026, 10, 8, tzinfo=timezone(timedelta(hours=8))),
            }
        )
    with pytest.raises(ValueError, match="cannot include"):
        PromptCallback.model_validate({**callback_fields, "package": {"partial": True}})


def test_completed_callback_requires_a_package_payload() -> None:
    callback = _callback_payload()
    callback.update(
        {
            "outcome": "completed",
            "package": None,
            "next_question_round": None,
            "allowed_fields": None,
            "locked_fields": None,
            "valid_until": None,
        }
    )

    with pytest.raises(ValueError, match="full prompt package"):
        PromptCallback.model_validate(callback)
