from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.core.domain_errors import DomainError
from app.schemas.prompt_preferences import (
    PromptCallbackRequest,
    PromptPreferenceRequest,
    RecommendationResultCallback,
    recommendation_callback_digest,
    valid_idempotency_key,
)
from app.schemas.recommendations import IMAGE_TYPES
from app.services.recommendation_callbacks import RecommendationCallbackService


def _prompt_wire_payload() -> dict[str, object]:
    return {
        "task_id": str(uuid4()),
        "workflow_run_id": str(uuid4()),
        "workflow_revision": 2,
        "submission_id": str(uuid4()),
        "phase": "followup_answer",
        "submission_content_digest_version": 1,
        "submission_content_digest": "ab" * 32,
        "result_digest_version": 1,
        "result_digest": "cd" * 32,
        "outcome": "needs_input",
        "package": None,
        "next_question_round": 2,
        "allowed_fields": ["language"],
        "locked_fields": ["platform", "usage_scene", "visual_style", "selling_points"],
        "valid_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "safe_reason_code": None,
    }


def test_prompt_callback_wire_hex_digests_decode_to_exact_32_bytes() -> None:
    envelope = PromptCallbackRequest.model_validate_json(
        json.dumps(_prompt_wire_payload()), strict=True
    )
    callback = envelope.to_domain()

    assert callback.submission_content_digest == bytes.fromhex("ab" * 32)
    assert callback.result_digest == bytes.fromhex("cd" * 32)
    assert len(callback.submission_content_digest) == 32
    assert len(callback.result_digest) == 32


def test_preference_request_uses_contract_field_names_and_strict_answer_types() -> None:
    request = PromptPreferenceRequest.model_validate(
        {
            "workflow_run_id": uuid4(),
            "workflow_revision": 1,
            "question_round": 1,
            "answers": {"language": {"mode": "B"}, "platform": "TEMU"},
        },
        strict=True,
    )

    assert set(request.answers) == {"language", "platform"}
    with pytest.raises(ValueError):
        PromptPreferenceRequest.model_validate(
            {
                "workflow_run_id": uuid4(),
                "workflow_revision": 1,
                "question_round": 1,
                "answers": {"3": {"mode": "B"}},
            },
            strict=True,
        )
    with pytest.raises(ValueError):
        PromptPreferenceRequest.model_validate(
            {
                "workflow_run_id": uuid4(),
                "workflow_revision": 1,
                "question_round": 1,
                "answers": {"platform": 4},
            },
            strict=True,
        )


@pytest.mark.parametrize(
    ("key", "expected"),
    [("valid-key_1", True), ("", False), ("contains space", False), ("\n", False)],
)
def test_idempotency_key_contract(key: str, expected: bool) -> None:
    assert valid_idempotency_key(key) is expected


@pytest.mark.parametrize("digest", ["AB" * 32, "g0" * 32, "ab" * 31, "ab" * 32 + "00"])
def test_prompt_callback_wire_rejects_noncanonical_sha256_hex(digest: str) -> None:
    payload = _prompt_wire_payload()
    payload["result_digest"] = digest

    with pytest.raises(ValueError):
        PromptCallbackRequest.model_validate_json(json.dumps(payload), strict=True)


def test_recommendation_callback_digest_is_hex_and_binds_synthetic_result() -> None:
    callback = RecommendationResultCallback(
        recommendation_id=uuid4(),
        reference_set_id=uuid4(),
        recommendation_revision=1,
        run_ref="mock-run:stable-id",
        status="ready",
        original_recommendation={"synthetic": True},
        public_options={"synthetic": True},
        image_types=IMAGE_TYPES,
        audit={"source": "synthetic_mock", "image_analysis_performed": False},
        state={"synthetic": True},
        form_ref="mock-form:stable-id",
        valid_until=datetime.now(UTC) + timedelta(hours=1),
        result_digest_version=1,
        result_digest="00" * 32,
    )
    payload = callback.model_dump(mode="python")
    payload["result_digest"] = recommendation_callback_digest(callback)
    validated = RecommendationResultCallback.model_validate(payload)

    assert validated.result_digest == recommendation_callback_digest(validated)
    assert len(validated.result_digest) == 64


def test_recommendation_callback_requires_explicit_synthetic_audit_shape() -> None:
    callback = RecommendationResultCallback(
        recommendation_id=uuid4(),
        reference_set_id=uuid4(),
        recommendation_revision=1,
        run_ref="mock-run:stable-id",
        status="ready",
        original_recommendation={"synthetic": True},
        public_options={"synthetic": True},
        image_types=IMAGE_TYPES,
        audit={"source": "real_dify"},
        state={},
        form_ref="mock-form:stable-id",
        valid_until=datetime.now(UTC) + timedelta(hours=1),
        result_digest_version=1,
        result_digest="00" * 32,
    )
    with pytest.raises(DomainError, match="回传内容无效"):
        RecommendationCallbackService._validate_synthetic_ready_payload(callback)
