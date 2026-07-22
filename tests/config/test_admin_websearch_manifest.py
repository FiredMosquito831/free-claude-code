"""Admin manifest contract for catalog-derived web search fields."""

from free_claude_code.config.admin.manifest import (
    FIELD_BY_KEY,
    SECTIONS,
    ConfigOptionSpec,
)
from free_claude_code.config.admin.websearch_manifest import (
    ROTATION_POLICY_OPTIONS,
    websearch_field_specs,
)
from free_claude_code.config.websearch_catalog import (
    SUPPORTED_WEBSEARCH_PROVIDER_IDS,
    WEBSEARCH_CATALOG,
)
from free_claude_code.websearch.rotation import ROTATION_POLICIES


def _option_values(field) -> tuple[str, ...]:
    return tuple(
        option.value if isinstance(option, ConfigOptionSpec) else option
        for option in field.options
    )


def test_websearch_section_follows_web_tools_section() -> None:
    section_ids = [section.section_id for section in SECTIONS]
    assert "websearch" in section_ids
    assert section_ids.index("websearch") == section_ids.index("web_tools") + 1
    section = next(s for s in SECTIONS if s.section_id == "websearch")
    assert section.label == "Web Search"


def test_web_search_provider_select_lists_catalog_in_order() -> None:
    field = FIELD_BY_KEY["WEB_SEARCH_PROVIDER"]
    assert field.section_id == "websearch"
    assert field.field_type == "select"
    assert field.settings_attr == "web_search_provider"
    assert field.default == "auto"
    assert _option_values(field) == (
        "auto",
        "off",
        *SUPPORTED_WEBSEARCH_PROVIDER_IDS,
    )
    labels = {
        option.value: option.label
        for option in field.options
        if isinstance(option, ConfigOptionSpec)
    }
    for provider_id, descriptor in WEBSEARCH_CATALOG.items():
        assert labels[provider_id] == descriptor.display_name


def test_secret_fields_generated_for_every_keyed_catalog_provider() -> None:
    keyed = [
        descriptor
        for descriptor in WEBSEARCH_CATALOG.values()
        if descriptor.credential_env is not None
    ]
    assert len(keyed) == 12
    for descriptor in keyed:
        field = FIELD_BY_KEY.get(descriptor.credential_env)
        assert field is not None, f"{descriptor.credential_env} missing from manifest"
        assert field.section_id == "websearch"
        assert field.field_type == "secret"
        assert field.secret is True
        assert field.settings_attr == descriptor.settings_attr


def test_keyless_providers_have_no_credential_field() -> None:
    for descriptor in WEBSEARCH_CATALOG.values():
        if descriptor.credential_env is None:
            assert f"{descriptor.provider_id.upper()}_API_KEY" not in FIELD_BY_KEY


def test_rotation_select_generated_per_credential_env() -> None:
    for descriptor in WEBSEARCH_CATALOG.values():
        if descriptor.credential_env is None:
            continue
        field = FIELD_BY_KEY.get(f"{descriptor.credential_env}_ROTATION")
        assert field is not None, (
            f"{descriptor.credential_env}_ROTATION missing from manifest"
        )
        assert field.section_id == "websearch"
        assert field.field_type == "select"
        # Rotation is dotenv-only: it must not bind a Settings attribute.
        assert field.settings_attr is None
        assert _option_values(field) == ("", *ROTATION_POLICIES)


def test_rotation_options_mirror_websearch_rotation_policies() -> None:
    assert ROTATION_POLICY_OPTIONS == ROTATION_POLICIES


def test_searxng_base_url_field() -> None:
    field = FIELD_BY_KEY["SEARXNG_BASE_URL"]
    assert field.section_id == "websearch"
    assert field.settings_attr == "searxng_base_url"
    assert field.secret is False


def test_websearch_field_specs_cover_section_fields() -> None:
    keys = {spec["key"] for spec in websearch_field_specs()}
    expected = {
        "WEB_SEARCH_PROVIDER",
        "SEARXNG_BASE_URL",
        *(
            descriptor.credential_env
            for descriptor in WEBSEARCH_CATALOG.values()
            if descriptor.credential_env is not None
        ),
        *(
            f"{descriptor.credential_env}_ROTATION"
            for descriptor in WEBSEARCH_CATALOG.values()
            if descriptor.credential_env is not None
        ),
    }
    assert keys == expected
    assert all(spec["section_id"] == "websearch" for spec in websearch_field_specs())
