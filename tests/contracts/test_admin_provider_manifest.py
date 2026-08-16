"""Ensure admin UI manifest exposes every catalog credential/proxy binding."""

from my_claude_code.config.admin.manifest import FIELD_BY_KEY
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.settings import Settings


def test_provider_catalog_remote_credentials_in_admin_manifest() -> None:
    missing: list[str] = []
    wrong_attr: list[str] = []

    for provider_id, desc in PROVIDER_CATALOG.items():
        if desc.credential_env is None:
            continue
        if desc.credential_attr is None:
            missing.append(
                f"{provider_id}: credential_env set but credential_attr missing"
            )
            continue
        entry = FIELD_BY_KEY.get(desc.credential_env)
        if entry is None:
            missing.append(
                f"{provider_id}: {desc.credential_env} not in admin FIELD_BY_KEY"
            )
            continue
        if entry.settings_attr != desc.credential_attr:
            wrong_attr.append(
                f"{provider_id}: {desc.credential_env} maps settings_attr="
                f"{entry.settings_attr!r}, catalog expects "
                f"{desc.credential_attr!r}"
            )

    assert not missing and not wrong_attr, "\n".join(missing + wrong_attr)


def test_provider_catalog_local_base_urls_in_admin_manifest() -> None:
    missing_key: list[str] = []
    wrong_attr: list[str] = []

    for provider_id, desc in PROVIDER_CATALOG.items():
        if desc.base_url_attr is None:
            continue
        mf = Settings.model_fields[desc.base_url_attr]
        alias = mf.validation_alias
        if alias is None:
            missing_key.append(
                f"{provider_id}: {desc.base_url_attr} has no validation_alias "
                "(admin manifest expects env-backed base URL)"
            )
            continue
        env_key = str(alias)
        entry = FIELD_BY_KEY.get(env_key)
        if entry is None:
            missing_key.append(
                f"{provider_id}: base URL env {env_key} not in FIELD_BY_KEY"
            )
            continue
        if entry.settings_attr != desc.base_url_attr:
            wrong_attr.append(
                f"{provider_id}: {env_key} maps settings_attr="
                f"{entry.settings_attr!r}, catalog expects {desc.base_url_attr!r}"
            )

    assert not missing_key and not wrong_attr, "\n".join(missing_key + wrong_attr)


def test_provider_catalog_proxy_attrs_in_admin_manifest() -> None:
    missing_key: list[str] = []
    wrong_attr: list[str] = []

    for provider_id, desc in PROVIDER_CATALOG.items():
        if desc.proxy_attr is None:
            continue
        mf = Settings.model_fields[desc.proxy_attr]
        alias = mf.validation_alias
        if alias is None:
            missing_key.append(
                f"{provider_id}: {desc.proxy_attr} has no validation_alias "
                "(admin manifest expects env-backed proxy)"
            )
            continue
        env_key = str(alias)
        entry = FIELD_BY_KEY.get(env_key)
        if entry is None:
            missing_key.append(
                f"{provider_id}: proxy env {env_key} not in FIELD_BY_KEY"
            )
            continue
        if entry.settings_attr != desc.proxy_attr:
            wrong_attr.append(
                f"{provider_id}: {env_key} maps settings_attr="
                f"{entry.settings_attr!r}, catalog expects {desc.proxy_attr!r}"
            )

    assert not missing_key and not wrong_attr, "\n".join(missing_key + wrong_attr)


def test_provider_catalog_display_names_are_admin_status_source() -> None:
    from my_claude_code.config.admin.status import provider_config_status
    from my_claude_code.config.admin.values import load_value_state

    status_by_provider = {
        entry["provider_id"]: entry
        for entry in provider_config_status(load_value_state())
    }

    assert set(status_by_provider) == set(PROVIDER_CATALOG)
    for provider_id, desc in PROVIDER_CATALOG.items():
        assert status_by_provider[provider_id]["display_name"] == desc.display_name
        expected_kind = "local" if desc.local else "remote"
        assert status_by_provider[provider_id]["kind"] == expected_kind


def test_cloudflare_account_id_is_admin_provider_field() -> None:
    entry = FIELD_BY_KEY["CLOUDFLARE_ACCOUNT_ID"]

    assert entry.settings_attr == "cloudflare_account_id"
    assert entry.section_id == "providers"
    assert entry.secret is False


def test_commandcode_card_owns_key_rotation_and_proxy_fields() -> None:
    from my_claude_code.config.admin.status import provider_config_status

    by_id = {entry["provider_id"]: entry for entry in provider_config_status({})}
    card = by_id["commandcode"]

    assert FIELD_BY_KEY["COMMANDCODE_API_KEY"].settings_attr == "commandcode_api_key"
    assert "COMMANDCODE_API_KEY_ROTATION" in FIELD_BY_KEY
    assert FIELD_BY_KEY["COMMANDCODE_PROXY"].settings_attr == "commandcode_proxy"
    assert card["credential_owner_id"] == "commandcode"
    assert card["credential_env"] == "COMMANDCODE_API_KEY"
    assert card["key_count"] == 0


def test_provider_status_reports_the_size_of_each_key_pool(monkeypatch) -> None:
    """The card face shows "3 keys - Round robin" without one request per provider.

    Secret values are masked to a constant before they reach the client, so the
    Admin UI cannot count a comma-separated pool itself, and fetching the count
    per provider would mean 35 requests on every page load.
    """
    from my_claude_code.config.admin.status import provider_config_status

    state = {
        "NVIDIA_NIM_API_KEY": {"value": "key-one, key-two ,key-three"},
        "GROQ_API_KEY": {"value": "solo-key"},
        "CEREBRAS_API_KEY": {"value": ""},
    }

    by_id = {entry["provider_id"]: entry for entry in provider_config_status(state)}

    assert by_id["nvidia_nim"]["key_count"] == 3
    assert by_id["groq"]["key_count"] == 1
    assert by_id["cerebras"]["key_count"] == 0
    # A blank pool must not read as configured.
    assert by_id["cerebras"]["status"] == "missing_key"
    assert by_id["nvidia_nim"]["status"] == "configured"


def test_every_remote_provider_can_be_given_a_key_from_its_own_card() -> None:
    """A card with no credential field and no owner offers nothing to configure.

    ``opencode_go`` shipped exactly that: it shares ``OPENCODE_API_KEY`` with
    ``opencode``, the manifest emits one field per credential rather than per
    provider, so its card rendered a single advanced proxy input and no way to
    add a key at all.
    """
    from my_claude_code.config.admin.status import provider_config_status

    stranded: list[str] = []
    for entry in provider_config_status({}):
        if entry["kind"] != "remote":
            continue
        owner = entry["credential_owner_id"]
        if owner is None:
            descriptor = PROVIDER_CATALOG[entry["provider_id"]]
            if descriptor.required_settings_attrs:
                # ADC-based providers (e.g. Vertex AI) have no secret key; they
                # are configured through their own settings fields, which must
                # all be editable in the Admin UI.
                missing = [
                    attr
                    for attr in descriptor.required_settings_attrs
                    if attr not in Settings.model_fields
                ]
                if missing:
                    stranded.append(
                        f"{entry['provider_id']}: required settings {missing} "
                        "are not editable"
                    )
                continue
            stranded.append(f"{entry['provider_id']}: no provider owns its credential")
            continue
        if owner == entry["provider_id"]:
            if entry["credential_env"] not in FIELD_BY_KEY:
                stranded.append(
                    f"{entry['provider_id']}: owns {entry['credential_env']} "
                    "but it is not an editable field"
                )
            continue
        # A borrower must name a real owner, or the UI cannot point anywhere.
        if owner not in PROVIDER_CATALOG:
            stranded.append(
                f"{entry['provider_id']}: owner {owner!r} is not a provider"
            )
        if not entry["credential_owner_name"]:
            stranded.append(f"{entry['provider_id']}: owner has no display name")

    assert not stranded, "\n".join(stranded)


def test_shared_credentials_name_each_other_in_both_directions() -> None:
    """OpenCode Zen and OpenCode Go are one account behind two endpoints."""
    from my_claude_code.config.admin.status import provider_config_status

    by_id = {entry["provider_id"]: entry for entry in provider_config_status({})}

    assert by_id["opencode"]["credential_owner_id"] == "opencode"
    assert by_id["opencode_go"]["credential_owner_id"] == "opencode"
    assert by_id["opencode_go"]["credential_owner_name"] == "OpenCode Zen"
    # The owner says who else draws on the key; the borrower does not list itself.
    assert [
        other["provider_id"] for other in by_id["opencode"]["credential_shared_with"]
    ] == ["opencode_go"]
    assert [
        other["provider_id"] for other in by_id["opencode_go"]["credential_shared_with"]
    ] == ["opencode"]
    # An unshared credential must not claim a sharer.
    assert by_id["groq"]["credential_shared_with"] == []
    assert by_id["groq"]["credential_owner_id"] == "groq"


def test_every_remote_provider_can_be_routed_through_a_proxy() -> None:
    """DeepSeek was the only remote provider with no proxy field.

    Every other remote provider accepts one, so the omission read as "DeepSeek
    cannot be proxied" rather than as the oversight it was.
    """
    without_proxy = [
        provider_id
        for provider_id, desc in PROVIDER_CATALOG.items()
        if not desc.local and desc.proxy_attr is None
    ]

    assert without_proxy == [], (
        f"these remote providers have no way to be proxied: {without_proxy}"
    )


def test_azure_openai_requires_a_base_url_naming_the_users_resource() -> None:
    """Azure's endpoint contains the customer's resource name.

    Shipping any default would send requests somewhere that is wrong for
    everyone, so the field is deliberately empty and the missing-URL error
    names the variable to set.
    """
    desc = PROVIDER_CATALOG["azure_openai"]

    assert desc.default_base_url is None
    assert desc.base_url_attr == "azure_openai_base_url"
    assert FIELD_BY_KEY["AZURE_OPENAI_BASE_URL"].default == ""
    assert "deployment name" in FIELD_BY_KEY["AZURE_OPENAI_API_KEY"].description


def test_every_remote_provider_reports_a_key_count() -> None:
    """A provider missing this renders a card face with no summary line."""
    from my_claude_code.config.admin.status import provider_config_status

    remote = [
        entry for entry in provider_config_status({}) if entry["kind"] == "remote"
    ]

    assert remote
    assert all("key_count" in entry for entry in remote)
    assert all(entry["key_count"] == 0 for entry in remote)
