"""Find every Claude Code settings.json this machine can reach, and say where it is.

A machine that runs WSL has two Claude Code installations and two
``~/.claude/settings.json`` files, and configuring the wrong one is the single
most common reason a proxy setting "does not apply". The dashboard cannot fix
that by guessing; it can only show both files, label which world each belongs
to, and say which one is actually pointed at this server.

Discovery covers:

* the native home, including a ``CLAUDE_CONFIG_DIR`` override
* the Windows-side home, when running under WSL
* every WSL distribution's home, when running on Windows

Only files that exist are returned. A path that merely could exist is noise on
a page whose job is to tell you what is real.

Nothing here raises. Reaching across the WSL boundary touches a filesystem that
may be stopped, slow, or absent, so every probe is wrapped and a failure just
means that world contributes nothing.
"""

import contextlib
import os
import subprocess
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal

from my_claude_code.config.paths import (
    CLAUDE_CONFIG_DIRNAME,
    CLAUDE_SETTINGS_FILENAME,
    WSL_OSRELEASE_PATH,
    claude_settings_path,
    windows_claude_settings_path,
)

type Origin = Literal["windows", "wsl", "linux", "macos"]

ORIGIN_LABELS: dict[str, str] = {
    "windows": "Windows",
    "wsl": "WSL",
    "linux": "Linux",
    "macos": "macOS",
}

WSL_UNC_ROOTS = (r"\\wsl.localhost", r"\\wsl$")
WSL_LIST_TIMEOUT_S = 5.0
WSL_PROBE_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class DiscoveredSettings:
    """One settings.json that exists, and the world it belongs to."""

    path: str
    origin: Origin
    detail: str

    @property
    def origin_label(self) -> str:
        return ORIGIN_LABELS.get(self.origin, self.origin)


def _is_wsl() -> bool:
    try:
        osrelease = Path(WSL_OSRELEASE_PATH).read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in osrelease.lower()


def native_origin() -> Origin:
    """Return the world the server itself is running in."""

    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "wsl" if _is_wsl() else "linux"


def _exists(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


@cache
def wsl_distributions() -> tuple[str, ...]:
    """Return the installed WSL distribution names, empty when unavailable.

    Only meaningful on Windows. ``wsl.exe -l -q`` is used rather than listing
    the UNC root, because enumerating ``\\\\wsl.localhost`` blocks for as long
    as the service takes to answer when no distribution is running, and this
    runs while a page is loading.
    """

    if sys.platform != "win32":
        return ()

    try:
        completed = subprocess.run(
            ["wsl.exe", "--list", "--quiet"],
            capture_output=True,
            timeout=WSL_LIST_TIMEOUT_S,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return ()

    if completed.returncode != 0:
        return ()

    # wsl.exe writes UTF-16LE on most builds, and plain UTF-8 on some.
    for encoding in ("utf-16-le", "utf-8"):
        try:
            text = completed.stdout.decode(encoding)
        except UnicodeDecodeError:
            continue
        names = [line.strip().strip("\x00") for line in text.splitlines()]
        found = tuple(name for name in names if name)
        if found:
            return found

    return ()


def _wsl_home_candidates(distro: str) -> list[Path]:
    """Return plausible settings.json paths inside one WSL distribution."""

    candidates: list[Path] = []
    username = os.environ.get("USERNAME") or os.environ.get("USER")

    for root in WSL_UNC_ROOTS:
        base = Path(root) / distro
        try:
            if not base.exists():
                continue
        except OSError:
            continue

        home = base / "home"
        names: list[str] = []
        if username:
            names.append(username)
        with contextlib.suppress(OSError):
            names.extend(entry.name for entry in home.iterdir() if entry.is_dir())

        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            candidates.append(
                home / name / CLAUDE_CONFIG_DIRNAME / CLAUDE_SETTINGS_FILENAME
            )

        # root's home is not under /home.
        candidates.append(
            base / "root" / CLAUDE_CONFIG_DIRNAME / CLAUDE_SETTINGS_FILENAME
        )
        break

    return candidates


def _wsl_settings_from_windows() -> list[DiscoveredSettings]:
    found: list[DiscoveredSettings] = []
    for distro in wsl_distributions():
        found.extend(
            DiscoveredSettings(path=str(candidate), origin="wsl", detail=distro)
            for candidate in _wsl_home_candidates(distro)
            if _exists(candidate)
        )
    return found


def discover_settings_files() -> list[DiscoveredSettings]:
    """Return every settings.json that exists, native world first.

    Deduplicated by path. The native file leads because it is the one the user
    is most likely to mean, and a cross-boundary file is only useful once you
    know it is a different world -- which is what ``origin`` is for.
    """

    origin = native_origin()
    found: list[DiscoveredSettings] = []
    seen: set[str] = set()

    def add(path: Path, entry_origin: Origin, detail: str) -> None:
        key = str(path)
        if key in seen or not _exists(path):
            return
        seen.add(key)
        found.append(DiscoveredSettings(path=key, origin=entry_origin, detail=detail))

    native = claude_settings_path()
    add(native, origin, "this machine")

    # CLAUDE_CONFIG_DIR relocates the whole tree; the default location may still
    # hold an older file, so both are worth showing when both exist.
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        add(
            Path(config_dir).expanduser() / CLAUDE_SETTINGS_FILENAME,
            origin,
            "CLAUDE_CONFIG_DIR",
        )
    default_home = Path.home() / CLAUDE_CONFIG_DIRNAME / CLAUDE_SETTINGS_FILENAME
    add(default_home, origin, "this machine")

    if origin == "wsl":
        windows_path = windows_claude_settings_path()
        if windows_path is not None:
            add(windows_path, "windows", "Windows side")

    if origin == "windows":
        for entry in _wsl_settings_from_windows():
            add(Path(entry.path), entry.origin, entry.detail)

    return found
