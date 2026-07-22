"""Catalog-derived Admin web search fields (selection, credentials, options)."""

from typing import Any

from free_claude_code.config.websearch_catalog import WEBSEARCH_CATALOG

from .spec import ConfigOptionSpec

# Mirrors websearch.rotation.ROTATION_POLICIES. The import boundary forbids
# config/ from importing websearch/, so the literal tuple lives here and
# tests/config/test_admin_websearch_manifest.py asserts parity.
ROTATION_POLICY_OPTIONS: tuple[str, ...] = (
    "single",
    "round_robin",
    "least_used",
    "failover",
)

_ROTATION_OPTION_LABELS: dict[str, str] = {
    "single": "Single key",
    "round_robin": "Round robin",
    "least_used": "Least used",
    "failover": "Failover",
}

ROTATION_DEFAULT_OPTION = ConfigOptionSpec(
    "",
    "Auto (failover across multiple keys, single otherwise)",
)


def websearch_field_specs() -> tuple[dict[str, Any], ...]:
    """Return web search fields generated from the web search catalog."""

    return (
        _provider_select_spec(),
        _searxng_base_url_spec(),
        *_credential_field_specs(),
        *_advanced_option_field_specs(),
    )


def _provider_select_spec() -> dict[str, Any]:
    return {
        "key": "WEB_SEARCH_PROVIDER",
        "label": "Web Search Provider",
        "section_id": "websearch",
        "field_type": "select",
        "settings_attr": "web_search_provider",
        "default": "auto",
        "options": (
            ConfigOptionSpec("auto", "Auto (first configured provider)"),
            ConfigOptionSpec("off", "Off (legacy DuckDuckGo scrape)"),
            *(
                ConfigOptionSpec(descriptor.provider_id, descriptor.display_name)
                for descriptor in WEBSEARCH_CATALOG.values()
            ),
        ),
        "description": (
            "Backend for Claude Code's web_search server tool. Auto uses the first "
            "configured provider below (else DuckDuckGo); Off keeps the legacy "
            "DuckDuckGo HTML scrape."
        ),
    }


def _searxng_base_url_spec() -> dict[str, Any]:
    return {
        "key": "SEARXNG_BASE_URL",
        "label": "SearXNG Base URL",
        "section_id": "websearch",
        "settings_attr": "searxng_base_url",
        "description": (
            "Self-hosted SearXNG instance URL; the instance must enable "
            "format=json in its settings.yml."
        ),
    }


def _credential_field_specs() -> tuple[dict[str, Any], ...]:
    specs: list[dict[str, Any]] = []
    for descriptor in WEBSEARCH_CATALOG.values():
        if descriptor.credential_env is None or descriptor.settings_attr is None:
            continue
        specs.append(
            {
                "key": descriptor.credential_env,
                "label": f"{descriptor.display_name} API Key",
                "section_id": "websearch",
                "field_type": "secret",
                "settings_attr": descriptor.settings_attr,
                "secret": True,
                "description": (
                    f"{descriptor.free_tier}. Comma-separate multiple keys for "
                    f"rotation. Obtain a key at {descriptor.credential_url}."
                ),
            }
        )
        specs.append(
            {
                "key": f"{descriptor.credential_env}_ROTATION",
                "label": f"{descriptor.display_name} Key Rotation",
                "section_id": "websearch",
                "field_type": "select",
                "options": (
                    ROTATION_DEFAULT_OPTION,
                    *(
                        ConfigOptionSpec(policy, _ROTATION_OPTION_LABELS[policy])
                        for policy in ROTATION_POLICY_OPTIONS
                    ),
                ),
                "description": (
                    "Rotation policy across the comma-separated keys above "
                    "(dotenv-only, hot-reloaded)."
                ),
            }
        )
    return tuple(specs)


# WebSearchOptionSpec.field_type -> ConfigFieldSpec.field_type. Unrecognized
# option types degrade to a plain text input so the admin UI keeps working if
# the catalog later grows new option types.
_ADVANCED_OPTION_FIELD_TYPES: dict[str, str] = {
    "select": "select",
    "text": "text",
    "number": "number",
    "boolean": "boolean",
}


def _advanced_option_field_specs() -> tuple[dict[str, Any], ...]:
    """Return dotenv-only advanced option fields from catalog descriptors."""

    specs: list[dict[str, Any]] = []
    for descriptor in WEBSEARCH_CATALOG.values():
        # Descriptors predate advanced_options on this branch; tolerate both
        # shapes so the manifest works before and after the catalog lands.
        for option in getattr(descriptor, "advanced_options", ()):
            spec: dict[str, Any] = {
                "key": option.env,
                "label": option.label,
                "section_id": "websearch",
                "field_type": _ADVANCED_OPTION_FIELD_TYPES.get(
                    option.field_type,
                    "text",
                ),
                "default": option.default,
                "advanced": True,
                "description": option.cost_note or "Dotenv-only advanced option.",
            }
            if option.field_type == "select":
                spec["options"] = tuple(
                    ConfigOptionSpec(value, label) for value, label in option.options
                )
            specs.append(spec)
    return tuple(specs)
