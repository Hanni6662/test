from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from app.schemas.business_sessions import (
    BusinessSessionSelectionRequest,
    DifyOriginalRecommendation,
)
from app.schemas.recommendations import (
    IMAGE_TYPES,
    PLATFORMS,
    PreferenceChoice,
    PreferenceFieldOptions,
    RecommendationPublicOptions,
)
from app.schemas.tasks import (
    CustomPreference,
    PreferenceMode,
    TaskPreferences,
)

_FIELD_NAMES = (
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
_FIELD_KINDS: tuple[Literal["select", "paragraph"], ...] = (
    "select",
    "paragraph",
    "select",
    "select",
    "paragraph",
    "select",
    "paragraph",
    "select",
    "paragraph",
    "paragraph",
)
_GENERATED_FIELDS = (
    ("1", "language"),
    ("3", "usage_scene"),
    ("4", "visual_style"),
    ("5", "selling_points"),
)
_MAX_PREPARE_OPTION_LENGTH = 900
_MAX_CATEGORY_LENGTH = 100
_AGENT_DESIGN_OPTION = "D|||由视觉审核 Agent 自由设计"
_CUSTOM_OPTION = "custom|||自定义"


class ReferenceImageNotObserved(ValueError):
    """The analysis explicitly says the primary image was not available."""


_MISSING_IMAGE = re.compile(
    r"(?:主(?:参考)?图|PRIMARY(?:\s*主(?:参考)?图)?|实际图片|实际图像)\s*"
    r"(?:为空|缺失|不存在|未(?:提供|上传|附带|收到)(?!尺寸|材质|参数|证明|等级))|"
    r"(?:未|没有|无法)(?:收到|提供|上传|附带|看到|读取|查看).{0,18}"
    r"(?:主(?:参考)?图|PRIMARY|实际图片|实际图像|图片|图像)|"
    r"\b(?:no|missing)\s+(?:primary\s+)?(?:reference\s+)?(?:image|photo|picture)s?\b|"
    r"\b(?:image|photo|picture)s?\s+(?:is\s+|was\s+|were\s+|are\s+)?"
    r"(?:not\s+(?:attached|provided|received)|missing|absent)\b",
    re.IGNORECASE,
)


def _reject_unobserved_primary(audit: Mapping[str, object]) -> None:
    # Treat only explicit absence in the primary observation as a failure.
    # Unconfirmed material, dimensions, hidden surfaces or absent AUX images are normal.
    for key in ("summary", "primary_view"):
        value = audit.get(key)
        if not isinstance(value, str):
            continue
        for sentence in re.split(r"[。；;，,.\n]", value):
            mentions_auxiliary = re.search(r"辅(?:助|图)|\bAUX\d*\b|auxiliary", sentence, re.I)
            mentions_primary = re.search(r"主(?:参考)?图|\bPRIMARY\b", sentence, re.I)
            if mentions_auxiliary and not mentions_primary:
                continue
            if _MISSING_IMAGE.search(sentence):
                raise ReferenceImageNotObserved("primary reference image was not observed")


@dataclass(frozen=True)
class V34RecommendationData:
    original_recommendation: DifyOriginalRecommendation
    public_options: RecommendationPublicOptions


@dataclass(frozen=True)
class V34SelectionConversion:
    raw_request: BusinessSessionSelectionRequest
    normalized_preferences: TaskPreferences
    form_values: dict[str, str]


@dataclass(frozen=True)
class Form0FieldDefinition:
    name: str
    kind: Literal["select", "paragraph"]
    allowed_values: tuple[str, ...]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _load_strict_json(raw: str, *, label: str) -> object:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be valid JSON without duplicate keys") from exc


def _required_text(value: object, *, label: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{label} is empty or exceeds {max_length} characters")
    return normalized


def recommendation_from_prepare_audit(audit_json: str) -> V34RecommendationData:
    """Convert only the structured ``prepare.audit`` JSON into recommendation DTOs."""
    if not isinstance(audit_json, str):
        raise ValueError("prepare.audit must be a JSON string")
    audit = _load_strict_json(audit_json, label="prepare.audit")
    if not isinstance(audit, dict):
        raise ValueError("prepare.audit must contain a JSON object")

    _reject_unobserved_primary(audit)

    category = _required_text(
        audit.get("declared_category"),
        label="declared_category",
        max_length=_MAX_CATEGORY_LENGTH,
    )
    fields = audit.get("fields")
    if not isinstance(fields, list) or len(fields) != 5:
        raise ValueError("prepare.audit fields must contain exactly five entries")

    field_texts: dict[str, list[str]] = {}
    for index, (expected_id, field) in enumerate(
        zip(("1", "2", "3", "4", "5"), fields, strict=True),
        start=1,
    ):
        if not isinstance(field, dict) or set(field) != {"id", "options"}:
            raise ValueError(f"field {index} must contain only id and options")
        if field.get("id") != expected_id:
            raise ValueError("prepare.audit fields must be ordered with IDs 1 through 5")

        options = field.get("options")
        if expected_id == "2":
            if options != list(PLATFORMS):
                raise ValueError("field 2 options must match the fixed platform whitelist")
            continue

        if not isinstance(options, list) or len(options) != 3:
            raise ValueError(f"field {expected_id} must contain exactly three options")
        texts = [
            _required_text(
                option,
                label=f"field {expected_id} recommendation",
                max_length=_MAX_PREPARE_OPTION_LENGTH,
            )
            for option in options
        ]
        if len({text.strip() for text in texts}) != 3:
            raise ValueError(f"field {expected_id} recommendations must be unique")
        field_name = dict(_GENERATED_FIELDS)[expected_id]
        field_texts[field_name] = texts

    original_recommendation = DifyOriginalRecommendation(
        category_snapshot=category,
        field_texts=field_texts,
        image_types=list(IMAGE_TYPES),
    )

    public_field_options: dict[str, PreferenceFieldOptions] = {}
    for _, field_name in _GENERATED_FIELDS:
        public_field_options[field_name] = PreferenceFieldOptions(
            choices=[
                PreferenceChoice(code=code, text=text)
                for code, text in zip(("A", "B", "C"), field_texts[field_name], strict=True)
            ]
        )

    public_options = RecommendationPublicOptions(
        language=public_field_options["language"],
        platform=list(PLATFORMS),
        usage_scene=public_field_options["usage_scene"],
        visual_style=public_field_options["visual_style"],
        selling_points=public_field_options["selling_points"],
    )
    return V34RecommendationData(
        original_recommendation=original_recommendation,
        public_options=public_options,
    )


def _normalize_preference(
    selection: PreferenceMode | CustomPreference,
) -> PreferenceMode | CustomPreference:
    if isinstance(selection, CustomPreference):
        return CustomPreference(mode="custom", text=selection.text.strip())
    return selection


def _validated_recommendation_snapshot(
    recommendation: DifyOriginalRecommendation,
) -> DifyOriginalRecommendation:
    if not isinstance(recommendation, DifyOriginalRecommendation):
        raise ValueError("recommendation must be a DifyOriginalRecommendation")
    try:
        validated = DifyOriginalRecommendation.model_validate(
            recommendation.model_dump(mode="python"),
            strict=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("stored recommendation snapshot is invalid") from exc

    normalized_category = _required_text(
        validated.category_snapshot,
        label="stored recommendation category",
        max_length=_MAX_CATEGORY_LENGTH,
    )
    normalized_fields: dict[str, list[str]] = {}
    for field_name in (name for _, name in _GENERATED_FIELDS):
        texts = validated.field_texts[field_name]
        checked_texts = [
            _required_text(
                text,
                label=f"stored {field_name} recommendation",
                max_length=_MAX_PREPARE_OPTION_LENGTH,
            )
            for text in texts
        ]
        if len({text.strip() for text in checked_texts}) != 3:
            raise ValueError(f"stored {field_name} recommendations must be unique")
        normalized_fields[field_name] = checked_texts
    return DifyOriginalRecommendation(
        category_snapshot=normalized_category,
        field_texts=normalized_fields,
        image_types=list(IMAGE_TYPES),
    )


def _choice_and_custom_value(
    selection: PreferenceMode | CustomPreference,
    *,
    field_texts: list[str],
) -> tuple[str, str]:
    if isinstance(selection, CustomPreference):
        return _CUSTOM_OPTION, selection.text
    if selection.mode in ("A", "B", "C"):
        index = ("A", "B", "C").index(selection.mode)
        if len(field_texts) != 3:
            raise ValueError("stored recommendation must contain three choices")
        return f"{selection.mode}|||{field_texts[index]}", ""
    if selection.mode == "D":
        return _AGENT_DESIGN_OPTION, ""
    raise ValueError("unsupported preference selection")


def selection_to_form_0(
    request: BusinessSessionSelectionRequest,
    recommendation: DifyOriginalRecommendation,
) -> V34SelectionConversion:
    validated_recommendation = _validated_recommendation_snapshot(recommendation)
    preference_names = (
        ("language", "1"),
        ("usage_scene", "3"),
        ("visual_style", "4"),
        ("selling_points", "5"),
    )
    normalized = {
        name: _normalize_preference(getattr(request.preferences, name))
        for name, _ in preference_names
    }
    normalized_preferences = TaskPreferences(
        language=normalized["language"],
        platform=request.preferences.platform,
        usage_scene=normalized["usage_scene"],
        visual_style=normalized["visual_style"],
        selling_points=normalized["selling_points"],
    )

    selections: dict[str, tuple[str, str]] = {}
    for name, field_id in preference_names:
        stored_texts = validated_recommendation.field_texts.get(name)
        if stored_texts is None:
            raise ValueError(f"stored recommendation is missing {name}")
        selections[field_id] = _choice_and_custom_value(
            getattr(normalized_preferences, name),
            field_texts=stored_texts,
        )

    image_types = request.image_types
    if (
        not 1 <= len(image_types) <= len(IMAGE_TYPES)
        or len(image_types) != len(set(image_types))
        or any(image_type not in IMAGE_TYPES for image_type in image_types)
    ):
        raise ValueError("image types must be unique supported values")
    requested_image_types = set(image_types)
    selected_image_types = [
        image_type for image_type in IMAGE_TYPES if image_type in requested_image_types
    ]

    form_values = {
        "choice_1": selections["1"][0],
        "custom_1": selections["1"][1],
        "choice_2": f"PLATFORM|||{normalized_preferences.platform}",
        "choice_3": selections["3"][0],
        "custom_3": selections["3"][1],
        "choice_4": selections["4"][0],
        "custom_4": selections["4"][1],
        "choice_5": selections["5"][0],
        "custom_5": selections["5"][1],
        "image_types": json.dumps(
            selected_image_types,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    return V34SelectionConversion(
        raw_request=request,
        normalized_preferences=normalized_preferences,
        form_values=form_values,
    )


def form_0_definition_for_recommendation(
    recommendation: DifyOriginalRecommendation,
) -> tuple[Form0FieldDefinition, ...]:
    """Rebuild the exact allowed-value contract already checked against Dify GET."""
    validated = _validated_recommendation_snapshot(recommendation)
    values: dict[str, tuple[str, ...]] = {}
    for name, field_name in (
        ("choice_1", "language"),
        ("choice_3", "usage_scene"),
        ("choice_4", "visual_style"),
        ("choice_5", "selling_points"),
    ):
        choices = validated.field_texts[field_name]
        values[name] = tuple(
            [f"{letter}|||{text}" for letter, text in zip("ABC", choices, strict=True)]
            + [_AGENT_DESIGN_OPTION, _CUSTOM_OPTION]
        )
    values["choice_2"] = tuple(f"PLATFORM|||{platform}" for platform in PLATFORMS)
    values["image_types"] = tuple(IMAGE_TYPES)
    return tuple(
        Form0FieldDefinition(name=name, kind=kind, allowed_values=values.get(name, ()))
        for name, kind in zip(_FIELD_NAMES, _FIELD_KINDS, strict=True)
    )


def order_form_0_submission(form_values: Mapping[str, object]) -> dict[str, str]:
    """Restore the baseline DSL's ten-field order after JSONB persistence."""
    if set(form_values) != set(_FIELD_NAMES):
        raise ValueError("form_0 values must contain exactly the ten DSL fields")
    ordered: dict[str, str] = {}
    for name in _FIELD_NAMES:
        value = form_values[name]
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        ordered[name] = value
    return ordered


def validate_form_0_submission(
    form_values: Mapping[str, str],
    definition: Sequence[Form0FieldDefinition],
) -> None:
    if tuple(form_values.keys()) != _FIELD_NAMES:
        raise ValueError("form_0 values must contain the ten fields in DSL order")
    if len(definition) != len(_FIELD_NAMES):
        raise ValueError("form_0 definition must contain exactly ten fields")

    definitions: dict[str, Form0FieldDefinition] = {}
    for index, (item, expected_name, expected_kind) in enumerate(
        zip(definition, _FIELD_NAMES, _FIELD_KINDS, strict=True),
        start=1,
    ):
        if not isinstance(item, Form0FieldDefinition):
            raise ValueError(f"form_0 definition entry {index} has an invalid type")
        if item.name != expected_name or item.kind != expected_kind:
            raise ValueError("form_0 definition names, order, or kinds do not match the DSL")
        if not isinstance(item.allowed_values, tuple) or any(
            not isinstance(value, str) for value in item.allowed_values
        ):
            raise ValueError(f"{item.name} allowed_values must be a tuple of strings")
        definitions[item.name] = item

    for name in _FIELD_NAMES:
        if not isinstance(form_values[name], str):
            raise ValueError(f"{name} must be a string")

    for name in ("choice_1", "choice_2", "choice_3", "choice_4", "choice_5"):
        if form_values[name] not in definitions[name].allowed_values:
            raise ValueError(f"{name} is not one of its measured allowed_values")

    for field_id in ("1", "3", "4", "5"):
        choice_value = form_values[f"choice_{field_id}"]
        custom_value = form_values[f"custom_{field_id}"]
        if choice_value == _CUSTOM_OPTION:
            if not custom_value.strip():
                raise ValueError(f"custom_{field_id} must be non-empty for a custom choice")
        elif custom_value:
            raise ValueError(f"custom_{field_id} must be empty for a non-custom choice")

    raw_image_types = form_values["image_types"]
    selected_image_types = _load_strict_json(raw_image_types, label="image_types")
    if not isinstance(selected_image_types, list) or not 1 <= len(selected_image_types) <= 8:
        raise ValueError("image_types must be a JSON array containing one to eight values")
    if any(not isinstance(value, str) for value in selected_image_types):
        raise ValueError("image_types entries must be strings")
    if len(selected_image_types) != len(set(selected_image_types)):
        raise ValueError("image_types must not contain duplicates")
    image_type_options = definitions["image_types"].allowed_values
    if any(
        value not in IMAGE_TYPES or value not in image_type_options
        for value in selected_image_types
    ):
        raise ValueError("image_types contains a value outside its supported allowed_values")
    selected_set = set(selected_image_types)
    expected_order = [image_type for image_type in IMAGE_TYPES if image_type in selected_set]
    if selected_image_types != expected_order:
        raise ValueError("image_types must follow IMAGE_TYPES order")


__all__ = [
    "Form0FieldDefinition",
    "V34RecommendationData",
    "V34SelectionConversion",
    "form_0_definition_for_recommendation",
    "order_form_0_submission",
    "recommendation_from_prepare_audit",
    "selection_to_form_0",
    "validate_form_0_submission",
]
