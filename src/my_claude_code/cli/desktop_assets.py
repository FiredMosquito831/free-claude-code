"""Packaged visual assets owned by the MCC desktop shell.

Two real brand assets ship under ``my_claude_code/assets``:

- ``app-icon.*`` (10% margin mark) for windows, taskbar, and app icons, in
  ``.png``, ``.ico`` (multi-size), and ``.icns`` (multi-size) formats.
- ``tray-icon.png`` (2% margin, same mark) sized for 16-24px tray rendering.

If either file is missing at runtime, a placeholder "MC" monogram is
generated with Pillow so the tray never launches without an icon. That
fallback should be unreachable in normal operation now that the real files
are packaged.
"""

import io
import os
from importlib.resources import files
from pathlib import Path

_APP_ICON_FILES = {
    ".png": "app-icon.png",
    ".ico": "app-icon.ico",
    ".icns": "app-icon.icns",
}

_TRAY_ICON_FILE = "tray-icon.png"

# Accent color drawn from the admin UI's "midnight" theme accent. The real
# logo replaces the asset, not this fallback's palette.
_ACCENT = (108, 141, 255, 255)
_TEXT = (255, 255, 255, 255)


def app_icon_bytes(suffix: str) -> bytes:
    """Read the packaged app icon matching a native file suffix.

    Falls back to a generated placeholder when the asset file has not shipped,
    so the tray never launches without an icon.
    """

    normalized_suffix = suffix.lower()
    if normalized_suffix not in _APP_ICON_FILES:
        supported = ", ".join(sorted(_APP_ICON_FILES))
        raise ValueError(
            f"Unsupported app icon format {suffix!r}; expected one of: {supported}"
        )

    asset_path = files("my_claude_code").joinpath(
        "assets", _APP_ICON_FILES[normalized_suffix]
    )
    try:
        return asset_path.read_bytes()
    except OSError:
        if normalized_suffix != ".png":
            # The placeholder generator only produces a PNG. Returning those
            # bytes for a ".ico"/".icns" request would write an invalid,
            # unusable icon file (Windows/macOS both refuse to load a PNG
            # with the wrong container), so fail loudly instead of shipping
            # a broken shortcut icon.
            raise FileNotFoundError(
                f"Packaged app icon {_APP_ICON_FILES[normalized_suffix]!r} is "
                f"missing and no {normalized_suffix} placeholder can be "
                "generated (only a .png placeholder is supported)."
            ) from None
        return _placeholder_png()


def tray_icon_bytes() -> bytes:
    """Read the packaged tray icon (2% margin variant for 16-24px rendering).

    Falls back to a generated placeholder when the asset file has not
    shipped, so the tray never launches without an icon.
    """

    asset_path = files("my_claude_code").joinpath("assets", _TRAY_ICON_FILE)
    try:
        return asset_path.read_bytes()
    except OSError:
        return _placeholder_png()


def export_app_icon(destination: Path) -> None:
    """Copy the packaged app icon to an installer-owned destination.

    ``destination.suffix`` selects the source format (``.png``, ``.ico``, or
    ``.icns``); the bytes are copied as-is, no conversion is performed.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(app_icon_bytes(destination.suffix))


def _placeholder_png() -> bytes:
    """Render a 128x128 RGBA placeholder icon and encode it as PNG.

    Pillow is a hard runtime dependency of the project, but the import stays
    local so asset reads on the happy path never pay for it.
    """

    from PIL import Image, ImageDraw

    size = 128
    radius = 28
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, size - 4, size - 4), radius=radius, fill=_ACCENT)

    font = _monogram_font()
    text = "MC"
    if font is not None:
        bbox = draw.textbbox((0, 0), text, font=font)
        position = (
            (size - (bbox[2] - bbox[0])) / 2 - bbox[0],
            (size - (bbox[3] - bbox[1])) / 2 - bbox[1],
        )
        draw.text(position, text, fill=_TEXT, font=font)
    else:
        draw.text((size / 2, size / 2), text, fill=_TEXT, anchor="mm")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _monogram_font():
    """Return a bundled bold font, or None to fall back to the bitmap default."""

    from PIL import ImageFont

    for size in (72, 64, 56):
        for candidate in _font_candidates():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default()
    except OSError:
        return None


def _font_candidates() -> tuple[str, ...]:
    if os.name == "nt":
        return (
            r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\arial.ttf",
        )
    return (
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
