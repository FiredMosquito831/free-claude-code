"""Tests for the Claude Code settings-file admin routes."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from free_claude_code.config.proxy_auth import proxy_auth_token
from free_claude_code.config.server_urls import local_proxy_root_url
from free_claude_code.config.settings import Settings
from tests.api.support import create_test_app


def _local_client(app):
    return TestClient(app, client=("127.0.0.1", 50000))


def _set_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def test_get_claude_settings_returns_default_path_and_status(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()

    response = _local_client(app).get("/admin/api/claude-settings")
    assert response.status_code == 200
    body = response.json()

    default_path = str(tmp_path / ".claude" / "settings.json")
    assert body["default_path"] == default_path
    assert default_path in body["suggested_paths"]
    assert body["status"]["path"] == default_path
    assert body["status"]["state"] == "unset"
    assert body["status"]["exists"] is False


def test_get_claude_settings_honours_caller_supplied_path(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()

    settings_file = tmp_path / "custom" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(json.dumps({"env": {}}), encoding="utf-8")

    response = _local_client(app).get(
        "/admin/api/claude-settings", params={"path": str(settings_file)}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"]["path"] == str(settings_file.resolve())
    assert body["status"]["exists"] is True
    assert body["status"]["state"] == "unset"


def test_get_claude_settings_rejects_relative_path(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()

    response = _local_client(app).get(
        "/admin/api/claude-settings", params={"path": "relative/settings.json"}
    )
    assert response.status_code == 400


def test_get_claude_settings_rejects_non_json_path(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()

    non_json = tmp_path / "settings.txt"
    response = _local_client(app).get(
        "/admin/api/claude-settings", params={"path": str(non_json)}
    )
    assert response.status_code == 400


def test_apply_and_unset_round_trip_against_tmp_path(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()
    settings = Settings()
    expected_base_url = local_proxy_root_url(settings)
    expected_auth_token = proxy_auth_token(settings.anthropic_auth_token)

    settings_file = tmp_path / "target" / "settings.json"

    apply_response = _local_client(app).post(
        "/admin/api/claude-settings/apply", json={"path": str(settings_file)}
    )
    assert apply_response.status_code == 200
    applied_status = apply_response.json()["status"]
    assert applied_status["state"] == "configured"
    assert applied_status["base_url_matches"] is True
    assert applied_status["auth_token_matches"] is True

    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["env"]["ANTHROPIC_BASE_URL"] == expected_base_url
    assert on_disk["env"]["ANTHROPIC_AUTH_TOKEN"] == expected_auth_token

    unset_response = _local_client(app).post(
        "/admin/api/claude-settings/unset", json={"path": str(settings_file)}
    )
    assert unset_response.status_code == 200
    unset_status = unset_response.json()["status"]
    assert unset_status["state"] == "unset"

    on_disk_after = json.loads(settings_file.read_text(encoding="utf-8"))
    assert "ANTHROPIC_BASE_URL" not in on_disk_after.get("env", {})
    assert "ANTHROPIC_AUTH_TOKEN" not in on_disk_after.get("env", {})


def test_apply_claude_settings_maps_settings_error_to_409(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()

    malformed = tmp_path / "malformed.json"
    malformed.write_text("not json", encoding="utf-8")

    response = _local_client(app).post(
        "/admin/api/claude-settings/apply", json={"path": str(malformed)}
    )
    assert response.status_code == 409


def test_unset_claude_settings_maps_settings_error_to_409(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()

    malformed = tmp_path / "malformed.json"
    malformed.write_text("not json", encoding="utf-8")

    response = _local_client(app).post(
        "/admin/api/claude-settings/unset", json={"path": str(malformed)}
    )
    assert response.status_code == 409


def test_claude_settings_responses_never_contain_the_auth_token(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "super-secret-token-value")
    app = create_test_app()

    settings_file = tmp_path / "target" / "settings.json"

    get_response = _local_client(app).get(
        "/admin/api/claude-settings", params={"path": str(settings_file)}
    )
    apply_response = _local_client(app).post(
        "/admin/api/claude-settings/apply", json={"path": str(settings_file)}
    )
    unset_response = _local_client(app).post(
        "/admin/api/claude-settings/unset", json={"path": str(settings_file)}
    )

    for response in (get_response, apply_response, unset_response):
        assert "super-secret-token-value" not in response.text


def test_unset_response_still_describes_what_a_reapply_would_write(
    monkeypatch, tmp_path
):
    # Unsetting must not blank out the expectations the card renders, or the UI
    # loses the URL it is offering to configure.
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()
    settings_file = tmp_path / ".claude" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(json.dumps({}), encoding="utf-8")

    client = _local_client(app)
    client.post("/admin/api/claude-settings/apply", json={"path": str(settings_file)})
    response = client.post(
        "/admin/api/claude-settings/unset", json={"path": str(settings_file)}
    )

    assert response.status_code == 200
    status = response.json()["status"]
    assert status["state"] == "unset"
    assert status["expected_base_url"] == local_proxy_root_url(Settings())
    assert proxy_auth_token(Settings().anthropic_auth_token) not in response.text
