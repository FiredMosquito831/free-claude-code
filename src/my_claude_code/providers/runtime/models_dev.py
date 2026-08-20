"""models.dev metadata fallback for custom and thin built-in providers.

Fetches https://models.dev/api.json (10s timeout) and caches it at
``config_dir_path()/cache/models-dev.json`` with a ``fetched_at`` timestamp.
The cache is used when it is fresh (<24h); a stale cache is still used while a
background refresh is scheduled. Everything is fully silent when offline:
discovery never fails because models.dev is unreachable.
"""

import asyncio
import json
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from my_claude_code.application.model_metadata import (
    ModelReasoningCapability,
    ProviderModelInfo,
)
from my_claude_code.config.paths import config_dir_path
from my_claude_code.core.reasoning import ReasoningEffort

MODELS_DEV_URL = "https://models.dev/api.json"
MODELS_DEV_CACHE_TTL_SECONDS = 24 * 60 * 60
MODELS_DEV_FETCH_TIMEOUT_SECONDS = 10.0
MODELS_DEV_CACHE_DIRNAME = "cache"
MODELS_DEV_CACHE_FILENAME = "models-dev.json"


def models_dev_cache_path() -> Path:
    """Return the default on-disk cache path for the models.dev index."""
    return config_dir_path() / MODELS_DEV_CACHE_DIRNAME / MODELS_DEV_CACHE_FILENAME


@dataclass(frozen=True, slots=True)
class ModelsDevCache:
    """Parsed models.dev cache payload with freshness."""

    index: Mapping[str, Any]
    fetched_at: datetime
    fresh: bool


def read_models_dev_cache(path: Path | None = None) -> ModelsDevCache | None:
    """Return the cached models.dev index, or None when absent/corrupt."""
    cache_path = path if path is not None else models_dev_cache_path()
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    index = payload.get("index")
    fetched_raw = payload.get("fetched_at")
    if not isinstance(index, dict) or not isinstance(fetched_raw, str):
        return None
    try:
        fetched_at = datetime.fromisoformat(fetched_raw)
    except ValueError:
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - fetched_at).total_seconds()
    return ModelsDevCache(
        index=index,
        fetched_at=fetched_at,
        fresh=age < MODELS_DEV_CACHE_TTL_SECONDS,
    )


def models_dev_provider_model_ids(
    provider: str, path: Path | None = None
) -> frozenset[str]:
    """Return the model ids models.dev publishes for one provider.

    Empty when the cache is missing or does not know the provider, so a caller
    can fall back to whatever it knows statically rather than losing models on
    a fresh install with no network.
    """

    cache = read_models_dev_cache(path)
    if cache is None:
        return frozenset()
    bucket = cache.index.get(provider)
    if not isinstance(bucket, Mapping):
        return frozenset()
    models = bucket.get("models")
    if not isinstance(models, Mapping):
        return frozenset()
    return frozenset(
        model_id for model_id in models if isinstance(model_id, str) and model_id
    )


def write_models_dev_cache(index: Mapping[str, Any], path: Path | None = None) -> Path:
    """Atomically persist the models.dev index with a fetch timestamp."""
    cache_path = path if path is not None else models_dev_cache_path()
    payload = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "index": index,
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    temp_path.replace(cache_path)
    return cache_path


async def fetch_models_dev_index() -> Mapping[str, Any] | None:
    """Fetch the models.dev index; return None silently on any failure."""
    try:
        async with httpx.AsyncClient(
            timeout=MODELS_DEV_FETCH_TIMEOUT_SECONDS
        ) as client:
            response = await client.get(MODELS_DEV_URL)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        logger.debug("models.dev fetch failed silently: {}", exc)
        return None
    return payload if isinstance(payload, dict) else None


async def refresh_models_dev_cache(path: Path | None = None) -> bool:
    """Fetch and persist the models.dev index; never raises."""
    index = await fetch_models_dev_index()
    if index is None:
        return False
    try:
        write_models_dev_cache(index, path)
    except OSError as exc:
        logger.debug("models.dev cache write failed silently: {}", exc)
        return False
    return True


def schedule_models_dev_refresh(path: Path | None = None) -> None:
    """Fire-and-forget background refresh; a later run picks up the cache."""
    try:
        task = asyncio.get_running_loop().create_task(refresh_models_dev_cache(path))
    except RuntimeError:
        return
    task.add_done_callback(_swallow_refresh_outcome)


def _swallow_refresh_outcome(task: asyncio.Task[bool]) -> None:
    if task.cancelled():
        return
    task.exception()


def _normalize_candidates(model_id: str) -> set[str]:
    """Return normalized match keys for one model id."""
    lowered = model_id.strip().lower()
    if not lowered:
        return set()
    candidates = {lowered}
    _prefix, separator, remainder = lowered.partition("/")
    if separator and remainder:
        candidates.add(remainder)
    last_segment = lowered.rsplit("/", 1)[-1]
    if last_segment:
        candidates.add(last_segment)
    return candidates


@dataclass(frozen=True, slots=True)
class _ModelsDevModelMetadata:
    context_length: int | None
    input_price: float | None
    output_price: float | None
    supports_vision: bool | None


def _flatten_index(index: Mapping[str, Any]) -> dict[str, _ModelsDevModelMetadata]:
    """Flatten models.dev providers into normalized model-id match keys."""
    flattened: dict[str, _ModelsDevModelMetadata] = {}
    for provider_bucket in index.values():
        if not isinstance(provider_bucket, Mapping):
            continue
        models = provider_bucket.get("models")
        if not isinstance(models, Mapping):
            continue
        for model_id, metadata in models.items():
            if not isinstance(model_id, str) or not isinstance(metadata, Mapping):
                continue
            parsed = _parse_models_dev_metadata(metadata)
            for candidate in _normalize_candidates(model_id):
                flattened.setdefault(candidate, parsed)
    return flattened


def _parse_models_dev_metadata(
    metadata: Mapping[str, Any],
) -> _ModelsDevModelMetadata:
    cost = metadata.get("cost")
    limit = metadata.get("limit")
    input_price = (
        _float_or_none(cost.get("input")) if isinstance(cost, Mapping) else None
    )
    output_price = (
        _float_or_none(cost.get("output")) if isinstance(cost, Mapping) else None
    )
    context_length = (
        _int_or_none(limit.get("context")) if isinstance(limit, Mapping) else None
    )
    return _ModelsDevModelMetadata(
        context_length=context_length,
        input_price=input_price,
        output_price=output_price,
        supports_vision=_accepts_image_input(metadata),
    )


def _accepts_image_input(metadata: Mapping[str, Any]) -> bool | None:
    """Read image support from a models.dev entry, or None when unstated."""
    modalities = metadata.get("modalities")
    if isinstance(modalities, Mapping):
        inputs = modalities.get("input")
        if isinstance(inputs, list):
            return any(
                isinstance(item, str) and item.strip().lower() == "image"
                for item in inputs
            )
    # Older entries predate ``modalities`` and only carry ``attachment``, which
    # is broader than images but is the only signal those rows have.
    attachment = metadata.get("attachment")
    return attachment if isinstance(attachment, bool) else None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def enrich_model_infos(
    model_infos: Iterable[ProviderModelInfo],
    index: Mapping[str, Any],
) -> tuple[ProviderModelInfo, ...]:
    """Fill models.dev metadata on model infos via name-normalized matching."""
    flattened = _flatten_index(index)
    if not flattened:
        return tuple(model_infos)
    enriched: list[ProviderModelInfo] = []
    for info in model_infos:
        metadata = next(
            (
                flattened[candidate]
                for candidate in sorted(
                    _normalize_candidates(info.model_id), key=len, reverse=True
                )
                if candidate in flattened
            ),
            None,
        )
        if metadata is None:
            enriched.append(info)
            continue
        enriched.append(
            ProviderModelInfo(
                model_id=info.model_id,
                supports_thinking=info.supports_thinking,
                supports_vision=(
                    info.supports_vision
                    if info.supports_vision is not None
                    else metadata.supports_vision
                ),
                context_length=info.context_length or metadata.context_length,
                input_price=(
                    info.input_price
                    if info.input_price is not None
                    else metadata.input_price
                ),
                output_price=(
                    info.output_price
                    if info.output_price is not None
                    else metadata.output_price
                ),
            )
        )
    return tuple(enriched)


async def enrich_provider_model_infos(
    model_infos: Iterable[ProviderModelInfo],
    path: Path | None = None,
) -> tuple[ProviderModelInfo, ...]:
    """Enrich model infos from the models.dev cache; schedule a refresh.

    Never performs blocking network I/O: a missing or stale cache schedules a
    fire-and-forget refresh and the current infos pass through unchanged.
    """
    infos = tuple(model_infos)
    cache = read_models_dev_cache(path)
    if cache is None or not cache.fresh:
        schedule_models_dev_refresh(path)
    if cache is None:
        return infos
    return enrich_model_infos(infos, cache.index)


# --------------------------------------------------------------------------
# Reasoning capability lookup (data + lookup only; no request-building code
# reads this yet — that is a later PR).
# --------------------------------------------------------------------------

# This project's provider ids don't always match models.dev's provider ids.
# This is the single place that maps one onto the other; extend it here, not
# with a parallel matcher elsewhere. Providers absent from this map are
# assumed to share their id with models.dev (checked first, so an alias entry
# is only needed when the ids genuinely differ).
PROVIDER_ID_ALIASES: dict[str, str] = {
    "open_router": "openrouter",
    "nvidia_nim": "nvidia",
    "fireworks": "fireworks-ai",
    "together": "togetherai",
    "novita": "novita-ai",
    "bedrock": "amazon-bedrock",
    "gemini": "google",
    "vertex": "google-vertex",
    "azure_openai": "azure",
    "cline": "cline-pass",
    "kimi_coding": "kimi-for-coding",
    "alibaba_cn": "alibaba-cn",
    "alibaba_coding": "alibaba-coding-plan",
    "alibaba_coding_cn": "alibaba-coding-plan-cn",
    "ollama_cloud": "ollama-cloud",
    "chatgpt_oauth": "openai",
    "anthropic_oauth": "anthropic",
    "github_models": "github-copilot",
}

_EFFORT_BY_VALUE: dict[str, ReasoningEffort] = {
    member.value: member for member in ReasoningEffort
}


def _parse_reasoning_capability(
    metadata: Mapping[str, Any],
) -> ModelReasoningCapability:
    """Parse ``reasoning``/``reasoning_options`` off one models.dev model entry."""
    raw_can_reason = metadata.get("reasoning")
    can_reason = raw_can_reason if isinstance(raw_can_reason, bool) else None

    options = metadata.get("reasoning_options")
    if not isinstance(options, list):
        # No (or malformed) options list: control styles are unknown, not
        # known-false. ``can_reason`` may still be known from the flag above.
        return ModelReasoningCapability(can_reason=can_reason)

    supports_effort = False
    supports_toggle = False
    supports_budget = False
    supported_efforts: frozenset[ReasoningEffort] | None = None
    for option in options:
        if not isinstance(option, Mapping):
            continue
        option_type = option.get("type")
        if option_type == "effort":
            supports_effort = True
            values = option.get("values")
            supported_efforts = (
                frozenset(
                    _EFFORT_BY_VALUE[value]
                    for value in values
                    if isinstance(value, str) and value in _EFFORT_BY_VALUE
                )
                if isinstance(values, list)
                else frozenset()
            )
        elif option_type == "toggle":
            supports_toggle = True
        elif option_type == "budget_tokens":
            supports_budget = True

    return ModelReasoningCapability(
        can_reason=can_reason,
        supports_effort_control=supports_effort,
        supports_toggle_control=supports_toggle,
        supports_budget_control=supports_budget,
        supported_efforts=supported_efforts,
    )


def _build_reasoning_index(
    index: Mapping[str, Any],
) -> dict[str, dict[str, ModelReasoningCapability]]:
    """Build ``{models.dev provider id: {normalized model id: capability}}``."""
    built: dict[str, dict[str, ModelReasoningCapability]] = {}
    for provider_id, bucket in index.items():
        if not isinstance(provider_id, str) or not isinstance(bucket, Mapping):
            continue
        models = bucket.get("models")
        if not isinstance(models, Mapping):
            continue
        per_model: dict[str, ModelReasoningCapability] = {}
        for model_id, metadata in models.items():
            if not isinstance(model_id, str) or not isinstance(metadata, Mapping):
                continue
            capability = _parse_reasoning_capability(metadata)
            for candidate in _normalize_candidates(model_id):
                per_model.setdefault(candidate, capability)
        if per_model:
            built[provider_id] = per_model
    return built


_reasoning_index_lock = threading.Lock()
# Path -> (source mtime, built index) so a 4MB parse happens at most once per
# on-disk cache generation, not once per lookup/request.
_reasoning_index_cache: dict[Path, tuple[float, dict[str, dict[str, Any]]]] = {}


def _cached_reasoning_index(
    path: Path | None,
) -> dict[str, dict[str, ModelReasoningCapability]]:
    cache_path = path if path is not None else models_dev_cache_path()
    try:
        mtime = cache_path.stat().st_mtime
    except OSError:
        # No on-disk cache yet (fresh install, offline): unknown, not an
        # error. A background refresh (elsewhere) will populate it later.
        return {}
    with _reasoning_index_lock:
        cached = _reasoning_index_cache.get(cache_path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    cache = read_models_dev_cache(cache_path)
    built = _build_reasoning_index(cache.index) if cache is not None else {}
    with _reasoning_index_lock:
        _reasoning_index_cache[cache_path] = (mtime, built)
    return built


def model_reasoning_capability_from_models_dev(
    provider_id: str, model_id: str, path: Path | None = None
) -> ModelReasoningCapability | None:
    """Return the models.dev-reported reasoning capability, or None if unknown.

    None means "no data at all" (provider or model absent from the index),
    which is distinct from a returned :class:`ModelReasoningCapability` whose
    fields are individually ``None``/``False``. Reads the disk-cached index
    only (never the in-memory :class:`ProviderModelCache`), so this works
    before any admin refresh has ever run, and it is a pure, cheap, memoized
    lookup: safe to call per request.
    """
    reasoning_index = _cached_reasoning_index(path)
    bucket = reasoning_index.get(provider_id)
    if bucket is None:
        alias = PROVIDER_ID_ALIASES.get(provider_id)
        if alias is not None:
            bucket = reasoning_index.get(alias)
    if bucket is None:
        return None
    for candidate in sorted(_normalize_candidates(model_id), key=len, reverse=True):
        found = bucket.get(candidate)
        if found is not None:
            return found
    return None


def _build_output_limit_index(
    index: Mapping[str, Any],
) -> dict[str, dict[str, int]]:
    """Build ``{models.dev provider id: {normalized model id: limit.output}}``.

    models.dev publishes ``limit.output`` for the overwhelming majority of its
    rows; a model without one is simply absent here, which callers must read
    as "unknown limit", never as "no limit".
    """
    built: dict[str, dict[str, int]] = {}
    for provider_id, bucket in index.items():
        if not isinstance(provider_id, str) or not isinstance(bucket, Mapping):
            continue
        models = bucket.get("models")
        if not isinstance(models, Mapping):
            continue
        per_model: dict[str, int] = {}
        for model_id, metadata in models.items():
            if not isinstance(model_id, str) or not isinstance(metadata, Mapping):
                continue
            limit = metadata.get("limit")
            if not isinstance(limit, Mapping):
                continue
            output = _int_or_none(limit.get("output"))
            if output is None or output <= 0:
                continue
            for candidate in _normalize_candidates(model_id):
                per_model.setdefault(candidate, output)
        if per_model:
            built[provider_id] = per_model
    return built


_output_limit_index_lock = threading.Lock()
_output_limit_index_cache: dict[Path, tuple[float, dict[str, dict[str, int]]]] = {}


def _cached_output_limit_index(path: Path | None) -> dict[str, dict[str, int]]:
    cache_path = path if path is not None else models_dev_cache_path()
    try:
        mtime = cache_path.stat().st_mtime
    except OSError:
        return {}
    with _output_limit_index_lock:
        cached = _output_limit_index_cache.get(cache_path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    cache = read_models_dev_cache(cache_path)
    built = _build_output_limit_index(cache.index) if cache is not None else {}
    with _output_limit_index_lock:
        _output_limit_index_cache[cache_path] = (mtime, built)
    return built


def model_output_limit_from_models_dev(
    provider_id: str, model_id: str, path: Path | None = None
) -> int | None:
    """Return the model's published output-token limit, or None when unknown.

    Same disk-cache-only, memoized lookup contract as
    :func:`model_reasoning_capability_from_models_dev`; safe per request.
    """
    limit_index = _cached_output_limit_index(path)
    bucket = limit_index.get(provider_id)
    if bucket is None:
        alias = PROVIDER_ID_ALIASES.get(provider_id)
        if alias is not None:
            bucket = limit_index.get(alias)
    if bucket is None:
        return None
    for candidate in sorted(_normalize_candidates(model_id), key=len, reverse=True):
        found = bucket.get(candidate)
        if found is not None:
            return found
    return None


def resolve_model_reasoning_capability(
    provider_id: str,
    model_id: str,
    provider_supports_thinking: bool | None,
    path: Path | None = None,
) -> ModelReasoningCapability | None:
    """Layer provider-reported capability over the models.dev fallback.

    ``provider_supports_thinking`` (typically
    ``ProviderModelInfo.supports_thinking``) wins for ``can_reason`` whenever
    it is not ``None``; control-style detail (effort/toggle/budget support,
    and the effort vocabulary) always comes from models.dev, since providers
    in this project only ever report the single thinking-supported boolean.
    Returns ``None`` only when neither layer has any data at all.
    """
    models_dev_capability = model_reasoning_capability_from_models_dev(
        provider_id, model_id, path
    )
    if provider_supports_thinking is None:
        return models_dev_capability
    if models_dev_capability is None:
        return ModelReasoningCapability(can_reason=provider_supports_thinking)
    return ModelReasoningCapability(
        can_reason=provider_supports_thinking,
        supports_effort_control=models_dev_capability.supports_effort_control,
        supports_toggle_control=models_dev_capability.supports_toggle_control,
        supports_budget_control=models_dev_capability.supports_budget_control,
        supported_efforts=models_dev_capability.supported_efforts,
    )
