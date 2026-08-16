"""Lightweight entrypoint for the optional MCC desktop shell."""

import sys
from collections.abc import Sequence
from pathlib import Path

from my_claude_code.cli.desktop_assets import export_app_icon
from my_claude_code.config.desktop import (
    SERVER_MODES,
    apply_tray_registration,
    load_desktop_state,
    set_server_mode,
    set_start_at_login,
)


def _print_state() -> None:
    state = load_desktop_state()
    print(f"tray_enabled={str(state.tray_enabled).lower()}")
    print(f"start_at_login={str(state.start_at_login).lower()}")
    print(f"minimize_to_tray={str(state.minimize_to_tray).lower()}")
    print(f"server_mode={state.server_mode}")


def _print_usage() -> None:
    print(
        "Usage: mcc-desktop [--server-mode spawn|attach|off] "
        "[--autostart on|off] "
        "[--start-at-login | --no-start-at-login | "
        "--tray-enabled | --no-tray-enabled | --status | --export-icon PATH]",
        file=sys.stderr,
    )


def launch(argv: Sequence[str] | None = None) -> None:
    """Apply a state toggle, export installer assets, or launch the tray."""

    args = tuple(sys.argv[1:] if argv is None else argv)

    if len(args) == 2 and args[0] == "--export-icon":
        export_app_icon(Path(args[1]))
        return

    toggle = {
        "--start-at-login": True,
        "--no-start-at-login": False,
        "--tray-enabled": True,
        "--no-tray-enabled": False,
    }
    if len(args) == 1 and args[0] in toggle:
        if args[0] in {"--start-at-login", "--no-start-at-login"}:
            set_start_at_login(toggle[args[0]])
        else:
            apply_tray_registration(toggle[args[0]])
        return

    if len(args) == 2 and args[0] == "--server-mode":
        if args[1] not in SERVER_MODES:
            print(
                f"Invalid server mode: {args[1]} "
                f"(expected one of {', '.join(SERVER_MODES)})",
                file=sys.stderr,
            )
            raise SystemExit(2)
        set_server_mode(args[1])
        return

    if len(args) == 2 and args[0] == "--autostart":
        if args[1] == "on":
            set_start_at_login(True)
        elif args[1] == "off":
            set_start_at_login(False)
        else:
            print("--autostart expects 'on' or 'off'", file=sys.stderr)
            raise SystemExit(2)
        return

    if len(args) == 1 and args[0] == "--status":
        _print_state()
        return

    if args:
        _print_usage()
        raise SystemExit(2)

    if sys.platform not in {"darwin", "win32"}:
        print(
            "MCC Desktop is supported on Windows and macOS. Linux launches the "
            "tray best-effort; pystray may not have a backend available.",
            file=sys.stderr,
        )

    from my_claude_code.cli.desktop_tray import launch as launch_tray

    launch_tray()
