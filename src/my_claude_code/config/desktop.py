"""Desktop tray preferences persisted in ``~/.fcc/desktop.json``.

The four flags here are *preferences*, not live process controls:

* ``tray_enabled``      -- whether the next tray launch should actually show
  the tray icon. Turning it off from the menu only persists the flag; the
  current tray loop keeps running until Quit.
* ``start_at_login``    -- whether to register an OS autostart entry.
* ``minimize_to_tray``  -- reserved for tray-aware window behaviour.
* ``server_auto_start`` -- whether the tray should spawn ``mcc-server`` when
  the health check finds the server down.

The server itself only persists these values through the admin API; it never
applies the OS autostart entry, because the server may run headless (no tray,
no desktop session). The next ``mcc-desktop``/tray launch reconciles the file
with the OS state via :func:`apply_start_at_login` / :func:`remove_start_at_login`.
"""

import json
import os
import sys
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path

from .paths import config_dir_path

DESKTOP_STATE_FILENAME = "desktop.json"

# Plist label and desktop-file id are stable identifiers the OS uses to look
# the entry back up. Keep them namespaced so a legacy install never collides
# with a fresh one.
LAUNCH_AGENT_LABEL = "com.myclaudecode.tray"
LINUX_AUTOSTART_ID = "my-claude-code-tray"

# Canonical field names and defaults for the desktop state file. Unknown keys
# in a stored file are ignored; unknown booleans submitted by callers are
# rejected in favour of these defaults.
_DEFAULTS: dict[str, bool] = {
    "tray_enabled": True,
    "start_at_login": False,
    "minimize_to_tray": False,
    "server_auto_start": True,
}


class DesktopStateError(Exception):
    """Raised when the persisted desktop state cannot be written."""


@dataclass(frozen=True)
class DesktopState:
    """Immutable snapshot of the persisted desktop preferences."""

    tray_enabled: bool
    start_at_login: bool
    minimize_to_tray: bool
    server_auto_start: bool


def desktop_state_path() -> Path:
    """Return the persisted desktop state path."""

    return config_dir_path() / DESKTOP_STATE_FILENAME


def _default_state() -> dict[str, bool]:
    return dict(_DEFAULTS)


def load_desktop_state() -> DesktopState:
    """Load the persisted desktop state; never raises.

    A missing or malformed file is treated as the default state, and unknown
    keys are ignored so an older build's file stays forward-compatible.
    """

    path = desktop_state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return DesktopState(**_default_state())

    try:
        data = json.loads(raw)
    except ValueError, TypeError:
        return DesktopState(**_default_state())

    if not isinstance(data, dict):
        return DesktopState(**_default_state())

    values = _default_state()
    for name in values:
        if isinstance(data.get(name), bool):
            values[name] = data[name]
    return DesktopState(**values)


def save_desktop_state(state: DesktopState) -> None:
    """Persist desktop state atomically. Raises :class:`DesktopStateError` on failure."""

    path = desktop_state_path()
    payload = json.dumps(
        {name: value for name, value in asdict(state).items() if name in _DEFAULTS}
    )

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as exc:
        raise DesktopStateError(f"Failed to save desktop state: {exc}") from exc


def _update_state(**overrides: bool) -> DesktopState:
    """Load, apply boolean overrides, persist, and return the new state."""

    current = load_desktop_state()
    values = asdict(current)
    for name, value in overrides.items():
        if name in values:
            values[name] = bool(value)
    updated = DesktopState(**values)
    save_desktop_state(updated)
    return updated


def set_tray_enabled(enabled: bool) -> DesktopState:
    return _update_state(tray_enabled=enabled)


def set_start_at_login(enabled: bool) -> DesktopState:
    """Persist the flag and reconcile the OS autostart entry."""

    if enabled:
        apply_start_at_login()
    else:
        remove_start_at_login()
    return _update_state(start_at_login=enabled)


def _autostart_command() -> list[str]:
    """Command the OS should run at login to launch the tray.

    Run this interpreter against the entrypoint module. This works from both a
    source checkout and an installed tool environment (``uv tool`` shims also
    resolve the interpreter), and avoids depending on a console-script shim
    whose exact name differs across platforms.
    """

    return [
        sys.executable,
        "-m",
        "my_claude_code.cli.desktop_entrypoint",
    ]


def _windows_run_key_value() -> str:
    return '"' + '" "'.join(_autostart_command()) + '"'


def _windows_run_key() -> str:
    return r"Software\Microsoft\Windows\CurrentVersion\Run"


def _macos_launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def _macos_launch_agent_plist() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{LAUNCH_AGENT_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"        <string>{_autostart_command()[0]}</string>\n"
        f"        <string>{_autostart_command()[1]}</string>\n"
        f"        <string>{_autostart_command()[2]}</string>\n"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _linux_autostart_path() -> Path:
    return Path.home() / ".config" / "autostart" / f"{LINUX_AUTOSTART_ID}.desktop"


def _linux_autostart_content() -> str:
    command = " ".join(_autostart_command())
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name=My Claude Code\n"
        f"Exec={command}\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def apply_start_at_login() -> None:
    """Register the tray to start at login for the current platform."""

    if sys.platform == "win32":
        _apply_windows_start_at_login()
    elif sys.platform == "darwin":
        _apply_macos_start_at_login()
    else:
        _apply_linux_start_at_login()


def remove_start_at_login() -> None:
    """Remove any autostart entry for the current platform, if present."""

    if sys.platform == "win32":
        _remove_windows_start_at_login()
    elif sys.platform == "darwin":
        _remove_macos_start_at_login()
    else:
        _remove_linux_start_at_login()


def _apply_windows_start_at_login() -> None:
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _windows_run_key(), 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(
            key, LAUNCH_AGENT_LABEL, 0, winreg.REG_SZ, _windows_run_key_value()
        )


def _remove_windows_start_at_login() -> None:
    import winreg

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _windows_run_key(), 0, winreg.KEY_SET_VALUE
        )
    except OSError:
        return
    try:
        winreg.DeleteValue(key, LAUNCH_AGENT_LABEL)
    except OSError:
        pass
    finally:
        winreg.CloseKey(key)


def _apply_macos_start_at_login() -> None:
    path = _macos_launch_agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_macos_launch_agent_plist(), encoding="utf-8")


def _remove_macos_start_at_login() -> None:
    with suppress(OSError):
        _macos_launch_agent_path().unlink(missing_ok=True)


def _apply_linux_start_at_login() -> None:
    path = _linux_autostart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_linux_autostart_content(), encoding="utf-8")


def _remove_linux_start_at_login() -> None:
    with suppress(OSError):
        _linux_autostart_path().unlink(missing_ok=True)


def apply_tray_registration(enabled: bool) -> None:
    """Persist the tray-enabled flag only.

    The actual tray show/hide is decided at launch: a running tray loop cannot
    remove its own icon portably, so the flag is read the next time
    ``mcc-desktop`` starts.
    """

    _update_state(tray_enabled=enabled)
