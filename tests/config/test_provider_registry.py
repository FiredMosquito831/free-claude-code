"""Tests for the dynamic custom provider registry."""

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.config.provider_registry import (
    CUSTOM_PROVIDERS_FILENAME,
    CustomProviderEntry,
    ProviderRegistry,
    get_provider_registry,
    reset_provider_registry,
)


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    return tmp_path / "custom_providers.json"


@pytest.fixture
def registry(registry_path: Path) -> ProviderRegistry:
    return ProviderRegistry(registry_path)


def _add_acme(registry: ProviderRegistry, **overrides: Any) -> CustomProviderEntry:
    kwargs: dict[str, Any] = {
        "display_name": "Acme AI",
        "base_url": "https://api.acme.test/v1",
        "api_keys": ("sk-acme-1",),
    }
    kwargs.update(overrides)
    return registry.add(**kwargs)


def test_add_slugifies_provider_id(registry: ProviderRegistry) -> None:
    entry = _add_acme(registry, display_name="Acme AI! Pro")

    assert entry.provider_id == "custom_acme_ai_pro"
    assert entry.display_name == "Acme AI! Pro"
    assert entry.credential_rotation == "failover"
    assert entry.enabled is True
    assert entry.added_at


def test_add_ensures_unique_ids(registry: ProviderRegistry) -> None:
    first = _add_acme(registry)
    second = _add_acme(registry)

    assert first.provider_id == "custom_acme_ai"
    assert second.provider_id == "custom_acme_ai_2"


def test_add_rejects_invalid_input(registry: ProviderRegistry) -> None:
    with pytest.raises(ValueError, match="display_name"):
        registry.add("", "https://api.acme.test/v1", ("k",))
    with pytest.raises(ValueError, match="base_url"):
        registry.add("Acme", "  ", ("k",))
    with pytest.raises(ValueError, match="at least one API key"):
        registry.add("Acme", "https://api.acme.test/v1", (" ",))
    with pytest.raises(ValueError, match="credential_rotation"):
        registry.add(
            "Acme",
            "https://api.acme.test/v1",
            ("k",),
            credential_rotation="chaos",
        )


def test_add_never_collides_with_static_catalog(registry: ProviderRegistry) -> None:
    entry = _add_acme(registry, display_name="Groq")

    assert entry.provider_id == "custom_groq"
    assert entry.provider_id not in PROVIDER_CATALOG


def test_persistence_roundtrip(registry_path: Path) -> None:
    registry = ProviderRegistry(registry_path)
    entry = _add_acme(
        registry,
        api_keys=("sk-1", "sk-2"),
        credential_rotation="round_robin",
        proxy="http://proxy.test:8080",
    )

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert payload["providers"][0]["provider_id"] == entry.provider_id
    assert payload["providers"][0]["api_keys"] == ["sk-1", "sk-2"]

    reloaded = ProviderRegistry(registry_path)
    restored = reloaded.get(entry.provider_id)
    assert restored == entry


def test_corrupt_file_is_tolerated(registry_path: Path) -> None:
    registry_path.write_text("not json", encoding="utf-8")

    registry = ProviderRegistry(registry_path)

    assert registry.list_custom() == ()
    assert registry.supported_ids() == tuple(PROVIDER_CATALOG)


def test_update_mutates_selected_fields(registry: ProviderRegistry) -> None:
    entry = _add_acme(registry)

    updated = registry.update(
        entry.provider_id,
        display_name="Acme Renamed",
        proxy=None,
        enabled=False,
        credential_rotation="least_used",
    )

    assert updated.display_name == "Acme Renamed"
    assert updated.proxy is None
    assert updated.enabled is False
    assert updated.credential_rotation == "least_used"
    assert updated.base_url == entry.base_url
    assert updated.added_at == entry.added_at


def test_update_can_replace_api_keys(registry: ProviderRegistry) -> None:
    entry = _add_acme(registry)

    updated = registry.update(entry.provider_id, api_keys=("sk-a", "sk-b", "sk-c"))

    assert updated.api_keys == ("sk-a", "sk-b", "sk-c")


def test_update_unknown_provider_raises(registry: ProviderRegistry) -> None:
    with pytest.raises(KeyError):
        registry.update("custom_missing", display_name="Nope")


def test_remove_deletes_and_persists(registry_path: Path) -> None:
    registry = ProviderRegistry(registry_path)
    entry = _add_acme(registry)

    removed = registry.remove(entry.provider_id)

    assert removed == entry
    assert registry.get(entry.provider_id) is None
    assert ProviderRegistry(registry_path).list_custom() == ()
    with pytest.raises(KeyError):
        registry.remove(entry.provider_id)


def test_all_descriptors_includes_only_enabled_custom(
    registry: ProviderRegistry,
) -> None:
    enabled = _add_acme(registry)
    disabled = _add_acme(registry, display_name="Beta")
    registry.update(disabled.provider_id, enabled=False)

    descriptors = registry.all_descriptors()

    assert set(descriptors) == set(PROVIDER_CATALOG) | {enabled.provider_id}
    dynamic = descriptors[enabled.provider_id]
    assert dynamic.dynamic is True
    assert dynamic.static_credential == "sk-acme-1"
    assert dynamic.default_base_url == "https://api.acme.test/v1"
    assert dynamic.credential_attr is None
    assert dynamic.base_url_attr is None


def test_supported_ids_static_order_then_custom_insertion(
    registry: ProviderRegistry,
) -> None:
    first = _add_acme(registry)
    second = _add_acme(registry, display_name="Beta")

    ids = registry.supported_ids()

    assert ids[: len(PROVIDER_CATALOG)] == tuple(PROVIDER_CATALOG)
    assert ids[len(PROVIDER_CATALOG) :] == (
        first.provider_id,
        second.provider_id,
    )


def test_on_change_hooks_fire_on_mutations(registry: ProviderRegistry) -> None:
    calls: list[str] = []
    registry.on_change(lambda: calls.append("hit"))

    entry = _add_acme(registry)
    registry.update(entry.provider_id, enabled=False)
    registry.remove(entry.provider_id)

    assert calls == ["hit", "hit", "hit"]


def test_concurrent_adds_stay_consistent(registry: ProviderRegistry) -> None:
    def add_one(index: int) -> None:
        registry.add(
            f"Provider {index}", f"https://api{index}.test/v1", (f"sk-{index}",)
        )

    threads = [threading.Thread(target=add_one, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    entries = registry.list_custom()
    assert len(entries) == 8
    assert len({entry.provider_id for entry in entries}) == 8


def test_singleton_roundtrip() -> None:
    reset_provider_registry()
    first = get_provider_registry()
    second = get_provider_registry()
    assert first is second
    reset_provider_registry()
    assert get_provider_registry() is not first


def test_default_storage_path_uses_config_dir(monkeypatch, tmp_path: Path) -> None:
    import free_claude_code.config.provider_registry as module

    monkeypatch.setattr(module, "config_dir_path", lambda: tmp_path)
    registry = ProviderRegistry()
    _add_acme(registry)

    assert (tmp_path / CUSTOM_PROVIDERS_FILENAME).is_file()
