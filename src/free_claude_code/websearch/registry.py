"""Web search provider registry: build from settings, resolve the active one, search.

Analytics seam: :func:`search` accepts an optional ``recorder`` callable invoked
with a :class:`SearchOutcome`; :func:`search_with_logging` defaults it to
``websearch.analytics.record_search`` when that module exists (Worker B).
"""

import importlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from loguru import logger

from free_claude_code.config.env_files import env_file_override
from free_claude_code.config.settings import Settings
from free_claude_code.config.websearch_catalog import (
    WEBSEARCH_CATALOG,
    WebSearchDescriptor,
)
from free_claude_code.core.websearch.models import WebSearchResponse

from .adapters import ADAPTER_CLASSES
from .base import BaseWebSearchProvider, WebSearchProviderConfig
from .errors import WebSearchConfigError, WebSearchError
from .options import read_websearch_options
from .rotation import (
    ROTATION_POLICIES,
    default_rotation_policy,
    parse_websearch_keys,
)

DEFAULT_HTTP_TIMEOUT = 20.0
ROTATION_ENV_SUFFIX = "_ROTATION"
WEBSEARCH_PROXY_ENV = "WEBSEARCH_PROXY"
_QUERY_LOG_CHARS = 256
_ERROR_MESSAGE_LOG_CHARS = 500


def build_providers(settings: Settings) -> dict[str, BaseWebSearchProvider]:
    """Build every configured provider (unconfigured ones are skipped)."""

    providers: dict[str, BaseWebSearchProvider] = {}
    for provider_id in WEBSEARCH_CATALOG:
        try:
            providers[provider_id] = build_provider(settings, provider_id)
        except WebSearchConfigError:
            continue
    return providers


def build_provider(settings: Settings, provider_id: str) -> BaseWebSearchProvider:
    """Build one provider or raise :class:`WebSearchConfigError` when unconfigured."""

    descriptor = WEBSEARCH_CATALOG.get(provider_id)
    if descriptor is None:
        raise WebSearchConfigError(
            provider_id, f"unknown web search provider: {provider_id!r}"
        )
    keys = _descriptor_keys(descriptor, settings)
    if descriptor.requires_key and not keys:
        raise WebSearchConfigError(
            provider_id,
            f"{descriptor.credential_env} is not configured "
            f"(set it in your .env to enable {descriptor.display_name})",
        )
    base_url = _descriptor_base_url(descriptor, settings)
    rotation = _resolve_rotation_policy(descriptor, len(keys))
    proxy = _env_or_dotenv(WEBSEARCH_PROXY_ENV)
    options = read_websearch_options(provider_id, descriptor)
    adapter_cls = ADAPTER_CLASSES[provider_id]
    return adapter_cls(
        WebSearchProviderConfig(
            api_keys=keys,
            credential_rotation=rotation,
            base_url=base_url,
            proxy=proxy or None,
            http_timeout=DEFAULT_HTTP_TIMEOUT,
            options=options,
        )
    )


def resolve_provider_id(settings: Settings) -> str | None:
    """Resolve ``web_search_provider``: explicit id, ``off`` -> None, ``auto`` ->
    first catalog provider with a configured key, else keyless ``ddgs``."""

    selection = settings.web_search_provider
    if selection == "off":
        return None
    if selection != "auto":
        if selection not in WEBSEARCH_CATALOG:
            raise WebSearchConfigError(
                selection,
                f"unknown web search provider: {selection!r}",
            )
        return selection
    for provider_id in WEBSEARCH_CATALOG:
        if provider_id == "ddgs":
            continue
        descriptor = WEBSEARCH_CATALOG[provider_id]
        if _descriptor_is_configured(descriptor, settings):
            return provider_id
    return "ddgs"


def active_provider(settings: Settings) -> BaseWebSearchProvider | None:
    """Build the selected provider; None when web search is ``off``."""

    provider_id = resolve_provider_id(settings)
    if provider_id is None:
        return None
    return build_provider(settings, provider_id)


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Analytics record fields for one web search call (contract with Worker B)."""

    ts_epoch: float
    ts_iso: str
    provider: str
    key_index: int
    key_label: str
    query: str
    results_count: int
    duration_ms: float
    status: str  # "success" | "error"
    error_kind: str | None
    error_message: str | None
    cost_usd: float | None


SearchRecorder = Callable[[SearchOutcome], None]


async def search(
    provider: BaseWebSearchProvider,
    query: str,
    *,
    max_results: int = 10,
    allowed_domains: tuple[str, ...] = (),
    blocked_domains: tuple[str, ...] = (),
    recorder: SearchRecorder | None = None,
) -> WebSearchResponse:
    """Run ``provider.search`` and optionally record the outcome via ``recorder``."""

    ts_epoch = time.time()
    started = time.perf_counter()
    try:
        response = await provider.search(
            query,
            max_results=max_results,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
        )
    except Exception as error:
        key_index = (
            error.key_index
            if isinstance(error, WebSearchError) and error.key_index is not None
            else 0
        )
        _emit(
            recorder,
            SearchOutcome(
                ts_epoch=ts_epoch,
                ts_iso=_iso(ts_epoch),
                provider=provider.provider_id,
                key_index=key_index,
                key_label=provider.key_label(key_index),
                query=query[:_QUERY_LOG_CHARS],
                results_count=0,
                duration_ms=_elapsed_ms(started),
                status="error",
                error_kind=(
                    error.kind if isinstance(error, WebSearchError) else "internal"
                ),
                error_message=str(error)[:_ERROR_MESSAGE_LOG_CHARS],
                cost_usd=None,
            ),
        )
        raise
    _emit(
        recorder,
        SearchOutcome(
            ts_epoch=ts_epoch,
            ts_iso=_iso(ts_epoch),
            provider=provider.provider_id,
            key_index=response.key_index,
            key_label=provider.key_label(response.key_index),
            query=query[:_QUERY_LOG_CHARS],
            results_count=len(response.results),
            duration_ms=_elapsed_ms(started),
            status="success",
            error_kind=None,
            error_message=None,
            cost_usd=response.cost_usd,
        ),
    )
    return response


async def search_with_logging(
    provider: BaseWebSearchProvider,
    query: str,
    *,
    max_results: int = 10,
    allowed_domains: tuple[str, ...] = (),
    blocked_domains: tuple[str, ...] = (),
    recorder: SearchRecorder | None = None,
) -> WebSearchResponse:
    """Search with analytics recording; defaults to the Worker B analytics store."""

    return await search(
        provider,
        query,
        max_results=max_results,
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
        recorder=recorder if recorder is not None else _default_recorder(),
    )


def _default_recorder() -> SearchRecorder | None:
    """Analytics seam: Worker B provides ``websearch.analytics.record_search``.

    Dynamic import on purpose: the module does not exist until the analytics
    worker lands, and a static import would break every caller.
    """

    try:
        module = importlib.import_module(f"{__package__}.analytics")
    except ImportError:
        return None
    record_search = getattr(module, "record_search", None)
    return record_search if callable(record_search) else None


def _emit(recorder: SearchRecorder | None, outcome: SearchOutcome) -> None:
    if recorder is None:
        return
    try:
        recorder(outcome)
    except Exception:
        logger.exception("websearch recorder failed for provider {}", outcome.provider)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _iso(ts_epoch: float) -> str:
    return datetime.fromtimestamp(ts_epoch, tz=UTC).isoformat()


def _descriptor_keys(
    descriptor: WebSearchDescriptor, settings: Settings
) -> tuple[str, ...]:
    if descriptor.settings_attr is None:
        return ()
    raw = getattr(settings, descriptor.settings_attr)
    return parse_websearch_keys(raw if isinstance(raw, str) else None)


def _descriptor_base_url(
    descriptor: WebSearchDescriptor, settings: Settings
) -> str | None:
    if descriptor.base_url_attr is None:
        return descriptor.default_base_url
    raw = getattr(settings, descriptor.base_url_attr)
    base_url = raw.strip() if isinstance(raw, str) else ""
    if not base_url:
        if descriptor.provider_id == "searxng":
            raise WebSearchConfigError(
                descriptor.provider_id,
                "SEARXNG_BASE_URL is required for the searxng provider "
                "(self-hosted instance with format=json enabled)",
            )
        return descriptor.default_base_url
    return base_url


def _descriptor_is_configured(
    descriptor: WebSearchDescriptor, settings: Settings
) -> bool:
    try:
        if descriptor.requires_key and not _descriptor_keys(descriptor, settings):
            return False
        _descriptor_base_url(descriptor, settings)
    except WebSearchConfigError:
        return False
    return True


def _resolve_rotation_policy(descriptor: WebSearchDescriptor, key_count: int) -> str:
    raw = (
        _env_or_dotenv(f"{descriptor.credential_env}{ROTATION_ENV_SUFFIX}")
        if descriptor.credential_env
        else None
    )
    if not raw:
        return default_rotation_policy(key_count)
    value = raw.strip().lower()
    if value not in ROTATION_POLICIES:
        logger.warning(
            "Invalid {} value {!r}; falling back to default rotation policy",
            f"{descriptor.credential_env}{ROTATION_ENV_SUFFIX}",
            raw,
        )
        return default_rotation_policy(key_count)
    return value


def _env_or_dotenv(key: str) -> str | None:
    """Process env wins; otherwise the last configured dotenv value."""

    if key in os.environ:
        return os.environ[key]
    return env_file_override(Settings.model_config, key)
