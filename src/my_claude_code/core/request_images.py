"""Thumbnails for the images a request carried.

A pasted screenshot is megabytes of base64 and the same image is re-sent on
every turn of the conversation it belongs to, so storing originals would dwarf
the transcripts they arrive with. What the request detail actually needs is
"what was the model looking at", which a downscaled copy answers.

Nothing here raises. An image the decoder cannot read is still worth recording
as an image that arrived -- a request must never fail because its screenshot
was odd.
"""

import base64
import binascii
import hashlib
import io
from dataclasses import dataclass

from loguru import logger

from my_claude_code.core.anthropic import ImageInput

# Stored thumbnails are WebP: at the same visual quality it is roughly a third
# of a JPEG, and unlike JPEG it keeps the alpha channel a UI screenshot may use.
THUMBNAIL_FORMAT = "WEBP"
THUMBNAIL_MEDIA_TYPE = "image/webp"
_THUMBNAIL_QUALITY = 72
# Decoder guard. Pillow refuses images above this many pixels as a decompression
# bomb; the explicit number keeps the refusal ours and logged.
_MAX_SOURCE_PIXELS = 80_000_000


@dataclass(frozen=True, slots=True)
class CapturedImage:
    """One image a request carried, as it will be stored."""

    # Content address of the *source* bytes, so the same picture re-sent on
    # every turn of a conversation is stored once.
    sha256: str
    kind: str
    media_type: str | None
    source_bytes: int | None
    width: int | None = None
    height: int | None = None
    thumbnail: bytes | None = None
    thumbnail_media_type: str | None = None


def capture_images(
    images: tuple[ImageInput, ...],
    *,
    max_pixels: int,
    store_pixels: bool = True,
) -> tuple[CapturedImage, ...]:
    """Describe every image on a request, thumbnailing the ones we can read."""
    return tuple(
        _capture_one(image, max_pixels=max_pixels, store_pixels=store_pixels)
        for image in images
    )


def _capture_one(
    image: ImageInput, *, max_pixels: int, store_pixels: bool
) -> CapturedImage:
    raw = _decode(image.data)
    sha256 = hashlib.sha256(
        raw if raw is not None else (image.url or "").encode("utf-8")
    ).hexdigest()
    captured = CapturedImage(
        sha256=sha256,
        kind=image.kind,
        media_type=image.media_type,
        source_bytes=len(raw) if raw is not None else image.approx_bytes,
    )
    if raw is None or not store_pixels or max_pixels <= 0:
        return captured
    return _with_thumbnail(captured, raw, max_pixels=max_pixels)


def _with_thumbnail(
    captured: CapturedImage, raw: bytes, *, max_pixels: int
) -> CapturedImage:
    """Return the image with a downscaled copy, or unchanged if unreadable."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        logger.debug("Image capture skipped: Pillow is not installed")
        return captured
    try:
        with Image.open(io.BytesIO(raw)) as source:
            width, height = source.size
            if width * height > _MAX_SOURCE_PIXELS:
                logger.warning(
                    "Image thumbnail skipped: {}x{} exceeds the decode guard",
                    width,
                    height,
                )
                return captured
            thumbnail = source.copy()
        thumbnail.thumbnail((max_pixels, max_pixels))
        if thumbnail.mode not in ("RGB", "RGBA"):
            thumbnail = thumbnail.convert("RGBA" if "A" in thumbnail.mode else "RGB")
        buffer = io.BytesIO()
        thumbnail.save(buffer, format=THUMBNAIL_FORMAT, quality=_THUMBNAIL_QUALITY)
    except Exception as exc:
        # A truncated upload, an unsupported codec, a PDF sent as a document:
        # all still count as visual input, which is the fact worth keeping.
        logger.debug("Image thumbnail skipped: {}", exc)
        return captured
    return CapturedImage(
        sha256=captured.sha256,
        kind=captured.kind,
        media_type=captured.media_type,
        source_bytes=captured.source_bytes,
        width=width,
        height=height,
        thumbnail=buffer.getvalue(),
        thumbnail_media_type=THUMBNAIL_MEDIA_TYPE,
    )


def _decode(data: str | None) -> bytes | None:
    if not data:
        return None
    try:
        return base64.b64decode(data, validate=True)
    except binascii.Error, ValueError:
        return None


__all__ = [
    "THUMBNAIL_MEDIA_TYPE",
    "CapturedImage",
    "capture_images",
]
