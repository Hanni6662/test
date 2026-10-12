from __future__ import annotations

import copy
import json
from uuid import uuid4

import pytest
from app.schemas.business_sessions import (
    BusinessSessionSelectionRequest,
    DifyOriginalRecommendation,
)
from app.schemas.recommendations import IMAGE_TYPES, PLATFORMS
from app.schemas.tasks import TaskPreferences
from app.services.chatflow_v34_conversion import (
    Form0FieldDefinition,
    V34RecommendationData,
    V34SelectionConversion,
    recommendation_from_prepare_audit,
    selection_to_form_0,
    validate_form_0_submission,
)

_GENERATED_IDS = ("1", "3", "4", "5")
_FIELD_TEXTS = {
    "1": ["简体中文：中文卖点信息", "英文：英文商业文案", "无文字：纯视觉表达"],
    "3": ["室内收纳场景", "户外使用场景", "通勤携带场景"],
    "4": ["简洁商业视觉", "明亮生活方式", "精致细节展示"],
    "5": ["可见蓝色外观", "清晰展示开合结构", "突出多个可见配件"],
}


def _audit_object() -> dict[str, object]:
    fields = []
    for field_id in ("1", "2", "3", "4", "5"):
        fields.append(
            {
                "id": field_id,
                "options": list(PLATFORMS) if field_id == "2" else list(_FIELD_TEXTS[field_id]),
            }
        )
    return {"declared_category": "家居收纳用品", "fields": fields}


def _audit_json(value: dict[str, object] | None = None) -> str:
    return json.dumps(value if value is not None else _audit_object(), ensure_ascii=False)


def _stored_recommendation() -> DifyOriginalRecommendation:
    converted = recommendation_from_prepare_audit(_audit_json())
    assert isinstance(converted, V34RecommendationData)
    return converted.original_recommendation


def _selection_request(
    *,
    modes: tuple[str, str, str, str] = ("A", "B", "C", "custom"),
    image_types: list[str] | None = None,
) -> BusinessSessionSelectionRequest:
    values = {
        "language": {"mode": modes[0]},
        "platform": "TEMU",
        "usage_scene": {"mode": modes[1]},
        "visual_style": {"mode": modes[2]},
        "selling_points": (
            {"mode": "custom", "text": "  仅展示图片可见的信息  "}
            if modes[3] == "custom"
            else {"mode": modes[3]}
        ),
    }
    return BusinessSessionSelectionRequest.model_validate(
        {
            "recommendation_id": str(uuid4()),
            "recommendation_revision": 7,
            "preferences": values,
            "image_types": image_types or [IMAGE_TYPES[5], IMAGE_TYPES[0], IMAGE_TYPES[2]],
        }
    )


def _form_definition(
    *,
    selected_values: dict[str, tuple[str, ...]] | None = None,
    kinds: dict[str, str] | None = None,
) -> tuple[Form0FieldDefinition, ...]:
    field_names = (
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
    select_names = {"choice_1", "choice_2", "choice_3", "choice_4", "choice_5"}
    values = selected_values or {}
    kind_overrides = kinds or {}
    return tuple(
        Form0FieldDefinition(
            name=name,
            kind=kind_overrides.get(name, "select" if name in select_names else "paragraph"),
            allowed_values=values.get(
                name,
                (
                    (
                        "A|||简体中文：中文卖点信息",
                        "B|||英文：英文商业文案",
                        "C|||无文字：纯视觉表达",
                        "D|||由视觉审核 Agent 自由设计",
                        "custom|||自定义",
                    )
                    if name == "choice_1"
                    else ("PLATFORM|||TEMU",)
                    if name == "choice_2"
                    else (
                        "A|||室内收纳场景",
                        "B|||户外使用场景",
                        "C|||通勤携带场景",
                        "D|||由视觉审核 Agent 自由设计",
                        "custom|||自定义",
                    )
                    if name == "choice_3"
                    else (
                        "A|||简洁商业视觉",
                        "B|||明亮生活方式",
                        "C|||精致细节展示",
                        "D|||由视觉审核 Agent 自由设计",
                        "custom|||自定义",
                    )
                    if name == "choice_4"
                    else (
                        "A|||可见蓝色外观",
                        "B|||清晰展示开合结构",
                        "C|||突出多个可见配件",
                        "D|||由视觉审核 Agent 自由设计",
                        "custom|||自定义",
                    )
                    if name == "choice_5"
                    else tuple(IMAGE_TYPES)
                    if name == "image_types"
                    else ()
                ),
            ),
        )
        for name in field_names
    )


def test_prepare_audit_maps_only_generated_fields_and_inserts_fixed_platforms() -> None:
    converted = recommendation_from_prepare_audit(_audit_json())

    assert isinstance(converted, V34RecommendationData)
    assert isinstance(converted.original_recommendation, DifyOriginalRecommendation)
    assert converted.original_recommendation.category_snapshot == "家居收纳用品"
    assert converted.original_recommendation.field_texts == {
        "language": _FIELD_TEXTS["1"],
        "usage_scene": _FIELD_TEXTS["3"],
        "visual_style": _FIELD_TEXTS["4"],
        "selling_points": _FIELD_TEXTS["5"],
    }
    assert converted.original_recommendation.image_types == list(IMAGE_TYPES)
    assert converted.public_options.platform == list(PLATFORMS)
    for name, field_id in (
        ("language", "1"),
        ("usage_scene", "3"),
        ("visual_style", "4"),
        ("selling_points", "5"),
    ):
        assert [choice.code for choice in getattr(converted.public_options, name).choices] == [
            "A",
            "B",
            "C",
        ]
        assert [choice.text for choice in getattr(converted.public_options, name).choices] == (
            _FIELD_TEXTS[field_id]
        )


def test_prepare_audit_text_matches_dsl_strip_and_post_strip_limits() -> None:
    audit = _audit_object()
    audit["declared_category"] = f"  {'品' * 100}  "
    for field in audit["fields"]:
        if field["id"] != "2":
            field["options"] = [f" \t{option}\n " for option in field["options"]]
    audit["fields"][2]["options"][0] = f"  {'场' * 900}  "

    converted = recommendation_from_prepare_audit(_audit_json(audit))

    assert converted.original_recommendation.category_snapshot == "品" * 100
    assert converted.original_recommendation.field_texts["language"] == _FIELD_TEXTS["1"]
    assert converted.original_recommendation.field_texts["usage_scene"][0] == "场" * 900
    assert len(converted.original_recommendation.field_texts["usage_scene"][0]) == 900


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        '{"declared_category":"A","declared_category":"B","fields":[]}',
        '{"declared_category":"A","fields":[{"id":"1","id":"3","options":[]}]}',
        '{"declared_category":"A","fields":[],"extra":NaN}',
    ],
)
def test_prepare_audit_rejects_invalid_json_duplicate_keys_and_non_json_constants(raw: str) -> None:
    with pytest.raises(ValueError):
        recommendation_from_prepare_audit(raw)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda audit: audit["fields"].pop(),
        lambda audit: audit["fields"].append({"id": "6", "options": ["x", "y", "z"]}),
        lambda audit: audit["fields"][1].update({"options": list(reversed(PLATFORMS))}),
        lambda audit: audit["fields"][1].update({"options": ["TEMU"] + list(PLATFORMS[1:])}),
        lambda audit: audit["fields"][1].update({"options": list(PLATFORMS[:-1])}),
        lambda audit: audit["fields"][0].update({"id": "3"}),
        lambda audit: audit["fields"].__setitem__(0, audit["fields"][2]),
        lambda audit: audit["fields"][2].update({"extra": "field"}),
        lambda audit: audit["fields"][2].update({"options": ["one", "one", "three"]}),
        lambda audit: audit["fields"][2].update({"options": ["one", "", "three"]}),
        lambda audit: audit["fields"][2].update({"options": ["x" * 901, "two", "three"]}),
    ],
)
def test_prepare_audit_rejects_wrong_fields_order_options_and_bounds(mutate) -> None:
    audit = copy.deepcopy(_audit_object())
    mutate(audit)
    with pytest.raises(ValueError):
        recommendation_from_prepare_audit(_audit_json(audit))


def test_selection_conversion_uses_stored_abc_and_trims_custom_without_mutating_inputs() -> None:
    request = _selection_request()
    recommendation = _stored_recommendation()
    request_before = request.model_dump(mode="json")
    recommendation_before = recommendation.model_dump(mode="json")

    converted = selection_to_form_0(request, recommendation)

    assert isinstance(converted, V34SelectionConversion)
    assert converted.raw_request.model_dump(mode="json") == request_before
    assert isinstance(converted.normalized_preferences, TaskPreferences)
    assert converted.normalized_preferences.selling_points.text == "仅展示图片可见的信息"
    assert converted.form_values == {
        "choice_1": f"A|||{_FIELD_TEXTS['1'][0]}",
        "custom_1": "",
        "choice_2": "PLATFORM|||TEMU",
        "choice_3": f"B|||{_FIELD_TEXTS['3'][1]}",
        "custom_3": "",
        "choice_4": f"C|||{_FIELD_TEXTS['4'][2]}",
        "custom_4": "",
        "choice_5": "custom|||自定义",
        "custom_5": "仅展示图片可见的信息",
        "image_types": json.dumps(
            [IMAGE_TYPES[0], IMAGE_TYPES[2], IMAGE_TYPES[5]],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    assert list(converted.form_values) == [
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
    ]
    assert "custom_2" not in converted.form_values
    assert request.model_dump(mode="json") == request_before
    assert recommendation.model_dump(mode="json") == recommendation_before


def test_selection_conversion_preserves_d_authorization_and_uses_fixed_platform_marker() -> None:
    request = _selection_request(modes=("D", "A", "B", "C"))
    converted = selection_to_form_0(request, _stored_recommendation())

    assert converted.form_values["choice_1"] == "D|||由视觉审核 Agent 自由设计"
    assert converted.form_values["custom_1"] == ""
    assert converted.form_values["choice_2"] == "PLATFORM|||TEMU"
    assert converted.form_values["choice_3"] == f"A|||{_FIELD_TEXTS['3'][0]}"
    assert converted.form_values["choice_4"] == f"B|||{_FIELD_TEXTS['4'][1]}"
    assert converted.form_values["choice_5"] == f"C|||{_FIELD_TEXTS['5'][2]}"
    assert all("D|||" not in values for values in _FIELD_TEXTS.values())


@pytest.mark.parametrize(
    "mutation",
    ["duplicate-option", "extra-option", "extra-field", "non-string", "over-limit"],
)
def test_selection_conversion_revalidates_mutated_nested_recommendation_data(mutation: str) -> None:
    request = _selection_request()
    recommendation = _stored_recommendation()
    if mutation == "duplicate-option":
        recommendation.field_texts["language"][1] = recommendation.field_texts["language"][0]
    elif mutation == "extra-option":
        recommendation.field_texts["language"].append("第四项")
    elif mutation == "extra-field":
        recommendation.field_texts["unexpected"] = ["x", "y", "z"]
    elif mutation == "non-string":
        recommendation.field_texts["language"][1] = None  # type: ignore[list-item]
    elif mutation == "over-limit":
        recommendation.field_texts["language"][1] = "超" * 901

    with pytest.raises(ValueError):
        selection_to_form_0(request, recommendation)


def test_form_submission_validates_normalized_definition_order_kinds_and_values() -> None:
    converted = selection_to_form_0(_selection_request(), _stored_recommendation())
    definition = _form_definition()

    assert validate_form_0_submission(converted.form_values, definition) is None


def test_form_submission_rejects_custom_choice_mismatch_and_blank_custom_value() -> None:
    converted = selection_to_form_0(_selection_request(), _stored_recommendation())
    definition = _form_definition()

    for custom_value in ("", "  \t "):
        invalid = {**converted.form_values, "custom_5": custom_value}
        with pytest.raises(ValueError):
            validate_form_0_submission(invalid, definition)

    invalid_non_custom = {**converted.form_values, "custom_1": "extra text"}
    with pytest.raises(ValueError):
        validate_form_0_submission(invalid_non_custom, definition)


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "missing",
        "reordered",
        "wrong-choice",
        "wrong-image-order",
        "bad-image-json",
        "non-string",
    ],
)
def test_form_submission_rejects_invalid_values_and_field_sets(mutation: str) -> None:
    converted = selection_to_form_0(_selection_request(), _stored_recommendation())
    values = dict(converted.form_values)
    definition = _form_definition()
    if mutation == "extra":
        values["unexpected"] = "x"
    elif mutation == "missing":
        values.pop("custom_1")
    elif mutation == "reordered":
        values = {"custom_1": values.pop("custom_1"), **values}
    elif mutation == "wrong-choice":
        values["choice_1"] = "A|||not in measured options"
    elif mutation == "wrong-image-order":
        values["image_types"] = json.dumps([IMAGE_TYPES[2], IMAGE_TYPES[0]], ensure_ascii=False)
    elif mutation == "bad-image-json":
        values["image_types"] = '["not-a-supported-image"]'
    elif mutation == "non-string":
        values["custom_3"] = 4  # type: ignore[assignment]

    with pytest.raises(ValueError):
        validate_form_0_submission(values, definition)


@pytest.mark.parametrize(
    "mutation",
    ["missing-definition", "wrong-order", "wrong-name", "wrong-kind", "bad-allowed-values"],
)
def test_form_submission_rejects_definition_that_does_not_match_measured_form(
    mutation: str,
) -> None:
    converted = selection_to_form_0(_selection_request(), _stored_recommendation())
    definition = list(_form_definition())
    if mutation == "missing-definition":
        definition.pop()
    elif mutation == "wrong-order":
        definition[0], definition[1] = definition[1], definition[0]
    elif mutation == "wrong-name":
        definition[0] = Form0FieldDefinition(
            name="choice_wrong", kind="select", allowed_values=definition[0].allowed_values
        )
    elif mutation == "wrong-kind":
        definition[1] = Form0FieldDefinition(name="custom_1", kind="select", allowed_values=())
    elif mutation == "bad-allowed-values":
        definition[0] = Form0FieldDefinition(
            name="choice_1", kind="select", allowed_values=("other",)
        )

    with pytest.raises(ValueError):
        validate_form_0_submission(converted.form_values, definition)


def test_form_submission_checks_image_type_subset_duplicates_and_canonical_order() -> None:
    converted = selection_to_form_0(_selection_request(), _stored_recommendation())
    values = dict(converted.form_values)

    for raw_types in (
        "[]",
        json.dumps([IMAGE_TYPES[0], IMAGE_TYPES[0]], ensure_ascii=False),
        json.dumps(["not-supported"], ensure_ascii=False),
        json.dumps([IMAGE_TYPES[1], IMAGE_TYPES[0]], ensure_ascii=False),
    ):
        invalid = {**values, "image_types": raw_types}
        with pytest.raises(ValueError):
            validate_form_0_submission(invalid, _form_definition())

    restricted_definition = _form_definition(selected_values={"image_types": (IMAGE_TYPES[0],)})
    invalid_against_definition = {
        **values,
        "image_types": json.dumps([IMAGE_TYPES[0], IMAGE_TYPES[2]]),
    }
    with pytest.raises(ValueError):
        validate_form_0_submission(invalid_against_definition, restricted_definition)
