import sys
from pathlib import Path

from my_claude_code.config import paths


def test_is_wsl_false_when_osrelease_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths, "WSL_OSRELEASE_PATH", str(tmp_path / "does-not-exist"))

    assert paths._is_wsl() is False
    assert paths.windows_claude_settings_path() is None


def test_is_wsl_false_for_plain_linux_osrelease(monkeypatch, tmp_path: Path) -> None:
    osrelease = tmp_path / "osrelease"
    osrelease.write_text("5.15.0-91-generic\n", encoding="utf-8")
    monkeypatch.setattr(paths, "WSL_OSRELEASE_PATH", str(osrelease))

    assert paths._is_wsl() is False


def test_is_wsl_true_for_mixed_case_microsoft_osrelease(
    monkeypatch, tmp_path: Path
) -> None:
    osrelease = tmp_path / "osrelease"
    osrelease.write_text("5.15.167.4-Microsoft-standard-WSL2\n", encoding="utf-8")
    monkeypatch.setattr(paths, "WSL_OSRELEASE_PATH", str(osrelease))

    assert paths._is_wsl() is True


def _write_wsl_osrelease(monkeypatch, tmp_path: Path) -> None:
    """Point WSL_OSRELEASE_PATH at a file containing a real WSL2 osrelease string."""

    osrelease = tmp_path / "osrelease"
    osrelease.write_text("5.15.167.4-microsoft-standard-WSL2\n", encoding="utf-8")
    monkeypatch.setattr(paths, "WSL_OSRELEASE_PATH", str(osrelease))


def test_windows_path_none_when_users_dir_missing(monkeypatch, tmp_path: Path) -> None:
    _write_wsl_osrelease(monkeypatch, tmp_path)
    monkeypatch.setattr(paths, "WSL_WINDOWS_USERS_DIR", str(tmp_path / "no-such-dir"))

    assert paths.windows_claude_settings_path() is None


def test_windows_path_found_via_claude_dir(monkeypatch, tmp_path: Path) -> None:
    _write_wsl_osrelease(monkeypatch, tmp_path)
    users_dir = tmp_path / "Users"
    users_dir.mkdir()
    other_user = users_dir / "OtherUser"
    other_user.mkdir()
    matching_user = users_dir / "MatchingUser"
    matching_user.mkdir()
    (matching_user / paths.CLAUDE_CONFIG_DIRNAME).mkdir()
    monkeypatch.setattr(paths, "WSL_WINDOWS_USERS_DIR", str(users_dir))

    result = paths.windows_claude_settings_path()

    assert (
        result
        == matching_user / paths.CLAUDE_CONFIG_DIRNAME / paths.CLAUDE_SETTINGS_FILENAME
    )


def test_windows_path_falls_back_to_username_env(monkeypatch, tmp_path: Path) -> None:
    _write_wsl_osrelease(monkeypatch, tmp_path)
    users_dir = tmp_path / "Users"
    users_dir.mkdir()
    (users_dir / "SomeoneElse").mkdir()
    target_user = users_dir / "envuser"
    target_user.mkdir()
    monkeypatch.setattr(paths, "WSL_WINDOWS_USERS_DIR", str(users_dir))
    monkeypatch.setenv("USERNAME", "envuser")
    monkeypatch.delenv("USER", raising=False)

    result = paths.windows_claude_settings_path()

    assert (
        result
        == target_user / paths.CLAUDE_CONFIG_DIRNAME / paths.CLAUDE_SETTINGS_FILENAME
    )


def test_windows_path_none_when_username_env_dir_does_not_exist(
    monkeypatch, tmp_path: Path
) -> None:
    _write_wsl_osrelease(monkeypatch, tmp_path)
    users_dir = tmp_path / "Users"
    users_dir.mkdir()
    (users_dir / "SomeoneElse").mkdir()
    monkeypatch.setattr(paths, "WSL_WINDOWS_USERS_DIR", str(users_dir))
    monkeypatch.setenv("USERNAME", "ghost-user")
    monkeypatch.delenv("USER", raising=False)

    result = paths.windows_claude_settings_path()

    assert result is None


def test_windows_path_scan_survives_unreadable_entry(
    monkeypatch, tmp_path: Path
) -> None:
    _write_wsl_osrelease(monkeypatch, tmp_path)
    users_dir = tmp_path / "Users"
    users_dir.mkdir()
    bad_entry = users_dir / "Bad"
    bad_entry.mkdir()
    good_entry = users_dir / "Good"
    good_entry.mkdir()
    (good_entry / paths.CLAUDE_CONFIG_DIRNAME).mkdir()
    monkeypatch.setattr(paths, "WSL_WINDOWS_USERS_DIR", str(users_dir))

    bad_claude_dir = bad_entry / paths.CLAUDE_CONFIG_DIRNAME
    original_is_dir = Path.is_dir

    def fake_is_dir(self: Path) -> bool:
        if self == bad_claude_dir:
            raise OSError("permission denied")
        return original_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", fake_is_dir)

    result = paths.windows_claude_settings_path()

    assert (
        result
        == good_entry / paths.CLAUDE_CONFIG_DIRNAME / paths.CLAUDE_SETTINGS_FILENAME
    )


def test_settings_candidates_single_path_when_not_wsl(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(paths, "WSL_OSRELEASE_PATH", str(tmp_path / "does-not-exist"))

    result = paths.claude_settings_candidates()

    assert result == [paths.claude_settings_path()]


def test_settings_candidates_dedup_and_ordered_under_wsl(
    monkeypatch, tmp_path: Path
) -> None:
    _write_wsl_osrelease(monkeypatch, tmp_path)
    users_dir = tmp_path / "Users"
    users_dir.mkdir()
    matching_user = users_dir / "MatchingUser"
    matching_user.mkdir()
    (matching_user / paths.CLAUDE_CONFIG_DIRNAME).mkdir()
    monkeypatch.setattr(paths, "WSL_WINDOWS_USERS_DIR", str(users_dir))

    result = paths.claude_settings_candidates()

    windows_path = (
        matching_user / paths.CLAUDE_CONFIG_DIRNAME / paths.CLAUDE_SETTINGS_FILENAME
    )
    assert result == [paths.claude_settings_path(), windows_path]

    # Calling again must not duplicate entries even if both paths coincide.
    monkeypatch.setattr(
        paths, "windows_claude_settings_path", lambda: paths.claude_settings_path()
    )
    deduped = paths.claude_settings_candidates()
    assert deduped == [paths.claude_settings_path()]


def test_managed_settings_paths_darwin(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    managed_file = tmp_path / "managed-settings.json"
    managed_file.write_text("{}", encoding="utf-8")
    dropin_dir = tmp_path / "managed-settings.d"
    dropin_dir.mkdir()
    monkeypatch.setattr(paths, "MACOS_MANAGED_SETTINGS_PATH", str(managed_file))
    monkeypatch.setattr(paths, "MACOS_MANAGED_SETTINGS_DROPIN_DIR", str(dropin_dir))

    result = paths.claude_managed_settings_paths()

    assert result == [managed_file]


def test_managed_settings_paths_windows(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    managed_file = tmp_path / "managed-settings.json"
    managed_file.write_text("{}", encoding="utf-8")
    dropin_dir = tmp_path / "managed-settings.d"
    dropin_dir.mkdir()
    monkeypatch.setattr(paths, "WINDOWS_MANAGED_SETTINGS_PATH", str(managed_file))
    monkeypatch.setattr(paths, "WINDOWS_MANAGED_SETTINGS_DROPIN_DIR", str(dropin_dir))

    result = paths.claude_managed_settings_paths()

    assert result == [managed_file]


def test_managed_settings_paths_linux_includes_dropin_json_only(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    managed_file = tmp_path / "managed-settings.json"
    managed_file.write_text("{}", encoding="utf-8")
    dropin_dir = tmp_path / "managed-settings.d"
    dropin_dir.mkdir()
    fragment = dropin_dir / "10-fragment.json"
    fragment.write_text("{}", encoding="utf-8")
    non_json = dropin_dir / "README.txt"
    non_json.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(paths, "LINUX_MANAGED_SETTINGS_PATH", str(managed_file))
    monkeypatch.setattr(paths, "LINUX_MANAGED_SETTINGS_DROPIN_DIR", str(dropin_dir))

    result = paths.claude_managed_settings_paths()

    assert result == [managed_file, fragment]


def test_managed_settings_paths_empty_when_nothing_exists(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        paths, "LINUX_MANAGED_SETTINGS_PATH", str(tmp_path / "does-not-exist.json")
    )
    monkeypatch.setattr(
        paths, "LINUX_MANAGED_SETTINGS_DROPIN_DIR", str(tmp_path / "no-dropin-dir")
    )

    assert paths.claude_managed_settings_paths() == []


def test_managed_settings_paths_survives_unreadable_dropin_dir(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        paths, "LINUX_MANAGED_SETTINGS_PATH", str(tmp_path / "does-not-exist.json")
    )
    dropin_dir = tmp_path / "managed-settings.d"
    dropin_dir.mkdir()
    monkeypatch.setattr(paths, "LINUX_MANAGED_SETTINGS_DROPIN_DIR", str(dropin_dir))

    def fake_is_dir(self: Path) -> bool:
        if self == dropin_dir:
            raise OSError("permission denied")
        return False

    monkeypatch.setattr(Path, "is_dir", fake_is_dir)

    assert paths.claude_managed_settings_paths() == []
