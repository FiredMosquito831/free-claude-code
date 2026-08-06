"""Local admin UI routes and APIs."""

import asyncio
import ipaddress
import time
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from free_claude_code.application.model_metadata import ProviderModelRefreshResult
from free_claude_code.application.release_updates import (
    get_release_status,
    perform_upgrade,
)
from free_claude_code.config.admin.manifest import FIELD_BY_KEY
from free_claude_code.config.admin.persistence import validate_updates
from free_claude_code.config.admin.sources import is_locked_source
from free_claude_code.config.admin.values import load_config_response, load_value_state
from free_claude_code.config.claude_settings import (
    ClaudeSettingsError,
    ClaudeSettingsStatus,
    apply_proxy_env,
    clear_proxy_env,
    read_status,
)
from free_claude_code.config.constants import (
    CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
)
from free_claude_code.config.credentials import parse_credential_keys
from free_claude_code.config.model_refs import configured_chat_model_refs
from free_claude_code.config.paths import (
    claude_settings_candidates,
    claude_settings_path,
)
from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.config.proxy_auth import proxy_auth_token
from free_claude_code.config.server_urls import local_proxy_root_url
from free_claude_code.config.settings import Settings
from free_claude_code.config.websearch_catalog import (
    WEBSEARCH_CATALOG,
    WebSearchDescriptor,
)
from free_claude_code.core.request_log import RequestLogStore, store_from_settings
from free_claude_code.providers.chatgpt_oauth.browser_login import (
    ChatGPTOAuthBrowserUnavailableError,
    browser_login_status,
    start_browser_login,
)
from free_claude_code.providers.chatgpt_oauth.credentials import (
    ChatGPTOAuthError,
    import_codex_cli_tokens,
)
from free_claude_code.providers.chatgpt_oauth.oauth_login import (
    CHATGPT_OAUTH_DEVICE_VERIFICATION_URL,
    _initiate_device_auth,
    exchange_device_auth_for_tokens,
)
from free_claude_code.providers.chatgpt_oauth.oauth_login import (
    ChatGPTOAuthLoginError as ChatGPTOAuthLoginFlowError,
)
from free_claude_code.providers.runtime.rotating import RotatingProvider
from free_claude_code.websearch.errors import WebSearchError
from free_claude_code.websearch.registry import search_with_logging
from free_claude_code.websearch.rotation import mask_key_label, parse_websearch_keys

from .dependencies import get_services, get_settings
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


class ClaudeSettingsPathPayload(BaseModel):
    """Optional target path submitted by the Claude settings admin card."""

    path: str | None = None


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


@lru_cache(maxsize=1)
def _bundled_image_names() -> frozenset[str]:
    directory = Path(__file__).parent / "admin_static" / "img"
    if not directory.is_dir():
        return frozenset()
    return frozenset(p.name for p in directory.iterdir() if p.suffix == ".png")


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


@router.get("/admin/img/{filename}", include_in_schema=False)
async def admin_image(filename: str, request: Request):
    """Serve bundled guide screenshots.

    Names are matched against the files actually shipped rather than joined
    onto a path, so a crafted filename cannot escape the directory.
    """

    require_loopback_admin(request)
    if filename not in _bundled_image_names():
        raise HTTPException(status_code=404, detail="Admin image not found")
    return _asset_response(f"img/{filename}")


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


_CREDENTIAL_ENV_KEYS = frozenset(
    descriptor.credential_env
    for descriptor in PROVIDER_CATALOG.values()
    if descriptor.credential_env is not None
)


class _CredentialKeyAddRequest(BaseModel):
    key: str


def _mask_credential_key(key: str) -> str:
    """Return a display-safe rendering of one credential key."""
    if len(key) <= 4:
        return "****"
    if len(key) <= 10:
        return f"{key[:2]}…{key[-2:]}"
    return f"{key[:6]}…{key[-4:]}"


def _credential_entry_or_404(env_key: str) -> dict[str, Any]:
    if env_key not in _CREDENTIAL_ENV_KEYS:
        raise HTTPException(status_code=404, detail="Unknown credential env key")
    return load_value_state().get(env_key, {"value": "", "source": "default"})


def _require_unlocked_credential(entry: dict[str, Any]) -> None:
    if is_locked_source(entry["source"]):
        raise HTTPException(
            status_code=409,
            detail=(
                "This credential is set via the process environment and cannot "
                "be edited from the dashboard."
            ),
        )


@router.get("/admin/api/credentials/{env_key}/keys")
async def list_credential_keys(
    env_key: str,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """List the configured keys for one provider credential (masked)."""
    require_loopback_admin(request)
    entry = _credential_entry_or_404(env_key)
    keys = parse_credential_keys(str(entry["value"]))

    # Best-effort live key health from the rotating provider, index-aligned
    # with the configured key list.
    health: list[dict[str, Any] | None] = [None] * len(keys)
    provider_id = next(
        (
            descriptor.provider_id
            for descriptor in PROVIDER_CATALOG.values()
            if descriptor.credential_env == env_key
        ),
        None,
    )
    if provider_id is not None:
        try:
            async with await services.requests.acquire() as lease:
                if lease.is_provider_cached(provider_id):
                    provider = lease.resolve_provider(provider_id)
                    if isinstance(provider, RotatingProvider):
                        snapshots = provider.key_health()
                        for i in range(min(len(keys), len(snapshots))):
                            health[i] = snapshots[i]
        except Exception:
            pass  # Health is informational only; never fail the listing.

    return {
        "env_key": env_key,
        "source": entry["source"],
        "locked": is_locked_source(entry["source"]),
        "count": len(keys),
        "keys": [_mask_credential_key(key) for key in keys],
        "health": health,
    }


@router.post("/admin/api/credentials/{env_key}/keys")
async def add_credential_key(
    env_key: str,
    payload: _CredentialKeyAddRequest,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """Append one key to a provider credential and apply immediately."""
    require_loopback_admin(request)
    entry = _credential_entry_or_404(env_key)
    _require_unlocked_credential(entry)

    new_key = payload.key.strip()
    if not new_key:
        raise HTTPException(status_code=400, detail="Key is empty")
    if "," in new_key:
        raise HTTPException(status_code=400, detail="Paste a single key without commas")

    keys = list(parse_credential_keys(str(entry["value"])))
    if new_key in keys:
        raise HTTPException(status_code=409, detail="Key is already configured")
    keys.append(new_key)

    result = await services.admin.apply_admin_config({env_key: ",".join(keys)})
    if not result.get("applied"):
        raise HTTPException(
            status_code=400,
            detail="; ".join(result.get("errors", [])) or "Update rejected",
        )
    return {
        "applied": True,
        "env_key": env_key,
        "count": len(keys),
        "added": _mask_credential_key(new_key),
        "restart": result.get("restart"),
    }


@router.delete("/admin/api/credentials/{env_key}/keys/{index}")
async def delete_credential_key(
    env_key: str,
    index: int,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """Remove one key from a provider credential and apply immediately."""
    require_loopback_admin(request)
    entry = _credential_entry_or_404(env_key)
    _require_unlocked_credential(entry)

    keys = list(parse_credential_keys(str(entry["value"])))
    if index < 0 or index >= len(keys):
        raise HTTPException(status_code=404, detail="Key index out of range")
    removed = keys.pop(index)

    result = await services.admin.apply_admin_config({env_key: ",".join(keys)})
    if not result.get("applied"):
        raise HTTPException(
            status_code=400,
            detail="; ".join(result.get("errors", [])) or "Update rejected",
        )
    return {
        "applied": True,
        "env_key": env_key,
        "count": len(keys),
        "removed": _mask_credential_key(removed),
        "restart": result.get("restart"),
    }


# --------------------------------------------------------------------- claude settings file


def _resolve_claude_settings_path(raw_path: str | None) -> Path:
    """Expand, resolve and validate a caller-supplied Claude settings path."""

    if raw_path is None:
        return claude_settings_path()

    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise HTTPException(status_code=400, detail="Path must be absolute")
    if path.suffix != ".json":
        raise HTTPException(status_code=400, detail="Path must point at a .json file")
    return path.resolve()


def _claude_settings_expectations(settings: Settings) -> tuple[str, str]:
    """Return the (base_url, auth_token) this proxy expects Claude Code to use."""

    return (
        local_proxy_root_url(settings),
        proxy_auth_token(settings.anthropic_auth_token),
    )


def _claude_settings_status_response(status: ClaudeSettingsStatus) -> dict[str, Any]:
    return {
        "status": status,
        "default_path": str(claude_settings_path()),
    }


async def _claude_settings_target(
    path: str, expected_base_url: str, expected_auth_token: str
) -> dict[str, Any]:
    """Evaluate a single detected settings candidate for the ``targets`` list."""

    target_status = await asyncio.to_thread(
        read_status,
        path=Path(path),
        expected_base_url=expected_base_url,
        expected_auth_token=expected_auth_token,
    )
    return {
        "path": path,
        "exists": target_status.exists,
        "state": target_status.state,
        "is_default": Path(path) == claude_settings_path(),
    }


@router.get("/admin/api/claude-settings")
async def get_claude_settings(
    request: Request,
    path: str | None = None,
    settings: Settings = Depends(get_settings),
):
    require_loopback_admin(request)
    target_path = _resolve_claude_settings_path(path)
    expected_base_url, expected_auth_token = _claude_settings_expectations(settings)
    status = await asyncio.to_thread(
        read_status,
        path=target_path,
        expected_base_url=expected_base_url,
        expected_auth_token=expected_auth_token,
    )
    candidates = await asyncio.to_thread(claude_settings_candidates)
    targets = [
        await _claude_settings_target(
            str(candidate), expected_base_url, expected_auth_token
        )
        for candidate in candidates
    ]
    response = _claude_settings_status_response(status)
    response["targets"] = targets
    return response


@router.post("/admin/api/claude-settings/apply")
async def apply_claude_settings(
    payload: ClaudeSettingsPathPayload,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    require_loopback_admin(request)
    target_path = _resolve_claude_settings_path(payload.path)
    expected_base_url, expected_auth_token = _claude_settings_expectations(settings)
    try:
        status = await asyncio.to_thread(
            apply_proxy_env,
            path=target_path,
            base_url=expected_base_url,
            auth_token=expected_auth_token,
        )
    except ClaudeSettingsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _claude_settings_status_response(status)


@router.post("/admin/api/claude-settings/unset")
async def unset_claude_settings(
    payload: ClaudeSettingsPathPayload,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    require_loopback_admin(request)
    target_path = _resolve_claude_settings_path(payload.path)
    # The expectations do not change what is removed; they keep the response
    # able to describe what a re-apply would write.
    expected_base_url, expected_auth_token = _claude_settings_expectations(settings)
    try:
        status = await asyncio.to_thread(
            clear_proxy_env,
            path=target_path,
            expected_base_url=expected_base_url,
            expected_auth_token=expected_auth_token,
        )
    except ClaudeSettingsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _claude_settings_status_response(status)


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


class _ChatGPTOAuthInitiateResponse(BaseModel):
    device_auth_id: str
    user_code: str
    verification_url: str


class _ChatGPTOAuthBrowserInitiateResponse(BaseModel):
    authorize_url: str
    expires_in: str


@router.post("/admin/api/chatgpt-oauth/browser/initiate")
async def chatgpt_oauth_browser_initiate(
    request: Request,
    same_host_confirmed: bool = False,
):
    """Start a browser-based ChatGPT OAuth login (PKCE + local callback)."""
    require_loopback_admin(request)
    try:
        payload = await asyncio.to_thread(
            start_browser_login,
            allow_remote=same_host_confirmed,
        )
    except ChatGPTOAuthBrowserUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _ChatGPTOAuthBrowserInitiateResponse(**payload)


@router.post("/admin/api/chatgpt-oauth/browser/status")
async def chatgpt_oauth_browser_status(request: Request):
    """Poll the status of the in-flight browser OAuth login."""
    require_loopback_admin(request)
    return await asyncio.to_thread(browser_login_status)


class _ChatGPTOAuthExchangeRequest(BaseModel):
    device_auth_id: str
    user_code: str


class _ChatGPTOAuthExchangeResponse(BaseModel):
    status: str
    credential_reference: str = ""
    account_id: str = ""
    message: str = ""


@router.post("/admin/api/chatgpt-oauth/initiate")
async def chatgpt_oauth_initiate(request: Request):
    """Start a ChatGPT/Codex OAuth device-auth flow from the admin UI."""
    require_loopback_admin(request)
    try:
        device_auth_id, user_code, _interval_ms = await asyncio.to_thread(
            _initiate_device_auth
        )
    except ChatGPTOAuthLoginFlowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _ChatGPTOAuthInitiateResponse(
        device_auth_id=device_auth_id,
        user_code=user_code,
        verification_url=CHATGPT_OAUTH_DEVICE_VERIFICATION_URL,
    )


@router.post("/admin/api/chatgpt-oauth/exchange")
async def chatgpt_oauth_exchange(
    payload: _ChatGPTOAuthExchangeRequest,
    request: Request,
):
    """Poll for ChatGPT/Codex OAuth completion and return tokens."""
    require_loopback_admin(request)
    try:
        tokens = await asyncio.to_thread(
            exchange_device_auth_for_tokens,
            payload.device_auth_id,
            payload.user_code,
            timeout_seconds=8.0,
        )
    except ChatGPTOAuthLoginFlowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if tokens is None:
        return _ChatGPTOAuthExchangeResponse(
            status="pending",
            message="Waiting for authorization. Open the verification URL and enter the code.",
        )
    return _ChatGPTOAuthExchangeResponse(
        status="complete",
        credential_reference=CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
        account_id=tokens.get("account_id", ""),
        message="Login successful. Credentials saved to FCC's private store.",
    )


class _ChatGPTOAuthImportCodexResponse(BaseModel):
    status: str
    credential_reference: str = ""
    account_id: str = ""
    message: str = ""


@router.post("/admin/api/chatgpt-oauth/import-codex")
async def chatgpt_oauth_import_codex(request: Request):
    """Import ChatGPT/Codex OAuth tokens from an existing Codex CLI install."""
    require_loopback_admin(request)
    try:
        credentials = await asyncio.to_thread(import_codex_cli_tokens)
    except ChatGPTOAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _ChatGPTOAuthImportCodexResponse(
        status="complete",
        credential_reference=CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
        account_id=credentials.account_id,
        message="Copied renewable Codex credentials into FCC's private store.",
    )


# --------------------------------------------------------------------- requests log


def _request_log_store_or_none(
    settings: Settings,
) -> RequestLogStore | None:
    return store_from_settings(settings)


def _validate_request_log_status(status: str | None) -> None:
    if status is not None and status not in {"success", "error", "cancelled"}:
        raise HTTPException(status_code=422, detail="Invalid status filter")


@router.get("/admin/api/requests")
async def list_request_log(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    provider: str | None = None,
    model: str | None = None,
    status: str | None = None,
    endpoint: str | None = None,
    key: str | None = None,
    since: float | None = None,
    until: float | None = None,
    q: str | None = None,
    settings: Settings = Depends(get_settings),
):
    """Page through the persisted request log (newest first)."""
    require_loopback_admin(request)
    store = _request_log_store_or_none(settings)
    if store is None:
        return {
            "enabled": False,
            "rows": [],
            "total": 0,
            "limit": limit,
            "offset": offset,
        }
    _validate_request_log_status(status)
    # SQLite work is synchronous; run it off the event loop so analytics
    # queries cannot stall proxy traffic.
    rows, total = await asyncio.to_thread(
        store.list_requests,
        limit=limit,
        offset=offset,
        provider=provider,
        model=model,
        status=status,
        endpoint=endpoint,
        key=key,
        since=since,
        until=until,
        q=q,
    )
    return {
        "enabled": True,
        "capture_bodies": bool(settings.request_log_capture_bodies),
        "rows": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/admin/api/requests/stats")
async def request_log_stats(
    request: Request,
    provider: str | None = None,
    model: str | None = None,
    status: str | None = None,
    endpoint: str | None = None,
    key: str | None = None,
    since: float | None = None,
    until: float | None = None,
    q: str | None = None,
    settings: Settings = Depends(get_settings),
):
    """Aggregate request analytics over an optional epoch-second window."""
    require_loopback_admin(request)
    store = _request_log_store_or_none(settings)
    if store is None:
        return {"enabled": False}
    _validate_request_log_status(status)
    result = await asyncio.to_thread(
        store.stats,
        provider=provider,
        model=model,
        status=status,
        endpoint=endpoint,
        key=key,
        since=since,
        until=until,
        q=q,
    )
    result["enabled"] = True
    result["capture_bodies"] = bool(settings.request_log_capture_bodies)
    return result


@router.get("/admin/api/requests/{request_id}")
async def get_request_log_entry(
    request_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """Return one request log row with full (uncapped) bodies."""
    require_loopback_admin(request)
    store = _request_log_store_or_none(settings)
    row = (
        await asyncio.to_thread(store.get_request, request_id)
        if store is not None
        else None
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Request log entry not found")
    return row


@router.delete("/admin/api/requests")
async def clear_request_log(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """Delete every persisted request log row."""
    require_loopback_admin(request)
    store = _request_log_store_or_none(settings)
    cleared = await asyncio.to_thread(store.clear) if store is not None else 0
    return {"cleared": cleared}


@router.get("/admin/api/version")
async def read_version(request: Request):
    """Running version plus the latest published release, if reachable."""
    require_loopback_admin(request)
    status = await get_release_status()
    return status.as_dict()


@router.post("/admin/api/version/check")
async def check_version(request: Request):
    """Re-query the release feed, bypassing the cached result."""
    require_loopback_admin(request)
    status = await get_release_status(force=True)
    return status.as_dict()


@router.post("/admin/api/version/upgrade")
async def upgrade_version(request: Request):
    """Install the latest release. The running server is left untouched.

    A live process keeps serving the code it already imported, so the response
    reports that a restart is required rather than restarting mid-request and
    dropping in-flight streams.
    """
    require_loopback_admin(request)
    result = await perform_upgrade()
    payload = result.as_dict()
    payload["restart_required"] = result.ok
    return payload
