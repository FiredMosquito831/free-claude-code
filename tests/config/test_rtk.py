"""Tests for config/rtk.py state persistence and machine reconciliation."""

import hashlib
import io
import json
import os
import stat
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from my_claude_code.config import rtk as rtk_config
from my_claude_code.config.rtk import (
    RTK_TELEMETRY_ENV,
    RTK_VERSION,
    RtkError,
    RtkState,
    _ensure_rtk_binary,
    _managed_binary_path,
    apply_rtk_state,
    load_rtk_state,
    rtk_state_path,
    rtk_status,
    save_rtk_state,
)

_RTK_PAYLOAD = b"fake-rtk-executable"


def _set_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def _asset_for(platform: str, machine: str) -> tuple[str, str]:
    key = (platform, rtk_config._normalized_architecture(machine))
    release = rtk_config._RELEASES[key]
    return release[0], release[1]


def _make_tar_gz_bytes(asset_name: str) -> bytes:
    buffer = io.BytesIO()
    executable_name = "rtk.exe" if asset_name.endswith(".zip") else "rtk"
    if executable_name == "rtk.exe":
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("rtk.exe", _RTK_PAYLOAD)
    else:
        with tarfile.open(fileobj=buffer, mode="w:gz", name=asset_name) as archive:
            info = tarfile.TarInfo("rtk")
            info.size = len(_RTK_PAYLOAD)
            archive.addfile(info, io.BytesIO(_RTK_PAYLOAD))
    return buffer.getvalue()


class TestLoadRtkState:
    def test_missing_file_returns_defaults(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        state = load_rtk_state()

        assert state == RtkState()

    def test_corrupt_file_returns_defaults(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = rtk_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json{{{", encoding="utf-8")

        state = load_rtk_state()

        assert state == RtkState()

    def test_non_dict_json_returns_defaults(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = rtk_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2, 3]", encoding="utf-8")

        assert load_rtk_state() == RtkState()

    def test_unknown_keys_are_ignored(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = rtk_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"claude": True, "future_key": "x", "bogus": 5}),
            encoding="utf-8",
        )

        state = load_rtk_state()

        assert state.claude is True
        assert state.codex is False
        assert state.pi is False

    def test_non_boolean_value_falls_back_to_default(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = rtk_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"claude": "yes", "codex": 1}),
            encoding="utf-8",
        )

        state = load_rtk_state()

        assert state.claude is False
        assert state.codex is False


class TestSaveRtkState:
    def test_round_trip(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        save_rtk_state(RtkState(claude=True, codex=True, pi=True))

        assert load_rtk_state() == RtkState(claude=True, codex=True, pi=True)

    def test_creates_parent_dirs(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        assert not rtk_state_path().exists()

        save_rtk_state(RtkState())

        assert rtk_state_path().is_file()

    def test_atomic_tmp_file_does_not_linger(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        save_rtk_state(RtkState())

        assert not rtk_state_path().with_suffix(".json.tmp").exists()

    def test_write_failure_raises_rtk_error(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        blocker = rtk_state_path().parent
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("blocked", encoding="utf-8")

        with pytest.raises(RtkError):
            save_rtk_state(RtkState())

    def test_writes_flat_boolean_json(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        save_rtk_state(RtkState(claude=True))

        persisted = json.loads(rtk_state_path().read_text(encoding="utf-8"))
        assert persisted == {"claude": True, "codex": False, "pi": False}


class TestPlatformSelection:
    def test_release_for_each_pinned_platform(self):
        assert rtk_config._RELEASES == {
            ("linux", "x86_64"): (
                "rtk-x86_64-unknown-linux-musl.tar.gz",
                "d94cc2a3e57fa534892b5235a726e7eeb7523f205a5f8f48f853bfcae7be7e33",
            ),
            ("linux", "aarch64"): (
                "rtk-aarch64-unknown-linux-gnu.tar.gz",
                "5cd3f7fa2697faf9e5b77a10ce4e699006e02d4752d792f06550697eb4b8e8a9",
            ),
            ("darwin", "x86_64"): (
                "rtk-x86_64-apple-darwin.tar.gz",
                "636f808db86b2cefab7db7dd9393da8b6e4721bb2ffaa0644e3ffa52d3420d81",
            ),
            ("darwin", "aarch64"): (
                "rtk-aarch64-apple-darwin.tar.gz",
                "b7c2218eca538b54e63fa594a8ce58bd3716851b01b3b0dc026515323baf6393",
            ),
            ("win32", "x86_64"): (
                "rtk-x86_64-pc-windows-msvc.zip",
                "3a1e114edce9080f8a10663e9c87488363a82f14a5ca8aab2ad416817f89d47c",
            ),
        }

    @pytest.mark.parametrize(
        ("platform_name", "machine", "asset_name"),
        [
            ("linux", "x86_64", "rtk-x86_64-unknown-linux-musl.tar.gz"),
            ("linux", "amd64", "rtk-x86_64-unknown-linux-musl.tar.gz"),
            ("linux", "aarch64", "rtk-aarch64-unknown-linux-gnu.tar.gz"),
            ("linux", "arm64", "rtk-aarch64-unknown-linux-gnu.tar.gz"),
            ("darwin", "x86_64", "rtk-x86_64-apple-darwin.tar.gz"),
            ("darwin", "aarch64", "rtk-aarch64-apple-darwin.tar.gz"),
            ("win32", "x86_64", "rtk-x86_64-pc-windows-msvc.zip"),
        ],
    )
    def test_release_for_current_platform(
        self, monkeypatch, platform_name, machine, asset_name
    ):
        monkeypatch.setattr(sys, "platform", platform_name)
        monkeypatch.setattr(rtk_config.platform, "machine", lambda: machine)

        assert rtk_config._release_for_current_platform()[0] == asset_name

    def test_unsupported_platform_raises(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "freebsd")
        monkeypatch.setattr(rtk_config.platform, "machine", lambda: "x86_64")

        with pytest.raises(RtkError):
            rtk_config._release_for_current_platform()


class TestEnsureRtkBinary:
    def test_downloads_verifies_extracts_and_chmods(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        asset_name, _ = _asset_for("linux", "x86_64")
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(rtk_config.platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(rtk_config.shutil, "which", lambda _name: None)

        archive_bytes = _make_tar_gz_bytes(asset_name)
        # Pin the expected digest to the fixture we built so verification passes.
        fixture_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        monkeypatch.setitem(
            rtk_config._RELEASES, ("linux", "x86_64"), (asset_name, fixture_sha256)
        )
        urls: list[str] = []

        def fake_urlopen(url, timeout=0):
            urls.append(url)
            assert timeout == 60
            return io.BytesIO(archive_bytes)

        monkeypatch.setattr(rtk_config.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(
            rtk_config.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(
                returncode=0, stdout="rtk 0.44.2\n", stderr=""
            ),
        )

        binary = _ensure_rtk_binary()

        assert urls == [
            f"https://github.com/rtk-ai/rtk/releases/download/v{RTK_VERSION}/{asset_name}"
        ]
        assert binary == _managed_binary_path()
        assert binary.read_bytes() == _RTK_PAYLOAD
        # The execute bit is only meaningful on POSIX; Windows chmod only flips
        # the read-only attribute, so it is asserted where it applies.
        if os.name == "posix":
            assert binary.stat().st_mode & stat.S_IXUSR

    def test_checksum_mismatch_raises_and_leaves_no_binary(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        asset_name, _ = _asset_for("linux", "x86_64")
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(rtk_config.platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(rtk_config.shutil, "which", lambda _name: None)

        archive_bytes = _make_tar_gz_bytes(asset_name)
        tampered = bytearray(archive_bytes)
        tampered[-1] ^= 0xFF
        monkeypatch.setattr(
            rtk_config.urllib.request,
            "urlopen",
            lambda *a, **k: io.BytesIO(bytes(tampered)),
        )

        with pytest.raises(RtkError, match="checksum verification failed"):
            _ensure_rtk_binary()

        assert not _managed_binary_path().exists()
        assert not _managed_binary_path().with_name(".rtk.tmp").exists()

    def test_download_failure_raises(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(rtk_config.platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(rtk_config.shutil, "which", lambda _name: None)

        def fail_urlopen(url, timeout=0):
            raise OSError("connection refused")

        monkeypatch.setattr(rtk_config.urllib.request, "urlopen", fail_urlopen)

        with pytest.raises(RtkError, match="Could not download"):
            _ensure_rtk_binary()

        assert not _managed_binary_path().exists()

    def test_existing_binary_is_verified_not_replaced(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        monkeypatch.setattr(rtk_config.shutil, "which", lambda _name: "/usr/bin/rtk")
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout="rtk 0.44.2\n", stderr="")

        monkeypatch.setattr(rtk_config.subprocess, "run", fake_run)

        binary = _ensure_rtk_binary()

        assert binary == Path("/usr/bin/rtk")
        assert calls == [["/usr/bin/rtk", "--version"]]
        assert not _managed_binary_path().exists()

    def test_unsupported_platform_raises_before_download(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "platform", "freebsd")
        monkeypatch.setattr(rtk_config.platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(rtk_config.shutil, "which", lambda _name: None)

        with pytest.raises(RtkError, match="no release"):
            _ensure_rtk_binary()


class TestApplyRtkState:
    def _record(self, monkeypatch):
        binary = Path("/fake/.local/bin/rtk")
        calls: list[tuple[list[str], dict]] = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout="rtk 0.44.2\n", stderr="")

        monkeypatch.setattr(rtk_config.shutil, "which", lambda _name: str(binary))
        monkeypatch.setattr(rtk_config.subprocess, "run", fake_run)
        return calls

    def test_enable_runs_enable_for_true_and_uninstall_for_false(
        self, monkeypatch, tmp_path
    ):
        _set_home(monkeypatch, tmp_path)
        calls = self._record(monkeypatch)

        apply_rtk_state(RtkState(claude=True, pi=True))

        commands = [command for command, _kwargs in calls]
        assert any(
            command[1:] == ["init", "-g", "--auto-patch"] for command in commands
        )
        assert not any(command[1:] == ["init", "-g", "--codex"] for command in commands)
        assert any(
            command[1:] == ["init", "-g", "--agent", "pi"] for command in commands
        )
        assert any(
            command[1:] == ["init", "--uninstall", "-g", "--codex"]
            for command in commands
        )

    def test_all_false_runs_uninstall_for_every_agent(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        calls = self._record(monkeypatch)

        apply_rtk_state(RtkState())

        commands = [command for command, _kwargs in calls]
        assert any(command[1:] == ["init", "-g", "--uninstall"] for command in commands)
        assert any(
            command[1:] == ["init", "--uninstall", "-g", "--codex"]
            for command in commands
        )
        assert any(
            command[1:] == ["init", "--uninstall", "-g", "--agent", "pi"]
            for command in commands
        )

    def test_telemetry_is_disabled_for_every_invocation(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        calls = self._record(monkeypatch)

        apply_rtk_state(RtkState(claude=True))

        for _command, kwargs in calls:
            assert kwargs["env"][RTK_TELEMETRY_ENV] == "1"

    def test_enable_claude_creates_claude_config_directory(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        binary = Path("/fake/.local/bin/rtk")
        monkeypatch.setattr(rtk_config.shutil, "which", lambda _name: str(binary))
        monkeypatch.setattr(
            rtk_config.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(
                returncode=0, stdout="rtk 0.44.2\n", stderr=""
            ),
        )

        apply_rtk_state(RtkState(claude=True))

        assert (Path.home() / ".claude").is_dir()

    def test_enable_claude_respects_claude_config_dir_env(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        custom = tmp_path / "custom-claude"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        binary = Path("/fake/.local/bin/rtk")
        monkeypatch.setattr(rtk_config.shutil, "which", lambda _name: str(binary))
        monkeypatch.setattr(
            rtk_config.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(
                returncode=0, stdout="rtk 0.44.2\n", stderr=""
            ),
        )

        apply_rtk_state(RtkState(claude=True))

        assert custom.is_dir()

    def test_uninstall_removes_managed_binary(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        managed = _managed_binary_path()
        managed.parent.mkdir(parents=True, exist_ok=True)
        managed.write_bytes(b"rtk")

        binary = Path("/fake/.local/bin/rtk")
        monkeypatch.setattr(rtk_config.shutil, "which", lambda _name: str(binary))
        monkeypatch.setattr(
            rtk_config.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )

        apply_rtk_state(RtkState(), uninstall=True)

        assert not managed.exists()

    def test_disabled_state_skips_download(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        monkeypatch.setattr(rtk_config.shutil, "which", lambda _name: None)
        monkeypatch.setattr(
            rtk_config, "_ensure_rtk_binary", lambda: pytest.fail("must not download")
        )
        monkeypatch.setattr(rtk_config, "_run_rtk", lambda *a, **k: None)

        apply_rtk_state(RtkState())

        assert not _managed_binary_path().exists()


class TestRtkStatus:
    def test_status_reports_state_and_binary(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        save_rtk_state(RtkState(claude=True))
        binary = Path("/fake/.local/bin/rtk")
        monkeypatch.setattr(rtk_config.shutil, "which", lambda _name: str(binary))
        monkeypatch.setattr(
            rtk_config.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(
                returncode=0, stdout="rtk 0.44.2\n", stderr=""
            ),
        )

        status = rtk_status()

        assert status == {
            "installed": True,
            "claude": True,
            "codex": False,
            "pi": False,
            "binary_path": str(binary),
            "version": "rtk 0.44.2",
        }

    def test_status_reports_not_installed(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        monkeypatch.setattr(rtk_config.shutil, "which", lambda _name: None)

        status = rtk_status()

        assert status["installed"] is False
        assert status["binary_path"] is None
        assert status["version"] is None
