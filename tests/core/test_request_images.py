"""Thumbnails: what is kept, what is refused, and what never raises."""

import base64
import io

from PIL import Image

from my_claude_code.core.anthropic import ImageInput
from my_claude_code.core.request_images import THUMBNAIL_MEDIA_TYPE, capture_images


def _png(size: tuple[int, int]) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _image(data: str | None = None, **kwargs) -> ImageInput:
    return ImageInput(kind="image", media_type="image/png", data=data, **kwargs)


def test_a_large_image_is_stored_as_a_small_thumbnail():
    source = _png((1600, 1200))

    captured = capture_images((_image(source),), max_pixels=512)[0]

    assert captured.width == 1600
    assert captured.height == 1200
    assert captured.thumbnail_media_type == THUMBNAIL_MEDIA_TYPE
    assert captured.thumbnail is not None
    with Image.open(io.BytesIO(captured.thumbnail)) as thumb:
        assert max(thumb.size) == 512
    assert len(captured.thumbnail) < len(base64.b64decode(source))


def test_the_same_image_gets_the_same_content_address():
    source = _png((64, 64))

    first, second = capture_images((_image(source), _image(source)), max_pixels=64)

    assert first.sha256 == second.sha256


def test_zero_pixels_records_the_image_without_storing_any():
    captured = capture_images((_image(_png((64, 64))),), max_pixels=0)[0]

    assert captured.thumbnail is None
    assert captured.source_bytes is not None


def test_capture_disabled_still_records_the_size():
    captured = capture_images(
        (_image(_png((64, 64))),), max_pixels=512, store_pixels=False
    )[0]

    assert captured.thumbnail is None
    assert captured.source_bytes is not None


def test_undecodable_bytes_are_still_recorded_as_an_image():
    captured = capture_images(
        (_image(base64.b64encode(b"not an image at all").decode("ascii")),),
        max_pixels=512,
    )[0]

    assert captured.thumbnail is None
    assert captured.source_bytes == 19


def test_invalid_base64_does_not_raise():
    captured = capture_images((_image("!!!! not base64 !!!!"),), max_pixels=512)[0]

    assert captured.thumbnail is None


def test_a_url_only_image_is_addressed_by_its_url():
    captured = capture_images(
        (ImageInput(kind="image", media_type=None, url="https://example.test/a.png"),),
        max_pixels=512,
    )[0]

    assert captured.thumbnail is None
    assert captured.sha256


def test_transparency_survives_the_thumbnail():
    buffer = io.BytesIO()
    Image.new("RGBA", (128, 128), (0, 0, 0, 0)).save(buffer, format="PNG")
    data = base64.b64encode(buffer.getvalue()).decode("ascii")

    captured = capture_images((_image(data),), max_pixels=64)[0]

    assert captured.thumbnail is not None
    with Image.open(io.BytesIO(captured.thumbnail)) as thumb:
        assert thumb.mode in ("RGBA", "P")


def test_a_small_image_is_not_scaled_up():
    captured = capture_images((_image(_png((40, 30))),), max_pixels=512)[0]

    assert captured.thumbnail is not None
    with Image.open(io.BytesIO(captured.thumbnail)) as thumb:
        assert thumb.size == (40, 30)
