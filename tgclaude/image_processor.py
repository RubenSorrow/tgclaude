"""Image normalization for Claude vision input.

Converts arbitrary image bytes into a JPEG suitable for the Claude API:
- Long edge capped at MAX_LONG_EDGE_PX (1568 px), preserving aspect ratio.
- Transparency flattened onto a white background before JPEG encoding.
- Re-encoded at JPEG_QUALITY (85).
- Output must be under MAX_OUTPUT_BYTES (5 MB) or ValueError is raised.
"""

from __future__ import annotations

import asyncio
import io
import logging
from functools import partial

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain exceptions
# ---------------------------------------------------------------------------


class ImageTooLargeError(ValueError):
    """Raised when the normalized image exceeds the output size limit."""


class UnsupportedImageError(ValueError):
    """Raised when the image format is unsupported or the file is corrupt."""


# ---------------------------------------------------------------------------
# Constants (from §IMG-05 spec)
# ---------------------------------------------------------------------------

MAX_LONG_EDGE_PX: int = 1568
JPEG_QUALITY: int = 85
MAX_OUTPUT_BYTES: int = 5_000_000
_JPEG_MEDIA_TYPE: str = "image/jpeg"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def normalize_image(data: bytes) -> tuple[bytes, str]:
    """Normalize raw image bytes for Claude vision input.

    Returns (jpeg_bytes, media_type) where media_type is always "image/jpeg".

    Raises:
        ValueError: if the image is unsupported/corrupt or output exceeds 5 MB.
    """
    loop = asyncio.get_running_loop()
    jpeg_bytes = await loop.run_in_executor(None, partial(_normalize_sync, data))
    return jpeg_bytes, _JPEG_MEDIA_TYPE


# ---------------------------------------------------------------------------
# CPU-bound synchronous implementation (runs in executor)
# ---------------------------------------------------------------------------


def _normalize_sync(data: bytes) -> bytes:
    """Decode, resize, flatten, and re-encode *data* as JPEG.

    Executed in a thread-pool executor — must not touch the event loop.

    Barricade: any Pillow error from any stage of the pipeline (open, load,
    resize, encode) is caught here and re-raised as a domain exception.
    Pillow decodes lazily — Image.open() only reads the header, so truncated
    or partially-corrupt images may only fail during resize or save.  We must
    therefore guard the entire pipeline, not just the open call.
    """
    try:
        image = _decode_image(data)
        image = _resize_to_long_edge(image, MAX_LONG_EDGE_PX)
        image = _flatten_transparency(image)
        jpeg_bytes = _encode_as_jpeg(image, JPEG_QUALITY)
    except (ImageTooLargeError, UnsupportedImageError):
        # Already a domain exception — let it through unchanged.
        raise
    except OSError as exc:
        # Covers "image file is truncated", "broken data stream", and other
        # I/O-level Pillow errors that surface during lazy decode/resize/encode.
        raise UnsupportedImageError("truncated or corrupt image data") from exc
    except Exception as exc:
        # Catch-all for any other Pillow internals that escape the specific
        # cases above (e.g. struct.error for malformed chunk data).
        raise UnsupportedImageError("image processing failed") from exc
    _assert_size_limit(jpeg_bytes, MAX_OUTPUT_BYTES)
    logger.debug(
        "normalize_image: output %d bytes (%dx%d)",
        len(jpeg_bytes),
        image.width,
        image.height,
    )
    return jpeg_bytes


def _decode_image(data: bytes) -> Image.Image:
    """Open raw bytes as a PIL Image.

    Raises:
        ValueError: if Pillow cannot identify the format.
    """
    try:
        return Image.open(io.BytesIO(data))
    except UnidentifiedImageError as exc:
        raise UnsupportedImageError("unsupported or corrupt image") from exc


def _resize_to_long_edge(image: Image.Image, max_long_edge: int) -> Image.Image:
    """Downscale *image* so its longest dimension is at most *max_long_edge* px.

    Uses LANCZOS resampling for quality. Returns the original if no resize needed.
    """
    long_edge = max(image.width, image.height)
    if long_edge <= max_long_edge:
        return image

    scale = max_long_edge / long_edge
    new_size = (round(image.width * scale), round(image.height * scale))
    return image.resize(new_size, Image.Resampling.LANCZOS)


def _flatten_transparency(image: Image.Image) -> Image.Image:
    """Composite image onto a white background to eliminate alpha channels.

    JPEG cannot encode transparency; RGBA and palette images with transparency
    must be flattened first. Returns the original if already opaque.
    """
    if image.mode not in ("RGBA", "P"):
        return image

    # Convert palette images to RGBA so we can composite uniformly.
    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    background.paste(rgba, mask=rgba.split()[3])  # alpha channel as mask
    return background.convert("RGB")


def _encode_as_jpeg(image: Image.Image, quality: int) -> bytes:
    """Encode *image* to JPEG bytes at the given *quality* level."""
    # Ensure RGB — JPEG encoder rejects other modes (e.g. pure LA or RGBA).
    rgb = image.convert("RGB") if image.mode != "RGB" else image
    buffer = io.BytesIO()
    rgb.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _assert_size_limit(data: bytes, limit: int) -> None:
    """Raise ValueError if *data* exceeds *limit* bytes.

    Even after a resize pass the JPEG output could theoretically exceed the
    limit for pathological inputs (e.g. a 1568×1568 image with extreme detail).
    """
    if len(data) > limit:
        raise ImageTooLargeError(
            f"Normalized image is {len(data)} bytes, exceeding the {limit}-byte limit."
        )
