"""Dynamic custom provider registry with JSON persistence.

Custom providers are user-defined OpenAI-compatible providers added at runtime
(via the Admin UI/API). They live next to the static
:data:`~free_claude_code.config.provider_catalog.PROVIDER_CATALOG`: the catalog
stays import-time frozen while this registry answers "which providers exist
right now" for validation, routing, factory construction, and discovery.

This module is config-local: it must never import ``config.settings`` so the
registry can load before Settings are rebuilt.
"""

import json
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from free_claude_code.config.paths import config_dir_path
from free_claude_code.config.provider_catalog import (
    PROVIDER_CATALOG,
    ProviderDescriptor,
)

CUSTOM_PROVIDER_ID_PREFIX = "custom_"
CUSTOM_PROVIDERS_FILENAME = "custom_providers.json"
DEFAULT_CUSTOM_CREDENTIAL_ROTATION = "failover"
CUSTOM_CREDENTIAL_ROTATION_POLICIES = frozenset(
    {"single", "round_robin", "least_used", "failover"}
)

_UNSET: object = object()


@dataclass(frozen=True, slots=True)
class CustomProviderEntry:
    """One user-defined OpenAI-compatible provider."""

    provider_id: str
    display_name: str
    base_url: str
    api_keys: tuple[str, ...]
    credential_rotation: str = DEFAULT_CUSTOM_CREDENTIAL_ROTATION
    proxy: str | None = None
    enabled: bool = True
    added_at: str = ""


def _slugify(display_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", display_name.lower()).strip("_")
    return slug or "provider"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ProviderRegistry:
    """Thread-safe registry of static + custom providers with persistence."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._custom: dict[str, CustomProviderEntry] = {}
        self._on_change: list[Callable[[], None]] = []
        self._loaded = False

    # ------------------------------------------------------------------ path

    def _storage_path(self) -> Path:
        if self._path is not None:
            return self._path
        return config_dir_path() / CUSTOM_PROVIDERS_FILENAME

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = self._storage_path()
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(
                "Custom provider registry load failed: path={} reason={}",
                path,
                exc,
            )
            return
        providers = payload.get("providers") if isinstance(payload, dict) else None
        if not isinstance(providers, list):
            return
        for item in providers:
            entry = self._entry_from_payload(item)
            if entry is not None and entry.provider_id not in self._custom:
                self._custom[entry.provider_id] = entry

    @staticmethod
    def _entry_from_payload(item: object) -> CustomProviderEntry | None:
        if not isinstance(item, dict):
            return None
        provider_id = item.get("provider_id")
        display_name = item.get("display_name")
        base_url = item.get("base_url")
        api_keys = item.get("api_keys")
        if not (
            isinstance(provider_id, str)
            and provider_id.startswith(CUSTOM_PROVIDER_ID_PREFIX)
            and isinstance(display_name, str)
            and isinstance(base_url, str)
            and isinstance(api_keys, list)
        ):
            return None
        keys = tuple(key for key in api_keys if isinstance(key, str) and key.strip())
        rotation = item.get("credential_rotation")
        proxy = item.get("proxy")
        added_at = item.get("added_at")
        return CustomProviderEntry(
            provider_id=provider_id,
            display_name=display_name,
            base_url=base_url,
            api_keys=keys,
            credential_rotation=(
                rotation
                if isinstance(rotation, str)
                and rotation in CUSTOM_CREDENTIAL_ROTATION_POLICIES
                else DEFAULT_CUSTOM_CREDENTIAL_ROTATION
            ),
            proxy=proxy if isinstance(proxy, str) and proxy.strip() else None,
            enabled=bool(item.get("enabled", True)),
            added_at=added_at if isinstance(added_at, str) else "",
        )

    # ------------------------------------------------------------- persistence

    def _persist_locked(self) -> None:
        path = self._storage_path()
        payload = {
            "providers": [
                {
                    "provider_id": entry.provider_id,
                    "display_name": entry.display_name,
                    "base_url": entry.base_url,
                    "api_keys": list(entry.api_keys),
                    "credential_rotation": entry.credential_rotation,
                    "proxy": entry.proxy,
                    "enabled": entry.enabled,
                    "added_at": entry.added_at,
                }
                for entry in self._custom.values()
            ]
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)

    # ------------------------------------------------------------------ reads

    def list_custom(self) -> tuple[CustomProviderEntry, ...]:
        """Return all custom providers in insertion order (enabled or not)."""
        with self._lock:
            self._ensure_loaded()
            return tuple(self._custom.values())

    def get(self, provider_id: str) -> CustomProviderEntry | None:
        """Return one custom provider entry by id, if present."""
        with self._lock:
            self._ensure_loaded()
            return self._custom.get(provider_id)

    def all_descriptors(self) -> Mapping[str, ProviderDescriptor]:
        """Return static catalog descriptors plus enabled custom providers."""
        with self._lock:
            self._ensure_loaded()
            descriptors: dict[str, ProviderDescriptor] = dict(PROVIDER_CATALOG)
            for entry in self._custom.values():
                if entry.enabled:
                    descriptors[entry.provider_id] = self.descriptor_for(entry)
            return descriptors

    def supported_ids(self) -> tuple[str, ...]:
        """Return static catalog order followed by enabled custom ids."""
        return tuple(self.all_descriptors())

    @staticmethod
    def descriptor_for(entry: CustomProviderEntry) -> ProviderDescriptor:
        """Build the dynamic descriptor for one custom provider entry."""
        return ProviderDescriptor(
            provider_id=entry.provider_id,
            display_name=entry.display_name,
            static_credential=entry.api_keys[0] if entry.api_keys else None,
            default_base_url=entry.base_url,
            dynamic=True,
        )

    # ---------------------------------------------------------------- mutations

    def add(
        self,
        display_name: str,
        base_url: str,
        api_keys: tuple[str, ...] | list[str],
        credential_rotation: str = DEFAULT_CUSTOM_CREDENTIAL_ROTATION,
        proxy: str | None = None,
        enabled: bool = True,
    ) -> CustomProviderEntry:
        """Register a new custom provider; the id is slugged from the name."""
        name = display_name.strip()
        if not name:
            raise ValueError("Custom provider display_name must not be empty")
        url = base_url.strip()
        if not url:
            raise ValueError("Custom provider base_url must not be empty")
        keys = tuple(key for key in (k.strip() for k in api_keys) if key)
        if not keys:
            raise ValueError("Custom provider requires at least one API key")
        if credential_rotation not in CUSTOM_CREDENTIAL_ROTATION_POLICIES:
            raise ValueError(
                f"Unknown credential_rotation: {credential_rotation!r}. "
                f"Valid: {sorted(CUSTOM_CREDENTIAL_ROTATION_POLICIES)}"
            )
        with self._lock:
            self._ensure_loaded()
            provider_id = self._unique_provider_id_locked(name)
            entry = CustomProviderEntry(
                provider_id=provider_id,
                display_name=name,
                base_url=url,
                api_keys=keys,
                credential_rotation=credential_rotation,
                proxy=proxy.strip()
                if isinstance(proxy, str) and proxy.strip()
                else None,
                enabled=enabled,
                added_at=_utc_now_iso(),
            )
            self._custom[provider_id] = entry
            self._persist_locked()
        self._notify_change()
        return entry

    def _unique_provider_id_locked(self, display_name: str) -> str:
        base = f"{CUSTOM_PROVIDER_ID_PREFIX}{_slugify(display_name)}"
        taken = set(PROVIDER_CATALOG) | set(self._custom)
        candidate = base
        suffix = 2
        while candidate in taken:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate

    def update(
        self,
        provider_id: str,
        *,
        display_name: str | None = None,
        base_url: str | None = None,
        api_keys: tuple[str, ...] | list[str] | None = None,
        credential_rotation: str | None = None,
        proxy: str | None | object = _UNSET,
        enabled: bool | None = None,
    ) -> CustomProviderEntry:
        """Update fields of one custom provider and return the new entry."""
        with self._lock:
            self._ensure_loaded()
            current = self._custom.get(provider_id)
            if current is None:
                raise KeyError(f"Unknown custom provider: {provider_id!r}")
            if credential_rotation is not None and (
                credential_rotation not in CUSTOM_CREDENTIAL_ROTATION_POLICIES
            ):
                raise ValueError(
                    f"Unknown credential_rotation: {credential_rotation!r}. "
                    f"Valid: {sorted(CUSTOM_CREDENTIAL_ROTATION_POLICIES)}"
                )
            updated = CustomProviderEntry(
                provider_id=current.provider_id,
                display_name=(
                    display_name.strip()
                    if isinstance(display_name, str) and display_name.strip()
                    else current.display_name
                ),
                base_url=(
                    base_url.strip()
                    if isinstance(base_url, str) and base_url.strip()
                    else current.base_url
                ),
                api_keys=(
                    tuple(key for key in (k.strip() for k in api_keys) if key)
                    if api_keys is not None
                    else current.api_keys
                ),
                credential_rotation=(
                    credential_rotation
                    if credential_rotation is not None
                    else current.credential_rotation
                ),
                proxy=(
                    current.proxy
                    if proxy is _UNSET
                    else (
                        proxy.strip()
                        if isinstance(proxy, str) and proxy.strip()
                        else None
                    )
                ),
                enabled=current.enabled if enabled is None else bool(enabled),
                added_at=current.added_at,
            )
            self._custom[provider_id] = updated
            self._persist_locked()
        self._notify_change()
        return updated

    def remove(self, provider_id: str) -> CustomProviderEntry:
        """Remove one custom provider and return the removed entry."""
        with self._lock:
            self._ensure_loaded()
            entry = self._custom.pop(provider_id, None)
            if entry is None:
                raise KeyError(f"Unknown custom provider: {provider_id!r}")
            self._persist_locked()
        self._notify_change()
        return entry

    # ------------------------------------------------------------------ hooks

    def on_change(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked after every registry mutation."""
        with self._lock:
            self._on_change.append(callback)

    def _notify_change(self) -> None:
        with self._lock:
            callbacks = tuple(self._on_change)
        for callback in callbacks:
            try:
                callback()
            except Exception as exc:
                logger.warning("Custom provider on_change hook failed: {}", exc)

    # ------------------------------------------------------------------ test

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._custom.clear()
            self._on_change.clear()
            self._loaded = False


_registry_lock = threading.Lock()
_registry: ProviderRegistry | None = None


def get_provider_registry() -> ProviderRegistry:
    """Return the process-wide provider registry (lazy, loads once)."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = ProviderRegistry()
    return _registry


def reset_provider_registry() -> None:
    """Drop the process-wide registry singleton (test isolation)."""
    global _registry
    with _registry_lock:
        _registry = None
