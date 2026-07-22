"""Local admin UI routes and APIs."""

import ipaddress
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from free_claude_code.application.model_metadata import ProviderModelRefreshResult
from free_claude_code.config.admin.manifest import FIELD_BY_KEY
from free_claude_code.config.admin.persistence import validate_updates
from free_claude_code.config.admin.sources import is_locked_source
from free_claude_code.config.admin.values import load_config_response, load_value_state
from free_claude_code.config.model_refs import configured_chat_model_refs
from free_claude_code.config.websearch_catalog import (
    WEBSEARCH_CATALOG,
    WebSearchDescriptor,
)
from free_claude_code.websearch.errors import WebSearchError
from free_claude_code.websearch.registry import search_with_logging
from free_claude_code.websearch.rotation import mask_key_label, parse_websearch_keys

from .dependencies import get_services
from .ports import ApiServices
from .web_tools.search_providers import cached_key_pool_snapshot, runtime_provider

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent / "admin_static"
LOCAL_PROVIDER_PATHS = {
    "lmstudio": "/models",
    "llamacpp": "/models",
    "ollama": "/api/tags",
}


class AdminConfigPayload(BaseModel):
    """Partial config update submitted by the admin UI."""

    values: dict[str, Any] = Field(default_factory=dict)


class WebSearchKeyPayload(BaseModel):
    """Single web search credential key submitted by the admin UI."""

    key: str


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _origin_is_local(origin: str | None) -> bool:
    if not origin:
        return True
    parsed = urlsplit(origin)
    return _is_loopback_host(parsed.hostname)


def require_loopback_admin(request: Request) -> None:
    """Allow admin access only from the local machine."""

    client_host = request.client.host if request.client else None
    if not _is_loopback_host(client_host):
        raise HTTPException(status_code=403, detail="Admin UI is local-only")

    origin = request.headers.get("origin")
    if not _origin_is_local(origin):
        raise HTTPException(status_code=403, detail="Admin UI is local-only")


def _asset_response(filename: str) -> FileResponse:
    path = STATIC_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Admin asset not found")
    return FileResponse(path)


@router.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    require_loopback_admin(request)
    return _asset_response("index.html")


@router.get("/admin/assets/{filename}", include_in_schema=False)
async def admin_asset(filename: str, request: Request):
    require_loopback_admin(request)
    if filename not in {"admin.css", "admin.js"}:
        raise HTTPException(status_code=404, detail="Admin asset not found")
    return _asset_response(filename)


@router.get("/admin/api/config")
async def get_admin_config(request: Request):
    require_loopback_admin(request)
    return load_config_response()


@router.post("/admin/api/config/validate")
async def validate_admin_config(payload: AdminConfigPayload, request: Request):
    require_loopback_admin(request)
    return validate_updates(_filtered_values(payload.values))


@router.post("/admin/api/config/apply")
async def apply_admin_config(
    payload: AdminConfigPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    result = await services.admin.apply_admin_config(_filtered_values(payload.values))
    restart = result.get("restart")
    if isinstance(restart, dict) and restart.get("automatic"):
        background_tasks.add_task(services.admin.request_restart)
    return result


@router.get("/admin/api/status")
async def admin_status(
    request: Request,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    return services.admin.admin_status()


@router.get("/admin/api/providers/local-status")
async def local_provider_status(request: Request):
    require_loopback_admin(request)
    config = load_config_response()
    values = {field["key"]: field["value"] for field in config["fields"]}
    checks = []
    for provider_id, path in LOCAL_PROVIDER_PATHS.items():
        base_url = _local_provider_url(provider_id, values)
        checks.append(await _check_local_provider(provider_id, base_url, path))
    return {"providers": checks}


@router.post("/admin/api/providers/{provider_id}/test")
async def test_provider(
    provider_id: str,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    return await services.admin.test_provider(provider_id)


@router.get("/admin/api/models")
async def models(
    request: Request,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    return _model_options(services)


@router.get("/admin/api/websearch/credentials/{env_key}/keys")
async def list_websearch_credential_keys(env_key: str, request: Request):
    require_loopback_admin(request)
    descriptor = _websearch_descriptor_for_env(env_key)
    state = load_value_state()
    entry = state.get(env_key, {"value": "", "source": "default"})
    keys = parse_websearch_keys(entry["value"])
    return {
        "provider_id": descriptor.provider_id,
        "env_key": env_key,
        "locked": is_locked_source(entry["source"]),
        "keys": [
            {"index": index, "key_label": mask_key_label(key)}
            for index, key in enumerate(keys)
        ],
        "health": cached_key_pool_snapshot(descriptor.provider_id),
    }


@router.post("/admin/api/websearch/credentials/{env_key}/keys")
async def add_websearch_credential_key(
    env_key: str,
    payload: WebSearchKeyPayload,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    descriptor = _websearch_descriptor_for_env(env_key)
    key = payload.key.strip()
    if not key or "," in key:
        raise HTTPException(
            status_code=422,
            detail="API key must be non-empty and must not contain commas",
        )
    keys = _editable_websearch_keys(env_key)
    result = await services.admin.apply_admin_config({env_key: ",".join([*keys, key])})
    return result | {
        "provider_id": descriptor.provider_id,
        "keys": _masked_keys(env_key),
    }


@router.delete("/admin/api/websearch/credentials/{env_key}/keys/{index}")
async def delete_websearch_credential_key(
    env_key: str,
    index: int,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    descriptor = _websearch_descriptor_for_env(env_key)
    keys = _editable_websearch_keys(env_key)
    if index < 0 or index >= len(keys):
        raise HTTPException(status_code=404, detail="Web search key index out of range")
    del keys[index]
    result = await services.admin.apply_admin_config({env_key: ",".join(keys)})
    return result | {
        "provider_id": descriptor.provider_id,
        "keys": _masked_keys(env_key),
    }


@router.post("/admin/api/websearch/providers/{provider_id}/test")
async def test_websearch_provider(
    provider_id: str,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    if provider_id not in WEBSEARCH_CATALOG:
        raise HTTPException(status_code=404, detail="Unknown web search provider")
    settings = services.requests.current_settings()
    started = time.perf_counter()
    try:
        provider = await runtime_provider(settings, provider_id)
        response = await search_with_logging(provider, "web search", max_results=3)
    except WebSearchError as error:
        return {
            "provider_id": provider_id,
            "ok": False,
            "latency_ms": _elapsed_millis(started),
            "error": _websearch_error_payload(error),
        }
    return {
        "provider_id": provider_id,
        "ok": True,
        "latency_ms": _elapsed_millis(started),
        "result_count": len(response.results),
        "titles": [item.title for item in response.results[:3]],
    }


def _websearch_descriptor_for_env(env_key: str) -> WebSearchDescriptor:
    for descriptor in WEBSEARCH_CATALOG.values():
        if descriptor.credential_env == env_key:
            return descriptor
    raise HTTPException(status_code=404, detail="Unknown web search credential")


def _editable_websearch_keys(env_key: str) -> list[str]:
    """Current parsed keys, refusing mutation when an external source owns the value."""

    entry = load_value_state().get(env_key, {"value": "", "source": "default"})
    if is_locked_source(entry["source"]):
        raise HTTPException(
            status_code=409,
            detail=f"{env_key} comes from a locked source ({entry['source']})",
        )
    return list(parse_websearch_keys(entry["value"]))


def _masked_keys(env_key: str) -> list[dict[str, Any]]:
    entry = load_value_state().get(env_key, {"value": "", "source": "default"})
    return [
        {"index": index, "key_label": mask_key_label(key)}
        for index, key in enumerate(parse_websearch_keys(entry["value"]))
    ]


def _websearch_error_payload(error: WebSearchError) -> dict[str, Any]:
    return {
        "kind": error.kind,
        "message": error.message,
        "status_code": error.status_code,
    }


def _elapsed_millis(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


@router.post("/admin/api/models/refresh")
async def refresh_models(
    request: Request,
    services: ApiServices = Depends(get_services),
):
    require_loopback_admin(request)
    result = await services.admin.refresh_models()
    return _model_options(services, refresh_result=result)


def _model_options(
    services: ApiServices,
    *,
    refresh_result: ProviderModelRefreshResult | None = None,
) -> dict[str, list[str]]:
    configured = {
        ref.model_ref
        for ref in configured_chat_model_refs(services.requests.current_settings())
    }
    discovered = {
        info.model_id for info in services.requests.cached_prefixed_model_infos()
    }
    failed_provider_ids = (
        refresh_result.failed_provider_ids if refresh_result is not None else ()
    )
    return {
        "models": sorted(configured | discovered, key=str.casefold),
        "failed_providers": list(failed_provider_ids),
    }


def _filtered_values(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key in FIELD_BY_KEY}


def _local_provider_url(provider_id: str, values: dict[str, str]) -> str:
    if provider_id == "lmstudio":
        return values.get("LM_STUDIO_BASE_URL", "")
    if provider_id == "llamacpp":
        return values.get("LLAMACPP_BASE_URL", "")
    if provider_id == "ollama":
        return values.get("OLLAMA_BASE_URL", "")
    return ""


async def _check_local_provider(
    provider_id: str, base_url: str, path: str
) -> dict[str, Any]:
    clean_url = base_url.strip().rstrip("/")
    if not clean_url:
        return {
            "provider_id": provider_id,
            "status": "missing_url",
            "label": "Missing URL",
            "base_url": base_url,
        }

    url = f"{clean_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            response = await client.get(url)
        ok = 200 <= response.status_code < 300
        return {
            "provider_id": provider_id,
            "status": "reachable" if ok else "offline",
            "label": "Reachable" if ok else "Offline",
            "base_url": base_url,
            "status_code": response.status_code,
        }
    except Exception as exc:
        return {
            "provider_id": provider_id,
            "status": "offline",
            "label": "Offline",
            "base_url": base_url,
            "error_type": type(exc).__name__,
        }
