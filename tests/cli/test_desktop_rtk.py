"""Tests for the RTK token-optimizer additions to the pystray tray menu.

The pystray classes only construct real ``Icon`` objects on a platform with a
backend, so the menu items are inspected via ``PystrayDesktopTray._menu()`` and
the toggle handlers are driven directly against the plain ``MenuItem`` objects
the menu returns. Assertions focus on the state file and the reconciler call,
which is the contract the tray shares with the CLI and the admin dashboard.
"""

from pathlib import Path
from typing import cast

import pytest

pytest.importorskip("pystray")

from my_claude_code.cli.desktop import DesktopController
from my_claude_code.cli.desktop_tray import PystrayDesktopTray
from my_claude_code.config import rtk as rtk_config
from my_claude_code.config.rtk import RtkState, load_rtk_state


def _set_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def _patched_tray(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    applied: list[RtkState] = []
    monkeypatch.setattr(
        "my_claude_code.cli.desktop_tray.apply_rtk_state",
        lambda state, **kwargs: applied.append(state),
    )
    monkeypatch.setattr(
        "my_claude_code.cli.desktop_tray.load_desktop_state",
        lambda: type(
            "DesktopState",
            (),
            {
                "tray_enabled": True,
                "start_at_login": False,
            },
        )(),
    )
    controller = cast(
        DesktopController, type("Controller", (), {"status": "running"})()
    )
    return PystrayDesktopTray(controller), applied


def _token_optimizer_menu(tray):
    for item in tray._menu():
        if item.text == "Token optimizer":
            return item.submenu
    raise AssertionError("Token optimizer submenu not found")


def _agent_item(menu, agent):
    for item in menu.items:
        if item.text == agent:
            return item
    raise AssertionError(f"{agent} menu item not found")


def _toggle(tray, item):
    # ``MenuItem.__call__(icon)`` invokes the action with ``(icon, item)``,
    # matching exactly what the native backend does when the user clicks it.
    item(None)


def test_token_optimizer_submenu_presents_three_checkable_agents(monkeypatch, tmp_path):
    tray, _applied = _patched_tray(monkeypatch, tmp_path)

    submenu = _token_optimizer_menu(tray)

    assert [item.text for item in submenu.items] == ["Claude Code", "Codex", "Pi"]


def test_toggling_agent_persists_and_reconciles(monkeypatch, tmp_path):
    tray, applied = _patched_tray(monkeypatch, tmp_path)
    submenu = _token_optimizer_menu(tray)
    claude_item = _agent_item(submenu, "Claude Code")

    _toggle(tray, claude_item)

    assert load_rtk_state() == RtkState(claude=True, codex=False, pi=False)
    assert applied == [RtkState(claude=True, codex=False, pi=False)]


def test_toggling_does_not_disturb_other_agents(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    rtk_config.save_rtk_state(RtkState(claude=True, codex=True, pi=True))
    applied: list[RtkState] = []
    monkeypatch.setattr(
        "my_claude_code.cli.desktop_tray.apply_rtk_state",
        lambda state, **kwargs: applied.append(state),
    )
    monkeypatch.setattr(
        "my_claude_code.cli.desktop_tray.load_desktop_state",
        lambda: type(
            "DesktopState",
            (),
            {
                "tray_enabled": True,
                "start_at_login": False,
            },
        )(),
    )
    controller = cast(
        DesktopController, type("Controller", (), {"status": "running"})()
    )
    tray = PystrayDesktopTray(controller)
    submenu = _token_optimizer_menu(tray)

    _toggle(tray, _agent_item(submenu, "Codex"))

    assert load_rtk_state() == RtkState(claude=True, codex=False, pi=True)
    assert applied == [RtkState(claude=True, codex=False, pi=True)]


def test_checked_reflects_persisted_state(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    rtk_config.save_rtk_state(RtkState(claude=True, codex=False, pi=True))
    tray, _applied = _patched_tray(monkeypatch, tmp_path)
    submenu = _token_optimizer_menu(tray)

    checks = {item.text: item.checked for item in submenu.items}

    assert checks == {"Claude Code": True, "Codex": False, "Pi": True}
