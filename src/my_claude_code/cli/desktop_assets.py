"""Packaged visual assets owned by the MCC desktop shell.

The tray icon is a placeholder: a solid accent rounded square with an "MC"
monogram. A real ``app-icon.png`` ships under ``my_claude_code/assets``; when
it is missing, a matching placeholder is generated at runtime with Pillow.
That means the project owner can swap in the real logo later by dropping a
single file at that path -- no code change.
"""

import io
import os
from importlib.resources import files
from pathlib import Path

_ICON_FILES = {
    ".png": "app-icon.png",
}

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
    if normalized_suffix not in _ICON_FILES:
        supported = ", ".join(sorted(_ICON_FILES))
        raise ValueError(
            f"Unsupported app icon format {suffix!r}; expected one of: {supported}"
        )

    asset_path = files("my_claude_code").joinpath(
        "assets", _ICON_FILES[normalized_suffix]
    )
    try:
        return asset_path.read_bytes()
    except OSError:
        return _placeholder_png()


def export_app_icon(destination: Path) -> None:
    """Copy the packaged app icon to an installer-owned destination."""

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
