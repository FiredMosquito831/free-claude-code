"""Tests for the models.dev metadata fallback."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.providers.runtime import models_dev
from my_claude_code.providers.runtime.models_dev import (
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


# --------------------------------------------------------------------------
# Reasoning capability lookup
# --------------------------------------------------------------------------

from my_claude_code.core.reasoning import ReasoningEffort  # noqa: E402
from my_claude_code.providers.runtime.models_dev import (  # noqa: E402
    PROVIDER_ID_ALIASES,
    model_reasoning_capability_from_models_dev,
    resolve_model_reasoning_capability,
)

_REASONING_INDEX = {
    "openrouter": {
        "models": {
            "acme/all-controls": {
                "reasoning": True,
                "reasoning_options": [
                    {"type": "toggle"},
                    {
                        "type": "effort",
                        "values": [
                            "none",
                            "minimal",
                            "low",
                            "medium",
                            "high",
                            "xhigh",
                            "max",
                            "default",
                        ],
                    },
                    {"type": "budget_tokens"},
                ],
            },
            "acme/no-reasoning": {"reasoning": False},
            "acme/malformed-options": {
                "reasoning": True,
                "reasoning_options": "not-a-list",
            },
            "acme/malformed-effort": {
                "reasoning": True,
                "reasoning_options": [{"type": "effort", "values": "not-a-list"}],
            },
        }
    },
    "anthropic": {
        "models": {
            "claude-x": {"reasoning": True, "reasoning_options": [{"type": "toggle"}]}
        }
    },
    "not-a-provider-bucket": "oops",
}


def _write_reasoning_cache(path: Path) -> None:
    write_models_dev_cache(_REASONING_INDEX, path)


def test_parses_effort_toggle_and_budget_together(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    capability = model_reasoning_capability_from_models_dev(
        "open_router", "acme/all-controls", path
    )

    assert capability is not None
    assert capability.can_reason is True
    assert capability.supports_effort_control is True
    assert capability.supports_toggle_control is True
    assert capability.supports_budget_control is True


def test_effort_values_map_onto_reasoning_effort_ignoring_unknown(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    capability = model_reasoning_capability_from_models_dev(
        "open_router", "acme/all-controls", path
    )

    assert capability is not None
    assert capability.supported_efforts == frozenset(
        {
            ReasoningEffort.MINIMAL,
            ReasoningEffort.LOW,
            ReasoningEffort.MEDIUM,
            ReasoningEffort.HIGH,
            ReasoningEffort.XHIGH,
            ReasoningEffort.MAX,
        }
    )
    # "none" and "default" are not ReasoningEffort members and must not raise.
    assert capability.supported_efforts is not None
    assert "none" not in [effort.value for effort in capability.supported_efforts]


def test_known_not_reasoning_is_distinct_from_unknown_model(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    known_false = model_reasoning_capability_from_models_dev(
        "open_router", "acme/no-reasoning", path
    )
    unknown = model_reasoning_capability_from_models_dev(
        "open_router", "acme/does-not-exist", path
    )

    assert known_false is not None
    assert known_false.can_reason is False
    assert unknown is None


@pytest.mark.parametrize(
    "provider_id,models_dev_id",
    [
        ("open_router", "openrouter"),
        ("azure_openai", "azure"),
        ("bedrock", "amazon-bedrock"),
        ("gemini", "google"),
        ("vertex", "google-vertex"),
        ("fireworks", "fireworks-ai"),
        ("together", "togetherai"),
        ("novita", "novita-ai"),
        ("cline", "cline-pass"),
        ("kimi_coding", "kimi-for-coding"),
        ("alibaba_cn", "alibaba-cn"),
        ("alibaba_coding", "alibaba-coding-plan"),
        ("alibaba_coding_cn", "alibaba-coding-plan-cn"),
        ("ollama_cloud", "ollama-cloud"),
        ("chatgpt_oauth", "openai"),
        ("anthropic_oauth", "anthropic"),
        ("github_models", "github-copilot"),
    ],
)
def test_all_declared_aliases_resolve(provider_id: str, models_dev_id: str) -> None:
    assert PROVIDER_ID_ALIASES[provider_id] == models_dev_id


def test_alias_resolution_actually_reaches_the_model(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    # "open_router" is our provider id; the fixture only has "openrouter".
    capability = model_reasoning_capability_from_models_dev(
        "open_router", "acme/no-reasoning", path
    )
    # "anthropic_oauth" aliases onto "anthropic", which does exist directly
    # here too, exercising both the alias path and a same-named provider.
    aliased = model_reasoning_capability_from_models_dev(
        "anthropic_oauth", "claude-x", path
    )

    assert capability is not None
    assert aliased is not None
    assert aliased.supports_toggle_control is True


def test_provider_absent_from_index_is_unknown_not_an_error(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    # "ollama" is a real project provider id with no models.dev entry at all.
    result = model_reasoning_capability_from_models_dev("ollama", "any-model", path)

    assert result is None


def test_layering_provider_reported_wins_when_known(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    # models.dev says False, provider says True: provider wins for can_reason.
    resolved = resolve_model_reasoning_capability(
        "open_router", "acme/no-reasoning", True, path
    )

    assert resolved is not None
    assert resolved.can_reason is True


def test_layering_falls_back_to_models_dev_when_provider_unknown(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    resolved = resolve_model_reasoning_capability(
        "open_router", "acme/all-controls", None, path
    )

    assert resolved is not None
    assert resolved.can_reason is True
    assert resolved.supports_budget_control is True


def test_layering_unknown_when_neither_layer_has_data(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    resolved = resolve_model_reasoning_capability("ollama", "any-model", None, path)

    assert resolved is None


def test_lookup_works_with_empty_in_memory_provider_model_cache(
    tmp_path: Path,
) -> None:
    """Anti-'gate that never opens' test.

    ProviderModelCache is populated only by an admin refresh and is empty on
    a fresh server. The reasoning lookup must read the disk-cached models.dev
    index directly and must not depend on ProviderModelCache at all.
    """
    from my_claude_code.providers.runtime.model_cache import ProviderModelCache

    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    empty_cache = ProviderModelCache(available_provider_ids=["open_router"])
    assert empty_cache.has_provider("open_router") is False

    capability = model_reasoning_capability_from_models_dev(
        "open_router", "acme/all-controls", path
    )

    assert capability is not None
    assert capability.can_reason is True


def test_malformed_reasoning_options_degrade_to_unknown_not_raise(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    not_a_list = model_reasoning_capability_from_models_dev(
        "open_router", "acme/malformed-options", path
    )
    bad_values = model_reasoning_capability_from_models_dev(
        "open_router", "acme/malformed-effort", path
    )

    assert not_a_list is not None
    assert not_a_list.can_reason is True
    assert not_a_list.supports_effort_control is None
    assert not_a_list.supports_toggle_control is None
    assert not_a_list.supports_budget_control is None

    assert bad_values is not None
    assert bad_values.supports_effort_control is True
    assert bad_values.supported_efforts == frozenset()


def test_missing_index_returns_unknown_not_raise(tmp_path: Path) -> None:
    result = model_reasoning_capability_from_models_dev(
        "open_router", "acme/all-controls", tmp_path / "nope.json"
    )

    assert result is None


def test_reasoning_index_is_memoized_until_mtime_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    calls: list[str] = []
    real_read = models_dev.read_models_dev_cache

    def _counting_read(cache_path: Path | None = None) -> object:
        calls.append("read")
        return real_read(cache_path)

    monkeypatch.setattr(models_dev, "read_models_dev_cache", _counting_read)
    models_dev._reasoning_index_cache.clear()

    model_reasoning_capability_from_models_dev("open_router", "acme/no-reasoning", path)
    model_reasoning_capability_from_models_dev("open_router", "acme/no-reasoning", path)
    model_reasoning_capability_from_models_dev("open_router", "acme/no-reasoning", path)

    assert calls == ["read"]

    _write_reasoning_cache(path)  # bumps mtime
    model_reasoning_capability_from_models_dev("open_router", "acme/no-reasoning", path)

    assert calls == ["read", "read"]


# --------------------------------------------------------------------------
# Output-token limit lookup
# --------------------------------------------------------------------------

from my_claude_code.providers.runtime.models_dev import (  # noqa: E402
    model_output_limit_from_models_dev,
)

_LIMIT_INDEX = {
    "openrouter": {
        "models": {
            "acme/limited": {"limit": {"context": 200000, "output": 64000}},
            "acme/no-limit-block": {"reasoning": True},
            "acme/context-only": {"limit": {"context": 200000}},
            "acme/zero-output": {"limit": {"output": 0}},
        }
    },
    "not-a-provider-bucket": "oops",
}


def _write_limit_cache(path: Path) -> None:
    write_models_dev_cache(_LIMIT_INDEX, path)


def test_output_limit_is_read_from_models_dev(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_limit_cache(path)

    assert model_output_limit_from_models_dev("open_router", "acme/limited", path) == (
        64000
    )
    # The alias map is honored exactly as the capability lookup honors it.
    assert model_output_limit_from_models_dev("openrouter", "limited", path) == 64000


@pytest.mark.parametrize(
    "model_id",
    ["acme/no-limit-block", "acme/context-only", "acme/zero-output", "acme/absent"],
)
def test_missing_or_unusable_output_limit_is_unknown(
    tmp_path: Path, model_id: str
) -> None:
    path = tmp_path / "models-dev.json"
    _write_limit_cache(path)

    assert model_output_limit_from_models_dev("open_router", model_id, path) is None


def test_output_limit_without_a_cache_is_unknown(tmp_path: Path) -> None:
    assert (
        model_output_limit_from_models_dev(
            "open_router", "acme/limited", tmp_path / "nope.json"
        )
        is None
    )


def test_output_limit_index_is_memoized_until_mtime_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "models-dev.json"
    _write_limit_cache(path)

    calls: list[str] = []
    real_read = models_dev.read_models_dev_cache

    def _counting_read(cache_path: Path | None = None) -> object:
        calls.append("read")
        return real_read(cache_path)

    monkeypatch.setattr(models_dev, "read_models_dev_cache", _counting_read)
    models_dev._output_limit_index_cache.clear()

    for _ in range(3):
        model_output_limit_from_models_dev("open_router", "acme/limited", path)

    assert calls == ["read"]


# --------------------------------------------------------------------------
# Provider alias coverage, drift guard, and tag-stripped model matching
# --------------------------------------------------------------------------

from my_claude_code.config.provider_catalog import PROVIDER_CATALOG  # noqa: E402

_MATCHING_INDEX = {
    "opencode-go": {"models": {"opencode-go/gpt-5": {"reasoning": True}}},
    "wafer.ai": {"models": {"wafer-1": {"reasoning": True}}},
    "moonshotai": {"models": {"kimi-k3": {"reasoning": True}}},
    "cloudflare-workers-ai": {"models": {"@cf/nvidia/nemotron": {"reasoning": True}}},
    # A "llama" provider exists here on purpose: our llamacpp provider must
    # still resolve to None (rejected pairing, see PROVIDER_ID_ALIASES).
    "llama": {"models": {"llama-4": {"reasoning": True}}},
    "openrouter": {
        "models": {
            "deepseek/deepseek-r1:free": {"reasoning": False},
            "vendor/tagged-only:free": {"reasoning": True},
            "vendor/both": {
                "reasoning": True,
                "reasoning_options": [{"type": "toggle"}],
            },
            "vendor/both:free": {"reasoning": False},
            "vendor/thinker": {"reasoning": False},
            "vendor/numeric": {"reasoning": False},
        }
    },
}


def _write_matching_cache(path: Path) -> None:
    write_models_dev_cache(_MATCHING_INDEX, path)


@pytest.mark.parametrize(
    ("provider_id", "model_id"),
    [
        ("opencode_go", "opencode-go/gpt-5"),
        ("wafer", "wafer-1"),
        ("kimi", "kimi-k3"),
        ("cloudflare", "@cf/nvidia/nemotron"),
    ],
)
def test_new_aliases_reach_their_models_dev_bucket(
    tmp_path: Path, provider_id: str, model_id: str
) -> None:
    path = tmp_path / "models-dev.json"
    _write_matching_cache(path)

    capability = model_reasoning_capability_from_models_dev(provider_id, model_id, path)

    assert capability is not None
    assert capability.can_reason is True


def test_every_alias_key_is_a_real_provider_id() -> None:
    unknown = sorted(set(PROVIDER_ID_ALIASES) - set(PROVIDER_CATALOG))

    assert unknown == []


def test_no_alias_maps_a_provider_id_onto_itself() -> None:
    self_mapped = sorted(
        key for key, value in PROVIDER_ID_ALIASES.items() if key == value
    )

    assert self_mapped == []


def test_alias_map_has_no_duplicate_keys() -> None:
    source = Path(models_dev.__file__).read_text(encoding="utf-8")
    block = source.split("PROVIDER_ID_ALIASES: dict[str, str] = {", 1)[1]
    block = block.split("\n}", 1)[0]
    keys = [
        line.split('"')[1]
        for line in block.splitlines()
        if line.strip().startswith('"')
    ]

    assert sorted(keys) == sorted(set(keys))


def test_free_tag_falls_back_to_the_untagged_entry(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_matching_cache(path)

    tagged = model_reasoning_capability_from_models_dev(
        "open_router", "vendor/thinker:free", path
    )

    # "vendor/thinker:free" is not listed; the tag is stripped and it resolves
    # to the untagged "vendor/thinker" entry in the same provider bucket.
    assert tagged is not None
    assert tagged.can_reason is False


def test_exact_match_wins_over_tag_stripped_match(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_matching_cache(path)

    exact = model_reasoning_capability_from_models_dev(
        "open_router", "vendor/both:free", path
    )

    assert exact is not None
    assert exact.can_reason is False
    # The untagged "vendor/both" carries a toggle; the exact ":free" row does
    # not, so getting the toggle here would mean the wrong row was returned.
    assert exact.supports_toggle_control is not True


def test_thinking_and_numeric_tags_are_not_stripped(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_matching_cache(path)

    thinking = model_reasoning_capability_from_models_dev(
        "open_router", "vendor/thinker:thinking", path
    )
    numeric = model_reasoning_capability_from_models_dev(
        "open_router", "vendor/numeric:32000", path
    )

    assert thinking is None
    assert numeric is None


def test_rejected_llamacpp_pairing_stays_unknown(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_matching_cache(path)

    # A "llama" provider is present in the fixture; llamacpp must NOT match it.
    assert (
        model_reasoning_capability_from_models_dev("llamacpp", "llama-4", path) is None
    )
