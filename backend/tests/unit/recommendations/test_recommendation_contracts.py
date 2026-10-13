from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from app.db.recommendation_models import Recommendation
from app.integrations.dify import DifyNotConfigured, MockDifyAdapter, create_dify_adapter
from app.integrations.dify.mock import build_mock_options, build_mock_original
from app.schemas.recommendations import (
    IMAGE_TYPES,
    PLATFORMS,
    RecommendationCreateRequest,
    RecommendationPublicOptions,
)
from app.services.recommendations import (
    RECOMMENDATION_EVENT_TYPE,
    RecommendationOptions,
    RecommendationService,
    recommendation_handler_registration,
    recommendation_handlers,
)
from pydantic import ValidationError


def test_recommendation_request_rejects_any_field_other_than_reference_set_id() -> None:
    reference_set_id = uuid4()

    assert RecommendationCreateRequest(reference_set_id=reference_set_id).reference_set_id == (
        reference_set_id
    )
    with pytest.raises(ValidationError):
        RecommendationCreateRequest.model_validate(
            {"reference_set_id": str(reference_set_id), "owner_user_id": str(uuid4())}
        )


def test_public_enum_values_match_task_create_v2_exactly() -> None:
    contracts_dir = os.environ.get("TASK_CONTRACTS_DIR")
    schema_path = (
        Path(contracts_dir) / "task-create-v2.schema.json"
        if contracts_dir
        else Path(__file__).resolve().parents[4]
        / "docs"
        / "contracts"
        / "task-create-v2.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert list(PLATFORMS) == schema["$defs"]["platform"]["enum"]
    assert list(IMAGE_TYPES) == schema["$defs"]["image_type"]["enum"]
    assert "SKU展示图" in IMAGE_TYPES
    assert "SKU 展示图" not in IMAGE_TYPES
    assert RecommendationPublicOptions.model_validate(build_mock_options())


def test_mock_fixture_keeps_full_abcs_and_says_it_did_not_analyze_images() -> None:
    options = build_mock_options()
    original = build_mock_original("测试品类", options)

    assert original["fixture_id"] == "synthetic-recommendation-v1"
    assert original["field_texts"]["selling_points"] == [
        "突出可核验的外观与结构，不添加未证实信息。",
        "围绕已知用途和可见细节组织卖点表达。",
        "避免未提供的性能、认证、材质和尺寸描述。",
    ]
    assert len(original["image_types"]) == 8
    assert MockDifyAdapter().mode == "mock"


def test_unconfigured_real_mode_fails_without_mock_fallback() -> None:
    with pytest.raises(DifyNotConfigured):
        create_dify_adapter("real")
    assert isinstance(create_dify_adapter("mock"), MockDifyAdapter)


def test_recommendation_options_configure_simulation_only() -> None:
    options = RecommendationOptions()

    assert options.max_active_per_owner == 1
    assert options.simulated_form_ttl_days == 3
    with pytest.raises(ValueError):
        RecommendationOptions(max_active_per_owner=0)


def test_recommendation_handler_registration_matches_s4_contract() -> None:
    options = RecommendationOptions()
    adapter = MockDifyAdapter()

    registration = recommendation_handler_registration(options, adapter)

    assert registration.operation_versions == frozenset({1})
    handlers = recommendation_handlers(options, adapter)
    assert set(handlers) == {RECOMMENDATION_EVENT_TYPE}
    assert handlers[RECOMMENDATION_EVENT_TYPE].operation_versions == frozenset({1})
    assert callable(handlers[RECOMMENDATION_EVENT_TYPE].handler)


def test_public_projection_excludes_internal_payload_and_projects_expiry() -> None:
    now = datetime.now(UTC)
    row = Recommendation(
        id=uuid4(),
        owner_user_id=uuid4(),
        reference_set_id=uuid4(),
        status="ready",
        revision=1,
        original_recommendation={"basis": "private raw text"},
        public_options=build_mock_options(),
        image_types=list(IMAGE_TYPES),
        audit={"internal_analysis": "not public"},
        state={"provider_run": "secret run reference"},
        run_ref="mock:private",
        form_ref="mock-form:private",
        valid_until=now - timedelta(seconds=1),
        data_expires_at=now + timedelta(days=60),
        ready_at=now - timedelta(days=3),
    )

    response = RecommendationService._to_public_response(row, now=now)
    payload = response.model_dump(mode="json")

    assert response.status == "expired"
    assert response.options is None
    assert response.revision == 1
    assert set(payload) == {
        "recommendation_id",
        "reference_set_id",
        "status",
        "revision",
        "options",
        "image_types",
        "valid_until",
        "error",
    }
    assert not {"audit", "state", "run_ref", "form_ref", "original_recommendation"} & set(payload)
