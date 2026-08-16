"""Desktop deployment preferences and per-platform login registration."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

from .claude_discovery import native_origin
from .paths import config_dir_path

DESKTOP_STATE_FILENAME = "desktop.json"
SERVER_MODES = ("spawn", "attach", "off")
type ServerMode = Literal["spawn", "attach", "off"]
type AutostartTarget = Literal["tray", "server"]

WINDOWS_RUN_VALUE = "MyClaudeCodeDesktop"
LAUNCH_AGENT_LABEL = "com.myclaudecode.tray"
LINUX_SYSTEMD_UNIT = "mcc-server.service"
LINUX_AUTOSTART_ID = "mcc-server"

_BOOLEAN_DEFAULTS: dict[str, bool] = {
    "tray_enabled": True,
    "start_at_login": False,
    "minimize_to_tray": False,
}


class DesktopStateError(Exception):
    """Raised when persisted desktop state cannot be written."""


@dataclass(frozen=True)
class DesktopState:
    """Immutable snapshot of desktop deployment preferences."""

    tray_enabled: bool = True
    start_at_login: bool = False
    minimize_to_tray: bool = False
    server_mode: ServerMode = "spawn"


def desktop_state_path() -> Path:
    return config_dir_path() / DESKTOP_STATE_FILENAME


def _default_state() -> DesktopState:
    return DesktopState()


def load_desktop_state() -> DesktopState:
    """Load desktop state, including the legacy boolean migration; never raises."""

    try:
        data = json.loads(desktop_state_path().read_text(encoding="utf-8"))
    except OSError, ValueError, TypeError:
        return _default_state()
    if not isinstance(data, dict):
        return _default_state()

    values: dict[str, bool | ServerMode] = dict(_BOOLEAN_DEFAULTS)
    for name in _BOOLEAN_DEFAULTS:
        if isinstance(data.get(name), bool):
            values[name] = data[name]

    raw_mode = data.get("server_mode")
    if raw_mode in SERVER_MODES:
        server_mode: ServerMode = raw_mode
    elif isinstance(data.get("server_auto_start"), bool):
        server_mode = "spawn" if data["server_auto_start"] else "attach"
    else:
        server_mode = "spawn"
    return DesktopState(
        tray_enabled=bool(values["tray_enabled"]),
        start_at_login=bool(values["start_at_login"]),
        minimize_to_tray=bool(values["minimize_to_tray"]),
        server_mode=server_mode,
    )


def save_desktop_state(state: DesktopState) -> None:
    """Persist desktop state atomically, without the retired legacy key."""

    path = desktop_state_path()
    payload = json.dumps(asdict(state))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as exc:
        raise DesktopStateError(f"Failed to save desktop state: {exc}") from exc


def _update_state(**overrides: bool | ServerMode) -> DesktopState:
    current = load_desktop_state()
    updated = DesktopState(
        tray_enabled=bool(overrides["tray_enabled"])
        if "tray_enabled" in overrides
        else current.tray_enabled,
        start_at_login=bool(overrides["start_at_login"])
        if "start_at_login" in overrides
        else current.start_at_login,
        minimize_to_tray=bool(overrides["minimize_to_tray"])
        if "minimize_to_tray" in overrides
        else current.minimize_to_tray,
        server_mode=overrides["server_mode"]
        if "server_mode" in overrides and overrides["server_mode"] in SERVER_MODES
        else current.server_mode,
    )
    save_desktop_state(updated)
    return updated


def set_server_mode(mode: str) -> DesktopState:
    if mode not in SERVER_MODES:
        raise ValueError(f"Invalid server mode: {mode}")
    validated = cast(ServerMode, mode)
    return _update_state(server_mode=validated)


def set_tray_enabled(enabled: bool) -> DesktopState:
    return _update_state(tray_enabled=enabled)


def default_autostart_target() -> AutostartTarget:
    """Return the ADR-defined target for the native platform."""

    return "tray" if native_origin() in {"windows", "macos"} else "server"


def set_start_at_login(
    enabled: bool, target: AutostartTarget | None = None
) -> DesktopState:
    target = target or default_autostart_target()
    if enabled:
        apply_start_at_login(target)
    else:
        remove_start_at_login(target)
    return _update_state(start_at_login=enabled)


def _autostart_command(target: AutostartTarget) -> list[str]:
    module = (
        "my_claude_code.cli.desktop_entrypoint"
        if target == "tray"
        else "my_claude_code.cli.entrypoints"
    )
    return [sys.executable, "-m", module]


def _windows_run_key_value(target: AutostartTarget) -> str:
    return '"' + '" "'.join(_autostart_command(target)) + '"'


def _windows_run_key() -> str:
    return r"Software\Microsoft\Windows\CurrentVersion\Run"


def _macos_launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def _macos_launch_agent_plist(target: AutostartTarget) -> str:
    arguments = "\n".join(
        f"        <string>{argument}</string>"
        for argument in _autostart_command(target)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        "    <key>Label</key>\n"
        f"    <string>{LAUNCH_AGENT_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n    <array>\n"
        f"{arguments}\n"
        "    </array>\n    <key>RunAtLoad</key>\n    <true/>\n"
        "</dict>\n</plist>\n"
    )


def _linux_systemd_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / LINUX_SYSTEMD_UNIT


def _linux_systemd_content() -> str:
    command = " ".join(shlex.quote(part) for part in _autostart_command("server"))
    return (
        "[Unit]\nDescription=My Claude Code server\nAfter=network-online.target\n\n"
        f"[Service]\nType=simple\nExecStart={command}\nRestart=on-failure\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def _linux_autostart_path() -> Path:
    return Path.home() / ".config" / "autostart" / f"{LINUX_AUTOSTART_ID}.desktop"


def _linux_autostart_content() -> str:
    command = " ".join(shlex.quote(part) for part in _autostart_command("server"))
    return (
        "[Desktop Entry]\nType=Application\nName=My Claude Code Server\n"
        f"Exec={command}\nX-GNOME-Autostart-enabled=true\n"
    )


def _systemd_user_available() -> bool:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return False
    try:
        result = subprocess.run(
            [systemctl, "--user", "show-environment"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return result.returncode == 0


def apply_start_at_login(target: AutostartTarget | None = None) -> None:
    """Register the selected target, defaulting to the platform's ADR target."""

    selected = target or default_autostart_target()
    origin = native_origin()
    if origin == "windows":
        _apply_windows_start_at_login(selected)
    elif origin == "macos":
        _apply_macos_start_at_login(selected)
    else:
        _apply_linux_start_at_login(selected)


def remove_start_at_login(target: AutostartTarget | None = None) -> None:
    selected = target or default_autostart_target()
    origin = native_origin()
    if origin == "windows":
        _remove_windows_start_at_login()
    elif origin == "macos":
        _remove_macos_start_at_login()
    else:
        _remove_linux_start_at_login(selected)


def _apply_windows_start_at_login(target: AutostartTarget) -> None:
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _windows_run_key(), 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(
            key, WINDOWS_RUN_VALUE, 0, winreg.REG_SZ, _windows_run_key_value(target)
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
        winreg.DeleteValue(key, WINDOWS_RUN_VALUE)
    except OSError:
        pass
    finally:
        winreg.CloseKey(key)


def _apply_macos_start_at_login(target: AutostartTarget) -> None:
    path = _macos_launch_agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_macos_launch_agent_plist(target), encoding="utf-8")


def _remove_macos_start_at_login() -> None:
    with suppress(OSError):
        _macos_launch_agent_path().unlink(missing_ok=True)


def _apply_linux_start_at_login(target: AutostartTarget) -> None:
    if target != "server":
        raise DesktopStateError("Linux/WSL autostart supports the headless server only")
    if _systemd_user_available():
        path = _linux_systemd_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_linux_systemd_content(), encoding="utf-8")
        systemctl = shutil.which("systemctl")
        if systemctl is not None:
            subprocess.run(
                [systemctl, "--user", "daemon-reload"], check=False, timeout=10
            )
            subprocess.run(
                [systemctl, "--user", "enable", "--now", LINUX_SYSTEMD_UNIT],
                check=False,
                timeout=10,
            )
        with suppress(OSError):
            _linux_autostart_path().unlink(missing_ok=True)
        return
    path = _linux_autostart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_linux_autostart_content(), encoding="utf-8")


def _remove_linux_start_at_login(target: AutostartTarget) -> None:
    if target != "server":
        return
    systemctl = shutil.which("systemctl")
    if systemctl is not None:
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [systemctl, "--user", "disable", "--now", LINUX_SYSTEMD_UNIT],
                check=False,
                timeout=10,
            )
            subprocess.run(
                [systemctl, "--user", "daemon-reload"], check=False, timeout=10
            )
    with suppress(OSError):
        _linux_systemd_path().unlink(missing_ok=True)
    with suppress(OSError):
        _linux_autostart_path().unlink(missing_ok=True)


def apply_tray_registration(enabled: bool) -> None:
    """Persist the tray flag; a running tray remains until Quit."""

    _update_state(tray_enabled=enabled)
