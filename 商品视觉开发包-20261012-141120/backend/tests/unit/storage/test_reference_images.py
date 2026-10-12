from __future__ import annotations

import io

import pytest
from app.core.domain_errors import DomainError
from app.services.references import ReferenceService, ReferenceUploadFile
from PIL import Image, PngImagePlugin


def _png(color: tuple[int, int, int] = (240, 10, 20), *, size: tuple[int, int] = (8, 6)) -> bytes:
    image = Image.new("RGB", size, color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _service_without_database() -> ReferenceService:
    return object.__new__(ReferenceService)


def test_png_metadata_is_removed_from_model_copy() -> None:
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("comment", "private metadata")
    image = Image.new("RGB", (8, 6), (20, 30, 40))
    output = io.BytesIO()
    image.save(output, format="PNG", pnginfo=metadata)

    prepared = _service_without_database()._decode_image("PRIMARY", "image/png", output.getvalue())

    with Image.open(io.BytesIO(prepared.model_bytes)) as normalized:
        assert normalized.size == (8, 6)
        assert normalized.info == {}
    assert prepared.source_bytes == output.getvalue()


def test_exif_orientation_is_applied_and_metadata_removed() -> None:
    image = Image.new("RGB", (7, 4), (10, 100, 220))
    exif = Image.Exif()
    exif[274] = 6
    output = io.BytesIO()
    image.save(output, format="JPEG", exif=exif)

    prepared = _service_without_database()._decode_image("PRIMARY", "image/jpeg", output.getvalue())

    assert (prepared.model_width, prepared.model_height) == (4, 7)
    with Image.open(io.BytesIO(prepared.model_bytes)) as normalized:
        assert normalized.getexif() == {}


def test_declared_mime_must_match_signature() -> None:
    with pytest.raises(DomainError) as error:
        _service_without_database()._decode_image("PRIMARY", "image/jpeg", _png())

    assert error.value.status_code == 422
    assert error.value.code == "REFERENCE_IMAGE_INVALID"


def test_animated_png_is_rejected() -> None:
    frames = [Image.new("RGB", (2, 2), color) for color in ((10, 20, 30), (30, 20, 10))]
    output = io.BytesIO()
    frames[0].save(output, format="PNG", save_all=True, append_images=frames[1:])

    with pytest.raises(DomainError) as error:
        _service_without_database()._decode_image("PRIMARY", "image/png", output.getvalue())

    assert error.value.code == "REFERENCE_IMAGE_ANIMATED"


def test_group_duplicate_uses_normalized_content_digest() -> None:
    service = _service_without_database()
    duplicate = ReferenceUploadFile(io.BytesIO(_png()), "image/png")

    with pytest.raises(DomainError) as error:
        service._prepare_batch(duplicate, [ReferenceUploadFile(io.BytesIO(_png()), "image/png")])

    assert error.value.code == "DUPLICATE_REFERENCE_IMAGE"


def test_per_image_byte_limit_is_checked_while_reading() -> None:
    oversized = io.BytesIO(b"x" * (10 * 1024 * 1024 + 1))

    with pytest.raises(DomainError) as error:
        _service_without_database()._read_limited(oversized, "PRIMARY")

    assert error.value.status_code == 413
    assert error.value.code == "UPLOAD_TOO_LARGE"
