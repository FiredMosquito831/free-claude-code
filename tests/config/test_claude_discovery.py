"""Discovery has to be honest about what exists and where it lives.

A machine with WSL has two Claude Code installations and two settings.json
files. The page's whole job is to stop you configuring the wrong one, which it
can only do if this module reports both, labels each correctly, and never
invents a file that is not there.
"""

import subprocess
import sys
from pathlib import Path

from my_claude_code.config import claude_discovery
from my_claude_code.config.claude_discovery import (
    ORIGIN_LABELS,
    DiscoveredSettings,
    discover_settings_files,
    native_origin,
    wsl_distributions,
)


def _settings(home: Path, body: str = "{}") -> Path:
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


class TestNativeOrigin:
    def test_windows_is_windows(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        assert native_origin() == "windows"

    def test_macos_is_macos(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        assert native_origin() == "macos"

    def test_plain_linux_is_linux(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(claude_discovery, "_is_wsl", lambda: False)
        assert native_origin() == "linux"

    def test_wsl_is_not_reported_as_linux(self, monkeypatch) -> None:
        """The distinction is the entire point: two worlds, two settings files."""

        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(claude_discovery, "_is_wsl", lambda: True)
        assert native_origin() == "wsl"

    def test_every_origin_has_a_label(self) -> None:
        for origin in ("windows", "wsl", "linux", "macos"):
            assert ORIGIN_LABELS[origin]


class TestDiscovery:
    def test_a_file_that_does_not_exist_is_not_reported(
        self, monkeypatch, tmp_path
    ) -> None:
        """Listing a path that could exist is noise on a page about what is real."""

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(claude_discovery, "native_origin", lambda: "linux")
        monkeypatch.setattr(
            claude_discovery, "windows_claude_settings_path", lambda: None
        )

        assert discover_settings_files() == []

    def test_the_native_file_is_found(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(claude_discovery, "native_origin", lambda: "linux")
        monkeypatch.setattr(
            claude_discovery, "windows_claude_settings_path", lambda: None
        )
        path = _settings(tmp_path)

        found = discover_settings_files()
        assert [entry.path for entry in found] == [str(path)]
        assert found[0].origin == "linux"

    def test_wsl_also_reports_the_windows_file(self, monkeypatch, tmp_path) -> None:
        """The two-install trap, as the page has to show it."""

        home = tmp_path / "linux"
        windows_home = tmp_path / "windows"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        native = _settings(home)
        windows = _settings(windows_home)

        monkeypatch.setattr(claude_discovery, "native_origin", lambda: "wsl")
        monkeypatch.setattr(
            claude_discovery, "windows_claude_settings_path", lambda: windows
        )

        found = discover_settings_files()
        by_origin = {entry.origin: entry.path for entry in found}
        assert by_origin["wsl"] == str(native)
        assert by_origin["windows"] == str(windows)

    def test_the_native_file_is_listed_first(self, monkeypatch, tmp_path) -> None:
        home = tmp_path / "linux"
        windows_home = tmp_path / "windows"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        _settings(home)
        monkeypatch.setattr(claude_discovery, "native_origin", lambda: "wsl")
        monkeypatch.setattr(
            claude_discovery,
            "windows_claude_settings_path",
            lambda: _settings(windows_home),
        )

        assert discover_settings_files()[0].origin == "wsl"

    def test_a_config_dir_override_is_reported(self, monkeypatch, tmp_path) -> None:
        home = tmp_path / "home"
        override = tmp_path / "elsewhere"
        override.mkdir(parents=True)
        (override / "settings.json").write_text("{}", encoding="utf-8")

        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(override))
        monkeypatch.setattr(claude_discovery, "native_origin", lambda: "linux")
        monkeypatch.setattr(
            claude_discovery, "windows_claude_settings_path", lambda: None
        )

        paths = [entry.path for entry in discover_settings_files()]
        assert str(override / "settings.json") in paths

    def test_one_path_is_reported_once(self, monkeypatch, tmp_path) -> None:
        """The native path and the default home path are often the same file."""

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(claude_discovery, "native_origin", lambda: "linux")
        monkeypatch.setattr(
            claude_discovery, "windows_claude_settings_path", lambda: None
        )
        _settings(tmp_path)

        found = discover_settings_files()
        assert len(found) == len({entry.path for entry in found})


class TestWslEnumeration:
    def test_nothing_is_enumerated_off_windows(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        wsl_distributions.cache_clear()
        assert wsl_distributions() == ()

    def test_a_missing_wsl_exe_is_not_an_error(self, monkeypatch) -> None:
        """A machine without WSL must not fail the page it is loading."""

        monkeypatch.setattr(sys, "platform", "win32")

        def boom(*args, **kwargs):
            raise FileNotFoundError("wsl.exe")

        monkeypatch.setattr(subprocess, "run", boom)
        wsl_distributions.cache_clear()
        assert wsl_distributions() == ()

    def test_a_hanging_wsl_exe_is_not_an_error(self, monkeypatch) -> None:
        """Discovery runs while a page loads, so a stopped WSL cannot block it."""

        monkeypatch.setattr(sys, "platform", "win32")

        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="wsl.exe", timeout=5)

        monkeypatch.setattr(subprocess, "run", timeout)
        wsl_distributions.cache_clear()
        assert wsl_distributions() == ()

    def test_utf16_output_is_decoded(self, monkeypatch) -> None:
        """wsl.exe writes UTF-16LE on most builds."""

        monkeypatch.setattr(sys, "platform", "win32")

        class Completed:
            returncode = 0
            stdout = "Ubuntu-24.04\ndocker-desktop\n".encode("utf-16-le")

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: Completed())
        wsl_distributions.cache_clear()
        assert wsl_distributions() == ("Ubuntu-24.04", "docker-desktop")

    def test_a_nonzero_exit_yields_nothing(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")

        class Completed:
            returncode = 1
            stdout = b""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: Completed())
        wsl_distributions.cache_clear()
        assert wsl_distributions() == ()


class TestOriginLabel:
    def test_label_is_human_readable(self) -> None:
        entry = DiscoveredSettings(path="/x", origin="wsl", detail="Ubuntu")
        assert entry.origin_label == "WSL"

    def test_the_label_map_covers_every_origin(self) -> None:
        """A missing label would render the raw literal at the reader."""

        assert set(ORIGIN_LABELS) == {"windows", "wsl", "linux", "macos"}
