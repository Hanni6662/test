from __future__ import annotations

import copy
import json

import pytest
from app.schemas.prompt_packages import PromptPackage
from app.schemas.tasks import TaskItemResponse, TaskResponse
from app.services.prompt_validation import (
    IMAGE_TYPES,
    PLATFORM_RULES,
    PromptPackageValidationError,
    validate_prompt_package,
)

_FIELD_NAMES = {
    "1": "语言种类",
    "2": "使用的电商平台",
    "3": "使用场景",
    "4": "视觉风格",
    "5": "商品卖点",
}


def _canvas_for(platform: str, image_type: str) -> str:
    if platform == "亚马逊高级A+":
        return "1464 x 1800 px" if image_type == IMAGE_TYPES[0] else "1464 x 600 px"
    if platform == "TEMU服饰类":
        return "3:4"
    if platform in {"淘宝", "京东", "拼多多", "1688"}:
        return "800 x 800 px"
    return "1:1"


def _package(
    image_types: list[str],
    *,
    platform: str = "亚马逊主图",
    auxiliary_count: int = 2,
    relation_overrides: dict[str, str] | None = None,
) -> tuple[dict[str, object], list[str], dict[str, str]]:
    auxiliary_ids = [f"AUX{index:02d}" for index in range(1, auxiliary_count + 1)]
    reference_ids = ["PRIMARY", *auxiliary_ids]
    relations = {
        aux_id: (relation_overrides or {}).get(aux_id, "supplement")
        for aux_id in auxiliary_ids
    }
    basis: dict[str, object] = {
        "product": "A light travel mug",
        "declared_category": "随行杯",
        "primary_view": "正面可见杯身和杯盖",
        "physical_profile": {
            "silhouette_proportions": "圆柱杯身，窄底宽口",
            "parts_count": "杯身与杯盖两部分",
            "openings_connections": "杯盖旋合在杯口",
            "material_appearance": "哑光金属外观",
            "color_zones": "杯身为蓝色",
            "pattern_logo_placement": "标识位于杯身正面",
            "visible_texture": "细腻哑光表面",
        },
        "auxiliary_relations": [
            {
                "id": aux_id,
                "view": "侧面细节",
                "relation": relation,
                "scope": "detail_only",
                "evidence": "仅供可见结构参考",
            }
            for aux_id, relation in relations.items()
        ],
        "asset_ids": {"primary": "PRIMARY", "auxiliary": auxiliary_ids},
        "identity_constraints": ["保留杯身轮廓", "保留杯盖连接方式", "保留蓝色色区"],
        "identity_prompt": "Keep the submitted product's shape and visible details.",
        "unknowns": ["未确认内部容量"],
        "preference_form": {
            key: {
                "name": name,
                "source": "PLATFORM" if key == "2" else "A",
                "value": platform if key == "2" else "中文商业表达",
            }
            for key, name in _FIELD_NAMES.items()
        },
        "image_types": list(image_types),
        "platform_profile": {
            "platform": platform,
            "rule": PLATFORM_RULES[platform],
            "canvases": [
                {"type": image_type, "canvas": _canvas_for(platform, image_type)}
                for image_type in image_types
            ],
        },
    }
    images = []
    for image_type in image_types:
        images.append(
            {
                "type": image_type,
                "canvas": _canvas_for(platform, image_type),
                "design_prompt": f"Create a clear composition for {image_type}.",
                "identity_prompt": basis["identity_prompt"],
                "resources": [
                    {"field": "1", "use": "按确认的语言表达文字"},
                    {"field": "2", "use": "采用所选平台画布和构图"},
                ],
                "reference_plan": {
                    "identity_anchor_id": "PRIMARY",
                    "view_reference_ids": auxiliary_ids,
                    "excluded_reference_ids": [],
                    "target_view": "杯身正面与杯盖连接处",
                },
            }
        )
    return {"basis": basis, "images": images}, reference_ids, relations


def _validate(package, references, relations, *, types=None, platform="亚马逊主图"):
    return validate_prompt_package(
        package,
        expected_image_types=(
            package["basis"]["image_types"] if types is None else types
        ),
        expected_platform=platform,
        legal_reference_ids=references,
        auxiliary_relation_by_id=relations,
    )


@pytest.mark.parametrize("count", [1, 3, 8])
def test_accepts_complete_ordered_packages_from_one_to_eight_images(count: int) -> None:
    selected = list(IMAGE_TYPES[:count])
    package, references, relations = _package(selected)

    normalized = _validate(package, references, relations)

    assert isinstance(normalized, PromptPackage)
    assert [item.type for item in normalized.images] == selected


def test_accepts_complete_deliver_json_string_and_preserves_prompt_text() -> None:
    package, references, relations = _package([IMAGE_TYPES[0]], auxiliary_count=1)
    exact_prompt = 'Retain “A/B” and the user\'s newline.\n第二行不应被裁剪。'
    package["images"][0]["design_prompt"] = exact_prompt
    before = copy.deepcopy(package)

    normalized = _validate(
        json.dumps(package, ensure_ascii=False),
        references,
        relations,
        types=[IMAGE_TYPES[0]],
    )

    assert normalized.images[0].design_prompt == exact_prompt
    assert package == before


def test_rejects_any_image_order_mismatch_as_a_whole_package() -> None:
    package, references, relations = _package(list(IMAGE_TYPES[:3]))
    package["images"][0], package["images"][1] = package["images"][1], package["images"][0]

    with pytest.raises(PromptPackageValidationError):
        _validate(package, references, relations)


@pytest.mark.parametrize(
    ("platform", "image_types", "expected_canvases"),
    [
        ("亚马逊高级A+", [IMAGE_TYPES[0], IMAGE_TYPES[1]], ["1464 x 1800 px", "1464 x 600 px"]),
        ("TEMU服饰类", [IMAGE_TYPES[2]], ["3:4"]),
        ("淘宝", [IMAGE_TYPES[3]], ["800 x 800 px"]),
    ],
)
def test_uses_published_platform_canvas_rules(
    platform: str, image_types: list[str], expected_canvases: list[str]
) -> None:
    package, references, relations = _package(image_types, platform=platform, auxiliary_count=0)

    normalized = _validate(package, references, relations, platform=platform)

    assert [item.canvas for item in normalized.images] == expected_canvases


def test_rejects_platform_profile_that_disagrees_with_task_snapshot() -> None:
    package, references, relations = _package([IMAGE_TYPES[0]], auxiliary_count=0)

    with pytest.raises(PromptPackageValidationError):
        _validate(package, references, relations, platform="TEMU")


def test_rejects_canvas_mismatch_between_basis_and_image() -> None:
    package, references, relations = _package([IMAGE_TYPES[0]], auxiliary_count=0)
    package["images"][0]["canvas"] = "3:4"

    with pytest.raises(PromptPackageValidationError):
        _validate(package, references, relations)

    package["images"][0]["canvas"] = "1:1"
    package["basis"]["platform_profile"]["canvases"][0]["canvas"] = "3:4"
    with pytest.raises(PromptPackageValidationError):
        _validate(package, references, relations)


def test_accepts_documented_prompt_length_boundaries_and_rejects_overflow() -> None:
    package, references, relations = _package([IMAGE_TYPES[0]], auxiliary_count=0)
    package["basis"]["identity_prompt"] = "I" * 3500
    package["images"][0]["identity_prompt"] = "I" * 3500
    package["images"][0]["design_prompt"] = "D" * 16000

    normalized = _validate(package, references, relations)
    assert len(normalized.images[0].design_prompt) == 16000

    package["images"][0]["design_prompt"] += "D"
    with pytest.raises(PromptPackageValidationError):
        _validate(package, references, relations)

    package["images"][0]["design_prompt"] = "D" * 16000
    package["basis"]["identity_prompt"] += "I"
    package["images"][0]["identity_prompt"] = package["basis"]["identity_prompt"]
    with pytest.raises(PromptPackageValidationError):
        _validate(package, references, relations)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda plan: plan.update(identity_anchor_id="AUX01"),
        lambda plan: plan.update(view_reference_ids=["AUX01", "AUX01"]),
        lambda plan: plan.update(excluded_reference_ids=["AUX99"]),
        lambda plan: plan.update(view_reference_ids=[], excluded_reference_ids=[]),
        lambda plan: plan.update(view_reference_ids=["AUX01"], excluded_reference_ids=["AUX02"]),
        lambda plan: plan.update(view_reference_ids=["AUX01"], excluded_reference_ids=["AUX01"]),
    ],
)
def test_rejects_invalid_anchor_reference_ids_and_auxiliary_partitions(mutate) -> None:
    package, references, relations = _package([IMAGE_TYPES[0]], auxiliary_count=2)
    mutate(package["images"][0]["reference_plan"])

    with pytest.raises(PromptPackageValidationError):
        _validate(package, references, relations)


def test_ignored_conflict_relation_does_not_filter_a_bound_reference() -> None:
    package, references, relations = _package(
        [IMAGE_TYPES[0]], auxiliary_count=1, relation_overrides={"AUX01": "ignored_conflict"}
    )

    normalized = _validate(package, references, relations)

    plan = normalized.images[0].reference_plan
    assert plan.view_reference_ids == ["AUX01"]
    assert plan.excluded_reference_ids == []


@pytest.mark.parametrize("auxiliary_count", [0, 1, 3])
def test_reference_plan_requires_every_bound_auxiliary_in_order(auxiliary_count: int) -> None:
    package, references, relations = _package([IMAGE_TYPES[0]], auxiliary_count=auxiliary_count)

    normalized = _validate(package, references, relations)

    assert normalized.images[0].reference_plan.view_reference_ids == references[1:]
    assert normalized.images[0].reference_plan.excluded_reference_ids == []


def test_reference_context_prevents_cross_task_asset_ids() -> None:
    package, references, relations = _package([IMAGE_TYPES[0]], auxiliary_count=1)

    with pytest.raises(PromptPackageValidationError):
        _validate(package, ["PRIMARY"], {})
    with pytest.raises(PromptPackageValidationError):
        _validate(package, ["PRIMARY", "AUX02"], {"AUX02": "supplement"})


def test_backend_rejects_legacy_package_without_task_bound_reference_schema() -> None:
    package, references, relations = _package([IMAGE_TYPES[0]], auxiliary_count=1)
    del package["basis"]["asset_ids"]

    with pytest.raises(PromptPackageValidationError):
        _validate(package, references, relations)


def test_resources_must_record_language_and_platform_preferences() -> None:
    package, references, relations = _package([IMAGE_TYPES[0]], auxiliary_count=0)
    package["images"][0]["resources"] = [{"field": "1", "use": "中文"}]

    with pytest.raises(PromptPackageValidationError):
        _validate(package, references, relations)


def test_json_duplicate_keys_and_unexpected_fields_are_rejected_safely() -> None:
    package, references, relations = _package([IMAGE_TYPES[0]], auxiliary_count=0)
    document = json.dumps(package, ensure_ascii=False)
    duplicate_basis = (
        '{"basis":'
        + json.dumps(package["basis"], ensure_ascii=False)
        + ","
        + document[1:]
    )
    with pytest.raises(PromptPackageValidationError) as caught:
        _validate(duplicate_basis, references, relations, types=[IMAGE_TYPES[0]])
    assert caught.value.code == "PROMPT_PACKAGE_INVALID"
    assert str(caught.value) == "提示词包校验失败"
    assert "Retain" not in str(caught.value)

    package["images"][0]["private_surprise"] = "unsupported"
    with pytest.raises(PromptPackageValidationError):
        _validate(package, references, relations)


def test_public_task_dtos_have_no_internal_prompt_or_basis_fields() -> None:
    private_fields = {
        "prompt",
        "design_prompt",
        "identity_prompt",
        "reference_plan",
        "basis",
        "audit",
        "snapshot",
    }

    for dto in (TaskItemResponse, TaskResponse):
        assert not private_fields.intersection(dto.model_fields)


@pytest.mark.parametrize("invalid_types", [[], list(IMAGE_TYPES) + [IMAGE_TYPES[0]], ["unknown"]])
def test_rejects_invalid_task_type_context(invalid_types: list[str]) -> None:
    package, references, relations = _package([IMAGE_TYPES[0]], auxiliary_count=0)

    with pytest.raises(PromptPackageValidationError):
        _validate(package, references, relations, types=invalid_types)


def test_rejects_noncanonical_task_type_order() -> None:
    selected = [IMAGE_TYPES[0], IMAGE_TYPES[2]]
    package, references, relations = _package(selected, auxiliary_count=0)

    with pytest.raises(PromptPackageValidationError):
        _validate(package, references, relations, types=list(reversed(selected)))
