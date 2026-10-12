"""Bounded image validation and private preview rendering for generated output."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_GENERATION_IMAGE_BYTES = 32 * 1024 * 1024
MAX_GENERATION_IMAGE_PIXELS = 25_000_000
MAX_PREVIEW_EDGE = 1024
PREVIEW_WEBP_QUALITY = 84

_SOURCE_MIME_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


class GenerationImageError(ValueError):
    """Safe image validation error without source bytes or decoder details."""

    def __init__(self, reason_code: str) -> None:
        if reason_code not in {
            "GENERATION_IMAGE_EMPTY",
            "GENERATION_IMAGE_TOO_LARGE",
            "GENERATION_IMAGE_INVALID",
            "GENERATION_IMAGE_FORMAT_UNSUPPORTED",
            "GENERATION_IMAGE_ANIMATED",
            "GENERATION_IMAGE_PIXEL_LIMIT",
        }:
            raise ValueError("reason_code must be a generation image error code")
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class RenderedGenerationOutput:
    """Validated source image and a bounded, metadata-free WebP preview."""

    original_bytes: bytes
    original_sha256: bytes
    original_mime_type: str
    width: int
    height: int
    preview_bytes: bytes
    preview_sha256: bytes
    preview_mime_type: str
    preview_width: int
    preview_height: int


def validate_and_render_generation_output(raw: bytes) -> RenderedGenerationOutput:
    """Validate one bounded static raster and render a WebP preview without enlarging it."""

    if not isinstance(raw, bytes):
        raise GenerationImageError("GENERATION_IMAGE_INVALID")
    if not raw:
        raise GenerationImageError("GENERATION_IMAGE_EMPTY")
    if len(raw) > MAX_GENERATION_IMAGE_BYTES:
        raise GenerationImageError("GENERATION_IMAGE_TOO_LARGE")

    try:
        with Image.open(BytesIO(raw)) as header:
            source_mime_type = _SOURCE_MIME_TYPES.get(header.format or "")
            if source_mime_type is None:
                raise GenerationImageError("GENERATION_IMAGE_FORMAT_UNSUPPORTED")
            if getattr(header, "is_animated", False) or getattr(header, "n_frames", 1) != 1:
                raise GenerationImageError("GENERATION_IMAGE_ANIMATED")
            width, height = header.size
            _check_dimensions(width, height)
            header.verify()

        with Image.open(BytesIO(raw)) as decoded:
            oriented = ImageOps.exif_transpose(decoded)
            width, height = oriented.size
            _check_dimensions(width, height)
            oriented.load()
            has_alpha = "A" in oriented.getbands() or "transparency" in oriented.info
            mode = "RGBA" if has_alpha else "RGB"
            converted = oriented.convert(mode)
            clean = Image.new(mode, converted.size)
            clean.paste(converted)
            clean.thumbnail(
                (MAX_PREVIEW_EDGE, MAX_PREVIEW_EDGE),
                Image.Resampling.LANCZOS,
            )
            preview_width, preview_height = clean.size
            preview_buffer = BytesIO()
            clean.save(
                preview_buffer,
                format="WEBP",
                quality=PREVIEW_WEBP_QUALITY,
                method=6,
            )
            preview_bytes = preview_buffer.getvalue()
    except GenerationImageError:
        raise
    except Image.DecompressionBombError:
        raise GenerationImageError("GENERATION_IMAGE_PIXEL_LIMIT") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, OverflowError):
        raise GenerationImageError("GENERATION_IMAGE_INVALID") from None

    return RenderedGenerationOutput(
        original_bytes=raw,
        original_sha256=hashlib.sha256(raw).digest(),
        original_mime_type=source_mime_type,
        width=width,
        height=height,
        preview_bytes=preview_bytes,
        preview_sha256=hashlib.sha256(preview_bytes).digest(),
        preview_mime_type="image/webp",
        preview_width=preview_width,
        preview_height=preview_height,
    )


def _check_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise GenerationImageError("GENERATION_IMAGE_INVALID")
    if width * height > MAX_GENERATION_IMAGE_PIXELS:
        raise GenerationImageError("GENERATION_IMAGE_PIXEL_LIMIT")
