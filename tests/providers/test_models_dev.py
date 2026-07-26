"""Tests for the models.dev metadata fallback."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from free_claude_code.application.model_metadata import ProviderModelInfo
from free_claude_code.providers.runtime import models_dev
from free_claude_code.providers.runtime.models_dev import (
    enrich_model_infos,
    enrich_provider_model_infos,
    read_models_dev_cache,
    refresh_models_dev_cache,
    write_models_dev_cache,
)

_INDEX = {
    "acme": {
        "models": {
            "acme/llama-3.3-70b": {
                "cost": {"input": 0.1, "output": 0.2},
                "limit": {"context": 131072},
            },
            "acme/small": {"cost": {"input": 0.01, "output": 0.02}},
        }
    },
    "other": {
        "models": {
            "other-org/deepseek-v3.2": {
                "cost": {"input": 0.3, "output": 0.5},
                "limit": {"context": 65536},
            }
        }
    },
}


def _write_cache(path: Path, *, age_hours: float = 0.0) -> None:
    fetched = datetime.now(UTC) - timedelta(hours=age_hours)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"fetched_at": fetched.isoformat(), "index": _INDEX}),
        encoding="utf-8",
    )


def test_cache_roundtrip_and_freshness(tmp_path: Path) -> None:
    path = tmp_path / "cache" / "models-dev.json"
    write_models_dev_cache(_INDEX, path)

    cache = read_models_dev_cache(path)

    assert cache is not None
    assert cache.fresh is True
    assert cache.index == _INDEX


def test_read_cache_missing_and_corrupt(tmp_path: Path) -> None:
    assert read_models_dev_cache(tmp_path / "nope.json") is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert read_models_dev_cache(corrupt) is None


def test_read_cache_stale_after_24h(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_cache(path, age_hours=25)

    cache = read_models_dev_cache(path)

    assert cache is not None
    assert cache.fresh is False


def test_enrich_matches_exact_and_normalized_ids() -> None:
    infos = (
        ProviderModelInfo(model_id="acme/llama-3.3-70b"),
        # Provider-side id carries extra prefix segments: last-segment match.
        ProviderModelInfo(model_id="accounts/acme/models/llama-3.3-70b"),
        # models.dev-side prefix stripped via candidate normalization.
        ProviderModelInfo(model_id="deepseek-v3.2"),
        ProviderModelInfo(model_id="unknown-model"),
    )

    enriched = enrich_model_infos(infos, _INDEX)

    assert enriched[0].context_length == 131072
    assert enriched[0].input_price == 0.1
    assert enriched[0].output_price == 0.2
    assert enriched[1].context_length == 131072
    assert enriched[2].context_length == 65536
    assert enriched[3].context_length is None
    assert enriched[3].input_price is None


def test_enrich_preserves_existing_metadata() -> None:
    infos = (
        ProviderModelInfo(
            model_id="acme/llama-3.3-70b",
            supports_thinking=True,
            context_length=1000,
            input_price=9.9,
        ),
    )

    enriched = enrich_model_infos(infos, _INDEX)

    assert enriched[0].supports_thinking is True
    assert enriched[0].context_length == 1000
    assert enriched[0].input_price == 9.9
    assert enriched[0].output_price == 0.2


@pytest.mark.asyncio
async def test_enrich_uses_cache_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "models-dev.json"
    _write_cache(path)

    async def _boom():
        raise AssertionError("network must not be touched")

    monkeypatch.setattr(models_dev, "fetch_models_dev_index", _boom)

    enriched = await enrich_provider_model_infos(
        [ProviderModelInfo(model_id="acme/llama-3.3-70b")], path
    )

    assert enriched[0].context_length == 131072


@pytest.mark.asyncio
async def test_enrich_without_cache_schedules_refresh_and_passes_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "models-dev.json"
    fetched: list[str] = []

    async def _fake_fetch():
        fetched.append("hit")
        return _INDEX

    monkeypatch.setattr(models_dev, "fetch_models_dev_index", _fake_fetch)

    enriched = await enrich_provider_model_infos(
        [ProviderModelInfo(model_id="acme/llama-3.3-70b")], path
    )

    assert enriched[0].context_length is None
    for _ in range(100):
        await asyncio.sleep(0.01)
        if path.is_file():
            break
    assert fetched == ["hit"]
    assert path.is_file()


@pytest.mark.asyncio
async def test_refresh_is_silent_when_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "models-dev.json"

    async def _offline():
        return None

    monkeypatch.setattr(models_dev, "fetch_models_dev_index", _offline)

    result = await refresh_models_dev_cache(path)

    assert result is False
    assert not path.exists()


@pytest.mark.asyncio
async def test_fetch_returns_none_on_httpx_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    class _FailingClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FailingClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str) -> object:
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(models_dev.httpx, "AsyncClient", _FailingClient)

    assert await models_dev.fetch_models_dev_index() is None


def test_normalize_candidates() -> None:
    candidates = models_dev._normalize_candidates("Acme/Llama-3.3-70B")

    assert "acme/llama-3.3-70b" in candidates
    assert "llama-3.3-70b" in candidates
