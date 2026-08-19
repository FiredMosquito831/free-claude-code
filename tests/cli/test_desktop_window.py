"""Tests for the desktop window provider chain and the Chromium binary search."""

from pathlib import Path

import pytest

from my_claude_code.cli import desktop_window
from my_claude_code.cli.desktop_window import (
    PROVIDER_CHAIN,
    AppModeWindow,
    BrowserTabWindow,
    PywebviewWindow,
    chromium_binary,
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


class TestChromiumSearch:
    def _which(self, monkeypatch, found: dict[str, str]):
        monkeypatch.setattr(desktop_window, "which", lambda name: found.get(name))
        monkeypatch.setattr(Path, "is_file", lambda self: False)

    def test_windows_prefers_edge(self, monkeypatch):
        monkeypatch.setattr(desktop_window.sys, "platform", "win32")
        self._which(monkeypatch, {"msedge": "E:/msedge.exe", "chrome": "C:/chrome.exe"})

        assert chromium_binary() == "E:/msedge.exe"

    def test_windows_falls_back_to_chrome_then_brave(self, monkeypatch):
        monkeypatch.setattr(desktop_window.sys, "platform", "win32")
        self._which(monkeypatch, {"brave": "B:/brave.exe"})

        assert chromium_binary() == "B:/brave.exe"

    def test_macos_uses_known_application_paths(self, monkeypatch):
        monkeypatch.setattr(desktop_window.sys, "platform", "darwin")
        monkeypatch.setattr(desktop_window, "which", lambda name: None)
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        monkeypatch.setattr(Path, "is_file", lambda self: self.as_posix() == chrome)

        assert chromium_binary() == chrome

    def test_linux_search_order(self, monkeypatch):
        monkeypatch.setattr(desktop_window.sys, "platform", "linux")
        self._which(
            monkeypatch, {"chromium": "/usr/bin/chromium", "brave-browser": "/b"}
        )

        assert chromium_binary() == "/usr/bin/chromium"

    def test_no_browser_found_returns_none(self, monkeypatch):
        monkeypatch.setattr(desktop_window.sys, "platform", "linux")
        self._which(monkeypatch, {})

        assert chromium_binary() is None


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
