"""Tests for the desktop controller's server-ownership mode branching."""

from pathlib import Path
from typing import Any, cast

import pytest

from my_claude_code.cli.desktop import DesktopController, DesktopError
from my_claude_code.config.desktop import DesktopState, ServerMode, save_desktop_state


def _set_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def _controller(
    monkeypatch,
    tmp_path,
    mode: str,
    *,
    preflight_result: str | None,
) -> tuple[DesktopController, list[Any]]:
    """Build a controller whose spawn path is recorded, never actually spawned."""

    _set_home(monkeypatch, tmp_path)
    save_desktop_state(DesktopState(server_mode=cast(ServerMode, mode)))

    spawned: list[Any] = []
    monkeypatch.setattr(
        "my_claude_code.cli.desktop.preflight_proxy",
        lambda url: preflight_result,
    )
    controller = DesktopController.__new__(DesktopController)
    object.__setattr__(
        controller, "_spawn_server", lambda settings: spawned.append(settings)
    )
    return controller, spawned


class TestEnsureServer:
    def test_spawn_mode_spawns_when_down(self, monkeypatch, tmp_path):
        controller, spawned = _controller(
            monkeypatch, tmp_path, "spawn", preflight_result="down"
        )

        controller.ensure_server()

        assert len(spawned) == 1

    def test_spawn_mode_noop_when_already_running(self, monkeypatch, tmp_path):
        controller, spawned = _controller(
            monkeypatch, tmp_path, "spawn", preflight_result=None
        )

        controller.ensure_server()

        assert spawned == []

    def test_attach_mode_never_spawns(self, monkeypatch, tmp_path):
        controller, spawned = _controller(
            monkeypatch, tmp_path, "attach", preflight_result="down"
        )

        controller.ensure_server()

        assert spawned == []

    def test_off_mode_never_spawns(self, monkeypatch, tmp_path):
        controller, spawned = _controller(
            monkeypatch, tmp_path, "off", preflight_result="down"
        )

        controller.ensure_server()

        assert spawned == []


class TestRestartServer:
    def test_restart_raises_outside_spawn(self, monkeypatch, tmp_path):
        controller, _spawned = _controller(
            monkeypatch, tmp_path, "attach", preflight_result="down"
        )

        with pytest.raises(DesktopError):
            controller.restart_server()

    def test_restart_raises_in_off(self, monkeypatch, tmp_path):
        controller, _spawned = _controller(
            monkeypatch, tmp_path, "off", preflight_result="down"
        )

        with pytest.raises(DesktopError):
            controller.restart_server()

    def test_restart_spawns_when_down_in_spawn(self, monkeypatch, tmp_path):
        controller, spawned = _controller(
            monkeypatch, tmp_path, "spawn", preflight_result="down"
        )

        controller.restart_server()

        assert len(spawned) == 1
