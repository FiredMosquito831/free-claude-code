"""Admin CRUD for dynamic custom OpenAI-compatible providers.

The registry module (``config.provider_registry``) lands with the backend
slice in a parallel worktree. This module codes straight against its contract:
``CustomProviderEntry(provider_id, display_name, base_url, api_keys,
credential_rotation, proxy, enabled, added_at)`` and a ``ProviderRegistry``
with ``list_custom()``, ``get()``, ``add()``, ``update()``, and ``remove()``.
Imports happen inside functions so this module loads before the merge; tests
inject a fake in-memory registry via dependency override.
"""

import importlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .admin_routes import _mask_credential_key, require_loopback_admin
from .dependencies import get_services
from .ports import ApiServices

router = APIRouter()

ROTATION_POLICIES = ("single", "round_robin", "least_used", "failover")
DEFAULT_ROTATION = "failover"
HOT_RELOAD_REASON = "custom_provider_change"


@dataclass(frozen=True, slots=True)
class CustomProviderEntryData:
    """Duck-typed mirror of ``config.provider_registry.CustomProviderEntry``.

    Used only when the real registry module is not importable yet (pre-merge
    tests); attribute-compatible with the contract entry.
    """

    provider_id: str
    display_name: str
    base_url: str
    api_keys: tuple[str, ...]
    credential_rotation: str
    proxy: str | None
    enabled: bool
    added_at: str


_REGISTRY_MODULE = "free_claude_code.config.provider_registry"


def _registry_module() -> Any:
    """Import the backend-owned registry module (dynamic: lands with merge)."""
    return importlib.import_module(_REGISTRY_MODULE)


def get_custom_provider_registry() -> Any:
    """Return the process-wide custom provider registry singleton.

    Dynamic import: the backend slice owns ``config.provider_registry`` and it
    may not exist in every checkout. Tests override this dependency with a fake.
    """
    try:
        module = _registry_module()
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail="Custom provider registry is unavailable",
        ) from exc
    return module.get_provider_registry()


def _new_entry(**fields: Any) -> Any:
    """Build one registry entry, preferring the real contract dataclass."""
    try:
        module = _registry_module()
    except ImportError:
        return CustomProviderEntryData(**fields)
    return module.CustomProviderEntry(**fields)


class CustomProviderCreatePayload(BaseModel):
    """Payload for registering one custom provider."""

    display_name: str
    base_url: str
    api_key: str
    credential_rotation: str = DEFAULT_ROTATION
    proxy: str | None = None


class CustomProviderUpdatePayload(BaseModel):
    """Partial update for one custom provider."""

    display_name: str | None = None
    base_url: str | None = None
    proxy: str | None = None
    enabled: bool | None = None
    credential_rotation: str | None = None


class CustomProviderKeyPayload(BaseModel):
    """Single API key appended to one custom provider."""

    api_key: str


def _validate_display_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Display name is empty")
    return name


def _validate_base_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(
            status_code=422,
            detail="base_url must be an http(s) URL",
        )
    return url


def _validate_api_key(value: str) -> str:
    key = value.strip()
    if not key:
        raise HTTPException(status_code=422, detail="API key is empty")
    if "," in key:
        raise HTTPException(
            status_code=422,
            detail="Paste a single API key without commas",
        )
    return key


def _validate_rotation(value: str) -> str:
    rotation = value.strip()
    if rotation not in ROTATION_POLICIES:
        raise HTTPException(
            status_code=422,
            detail=(
                "credential_rotation must be one of: " + ", ".join(ROTATION_POLICIES)
            ),
        )
    return rotation


def _normalize_proxy(value: str | None) -> str | None:
    if value is None:
        return None
    proxy = value.strip()
    if not proxy:
        return None
    parsed = urlsplit(proxy)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"}:
        raise HTTPException(
            status_code=422,
            detail="proxy must be an http(s) or socks5 URL",
        )
    return proxy


def _slug_provider_id(display_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", display_name.strip().lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if not slug:
        raise HTTPException(
            status_code=422,
            detail="Display name must contain at least one letter or digit",
        )
    return f"custom_{slug}"


def _registry_get(registry: Any, provider_id: str) -> Any | None:
    try:
        return registry.get(provider_id)
    except KeyError, ValueError:
        return None


def _registry_get_or_404(registry: Any, provider_id: str) -> Any:
    entry = _registry_get(registry, provider_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown custom provider")
    return entry


def _serialize_entry(
    entry: Any, cached_models: dict[str, frozenset[str]]
) -> dict[str, Any]:
    keys = tuple(getattr(entry, "api_keys", ()) or ())
    enabled = bool(getattr(entry, "enabled", True))
    if not enabled:
        status = "disabled"
    elif keys:
        status = "configured"
    else:
        status = "missing_key"
    models = sorted(cached_models.get(str(getattr(entry, "provider_id", "")), ()))
    return {
        "provider_id": getattr(entry, "provider_id", ""),
        "display_name": getattr(entry, "display_name", ""),
        "base_url": getattr(entry, "base_url", ""),
        "key_count": len(keys),
        "masked_keys": [_mask_credential_key(key) for key in keys],
        "credential_rotation": getattr(entry, "credential_rotation", DEFAULT_ROTATION),
        "proxy": getattr(entry, "proxy", None),
        "enabled": enabled,
        "model_count": len(models),
        "status": status,
        "models": models,
        "added_at": getattr(entry, "added_at", ""),
    }


async def _reload_provider_runtime(services: ApiServices) -> None:
    """Republish the provider generation after a registry mutation."""
    await services.admin.reload_providers(reason=HOT_RELOAD_REASON)


@router.get("/admin/api/custom-providers")
async def list_custom_providers(
    request: Request,
    registry: Any = Depends(get_custom_provider_registry),
    services: ApiServices = Depends(get_services),
):
    """List every registered custom provider with cached model metadata."""
    require_loopback_admin(request)
    cached = services.admin.cached_model_ids()
    return {
        "providers": [
            _serialize_entry(entry, cached) for entry in registry.list_custom()
        ]
    }


@router.post("/admin/api/custom-providers")
async def create_custom_provider(
    payload: CustomProviderCreatePayload,
    request: Request,
    registry: Any = Depends(get_custom_provider_registry),
    services: ApiServices = Depends(get_services),
):
    """Register one custom provider, hot reload, and detect its models live."""
    require_loopback_admin(request)
    display_name = _validate_display_name(payload.display_name)
    base_url = _validate_base_url(payload.base_url)
    api_key = _validate_api_key(payload.api_key)
    rotation = _validate_rotation(payload.credential_rotation)
    proxy = _normalize_proxy(payload.proxy)
    provider_id = _slug_provider_id(display_name)
    if _registry_get(registry, provider_id) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Custom provider '{provider_id}' already exists",
        )
    entry = _new_entry(
        provider_id=provider_id,
        display_name=display_name,
        base_url=base_url,
        api_keys=(api_key,),
        credential_rotation=rotation,
        proxy=proxy,
        enabled=True,
        added_at=datetime.now(UTC).isoformat(),
    )
    try:
        registry.add(entry)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _reload_provider_runtime(services)

    stored = _registry_get(registry, provider_id) or entry
    result = _serialize_entry(stored, services.admin.cached_model_ids())
    test = await services.admin.test_provider(provider_id)
    if test.get("ok"):
        models = sorted(str(model) for model in test.get("models", []))
        result["models"] = models
        result["model_count"] = len(models)
    else:
        result["test_error"] = test.get("error_type", "test_failed")
    return result


@router.patch("/admin/api/custom-providers/{provider_id}")
async def update_custom_provider(
    provider_id: str,
    payload: CustomProviderUpdatePayload,
    request: Request,
    registry: Any = Depends(get_custom_provider_registry),
    services: ApiServices = Depends(get_services),
):
    """Apply a partial update to one custom provider and hot reload."""
    require_loopback_admin(request)
    _registry_get_or_404(registry, provider_id)

    changes: dict[str, Any] = {}
    if payload.display_name is not None:
        changes["display_name"] = _validate_display_name(payload.display_name)
    if payload.base_url is not None:
        changes["base_url"] = _validate_base_url(payload.base_url)
    if payload.credential_rotation is not None:
        changes["credential_rotation"] = _validate_rotation(payload.credential_rotation)
    if "proxy" in payload.model_fields_set:
        changes["proxy"] = _normalize_proxy(payload.proxy)
    if payload.enabled is not None:
        changes["enabled"] = payload.enabled
    if not changes:
        raise HTTPException(status_code=422, detail="No updatable fields provided")

    try:
        registry.update(provider_id, **changes)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown custom provider") from exc
    await _reload_provider_runtime(services)
    stored = _registry_get_or_404(registry, provider_id)
    return _serialize_entry(stored, services.admin.cached_model_ids())


@router.post("/admin/api/custom-providers/{provider_id}/keys")
async def add_custom_provider_key(
    provider_id: str,
    payload: CustomProviderKeyPayload,
    request: Request,
    registry: Any = Depends(get_custom_provider_registry),
    services: ApiServices = Depends(get_services),
):
    """Append one API key to a custom provider and hot reload."""
    require_loopback_admin(request)
    entry = _registry_get_or_404(registry, provider_id)
    api_key = _validate_api_key(payload.api_key)
    keys: list[str] = [str(key) for key in (getattr(entry, "api_keys", ()) or ())]
    if api_key in keys:
        raise HTTPException(status_code=409, detail="Key is already configured")
    keys.append(api_key)
    try:
        registry.update(provider_id, api_keys=tuple(keys))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown custom provider") from exc
    await _reload_provider_runtime(services)
    stored = _registry_get_or_404(registry, provider_id)
    result = _serialize_entry(stored, services.admin.cached_model_ids())
    result["added"] = _mask_credential_key(api_key)
    return result


@router.delete("/admin/api/custom-providers/{provider_id}/keys/{index}")
async def delete_custom_provider_key(
    provider_id: str,
    index: int,
    request: Request,
    registry: Any = Depends(get_custom_provider_registry),
    services: ApiServices = Depends(get_services),
):
    """Remove one API key by index and hot reload (last key keeps the entry)."""
    require_loopback_admin(request)
    entry = _registry_get_or_404(registry, provider_id)
    keys: list[str] = [str(key) for key in (getattr(entry, "api_keys", ()) or ())]
    if index < 0 or index >= len(keys):
        raise HTTPException(status_code=404, detail="Key index out of range")
    removed = keys.pop(index)
    try:
        registry.update(provider_id, api_keys=tuple(keys))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown custom provider") from exc
    await _reload_provider_runtime(services)
    stored = _registry_get_or_404(registry, provider_id)
    result = _serialize_entry(stored, services.admin.cached_model_ids())
    result["removed"] = _mask_credential_key(removed)
    return result


@router.delete("/admin/api/custom-providers/{provider_id}")
async def delete_custom_provider(
    provider_id: str,
    request: Request,
    registry: Any = Depends(get_custom_provider_registry),
    services: ApiServices = Depends(get_services),
):
    """Remove one custom provider and hot reload."""
    require_loopback_admin(request)
    _registry_get_or_404(registry, provider_id)
    try:
        registry.remove(provider_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown custom provider") from exc
    await _reload_provider_runtime(services)
    return {"applied": True, "provider_id": provider_id, "removed": True}
