"""Tests for config/desktop.py and the admin desktop endpoints."""

import json
import sys
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config import desktop as desktop_config
from my_claude_code.config.desktop import (
    LAUNCH_AGENT_LABEL,
    LINUX_AUTOSTART_ID,
    DesktopState,
    DesktopStateError,
    load_desktop_state,
    save_desktop_state,
)
from my_claude_code.config.paths import config_dir_path
from tests.api.support import create_test_app


def _set_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def _local_client(app):
    return TestClient(app, client=("127.0.0.1", 50000))


class TestLoadDesktopState:
    def test_missing_file_returns_defaults(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        state = load_desktop_state()

        assert state.tray_enabled is True
        assert state.start_at_login is False
        assert state.minimize_to_tray is False
        assert state.server_auto_start is True

    def test_corrupt_file_returns_defaults(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = desktop_config.desktop_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json{{{", encoding="utf-8")

        state = load_desktop_state()

        assert state.tray_enabled is True
        assert state.start_at_login is False

    def test_non_dict_json_returns_defaults(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = desktop_config.desktop_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2, 3]", encoding="utf-8")

        state = load_desktop_state()

        assert state.server_auto_start is True

    def test_unknown_keys_are_ignored(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = desktop_config.desktop_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"tray_enabled": False, "future_key": "x", "bogus": 5}),
            encoding="utf-8",
        )

        state = load_desktop_state()

        assert state.tray_enabled is False
        assert state.start_at_login is False

    def test_non_boolean_value_falls_back_to_default(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = desktop_config.desktop_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"tray_enabled": "yes", "start_at_login": 1}),
            encoding="utf-8",
        )

        state = load_desktop_state()

        assert state.tray_enabled is True
        assert state.start_at_login is False


class TestSaveDesktopState:
    def test_round_trip(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        save_desktop_state(
            DesktopState(
                tray_enabled=False,
                start_at_login=True,
                minimize_to_tray=True,
                server_auto_start=False,
            )
        )
        state = load_desktop_state()

        assert state.tray_enabled is False
        assert state.start_at_login is True
        assert state.minimize_to_tray is True
        assert state.server_auto_start is False

    def test_creates_parent_dirs(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        assert not config_dir_path().exists()

        save_desktop_state(load_desktop_state())

        assert desktop_config.desktop_state_path().is_file()

    def test_atomic_tmp_file_does_not_linger(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        save_desktop_state(load_desktop_state())

        tmp_path_candidate = desktop_config.desktop_state_path().with_suffix(
            ".json.tmp"
        )
        assert not tmp_path_candidate.exists()

    def test_write_failure_raises_desktop_state_error(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        blocker = config_dir_path()
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("blocked", encoding="utf-8")

        with pytest.raises(DesktopStateError):
            save_desktop_state(load_desktop_state())


class _FakeWinreg:
    """Minimal winreg stand-in exercising the same call surface."""

    HKEY_CURRENT_USER = object()
    KEY_SET_VALUE = 1
    REG_SZ = 1

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.closed = False

    def OpenKey(self, root, subkey, reserved, access):
        assert root is self.HKEY_CURRENT_USER
        assert subkey == r"Software\Microsoft\Windows\CurrentVersion\Run"
        return self

    def SetValueEx(self, key, name, reserved, kind, value):
        assert key is self
        assert name == LAUNCH_AGENT_LABEL
        self.values[name] = value

    def DeleteValue(self, key, name):
        assert key is self
        assert name == LAUNCH_AGENT_LABEL
        self.values.pop(name, None)

    def CloseKey(self, key):
        assert key is self
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


@pytest.fixture
def fake_winreg(monkeypatch):
    fake = _FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    return fake


class TestWindowsStartAtLogin:
    def test_apply_writes_run_key(self, monkeypatch, tmp_path, fake_winreg):
        _set_home(monkeypatch, tmp_path)

        desktop_config.apply_start_at_login()

        assert LAUNCH_AGENT_LABEL in fake_winreg.values
        assert (
            "my_claude_code.cli.desktop_entrypoint"
            in fake_winreg.values[LAUNCH_AGENT_LABEL]
        )

    def test_remove_deletes_run_key(self, monkeypatch, tmp_path, fake_winreg):
        _set_home(monkeypatch, tmp_path)
        fake_winreg.values[LAUNCH_AGENT_LABEL] = "whatever"

        desktop_config.remove_start_at_login()

        assert LAUNCH_AGENT_LABEL not in fake_winreg.values
        assert fake_winreg.closed is True

    def test_remove_missing_run_key_is_quiet(self, monkeypatch, tmp_path, fake_winreg):
        _set_home(monkeypatch, tmp_path)

        desktop_config.remove_start_at_login()

        assert LAUNCH_AGENT_LABEL not in fake_winreg.values


class TestMacOSStartAtLogin:
    @pytest.fixture(autouse=True)
    def _platform(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")

    def test_apply_writes_launch_agent_plist(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        desktop_config.apply_start_at_login()

        path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
        content = path.read_text(encoding="utf-8")
        assert path.is_file()
        assert "<key>RunAtLoad</key>" in content
        assert "<true/>" in content
        assert LAUNCH_AGENT_LABEL in content
        assert "my_claude_code.cli.desktop_entrypoint" in content

    def test_remove_deletes_launch_agent_plist(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("present", encoding="utf-8")

        desktop_config.remove_start_at_login()

        assert not path.exists()


class TestLinuxStartAtLogin:
    @pytest.fixture(autouse=True)
    def _platform(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")

    def test_apply_writes_autostart_desktop_file(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        desktop_config.apply_start_at_login()

        path = Path.home() / ".config" / "autostart" / f"{LINUX_AUTOSTART_ID}.desktop"
        content = path.read_text(encoding="utf-8")
        assert path.is_file()
        assert "[Desktop Entry]" in content
        assert "X-GNOME-Autostart-enabled=true" in content
        assert "my_claude_code.cli.desktop_entrypoint" in content

    def test_remove_deletes_autostart_desktop_file(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = Path.home() / ".config" / "autostart" / f"{LINUX_AUTOSTART_ID}.desktop"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("present", encoding="utf-8")

        desktop_config.remove_start_at_login()

        assert not path.exists()


class TestAdminDesktopEndpoints:
    def _client(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        app = create_test_app()
        return _local_client(app)

    def test_get_returns_defaults(self, monkeypatch, tmp_path):
        with self._client(monkeypatch, tmp_path) as client:
            response = client.get("/admin/api/desktop")

        assert response.status_code == 200
        body = response.json()
        assert body["tray_enabled"] is True
        assert body["start_at_login"] is False
        assert body["minimize_to_tray"] is False
        assert body["server_auto_start"] is True

    def test_post_updates_only_submitted_flags(self, monkeypatch, tmp_path):
        with self._client(monkeypatch, tmp_path) as client:
            response = client.post(
                "/admin/api/desktop",
                json={"start_at_login": True},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["start_at_login"] is True
        # Unsubmitted flags keep their persisted/default values.
        assert body["tray_enabled"] is True

    def test_post_persists_to_disk(self, monkeypatch, tmp_path):
        with self._client(monkeypatch, tmp_path) as client:
            client.post(
                "/admin/api/desktop",
                json={"tray_enabled": False, "minimize_to_tray": True},
            )

        path = desktop_config.desktop_state_path()
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert persisted["tray_enabled"] is False
        assert persisted["minimize_to_tray"] is True
        # A partial update leaves the unsubmitted flag at its default.
        assert persisted["server_auto_start"] is True

    def test_post_round_trips(self, monkeypatch, tmp_path):
        with self._client(monkeypatch, tmp_path) as client:
            client.post("/admin/api/desktop", json={"start_at_login": True})
            response = client.get("/admin/api/desktop")

        assert response.json()["start_at_login"] is True

    def test_post_empty_body_returns_current_state(self, monkeypatch, tmp_path):
        with self._client(monkeypatch, tmp_path) as client:
            response = client.post("/admin/api/desktop", json={})

        assert response.status_code == 200
        assert response.json()["tray_enabled"] is True

    def test_post_ignores_unknown_fields(self, monkeypatch, tmp_path):
        with self._client(monkeypatch, tmp_path) as client:
            response = client.post(
                "/admin/api/desktop",
                json={"start_at_login": True, "hack": "nope"},
            )

        assert response.status_code == 200
        assert response.json()["start_at_login"] is True

    def test_non_loopback_client_is_rejected(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        app = create_test_app()
        with TestClient(app, client=("203.0.113.9", 50000)) as client:
            response = client.get("/admin/api/desktop")

        assert response.status_code == 403


def test_desktop_gui_scripts_are_registered() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    gui_scripts = manifest["project"]["gui-scripts"]
    assert gui_scripts["mcc-desktop"] == "my_claude_code.cli.desktop_entrypoint:launch"
    assert gui_scripts["fcc-desktop"] == "my_claude_code.cli.desktop_entrypoint:launch"


def test_desktop_entrypoint_is_callable() -> None:
    from my_claude_code.cli import desktop_entrypoint

    assert callable(desktop_entrypoint.launch)


def test_apply_tray_registration_persists_only_the_flag(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)

    desktop_config.apply_tray_registration(False)

    state = load_desktop_state()
    assert state.tray_enabled is False
    assert state.start_at_login is False


def test_set_start_at_login_persists_flag_and_reconciles_os(
    monkeypatch, tmp_path, fake_winreg
):
    _set_home(monkeypatch, tmp_path)

    desktop_config.set_start_at_login(True)

    assert load_desktop_state().start_at_login is True
    assert LAUNCH_AGENT_LABEL in fake_winreg.values

    desktop_config.set_start_at_login(False)

    assert load_desktop_state().start_at_login is False
    assert LAUNCH_AGENT_LABEL not in fake_winreg.values
