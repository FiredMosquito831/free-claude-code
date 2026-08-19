"""Tests for the packaged MCC desktop icon assets.

Verifies the real shipped assets (app-icon.png/.ico/.icns, tray-icon.png)
decode correctly and are not the Pillow-generated placeholder monogram.
"""

import io
import struct
from pathlib import Path

import pytest
from PIL import Image
from PIL.IcoImagePlugin import IcoImageFile

from my_claude_code.cli.desktop_assets import (
    _placeholder_png,
    app_icon_bytes,
    export_app_icon,
    tray_icon_bytes,
)

_EXPECTED_ICO_SIZES = {16, 24, 32, 48, 64, 128, 256}


def test_app_icon_png_dimensions_and_alpha() -> None:
    data = app_icon_bytes(".png")
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (256, 256)
        assert image.mode == "RGBA"
        alpha_channel = image.getchannel("A")
        min_alpha, _max_alpha = alpha_channel.getextrema()
        assert min_alpha == 0, "expected real transparency in app-icon.png"


def test_tray_icon_png_dimensions_and_alpha() -> None:
    data = tray_icon_bytes()
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (128, 128)
        assert image.mode == "RGBA"
        alpha_channel = image.getchannel("A")
        min_alpha, _max_alpha = alpha_channel.getextrema()
        assert min_alpha == 0, "expected real transparency in tray-icon.png"


def test_app_icon_and_tray_icon_are_distinct_files() -> None:
    assert app_icon_bytes(".png") != tray_icon_bytes()


def test_app_icon_png_is_not_the_placeholder_monogram() -> None:
    real = app_icon_bytes(".png")
    placeholder = _placeholder_png()
    assert real != placeholder


def test_tray_icon_png_is_not_the_placeholder_monogram() -> None:
    real = tray_icon_bytes()
    placeholder = _placeholder_png()
    assert real != placeholder


def test_app_icon_ico_contains_expected_size_set() -> None:
    data = app_icon_bytes(".ico")
    with Image.open(io.BytesIO(data)) as image:
        assert isinstance(image, IcoImageFile)
        sizes = {size[0] for size in image.ico.sizes()}
        assert sizes == _EXPECTED_ICO_SIZES


def test_app_icon_icns_has_valid_magic_and_length_header() -> None:
    data = app_icon_bytes(".icns")
    assert data[:4] == b"icns"
    (declared_length,) = struct.unpack(">I", data[4:8])
    assert declared_length <= len(data)
    assert declared_length > 0


def test_export_app_icon_writes_png(tmp_path: Path) -> None:
    destination = tmp_path / "exported.png"
    export_app_icon(destination)
    assert destination.read_bytes() == app_icon_bytes(".png")


def test_export_app_icon_writes_ico(tmp_path: Path) -> None:
    destination = tmp_path / "exported.ico"
    export_app_icon(destination)
    assert destination.read_bytes() == app_icon_bytes(".ico")


def test_export_app_icon_writes_icns(tmp_path: Path) -> None:
    destination = tmp_path / "exported.icns"
    export_app_icon(destination)
    assert destination.read_bytes() == app_icon_bytes(".icns")


def test_app_icon_bytes_rejects_unsupported_suffix() -> None:
    with pytest.raises(ValueError, match="Unsupported app icon format"):
        app_icon_bytes(".bmp")
