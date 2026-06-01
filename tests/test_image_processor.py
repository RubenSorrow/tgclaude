"""Tests for tgclaude.image_processor.normalize_image.

All tests exercise the public async API and verify observable outputs:
returned bytes and media_type string. Real in-memory Pillow images are used
throughout — the normalization logic is never mocked.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from tgclaude.image_processor import MAX_LONG_EDGE_PX, normalize_image


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_png(width: int, height: int, mode: str = "RGB") -> bytes:
    """Return in-memory PNG bytes for an image of the given size and mode."""
    img = Image.new(mode, (width, height), color=(128, 64, 32) if mode == "RGB" else (128, 64, 32, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_webp(width: int, height: int) -> bytes:
    """Return in-memory WebP bytes for a small solid-colour image."""
    img = Image.new("RGB", (width, height), color=(10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="WEBP")
    return buf.getvalue()


def _open_jpeg(data: bytes) -> Image.Image:
    """Open JPEG bytes with Pillow and return the Image."""
    return Image.open(io.BytesIO(data))


# ---------------------------------------------------------------------------
# Resize behaviour
# ---------------------------------------------------------------------------


async def test_normalize_image_resizes_long_edge_to_1568px() -> None:
    """A 3000×2000 PNG must be downscaled so its long edge equals MAX_LONG_EDGE_PX."""
    data = _make_png(3000, 2000)

    jpeg_bytes, media_type = await normalize_image(data)

    img = _open_jpeg(jpeg_bytes)
    long_edge = max(img.width, img.height)
    assert long_edge <= MAX_LONG_EDGE_PX
    assert media_type == "image/jpeg"


async def test_normalize_image_preserves_aspect_ratio_on_resize() -> None:
    """Aspect ratio must be preserved after downscaling (within 1 px rounding)."""
    width, height = 3000, 2000
    data = _make_png(width, height)

    jpeg_bytes, _ = await normalize_image(data)

    img = _open_jpeg(jpeg_bytes)
    original_ratio = width / height
    output_ratio = img.width / img.height
    assert abs(original_ratio - output_ratio) < 0.02


# ---------------------------------------------------------------------------
# Transparency flattening
# ---------------------------------------------------------------------------


async def test_normalize_image_flattens_transparency_to_rgb() -> None:
    """An RGBA PNG with transparent pixels must be returned as an RGB JPEG (no alpha)."""
    # Create an RGBA image where some pixels are fully transparent.
    img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    img.paste(Image.new("RGBA", (50, 50), (255, 0, 0, 255)), (0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()

    jpeg_bytes, media_type = await normalize_image(data)

    result = _open_jpeg(jpeg_bytes)
    assert result.mode == "RGB"
    assert media_type == "image/jpeg"


# ---------------------------------------------------------------------------
# Already-small images
# ---------------------------------------------------------------------------


async def test_normalize_image_does_not_upscale_small_image() -> None:
    """An image whose long edge is already below MAX_LONG_EDGE_PX must not be enlarged."""
    small_width, small_height = 200, 150
    data = _make_png(small_width, small_height)

    jpeg_bytes, media_type = await normalize_image(data)

    img = _open_jpeg(jpeg_bytes)
    # Output dimensions must be at most the original (no upscaling).
    assert img.width <= small_width
    assert img.height <= small_height
    assert media_type == "image/jpeg"


async def test_normalize_image_small_image_still_returns_jpeg() -> None:
    """Even a tiny image (50×50) must be re-encoded as JPEG, not left as PNG."""
    data = _make_png(50, 50)

    jpeg_bytes, media_type = await normalize_image(data)

    # Verify the magic bytes are JPEG (FF D8 FF).
    assert jpeg_bytes[:3] == b"\xff\xd8\xff"
    assert media_type == "image/jpeg"


# ---------------------------------------------------------------------------
# Corrupt input
# ---------------------------------------------------------------------------


async def test_normalize_image_raises_value_error_on_corrupt_bytes() -> None:
    """Passing garbage bytes must raise ValueError, not propagate a Pillow exception."""
    with pytest.raises(ValueError):
        await normalize_image(b"not an image at all \x00\x01\x02")


async def test_normalize_image_raises_value_error_on_empty_bytes() -> None:
    """Empty byte string must also raise ValueError."""
    with pytest.raises(ValueError):
        await normalize_image(b"")


# ---------------------------------------------------------------------------
# Media type is always JPEG regardless of input format
# ---------------------------------------------------------------------------


async def test_normalize_image_returns_jpeg_media_type_for_png_input() -> None:
    """PNG input must produce media_type 'image/jpeg'."""
    data = _make_png(100, 100)

    _, media_type = await normalize_image(data)

    assert media_type == "image/jpeg"


async def test_normalize_image_returns_jpeg_media_type_for_webp_input() -> None:
    """WebP input must produce media_type 'image/jpeg'."""
    data = _make_webp(100, 100)

    _, media_type = await normalize_image(data)

    assert media_type == "image/jpeg"


async def test_normalize_image_output_bytes_are_valid_jpeg() -> None:
    """Output bytes must be parseable by Pillow as a JPEG regardless of input format."""
    data = _make_webp(300, 200)

    jpeg_bytes, _ = await normalize_image(data)

    img = _open_jpeg(jpeg_bytes)
    assert img.format == "JPEG"


# ---------------------------------------------------------------------------
# Typed domain exception classes (IMG-08, GAP-2b)
# ---------------------------------------------------------------------------


async def test_corrupt_bytes_raise_unsupported_image_error_not_generic_value_error() -> None:
    """Corrupt/unsupported bytes must raise UnsupportedImageError specifically,
    not just the base ValueError, so callers can distinguish the error type."""
    from tgclaude.image_processor import UnsupportedImageError

    with pytest.raises(UnsupportedImageError):
        await normalize_image(b"this is not an image \x00\x01\x02\x03")


async def test_empty_bytes_raise_unsupported_image_error() -> None:
    """An empty byte string must raise UnsupportedImageError."""
    from tgclaude.image_processor import UnsupportedImageError

    with pytest.raises(UnsupportedImageError):
        await normalize_image(b"")


async def test_still_too_large_after_resize_raises_image_too_large_error() -> None:
    """An image whose JPEG encoding still exceeds MAX_OUTPUT_BYTES after normalization
    must raise ImageTooLargeError (not a generic ValueError)."""
    from unittest.mock import patch

    from tgclaude.image_processor import ImageTooLargeError

    # Patch _assert_size_limit to unconditionally raise ImageTooLargeError,
    # simulating the post-resize size check failing on an adversarial input.
    def _force_overflow(data: bytes, limit: int) -> None:
        raise ImageTooLargeError(f"Normalized image is {len(data)} bytes, exceeding {limit}.")

    small_png = _make_png(100, 100)

    with patch("tgclaude.image_processor._assert_size_limit", side_effect=_force_overflow):
        with pytest.raises(ImageTooLargeError):
            await normalize_image(small_png)


async def test_image_too_large_error_is_subclass_of_value_error() -> None:
    """ImageTooLargeError must be a subclass of ValueError for backwards compatibility."""
    from tgclaude.image_processor import ImageTooLargeError

    assert issubclass(ImageTooLargeError, ValueError)


async def test_unsupported_image_error_is_subclass_of_value_error() -> None:
    """UnsupportedImageError must be a subclass of ValueError for backwards compatibility."""
    from tgclaude.image_processor import UnsupportedImageError

    assert issubclass(UnsupportedImageError, ValueError)


# ---------------------------------------------------------------------------
# Truncated / partially-corrupt images (lazy-decode barricade)
# ---------------------------------------------------------------------------


def _make_truncated_png(truncation_bytes: int = 100) -> bytes:
    """Return a real PNG with the last *truncation_bytes* removed.

    The resulting bytes have a valid PNG header (so Image.open() succeeds)
    but incomplete pixel data (so Pillow raises OSError during decode/resize).
    """
    buf = io.BytesIO()
    img = Image.new("RGB", (50, 50), color=(255, 0, 0))
    img.save(buf, format="PNG")
    return buf.getvalue()[:-truncation_bytes]


async def test_truncated_png_raises_unsupported_image_error() -> None:
    """A PNG with valid header but truncated pixel data must raise UnsupportedImageError.

    This guards the lazy-decode barricade: Image.open() only reads the header,
    so truncation is only detected during resize/encode.  The error must be
    wrapped as UnsupportedImageError, not a bare OSError propagating to the caller.
    """
    from tgclaude.image_processor import UnsupportedImageError

    truncated = _make_truncated_png(truncation_bytes=100)

    with pytest.raises(UnsupportedImageError):
        await normalize_image(truncated)
