"""Tests for version reporting and the dashboard-triggered upgrade."""

import hashlib
import subprocess
from pathlib import Path

import pytest

from free_claude_code.application import release_updates
from free_claude_code.application.release_updates import (
    UpgradeResult,
    get_release_status,
    is_newer,
    parse_version,
    perform_upgrade,
    reset_cache_for_tests,
    upgrade_to_latest,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_cache_for_tests()
    yield
    reset_cache_for_tests()


def _release(tag: str = "v9.9.9", *, digest: str | None = None, name: str = "w.whl"):
    asset: dict[str, object] = {
        "name": name,
        "browser_download_url": f"https://example.invalid/{name}",
    }
    if digest is not None:
        asset["digest"] = f"sha256:{digest}"
    return {
        "tag_name": tag,
        "html_url": f"https://example.invalid/releases/{tag}",
        "name": f"{tag} - title",
        "published_at": "2026-07-30T23:09:20Z",
        "assets": [asset],
    }


# ----------------------------------------------------------------- versions


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4.14.2", (4, 14, 2)),
        ("v4.14.2", (4, 14, 2)),
        ("  V4.14.2  ", (4, 14, 2)),
        ("4.15", (4, 15)),
        ("", ()),
        (None, ()),
        ("not-a-version", ()),
    ],
)
def test_parse_version(text, expected) -> None:
    assert parse_version(text) == expected


def test_version_comparison_is_numeric_not_lexical() -> None:
    """4.14.10 must outrank 4.14.9; string comparison would get this wrong."""
    assert is_newer("4.14.10", "4.14.9") is True
    assert is_newer("4.14.9", "4.14.10") is False
    assert is_newer("v4.15.0", "4.14.2") is True
    assert is_newer("4.14.2", "4.14.2") is False


def test_unknown_versions_never_look_newer() -> None:
    assert is_newer(None, "4.14.2") is False
    assert is_newer("garbage", "4.14.2") is False
    assert is_newer("4.15.0", "unknown") is False


# ------------------------------------------------------------------ status


@pytest.mark.asyncio
async def test_status_reports_update_when_release_is_newer(monkeypatch) -> None:
    monkeypatch.setattr(release_updates, "current_version", lambda: "4.14.2")

    async def _fetch():
        return _release("v4.15.0"), None

    monkeypatch.setattr(release_updates, "_fetch_latest_release", _fetch)
    status = await get_release_status()
    assert status.current == "4.14.2"
    assert status.latest == "4.15.0"
    assert status.update_available is True
    assert status.release_url is not None
    assert status.release_url.endswith("v4.15.0")


@pytest.mark.asyncio
async def test_status_has_no_update_when_current(monkeypatch) -> None:
    monkeypatch.setattr(release_updates, "current_version", lambda: "4.15.0")

    async def _fetch():
        return _release("v4.15.0"), None

    monkeypatch.setattr(release_updates, "_fetch_latest_release", _fetch)
    status = await get_release_status()
    assert status.update_available is False


@pytest.mark.asyncio
async def test_offline_still_reports_the_running_version(monkeypatch) -> None:
    """A failed release check must never blank the version panel."""
    monkeypatch.setattr(release_updates, "current_version", lambda: "4.14.2")

    async def _fetch():
        return None, "Could not reach the release feed (ConnectError)."

    monkeypatch.setattr(release_updates, "_fetch_latest_release", _fetch)
    status = await get_release_status()
    assert status.current == "4.14.2"
    assert status.latest is None
    assert status.update_available is False
    assert status.error is not None
    assert "release feed" in status.error


@pytest.mark.asyncio
async def test_release_lookup_is_cached_until_forced(monkeypatch) -> None:
    monkeypatch.setattr(release_updates, "current_version", lambda: "4.14.2")
    calls = 0

    async def _fetch():
        nonlocal calls
        calls += 1
        return _release("v4.15.0"), None

    monkeypatch.setattr(release_updates, "_fetch_latest_release", _fetch)
    await get_release_status()
    await get_release_status()
    await get_release_status()
    assert calls == 1, "cached lookups must not re-hit the release feed"
    await get_release_status(force=True)
    assert calls == 2


# ----------------------------------------------------------------- upgrade


def _stub_download(monkeypatch, payload: bytes):
    class _Response:
        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self):
            yield payload

    class _Stream:
        def __enter__(self):
            return _Response()

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(release_updates.httpx, "stream", lambda *a, **k: _Stream())


def test_upgrade_refuses_a_wheel_whose_checksum_does_not_match(monkeypatch) -> None:
    """Same refusal the install scripts make, so the UI path is not weaker."""
    monkeypatch.setattr(release_updates.shutil, "which", lambda _n: "/usr/bin/uv")
    _stub_download(monkeypatch, b"actual-bytes")
    ran = False

    def _run(*_args, **_kwargs):
        nonlocal ran
        ran = True
        raise AssertionError("must not install a mismatched wheel")

    monkeypatch.setattr(release_updates.subprocess, "run", _run)

    result = upgrade_to_latest(_release(digest="0" * 64))
    assert result.ok is False
    assert "checksum mismatch" in result.message
    assert ran is False


def test_upgrade_installs_a_verified_wheel(monkeypatch, tmp_path) -> None:
    body = b"wheel-bytes"
    digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(release_updates.shutil, "which", lambda _n: "/usr/bin/uv")
    _stub_download(monkeypatch, body)
    monkeypatch.setattr(
        release_updates, "_installed_extras_and_python", lambda: ([], "3.14.0")
    )
    captured: dict[str, list[str]] = {}

    def _run(command, **_kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")

    monkeypatch.setattr(release_updates.subprocess, "run", _run)

    result = upgrade_to_latest(_release("v4.15.0", digest=digest))
    assert result.ok is True
    assert result.installed_version == "4.15.0"
    assert "Restart the server" in result.message
    command = captured["command"]
    assert "--force" in command
    assert "--refresh-package" in command
    assert "3.14.0" in command


def test_upgrade_preserves_installed_extras(monkeypatch) -> None:
    """A reinstall must not silently drop voice support."""
    body = b"wheel-bytes"
    digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(release_updates.shutil, "which", lambda _n: "/usr/bin/uv")
    _stub_download(monkeypatch, body)
    monkeypatch.setattr(
        release_updates, "_installed_extras_and_python", lambda: (["voice"], "3.14.0")
    )
    captured: dict[str, list[str]] = {}

    def _run(command, **_kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(release_updates.subprocess, "run", _run)

    result = upgrade_to_latest(_release("v4.15.0", digest=digest))
    assert result.ok is True
    assert any("[voice]" in str(part) for part in captured["command"])


def test_upgrade_reports_a_failing_install_command(monkeypatch) -> None:
    body = b"wheel-bytes"
    digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(release_updates.shutil, "which", lambda _n: "/usr/bin/uv")
    _stub_download(monkeypatch, body)
    monkeypatch.setattr(
        release_updates, "_installed_extras_and_python", lambda: ([], "3.14.0")
    )
    monkeypatch.setattr(
        release_updates.subprocess,
        "run",
        lambda command, **_k: subprocess.CompletedProcess(
            command, 2, stdout="", stderr="resolution failed"
        ),
    )
    result = upgrade_to_latest(_release("v4.15.0", digest=digest))
    assert result.ok is False
    assert "exited with code 2" in result.message
    assert any("resolution failed" in line for line in result.log)


def test_upgrade_without_uv_explains_itself(monkeypatch) -> None:
    monkeypatch.setattr(release_updates.shutil, "which", lambda _n: None)
    result = upgrade_to_latest(_release())
    assert result.ok is False
    assert "uv was not found" in result.message


def test_upgrade_requires_a_wheel_asset(monkeypatch) -> None:
    monkeypatch.setattr(release_updates.shutil, "which", lambda _n: "/usr/bin/uv")
    payload = _release()
    payload["assets"] = [{"name": "notes.txt"}]
    result = upgrade_to_latest(payload)
    assert result.ok is False
    assert "no wheel" in result.message


@pytest.mark.asyncio
async def test_perform_upgrade_declines_when_already_current(monkeypatch) -> None:
    monkeypatch.setattr(release_updates, "current_version", lambda: "4.15.0")

    async def _fetch():
        return _release("v4.15.0"), None

    monkeypatch.setattr(release_updates, "_fetch_latest_release", _fetch)
    result = await perform_upgrade()
    assert result.ok is False
    assert "Already on the latest" in result.message


@pytest.mark.asyncio
async def test_perform_upgrade_runs_off_the_event_loop(monkeypatch) -> None:
    """The install is a slow subprocess and must not block the loop."""
    monkeypatch.setattr(release_updates, "current_version", lambda: "4.14.2")

    async def _fetch():
        return _release("v4.15.0"), None

    monkeypatch.setattr(release_updates, "_fetch_latest_release", _fetch)
    threads: list[str] = []

    def _upgrade(_payload):
        import threading

        threads.append(threading.current_thread().name)
        return UpgradeResult(ok=True, message="done", installed_version="4.15.0")

    monkeypatch.setattr(release_updates, "upgrade_to_latest", _upgrade)
    result = await perform_upgrade()
    assert result.ok is True
    assert threads and "MainThread" not in threads[0]


def test_extras_and_python_come_from_the_uv_receipt(monkeypatch, tmp_path) -> None:
    receipt = tmp_path / "uv-receipt.toml"
    receipt.write_text(
        "[tool]\n"
        'requirements = [{ name = "free-claude-code", path = "/x.whl",'
        ' extras = ["voice"] }]\n'
        'python = "3.14.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(release_updates, "_receipt_path", lambda: receipt)
    extras, python = release_updates._installed_extras_and_python()
    assert extras == ["voice"]
    assert python == "3.14.0"


def test_missing_receipt_falls_back_to_the_running_python(monkeypatch) -> None:
    monkeypatch.setattr(
        release_updates, "_receipt_path", lambda: Path("/definitely/missing.toml")
    )
    extras, python = release_updates._installed_extras_and_python()
    assert extras == []
    assert python.count(".") == 2
