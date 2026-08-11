"""models.dev metadata fallback for custom and thin built-in providers.

Fetches https://models.dev/api.json (10s timeout) and caches it at
``config_dir_path()/cache/models-dev.json`` with a ``fetched_at`` timestamp.
The cache is used when it is fresh (<24h); a stale cache is still used while a
background refresh is scheduled. Everything is fully silent when offline:
discovery never fails because models.dev is unreachable.
"""

import asyncio
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.paths import config_dir_path

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
