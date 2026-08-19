"""Tests for the desktop window provider chain and the Chromium binary search."""

import pytest

from my_claude_code.cli import desktop_window
from my_claude_code.cli.desktop_window import (
    PROVIDER_CHAIN,
    AppModeWindow,
    BrowserTabWindow,
    PywebviewWindow,
    create_window,
)


@pytest.fixture
def providers(monkeypatch):
    """Control availability of each provider independently of this machine."""

    state = {"app-mode": False, "pywebview": False}

    def app_mode() -> AppModeWindow | None:
        return AppModeWindow("chrome") if state["app-mode"] else None

    def pywebview() -> PywebviewWindow | None:
        return PywebviewWindow(object()) if state["pywebview"] else None

    monkeypatch.setitem(desktop_window._PROVIDERS, "app-mode", app_mode)
    monkeypatch.setitem(desktop_window._PROVIDERS, "pywebview", pywebview)
    return state


class TestChainOrder:
    def test_chain_prefers_app_mode(self, providers):
        providers["app-mode"] = True
        providers["pywebview"] = True

        assert isinstance(create_window("auto"), AppModeWindow)

    def test_chain_falls_to_pywebview_without_chromium(self, providers):
        providers["pywebview"] = True

        assert isinstance(create_window("auto"), PywebviewWindow)

    def test_chain_falls_to_browser_when_nothing_is_available(self, providers):
        assert isinstance(create_window("auto"), BrowserTabWindow)

    def test_default_chain_order_is_app_mode_first(self):
        assert PROVIDER_CHAIN == ("app-mode", "pywebview", "browser")


class TestExplicitPin:
    def test_pin_is_honoured_when_available(self, providers):
        providers["app-mode"] = True
        providers["pywebview"] = True

        assert isinstance(create_window("pywebview"), PywebviewWindow)

    def test_unavailable_pin_degrades_instead_of_raising(self, providers, caplog):
        providers["app-mode"] = True

        with caplog.at_level("WARNING"):
            window = create_window("pywebview")

        assert isinstance(window, AppModeWindow)
        assert "unavailable" in caplog.text

    def test_unknown_preference_falls_back_to_auto(self, providers, caplog):
        providers["app-mode"] = True

        with caplog.at_level("WARNING"):
            window = create_window("holographic")

        assert isinstance(window, AppModeWindow)
        assert "auto" in caplog.text

    def test_browser_pin_never_escalates(self, providers):
        providers["app-mode"] = True

        assert isinstance(create_window("browser"), BrowserTabWindow)


class TestAppModeCommand:
    def test_command_carries_app_profile_and_size(self, monkeypatch, tmp_path):
        monkeypatch.setattr(desktop_window.sys, "platform", "win32")
        window = AppModeWindow("chrome.exe", profile_dir=tmp_path / "profile")

        command = window.command("http://127.0.0.1:8082/admin")

        assert command[0] == "chrome.exe"
        assert "--app=http://127.0.0.1:8082/admin" in command
        assert f"--user-data-dir={tmp_path / 'profile'}" in command
        assert "--window-size=1400,900" in command
        assert not any(part.startswith("--class=") for part in command)

    def test_linux_command_sets_wm_class(self, monkeypatch, tmp_path):
        monkeypatch.setattr(desktop_window.sys, "platform", "linux")
        window = AppModeWindow("chromium", profile_dir=tmp_path / "profile")

        assert "--class=MyClaudeCode" in window.command("http://127.0.0.1:8082/admin")

    def test_focus_without_a_process_reports_failure(self, tmp_path):
        window = AppModeWindow("chrome", profile_dir=tmp_path)

        assert window.focus() is False
        assert window.is_open is False


class TestAppModeWindowSizePersistence:
    """``--window-size`` must not fight Chromium's own remembered geometry."""

    def _write_placement(self, profile_dir, present=True):
        prefs_dir = profile_dir / "Default"
        prefs_dir.mkdir(parents=True, exist_ok=True)
        payload = (
            {"browser": {"window_placement": {"left": 10, "top": 10}}}
            if present
            else {"browser": {}}
        )
        (prefs_dir / "Preferences").write_text(
            __import__("json").dumps(payload), encoding="utf-8"
        )

    def test_first_run_passes_configured_size(self, monkeypatch, tmp_path):
        monkeypatch.setattr(desktop_window.sys, "platform", "win32")
        window = AppModeWindow("chrome.exe", profile_dir=tmp_path / "profile")

        command = window.command("http://127.0.0.1:8082/admin")

        assert "--window-size=1400,900" in command

    def test_remembered_placement_skips_window_size(self, monkeypatch, tmp_path):
        monkeypatch.setattr(desktop_window.sys, "platform", "win32")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        profile_dir = tmp_path / "profile"
        self._write_placement(profile_dir, present=True)
        # A prior launch already forced today's configured size once.
        desktop_window.record_applied_window_size(1400, 900)
        window = AppModeWindow("chrome.exe", profile_dir=profile_dir)

        command = window.command("http://127.0.0.1:8082/admin")

        assert not any(part.startswith("--window-size=") for part in command)

    def test_changed_config_overrides_remembered_placement(self, monkeypatch, tmp_path):
        """A config change since the last forced size takes effect once."""

        monkeypatch.setattr(desktop_window.sys, "platform", "win32")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        profile_dir = tmp_path / "profile"
        self._write_placement(profile_dir, present=True)
        # The last time we forced a size, it matched the (then-current) config.
        desktop_window.record_applied_window_size(1400, 900)
        window = AppModeWindow("chrome.exe", profile_dir=profile_dir)

        # Simulate the user changing DESKTOP_WINDOW_WIDTH in the dashboard.
        monkeypatch.setenv("DESKTOP_WINDOW_WIDTH", "1600")
        desktop_window.get_settings.cache_clear()
        try:
            command = window.command("http://127.0.0.1:8082/admin")
            assert "--window-size=1600,900" in command
        finally:
            desktop_window.get_settings.cache_clear()

    def test_corrupt_preferences_file_is_treated_as_no_memory(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(desktop_window.sys, "platform", "win32")
        profile_dir = tmp_path / "profile"
        prefs_dir = profile_dir / "Default"
        prefs_dir.mkdir(parents=True, exist_ok=True)
        (prefs_dir / "Preferences").write_text("{not json", encoding="utf-8")
        window = AppModeWindow("chrome.exe", profile_dir=profile_dir)

        command = window.command("http://127.0.0.1:8082/admin")

        assert "--window-size=1400,900" in command

    def test_missing_preferences_file_is_treated_as_no_memory(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(desktop_window.sys, "platform", "win32")
        window = AppModeWindow("chrome.exe", profile_dir=tmp_path / "no-such-profile")

        command = window.command("http://127.0.0.1:8082/admin")

        assert "--window-size=1400,900" in command

    def test_open_records_applied_size_only_when_size_was_passed(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(desktop_window.sys, "platform", "win32")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        profile_dir = tmp_path / "profile"

        class _FakeProcess:
            def poll(self):
                return None

        monkeypatch.setattr(
            desktop_window.subprocess, "Popen", lambda *a, **k: _FakeProcess()
        )
        window = AppModeWindow("chrome.exe", profile_dir=profile_dir)

        window.open("http://127.0.0.1:8082/admin")

        state = desktop_window.load_desktop_state()
        assert state.last_applied_window_width == 1400
        assert state.last_applied_window_height == 900


class TestBrowserTabWindow:
    def test_open_uses_webbrowser_and_cannot_focus(self, monkeypatch):
        opened: list[str] = []
        monkeypatch.setattr(
            desktop_window.webbrowser, "open", lambda url: opened.append(url)
        )
        window = BrowserTabWindow()

        window.open("http://127.0.0.1:8082/admin")

        assert opened == ["http://127.0.0.1:8082/admin"]
        assert window.is_open is True
        assert window.focus() is False


class TestPywebviewAvailability:
    def test_unavailable_on_macos_because_the_tray_owns_the_main_thread(
        self, monkeypatch
    ):
        monkeypatch.setattr(desktop_window.sys, "platform", "darwin")

        assert PywebviewWindow.available() is False
