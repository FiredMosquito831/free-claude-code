from dataclasses import FrozenInstanceError

import pytest

from free_claude_code.config.provider_catalog import (
    CUSTOM_PROVIDER_GROUP,
    PROVIDER_CATALOG,
    PROVIDER_GROUP_IDS,
    PROVIDER_GROUPS,
    ProviderDescriptor,
)


def test_provider_descriptors_are_immutable_values() -> None:
    descriptor = ProviderDescriptor(
        provider_id="local",
        display_name="Local",
        local=True,
    )

    assert descriptor.local is True
    assert not hasattr(descriptor, "__dict__")
    with pytest.raises(FrozenInstanceError):
        descriptor.__setattr__("local", False)


def test_catalog_has_no_transport_metadata() -> None:
    assert "transport_type" not in ProviderDescriptor.__slots__
    assert "capabilities" not in ProviderDescriptor.__slots__


def test_catalog_local_assignments_are_exact() -> None:
    assert {
        provider_id
        for provider_id, descriptor in PROVIDER_CATALOG.items()
        if descriptor.local
    } == {"lmstudio", "llamacpp", "ollama"}


def test_ollama_cloud_is_remote_and_distinct_from_local_ollama() -> None:
    cloud = PROVIDER_CATALOG["ollama_cloud"]
    local = PROVIDER_CATALOG["ollama"]

    assert cloud.local is False
    assert cloud.credential_env == "OLLAMA_API_KEY"
    assert local.local is True
    assert local.credential_env is None


def test_every_provider_declares_a_group() -> None:
    """A new provider must choose a group; the default is empty so it cannot drift.

    ``ProviderDescriptor.group`` deliberately defaults to ``""`` rather than to a
    plausible-looking group, so forgetting it fails here instead of filing the
    provider under something wrong in the Admin UI.
    """
    missing = [
        provider_id
        for provider_id, descriptor in PROVIDER_CATALOG.items()
        if not descriptor.group
    ]

    assert missing == []


def test_provider_groups_are_known_and_non_custom() -> None:
    unknown = {
        descriptor.group
        for descriptor in PROVIDER_CATALOG.values()
        if descriptor.group not in PROVIDER_GROUP_IDS
    }

    assert unknown == set()
    # ``custom`` belongs to registry-defined providers, never to a built-in.
    assert CUSTOM_PROVIDER_GROUP not in {
        descriptor.group for descriptor in PROVIDER_CATALOG.values()
    }


def test_every_declared_group_has_at_least_one_provider() -> None:
    """An empty group renders as a heading with nothing under it."""
    used = {descriptor.group for descriptor in PROVIDER_CATALOG.values()}
    unused = [
        group.group_id
        for group in PROVIDER_GROUPS
        if group.group_id != CUSTOM_PROVIDER_GROUP and group.group_id not in used
    ]

    assert unused == []


def test_local_providers_are_grouped_as_local() -> None:
    assert {
        provider_id
        for provider_id, descriptor in PROVIDER_CATALOG.items()
        if descriptor.group == "local"
    } == {"lmstudio", "llamacpp", "ollama"}


def test_provider_group_ids_are_unique_and_ordered() -> None:
    ids = [group.group_id for group in PROVIDER_GROUPS]

    assert len(set(ids)) == len(ids)
    assert tuple(ids) == PROVIDER_GROUP_IDS
    # ``custom`` is last so user-defined providers sort after the built-ins.
    assert ids[-1] == CUSTOM_PROVIDER_GROUP
