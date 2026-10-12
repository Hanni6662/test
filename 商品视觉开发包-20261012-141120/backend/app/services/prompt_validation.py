from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import ValidationError

from app.schemas.prompt_packages import PromptPackage

IMAGE_TYPES: Final[tuple[str, ...]] = (
    "品牌导购主视觉",
    "核心卖点信息图",
    "生活方式海报",
    "多场景展示图",
    "场景化卖点图",
    "使用体验图",
    "SKU展示图",
    "细节展示图",
)
PLATFORMS: Final[tuple[str, ...]] = (
    "亚马逊主图",
    "亚马逊高级A+",
    "TEMU",
    "TEMU服饰类",
    "TIKTOK",
    "淘宝",
    "京东",
    "拼多多",
    "1688",
)
PLATFORM_RULES: Final[dict[str, str]] = {
    "亚马逊主图": (
        "整套采用 1:1。品牌导购主视觉若作为商品首图，使用纯白背景，不添加文字、边框、水印或装饰；"
        "其他图片可按职责展示场景、细节与信息层级。"
    ),
    "亚马逊高级A+": (
        "品牌导购主视觉采用 1464 x 1800 px 纵向沉浸式构图；"
        "其余图片采用 1464 x 600 px 横向模块化构图。"
    ),
    "TEMU": "整套采用 1:1，商品清晰完整且主体突出；按图片职责使用场景、细节与简洁信息表达。",
    "TEMU服饰类": (
        "整套采用 3:4 竖向构图，完整表现服饰轮廓，并为人物动作、穿着状态和场景留出空间。"
    ),
    "TIKTOK": "整套采用 1:1，商品清晰完整；辅助图片强调真实使用、动作感、多角度和快速理解。",
    "淘宝": (
        "整套采用 800 x 800 px 方形画布，商品主体清晰、完整、易辨识；"
        "避免与商品无关的元素干扰主体。"
    ),
    "京东": (
        "整套采用 800 x 800 px 方形画布，商品主体清晰、完整、占据视觉中心；"
        "根据图片职责控制场景与信息层级。"
    ),
    "拼多多": (
        "整套采用 800 x 800 px 方形画布，商品主体清晰且容易快速识别；"
        "装饰与文字不得遮挡商品关键结构。"
    ),
    "1688": (
        "整套采用 800 x 800 px 方形画布，以清晰呈现商品外观、结构与可见细节为主；"
        "背景与信息表达应服务于供货商品的快速判断。"
    ),
}
_RELATIONS: Final[frozenset[str]] = frozenset(
    {"confirm", "supplement", "ignored_conflict"}
)
_PREFERENCE_NAMES: Final[dict[str, str]] = {
    "1": "语言种类",
    "2": "使用的电商平台",
    "3": "使用场景",
    "4": "视觉风格",
    "5": "商品卖点",
}
_MAX_PACKAGE_BYTES: Final[int] = 256_000
_MAX_ENCODED_PACKAGE_BYTES: Final[int] = 3 * _MAX_PACKAGE_BYTES
PROMPT_PACKAGE_VALIDATOR_VERSION: Final[str] = "prompt-package-validator-v3.3-r2-all-references"
PROMPT_PACKAGE_LEGACY_VALIDATOR_VERSIONS: Final[frozenset[str]] = frozenset(
    {"prompt-package-validator-v3.3-r1"}
)


class PromptPackageValidationError(ValueError):
    """Safe validation failure that deliberately carries no submitted content."""

    code = "PROMPT_PACKAGE_INVALID"
    message = "提示词包校验失败"

    def __init__(self) -> None:
        super().__init__(self.message)


class _DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _decode_complete_package(raw: object) -> object:
    if isinstance(raw, str):
        try:
            encoded = raw.encode("utf-8")
            if len(encoded) > _MAX_ENCODED_PACKAGE_BYTES:
                raise PromptPackageValidationError
            return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        except PromptPackageValidationError:
            raise
        except (UnicodeEncodeError, json.JSONDecodeError, _DuplicateJsonKey, ValueError):
            raise PromptPackageValidationError from None
    return raw


def _canvas_for(platform: str, image_type: str) -> str:
    if platform == "亚马逊高级A+":
        return "1464 x 1800 px" if image_type == IMAGE_TYPES[0] else "1464 x 600 px"
    if platform == "TEMU服饰类":
        return "3:4"
    if platform in {"淘宝", "京东", "拼多多", "1688"}:
        return "800 x 800 px"
    return "1:1"


def _context_types(expected_image_types: Sequence[str]) -> tuple[str, ...]:
    if (
        isinstance(expected_image_types, (str, bytes))
        or not isinstance(expected_image_types, Sequence)
    ):
        raise PromptPackageValidationError
    values = tuple(expected_image_types)
    if (
        not 1 <= len(values) <= 8
        or len(values) != len(set(values))
        or any(value not in IMAGE_TYPES for value in values)
        or values != tuple(image_type for image_type in IMAGE_TYPES if image_type in values)
    ):
        raise PromptPackageValidationError
    return values


def _context_references(
    legal_reference_ids: Sequence[str], auxiliary_relation_by_id: Mapping[str, str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if (
        isinstance(legal_reference_ids, (str, bytes))
        or not isinstance(legal_reference_ids, Sequence)
        or not isinstance(auxiliary_relation_by_id, Mapping)
    ):
        raise PromptPackageValidationError
    references = tuple(legal_reference_ids)
    if not 1 <= len(references) <= 4:
        raise PromptPackageValidationError
    expected = ("PRIMARY", *(f"AUX{index:02d}" for index in range(1, len(references))))
    if references != expected:
        raise PromptPackageValidationError
    expected_auxiliary = expected[1:]
    if set(auxiliary_relation_by_id) != set(expected_auxiliary):
        raise PromptPackageValidationError
    if any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or value not in _RELATIONS
        for key, value in auxiliary_relation_by_id.items()
    ):
        raise PromptPackageValidationError
    return references, expected_auxiliary


def _validate_correlations(
    package: PromptPackage,
    *,
    expected_image_types: tuple[str, ...],
    expected_platform: str,
    legal_reference_ids: tuple[str, ...],
    auxiliary_reference_ids: tuple[str, ...],
    auxiliary_relation_by_id: Mapping[str, str],
) -> None:
    basis = package.basis
    basis_types = tuple(basis.image_types)
    image_types = tuple(image.type for image in package.images)
    if basis_types != expected_image_types or image_types != expected_image_types:
        raise PromptPackageValidationError

    if expected_platform not in PLATFORMS:
        raise PromptPackageValidationError
    profile = basis.platform_profile
    if (
        profile.platform != expected_platform
        or profile.rule != PLATFORM_RULES[expected_platform]
        or len(profile.canvases) != len(expected_image_types)
    ):
        raise PromptPackageValidationError

    preference_form = basis.preference_form
    if set(preference_form) != set(_PREFERENCE_NAMES):
        raise PromptPackageValidationError
    for key, name in _PREFERENCE_NAMES.items():
        preference = preference_form[key]
        if preference.name != name:
            raise PromptPackageValidationError
        if key == "2":
            if preference.source != "PLATFORM" or preference.value != expected_platform:
                raise PromptPackageValidationError
        elif preference.source == "PLATFORM":
            raise PromptPackageValidationError

    if (
        basis.asset_ids.primary != "PRIMARY"
        or tuple(basis.asset_ids.auxiliary) != auxiliary_reference_ids
    ):
        raise PromptPackageValidationError
    if tuple(entry.id for entry in basis.auxiliary_relations) != auxiliary_reference_ids:
        raise PromptPackageValidationError
    if len(basis.auxiliary_relations) != len(auxiliary_relation_by_id):
        raise PromptPackageValidationError
    for relation in basis.auxiliary_relations:
        if relation.relation != auxiliary_relation_by_id[relation.id]:
            raise PromptPackageValidationError

    for position, (image, image_type) in enumerate(
        zip(package.images, expected_image_types, strict=True)
    ):
        expected_canvas = _canvas_for(expected_platform, image_type)
        profile_canvas = profile.canvases[position]
        if (
            profile_canvas.type != image_type
            or profile_canvas.canvas != expected_canvas
            or image.canvas != expected_canvas
        ):
            raise PromptPackageValidationError
        if image.identity_prompt != basis.identity_prompt:
            raise PromptPackageValidationError

        resource_fields = {resource.field for resource in image.resources}
        if not {"1", "2"}.issubset(resource_fields):
            raise PromptPackageValidationError
        if any(resource.field not in preference_form for resource in image.resources):
            raise PromptPackageValidationError

        plan = image.reference_plan
        view_ids = tuple(plan.view_reference_ids)
        excluded_ids = tuple(plan.excluded_reference_ids)
        if plan.identity_anchor_id != "PRIMARY":
            raise PromptPackageValidationError
        if view_ids != auxiliary_reference_ids or excluded_ids:
            raise PromptPackageValidationError


def validate_prompt_package(
    raw: object,
    *,
    expected_image_types: Sequence[str],
    expected_platform: str,
    legal_reference_ids: Sequence[str],
    auxiliary_relation_by_id: Mapping[str, str],
) -> PromptPackage:
    """Validate one complete internal v3.3 package against persisted task context.

    `raw` may be the JSON object or the complete JSON string emitted as
    `deliver.package`. Markdown answers, SSE fragments, database access, network
    calls, and model calls are deliberately outside this function.
    """
    try:
        ordered_types = _context_types(expected_image_types)
        references, auxiliary = _context_references(
            legal_reference_ids, auxiliary_relation_by_id
        )
        decoded = _decode_complete_package(raw)
        package = PromptPackage.model_validate(decoded)
        canonical = json.dumps(
            package.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(canonical.encode("utf-8")) > _MAX_PACKAGE_BYTES:
            raise PromptPackageValidationError
        _validate_correlations(
            package,
            expected_image_types=ordered_types,
            expected_platform=expected_platform,
            legal_reference_ids=references,
            auxiliary_reference_ids=auxiliary,
            auxiliary_relation_by_id=auxiliary_relation_by_id,
        )
        return package
    except PromptPackageValidationError:
        raise
    except (ValidationError, TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise PromptPackageValidationError from None
