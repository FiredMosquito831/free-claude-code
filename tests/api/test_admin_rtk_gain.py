"""Tests for the admin RTK gain endpoint."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from my_claude_code.config import rtk as rtk_config
from tests.api.support import create_test_app

_REASONS = (
    "not_installed",
    "run_failed",
    "empty_output",
    "invalid_json",
    "unexpected_schema",
    "timeout",
)


def _set_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def _local_client(app):
    return TestClient(app, client=("127.0.0.1", 50000))


def _get_gain(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()
    with _local_client(app) as client:
        return client.get("/admin/api/rtk/gain")


def test_returns_parsed_gain(monkeypatch, tmp_path):
    payload = {"summary": {"total_saved": 99, "total_commands": 3}}

    def fake_run(command, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=0)

    monkeypatch.setattr(rtk_config, "_available_binary", lambda: Path("/usr/bin/rtk"))
    monkeypatch.setattr(rtk_config.subprocess, "run", fake_run)

    response = _get_gain(monkeypatch, tmp_path)

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["summary"]["total_saved"] == 99
    assert body["summary"]["avg_savings_pct"] is None


def test_missing_binary_returns_200_with_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(rtk_config, "_available_binary", lambda: None)

    response = _get_gain(monkeypatch, tmp_path)

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["reason"] == "not_installed"
    assert body["summary"] is None


def test_every_failure_reason_returns_200(monkeypatch, tmp_path):
    for reason in _REASONS:
        monkeypatch.setattr(
            rtk_config,
            "read_rtk_gain",
            lambda reason=reason: {
                "available": False,
                "reason": reason,
                "detail": "stubbed",
                "binary_path": None,
                "summary": None,
                "periods": None,
                "raw": None,
            },
        )
        monkeypatch.setattr(
            "my_claude_code.api.admin_routes.read_rtk_gain",
            rtk_config.read_rtk_gain,
        )

        response = _get_gain(monkeypatch, tmp_path)

        assert response.status_code == 200, reason
        assert response.json()["reason"] == reason


def test_status_exposes_the_pinned_version(monkeypatch, tmp_path):
    monkeypatch.setattr(rtk_config, "_available_binary", lambda: None)
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()

    with _local_client(app) as client:
        body = client.get("/admin/api/rtk").json()

    assert body["pinned_version"] == rtk_config.RTK_VERSION
    assert body["installed_version"] is None
    assert body["version_matches_pin"] is None


def test_status_flags_a_binary_that_drifted_from_the_pin(monkeypatch, tmp_path):
    monkeypatch.setattr(rtk_config, "_available_binary", lambda: Path("/usr/bin/rtk"))
    monkeypatch.setattr(rtk_config, "_verify_rtk", lambda _b: "rtk 0.1.2")
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()

    with _local_client(app) as client:
        body = client.get("/admin/api/rtk").json()

    assert body["installed_version"] == "0.1.2"
    assert body["version_matches_pin"] is False
