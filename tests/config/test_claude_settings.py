import json
from pathlib import Path

import pytest

from free_claude_code.config.claude_settings import (
    CLAUDE_AUTH_TOKEN_ENV,
    CLAUDE_BASE_URL_ENV,
    CLAUDE_SETTINGS_BACKUP_SUFFIX,
    ClaudeSettingsError,
    apply_proxy_env,
    clear_proxy_env,
    read_status,
)

BASE_URL = "http://127.0.0.1:8317"
AUTH_TOKEN = "fcc-secret-token"


def test_missing_file_is_unset(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"

    status = read_status(
        path=path, expected_base_url=BASE_URL, expected_auth_token=AUTH_TOKEN
    )

    assert status.exists is False
    assert status.state == "unset"
    assert status.error is None


def test_apply_creates_file_with_expected_content(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"

    status = apply_proxy_env(path=path, base_url=BASE_URL, auth_token=AUTH_TOKEN)

    assert status.state == "configured"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {
        "env": {CLAUDE_BASE_URL_ENV: BASE_URL, CLAUDE_AUTH_TOKEN_ENV: AUTH_TOKEN}
    }


def test_apply_preserves_unrelated_keys(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "theme": "dark",
                "env": {"OTHER_KEY": "keep-me"},
            }
        ),
        encoding="utf-8",
    )

    apply_proxy_env(path=path, base_url=BASE_URL, auth_token=AUTH_TOKEN)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["env"]["OTHER_KEY"] == "keep-me"
    assert data["env"][CLAUDE_BASE_URL_ENV] == BASE_URL
    assert data["env"][CLAUDE_AUTH_TOKEN_ENV] == AUTH_TOKEN


def test_mismatch_before_apply_configured_after(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "env": {
                    CLAUDE_BASE_URL_ENV: "http://wrong",
                    CLAUDE_AUTH_TOKEN_ENV: "wrong-token",
                }
            }
        ),
        encoding="utf-8",
    )

    before = read_status(
        path=path, expected_base_url=BASE_URL, expected_auth_token=AUTH_TOKEN
    )
    assert before.state == "mismatch"

    apply_proxy_env(path=path, base_url=BASE_URL, auth_token=AUTH_TOKEN)

    after = read_status(
        path=path, expected_base_url=BASE_URL, expected_auth_token=AUTH_TOKEN
    )
    assert after.state == "configured"


def test_malformed_json_is_unreadable_and_apply_raises_without_modifying_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    original = "{not valid json"
    path.write_text(original, encoding="utf-8")

    status = read_status(
        path=path, expected_base_url=BASE_URL, expected_auth_token=AUTH_TOKEN
    )
    assert status.parsed is False
    assert status.state == "unreadable"
    assert status.error is not None

    with pytest.raises(ClaudeSettingsError):
        apply_proxy_env(path=path, base_url=BASE_URL, auth_token=AUTH_TOKEN)

    assert path.read_text(encoding="utf-8") == original


def test_top_level_array_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    status = read_status(
        path=path, expected_base_url=BASE_URL, expected_auth_token=AUTH_TOKEN
    )

    assert status.parsed is False
    assert status.state == "unreadable"


def test_backup_created_once_and_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"env": {}}), encoding="utf-8")
    backup_path = path.with_name(path.name + CLAUDE_SETTINGS_BACKUP_SUFFIX)

    apply_proxy_env(path=path, base_url=BASE_URL, auth_token=AUTH_TOKEN)
    assert backup_path.exists()
    first_backup_content = backup_path.read_text(encoding="utf-8")
    assert json.loads(first_backup_content) == {"env": {}}

    apply_proxy_env(path=path, base_url="http://changed", auth_token="changed-token")

    assert backup_path.read_text(encoding="utf-8") == first_backup_content


def test_clear_removes_only_expected_keys_and_drops_empty_env(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "theme": "dark",
                "env": {
                    CLAUDE_BASE_URL_ENV: BASE_URL,
                    CLAUDE_AUTH_TOKEN_ENV: AUTH_TOKEN,
                },
            }
        ),
        encoding="utf-8",
    )

    status = clear_proxy_env(path=path)

    assert status.state == "unset"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"theme": "dark"}


def test_clear_preserves_unrelated_env_entries(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "env": {
                    CLAUDE_BASE_URL_ENV: BASE_URL,
                    CLAUDE_AUTH_TOKEN_ENV: AUTH_TOKEN,
                    "OTHER_KEY": "keep-me",
                }
            }
        ),
        encoding="utf-8",
    )

    clear_proxy_env(path=path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"env": {"OTHER_KEY": "keep-me"}}


def test_clear_on_file_without_keys_performs_no_write(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    mtime_before = path.stat().st_mtime_ns

    status = clear_proxy_env(path=path)

    assert status.state == "unset"
    assert path.stat().st_mtime_ns == mtime_before
    assert json.loads(path.read_text(encoding="utf-8")) == {"theme": "dark"}


def test_clear_missing_file_returns_status_without_error(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"

    status = clear_proxy_env(path=path)

    assert status.exists is False
    assert status.error is None


def test_local_override_detected_when_sibling_sets_base_url(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({}), encoding="utf-8")
    local_path = tmp_path / "settings.local.json"
    local_path.write_text(
        json.dumps({"env": {CLAUDE_BASE_URL_ENV: "http://local-override"}}),
        encoding="utf-8",
    )

    status = read_status(
        path=path, expected_base_url=BASE_URL, expected_auth_token=AUTH_TOKEN
    )

    assert status.local_override == str(local_path)


def test_local_override_none_when_sibling_malformed(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({}), encoding="utf-8")
    local_path = tmp_path / "settings.local.json"
    local_path.write_text("{not valid json", encoding="utf-8")

    status = read_status(
        path=path, expected_base_url=BASE_URL, expected_auth_token=AUTH_TOKEN
    )

    assert status.local_override is None


def test_no_status_field_ever_contains_raw_auth_token(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"

    status = apply_proxy_env(path=path, base_url=BASE_URL, auth_token=AUTH_TOKEN)

    for value in vars(status).values():
        assert AUTH_TOKEN not in str(value)


def test_non_object_env_is_unreadable_and_never_clobbered(tmp_path: Path) -> None:
    # A present-but-wrong-shaped "env" used to be replaced wholesale, destroying
    # whatever the user meant by it. It is a refusal, like a parse failure.
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"env": "not-an-object", "keep": 1}), encoding="utf-8")
    original = path.read_bytes()

    status = read_status(
        path=path, expected_base_url=BASE_URL, expected_auth_token=AUTH_TOKEN
    )

    assert status.state == "unreadable"
    assert status.parsed is False

    with pytest.raises(ClaudeSettingsError):
        apply_proxy_env(path=path, base_url=BASE_URL, auth_token=AUTH_TOKEN)
    with pytest.raises(ClaudeSettingsError):
        clear_proxy_env(path=path)

    assert path.read_bytes() == original
    assert not (tmp_path / f"settings.json{CLAUDE_SETTINGS_BACKUP_SUFFIX}").exists()


def test_clear_carries_expected_values_into_status(tmp_path: Path) -> None:
    # The unset response still has to tell the UI what a re-apply would write.
    path = tmp_path / "settings.json"
    apply_proxy_env(path=path, base_url=BASE_URL, auth_token=AUTH_TOKEN)

    status = clear_proxy_env(
        path=path, expected_base_url=BASE_URL, expected_auth_token=AUTH_TOKEN
    )

    assert status.state == "unset"
    assert status.expected_base_url == BASE_URL
    assert AUTH_TOKEN not in json.dumps(vars(status))
