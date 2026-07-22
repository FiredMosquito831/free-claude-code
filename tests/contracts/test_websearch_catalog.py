"""Freeze ``WEBSEARCH_CATALOG`` insertion order and field sanity."""

from free_claude_code.config.settings import Settings
from free_claude_code.config.websearch_catalog import (
    SUPPORTED_WEBSEARCH_PROVIDER_IDS,
    WEBSEARCH_CATALOG,
)
from free_claude_code.websearch.adapters import ADAPTER_CLASSES

_EXPECTED_WEBSEARCH_ORDER: tuple[str, ...] = (
    "ddgs",
    "ollama",
    "exa",
    "tavily",
    "brave",
    "searxng",
    "jina",
    "serper",
    "firecrawl",
    "linkup",
    "perplexity",
    "parallel",
    "searchapi",
    "serpapi",
)


def test_websearch_catalog_key_order_matches_canonical_plan() -> None:
    """ddgs first (keyless fallback); keyed providers follow per coordination spec."""

    assert tuple(WEBSEARCH_CATALOG.keys()) == _EXPECTED_WEBSEARCH_ORDER
    assert SUPPORTED_WEBSEARCH_PROVIDER_IDS == _EXPECTED_WEBSEARCH_ORDER


def test_websearch_catalog_descriptors_are_internally_consistent() -> None:
    problems: list[str] = []
    for provider_id, desc in WEBSEARCH_CATALOG.items():
        if desc.provider_id != provider_id:
            problems.append(f"{provider_id}: provider_id mismatch {desc.provider_id!r}")
        if not desc.display_name.strip():
            problems.append(f"{provider_id}: display_name is empty")
        if desc.requires_key:
            if desc.credential_env is None:
                problems.append(
                    f"{provider_id}: requires_key but credential_env is None"
                )
            if desc.settings_attr is None:
                problems.append(
                    f"{provider_id}: requires_key but settings_attr is None"
                )
        else:
            if desc.credential_env is not None:
                problems.append(f"{provider_id}: keyless but credential_env set")
            if desc.settings_attr is not None:
                problems.append(f"{provider_id}: keyless but settings_attr set")
        if desc.credential_env is not None and not desc.credential_env.endswith("_KEY"):
            problems.append(
                f"{provider_id}: credential_env {desc.credential_env} shape"
            )
        if (
            desc.base_url_attr is None
            and desc.default_base_url is None
            and desc.requires_key
        ):
            problems.append(f"{provider_id}: keyed provider without a default base URL")

    assert problems == []


def test_websearch_catalog_settings_attrs_match_settings_fields() -> None:
    """Every settings_attr exists on Settings with the matching env alias."""

    problems: list[str] = []
    for provider_id, desc in WEBSEARCH_CATALOG.items():
        for attr, expected_env in (
            (desc.settings_attr, desc.credential_env),
            (desc.base_url_attr, "SEARXNG_BASE_URL"),
        ):
            if attr is None:
                continue
            field = Settings.model_fields.get(attr)
            if field is None:
                problems.append(f"{provider_id}: Settings.{attr} missing")
                continue
            if expected_env is not None and str(field.validation_alias) != expected_env:
                problems.append(
                    f"{provider_id}: Settings.{attr} alias "
                    f"{field.validation_alias!r} != {expected_env!r}"
                )

    assert problems == []


def test_websearch_catalog_matches_adapter_registry() -> None:
    """ADAPTER_CLASSES mirrors the catalog ids, order, provider ids, and flags."""

    assert tuple(ADAPTER_CLASSES.keys()) == _EXPECTED_WEBSEARCH_ORDER
    problems: list[str] = []
    for provider_id, adapter_cls in ADAPTER_CLASSES.items():
        desc = WEBSEARCH_CATALOG[provider_id]
        if provider_id != adapter_cls.PROVIDER_ID:
            problems.append(f"{provider_id}: PROVIDER_ID {adapter_cls.PROVIDER_ID!r}")
        if desc.supports_domains != adapter_cls.SUPPORTS_DOMAINS:
            problems.append(
                f"{provider_id}: SUPPORTS_DOMAINS={adapter_cls.SUPPORTS_DOMAINS} "
                f"but catalog supports_domains={desc.supports_domains}"
            )

    assert problems == []
