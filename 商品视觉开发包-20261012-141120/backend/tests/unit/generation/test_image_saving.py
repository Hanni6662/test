from __future__ import annotations

from io import BytesIO

import pytest
from app.services import generation_image_saving as image_saving
from app.services.generation_image_saving import (
    GenerationImageError,
    validate_and_render_generation_output,
)
from PIL import Image


def _encoded_image(
    image_format: str = "PNG",
    *,
    size: tuple[int, int] = (64, 32),
    color: tuple[int, ...] = (30, 80, 120),
) -> bytes:
    image = Image.new("RGB", size, color)
    output = BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def test_generation_image_is_validated_and_preview_is_deterministic_webp() -> None:
    raw = _encoded_image(size=(1500, 750))

    first = validate_and_render_generation_output(raw)
    second = validate_and_render_generation_output(raw)

    assert first.original_bytes == raw
    assert first.original_mime_type == "image/png"
    assert (first.width, first.height) == (1500, 750)
    assert first.original_sha256 == second.original_sha256
    assert first.preview_bytes == second.preview_bytes
    assert first.preview_sha256 == second.preview_sha256
    assert first.preview_mime_type == "image/webp"
    assert (first.preview_width, first.preview_height) == (1024, 512)
    with Image.open(BytesIO(first.preview_bytes)) as preview:
        assert preview.format == "WEBP"
        assert preview.size == (1024, 512)


def test_small_images_are_not_upscaled() -> None:
    rendered = validate_and_render_generation_output(_encoded_image(size=(80, 45)))

    assert (rendered.preview_width, rendered.preview_height) == (80, 45)


def test_exif_orientation_is_applied_to_reported_dimensions_and_preview() -> None:
    image = Image.new("RGB", (40, 20), (20, 40, 60))
    exif = Image.Exif()
    exif[274] = 6
    output = BytesIO()
    image.save(output, format="JPEG", exif=exif)

    rendered = validate_and_render_generation_output(output.getvalue())

    assert rendered.original_mime_type == "image/jpeg"
    assert (rendered.width, rendered.height) == (20, 40)
    assert (rendered.preview_width, rendered.preview_height) == (20, 40)


@pytest.mark.parametrize(
    ("raw", "limit", "reason"),
    [
        (b"", 1024, "GENERATION_IMAGE_EMPTY"),
        (b"not an image", 1024, "GENERATION_IMAGE_INVALID"),
        (b"0123456789", 4, "GENERATION_IMAGE_TOO_LARGE"),
    ],
)
def test_invalid_or_oversized_generation_bytes_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
    limit: int,
    reason: str,
) -> None:
    monkeypatch.setattr(image_saving, "MAX_GENERATION_IMAGE_BYTES", limit)

    with pytest.raises(GenerationImageError) as raised:
        validate_and_render_generation_output(raw)

    assert raised.value.reason_code == reason
    if raw:
        assert raw.decode("ascii", errors="ignore") not in str(raised.value)


def test_pixel_limit_is_checked_before_full_image_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _encoded_image(size=(11, 10))
    monkeypatch.setattr(image_saving, "MAX_GENERATION_IMAGE_PIXELS", 100)

    with pytest.raises(GenerationImageError) as raised:
        validate_and_render_generation_output(raw)

    assert raised.value.reason_code == "GENERATION_IMAGE_PIXEL_LIMIT"


def test_animated_images_are_rejected() -> None:
    first = Image.new("RGB", (8, 8), (255, 0, 0))
    second = Image.new("RGB", (8, 8), (0, 0, 255))
    output = BytesIO()
    first.save(output, format="PNG", save_all=True, append_images=[second], duration=10)

    with pytest.raises(GenerationImageError) as raised:
        validate_and_render_generation_output(output.getvalue())

    assert raised.value.reason_code == "GENERATION_IMAGE_ANIMATED"
