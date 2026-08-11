"""Admin custom provider CRUD endpoints against the real provider registry.

These tests used to inject a hand-written fake registry. Its ``add()`` took a
prebuilt entry, the real ``ProviderRegistry.add()`` takes the fields and
allocates the id itself, and nothing compared the two -- so every create
returned HTTP 500 in production while this file stayed green. The registry is
an in-memory dict plus one JSON file, so there is no reason to double it: point
the real thing at ``tmp_path`` and the contract cannot drift again.
"""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import admin_custom_routes
from my_claude_code.config.provider_registry import ProviderRegistry
from tests.api.support import create_test_app, runtime_for_app

_ENV_KEYS = ("FCC_ENV_FILE",)


def _registry(tmp_path: Path) -> ProviderRegistry:
    return ProviderRegistry(tmp_path / "custom_providers.json")


def _seeded_registry(tmp_path: Path, **overrides: Any) -> ProviderRegistry:
    """Return a registry holding one ``custom_acme`` entry."""
    registry = _registry(tmp_path)
    entry = registry.add(
        display_name="Acme",
        base_url="https://api.acme.example/v1",
        api_keys=("sk-acme-aaaa1111bbbb",),
        credential_rotation="failover",
    )
    if overrides:
        registry.update(entry.provider_id, **overrides)
    return registry


def _local_client(app):
    return TestClient(app, client=("127.0.0.1", 50000))


def _make_app(monkeypatch, tmp_path: Path, registry: ProviderRegistry):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    app = create_test_app()
    runtime = runtime_for_app(app)
    reload_providers = AsyncMock()
    test_provider = AsyncMock(
        return_value={"provider_id": "custom_acme", "ok": True, "models": ["m2", "m1"]}
    )
    monkeypatch.setattr(runtime, "reload_providers", reload_providers)
    monkeypatch.setattr(runtime, "test_provider", test_provider)
    app.dependency_overrides[admin_custom_routes.get_custom_provider_registry] = (
        lambda: registry
    )
    return app, reload_providers, test_provider


def test_list_custom_providers_empty(monkeypatch, tmp_path):
    app, _, _ = _make_app(monkeypatch, tmp_path, _registry(tmp_path))

    response = _local_client(app).get("/admin/api/custom-providers")

    assert response.status_code == 200
    assert response.json() == {"providers": []}


def test_list_custom_providers_serializes_entries(monkeypatch, tmp_path):
    registry = _seeded_registry(tmp_path)
    app, _, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).get("/admin/api/custom-providers")

    assert response.status_code == 200
    (provider,) = response.json()["providers"]
    assert provider["provider_id"] == "custom_acme"
    assert provider["display_name"] == "Acme"
    assert provider["base_url"] == "https://api.acme.example/v1"
    assert provider["key_count"] == 1
    assert provider["masked_keys"] == ["sk-acm…bbbb"]
    assert provider["credential_rotation"] == "failover"
    assert provider["proxy"] is None
    assert provider["enabled"] is True
    assert provider["status"] == "configured"
    assert provider["models"] == []
    assert provider["model_count"] == 0
    assert provider["added_at"].startswith("20")
    assert "sk-acme-aaaa1111bbbb" not in response.text


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({"enabled": False}, "disabled"),
        ({"api_keys": ()}, "missing_key"),
        ({"api_keys": (), "enabled": False}, "disabled"),
    ],
)
def test_list_custom_providers_status_mapping(monkeypatch, tmp_path, overrides, status):
    registry = _seeded_registry(tmp_path, **overrides)
    app, _, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).get("/admin/api/custom-providers")

    assert response.json()["providers"][0]["status"] == status


def test_create_custom_provider_registers_and_detects_models(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    app, reload_providers, test_provider = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).post(
        "/admin/api/custom-providers",
        json={
            "display_name": "Acme AI",
            "base_url": "https://api.acme.example/v1/",
            "api_key": "sk-acme-aaaa1111bbbb",
            "credential_rotation": "round_robin",
            "proxy": "http://127.0.0.1:7890",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider_id"] == "custom_acme_ai"
    assert body["display_name"] == "Acme AI"
    assert body["base_url"] == "https://api.acme.example/v1"
    assert body["key_count"] == 1
    assert body["masked_keys"] == ["sk-acm…bbbb"]
    assert body["credential_rotation"] == "round_robin"
    assert body["proxy"] == "http://127.0.0.1:7890"
    assert body["status"] == "configured"
    assert body["models"] == ["m1", "m2"]
    assert body["model_count"] == 2
    assert "test_error" not in body
    assert "sk-acme-aaaa1111bbbb" not in response.text

    stored = registry.get("custom_acme_ai")
    assert stored is not None
    assert stored.api_keys == ("sk-acme-aaaa1111bbbb",)
    assert stored.enabled is True
    reload_providers.assert_awaited_once_with(reason="custom_provider_change")
    test_provider.assert_awaited_once_with("custom_acme_ai")


def test_create_custom_provider_test_failure_is_non_fatal(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    app, reload_providers, test_provider = _make_app(monkeypatch, tmp_path, registry)
    test_provider.return_value = {
        "provider_id": "custom_acme_ai",
        "ok": False,
        "error_type": "ConnectError",
    }

    response = _local_client(app).post(
        "/admin/api/custom-providers",
        json={
            "display_name": "Acme AI",
            "base_url": "https://api.acme.example/v1",
            "api_key": "sk-acme-aaaa1111bbbb",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider_id"] == "custom_acme_ai"
    assert body["test_error"] == "ConnectError"
    assert body["models"] == []
    assert registry.get("custom_acme_ai") is not None
    reload_providers.assert_awaited_once()


def test_create_custom_provider_default_rotation(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    app, _, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).post(
        "/admin/api/custom-providers",
        json={
            "display_name": "Acme",
            "base_url": "https://api.acme.example/v1",
            "api_key": "sk-acme-aaaa1111bbbb",
        },
    )

    assert response.status_code == 200
    assert response.json()["credential_rotation"] == "failover"


def test_create_custom_provider_duplicate_slug_is_409(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    app, _, _ = _make_app(monkeypatch, tmp_path, registry)
    payload = {
        "display_name": "Acme",
        "base_url": "https://api.acme.example/v1",
        "api_key": "sk-acme-aaaa1111bbbb",
    }
    assert (
        _local_client(app).post("/admin/api/custom-providers", json=payload).is_success
    )

    response = _local_client(app).post("/admin/api/custom-providers", json=payload)

    assert response.status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"display_name": "", "base_url": "https://a.example", "api_key": "k"},
        {"display_name": "   ", "base_url": "https://a.example", "api_key": "k"},
        {"display_name": "!!!", "base_url": "https://a.example", "api_key": "k"},
        {"display_name": "Acme", "base_url": "not-a-url", "api_key": "k"},
        {"display_name": "Acme", "base_url": "ftp://a.example", "api_key": "k"},
        {"display_name": "Acme", "base_url": "https://a.example", "api_key": ""},
        {"display_name": "Acme", "base_url": "https://a.example", "api_key": "  "},
        {"display_name": "Acme", "base_url": "https://a.example", "api_key": "k1,k2"},
        {
            "display_name": "Acme",
            "base_url": "https://a.example",
            "api_key": "k",
            "credential_rotation": "random",
        },
        {
            "display_name": "Acme",
            "base_url": "https://a.example",
            "api_key": "k",
            "proxy": "gopher://proxy",
        },
    ],
)
def test_create_custom_provider_validation_errors(monkeypatch, tmp_path, payload):
    app, reload_providers, _ = _make_app(monkeypatch, tmp_path, _registry(tmp_path))

    response = _local_client(app).post("/admin/api/custom-providers", json=payload)

    assert response.status_code == 422
    reload_providers.assert_not_awaited()


def test_update_custom_provider_applies_changes_and_reloads(monkeypatch, tmp_path):
    registry = _seeded_registry(tmp_path)
    app, reload_providers, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).patch(
        "/admin/api/custom-providers/custom_acme",
        json={
            "display_name": "Acme Renamed",
            "base_url": "https://v2.acme.example/v1",
            "credential_rotation": "least_used",
            "enabled": False,
            "proxy": "http://127.0.0.1:8080",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "Acme Renamed"
    assert body["base_url"] == "https://v2.acme.example/v1"
    assert body["credential_rotation"] == "least_used"
    assert body["enabled"] is False
    assert body["status"] == "disabled"
    assert body["proxy"] == "http://127.0.0.1:8080"
    reload_providers.assert_awaited_once_with(reason="custom_provider_change")


def test_update_custom_provider_clears_proxy(monkeypatch, tmp_path):
    registry = _seeded_registry(tmp_path, proxy="http://127.0.0.1:8080")
    app, _, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).patch(
        "/admin/api/custom-providers/custom_acme",
        json={"proxy": ""},
    )

    assert response.status_code == 200
    assert response.json()["proxy"] is None


def test_update_custom_provider_unknown_is_404(monkeypatch, tmp_path):
    app, reload_providers, _ = _make_app(monkeypatch, tmp_path, _registry(tmp_path))

    response = _local_client(app).patch(
        "/admin/api/custom-providers/custom_nope",
        json={"enabled": False},
    )

    assert response.status_code == 404
    reload_providers.assert_not_awaited()


def test_update_custom_provider_empty_body_is_422(monkeypatch, tmp_path):
    registry = _seeded_registry(tmp_path)
    app, reload_providers, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).patch(
        "/admin/api/custom-providers/custom_acme",
        json={},
    )

    assert response.status_code == 422
    reload_providers.assert_not_awaited()


@pytest.mark.parametrize(
    "payload",
    [
        {"display_name": "  "},
        {"base_url": "not-a-url"},
        {"credential_rotation": "random"},
    ],
)
def test_update_custom_provider_validation_errors(monkeypatch, tmp_path, payload):
    registry = _seeded_registry(tmp_path)
    app, reload_providers, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).patch(
        "/admin/api/custom-providers/custom_acme",
        json=payload,
    )

    assert response.status_code == 422
    reload_providers.assert_not_awaited()


def test_add_custom_provider_key_appends_and_reloads(monkeypatch, tmp_path):
    registry = _seeded_registry(tmp_path)
    app, reload_providers, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).post(
        "/admin/api/custom-providers/custom_acme/keys",
        json={"api_key": "sk-acme-cccc2222dddd"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["key_count"] == 2
    assert body["masked_keys"] == ["sk-acm…bbbb", "sk-acm…dddd"]
    assert body["added"] == "sk-acm…dddd"
    stored_keys = registry.get("custom_acme")
    assert stored_keys is not None
    assert stored_keys.api_keys == (
        "sk-acme-aaaa1111bbbb",
        "sk-acme-cccc2222dddd",
    )
    reload_providers.assert_awaited_once_with(reason="custom_provider_change")
    assert "sk-acme-cccc2222dddd" not in response.text


def test_add_custom_provider_key_duplicate_is_409(monkeypatch, tmp_path):
    registry = _seeded_registry(tmp_path)
    app, _, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).post(
        "/admin/api/custom-providers/custom_acme/keys",
        json={"api_key": "sk-acme-aaaa1111bbbb"},
    )

    assert response.status_code == 409


def test_add_custom_provider_key_unknown_provider_is_404(monkeypatch, tmp_path):
    app, _, _ = _make_app(monkeypatch, tmp_path, _registry(tmp_path))

    response = _local_client(app).post(
        "/admin/api/custom-providers/custom_nope/keys",
        json={"api_key": "sk-acme-aaaa1111bbbb"},
    )

    assert response.status_code == 404


@pytest.mark.parametrize("bad_key", ["", "   ", "k1,k2"])
def test_add_custom_provider_key_rejects_empty_or_comma_keys(
    monkeypatch, tmp_path, bad_key
):
    registry = _seeded_registry(tmp_path)
    app, reload_providers, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).post(
        "/admin/api/custom-providers/custom_acme/keys",
        json={"api_key": bad_key},
    )

    assert response.status_code == 422
    reload_providers.assert_not_awaited()


def test_delete_custom_provider_key_removes_index(monkeypatch, tmp_path):
    registry = _seeded_registry(
        tmp_path, api_keys=("sk-acme-aaaa1111bbbb", "sk-acme-cccc2222dddd")
    )
    app, reload_providers, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).delete(
        "/admin/api/custom-providers/custom_acme/keys/0"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["key_count"] == 1
    assert body["masked_keys"] == ["sk-acm…dddd"]
    assert body["removed"] == "sk-acm…bbbb"
    reload_providers.assert_awaited_once_with(reason="custom_provider_change")


def test_delete_custom_provider_last_key_keeps_provider(monkeypatch, tmp_path):
    registry = _seeded_registry(tmp_path)
    app, _, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).delete(
        "/admin/api/custom-providers/custom_acme/keys/0"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["key_count"] == 0
    assert body["status"] == "missing_key"
    assert registry.get("custom_acme") is not None


def test_delete_custom_provider_key_out_of_range_is_404(monkeypatch, tmp_path):
    registry = _seeded_registry(tmp_path)
    app, reload_providers, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).delete(
        "/admin/api/custom-providers/custom_acme/keys/3"
    )

    assert response.status_code == 404
    reload_providers.assert_not_awaited()


def test_delete_custom_provider_removes_and_reloads(monkeypatch, tmp_path):
    registry = _seeded_registry(tmp_path)
    app, reload_providers, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).delete("/admin/api/custom-providers/custom_acme")

    assert response.status_code == 200
    assert response.json() == {
        "applied": True,
        "provider_id": "custom_acme",
        "removed": True,
    }
    assert registry.get("custom_acme") is None
    reload_providers.assert_awaited_once_with(reason="custom_provider_change")


def test_delete_custom_provider_unknown_is_404(monkeypatch, tmp_path):
    app, reload_providers, _ = _make_app(monkeypatch, tmp_path, _registry(tmp_path))

    response = _local_client(app).delete("/admin/api/custom-providers/custom_nope")

    assert response.status_code == 404
    reload_providers.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/admin/api/custom-providers", None),
        (
            "post",
            "/admin/api/custom-providers",
            {
                "display_name": "Acme",
                "base_url": "https://a.example",
                "api_key": "k",
            },
        ),
        ("patch", "/admin/api/custom-providers/custom_acme", {"enabled": False}),
        (
            "post",
            "/admin/api/custom-providers/custom_acme/keys",
            {"api_key": "k2"},
        ),
        ("delete", "/admin/api/custom-providers/custom_acme/keys/0", None),
        ("delete", "/admin/api/custom-providers/custom_acme", None),
    ],
)
def test_custom_provider_endpoints_are_loopback_only(
    monkeypatch, tmp_path, method, path, payload
):
    registry = _seeded_registry(tmp_path)
    app, _, _ = _make_app(monkeypatch, tmp_path, registry)
    remote = TestClient(app, client=("203.0.113.10", 50000))

    response = remote.request(method, path, json=payload)

    assert response.status_code == 403
