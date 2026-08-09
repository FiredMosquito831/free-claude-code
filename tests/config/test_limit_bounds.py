"""A limit always resolves to something usable, however it was written."""

from typing import Any

import pytest

from free_claude_code.config.admin.manifest import FIELDS
from free_claude_code.config.admin.validation import range_errors
from free_claude_code.config.limits import LIMIT_RANGES, ZSTD_MAX_LEVEL, range_for
from free_claude_code.config.settings import Settings

LIMIT_ATTRS = tuple(LIMIT_RANGES)


def _alias(attr: str) -> str:
    """Settings fields are populated by env alias, not by field name."""
    field = Settings.model_fields[attr]
    return str(field.validation_alias) if field.validation_alias else attr.upper()


def _settings(**env: str) -> Settings:
    # Same shape the admin validator uses: env values arrive as strings and the
    # model coerces them, which a precisely-typed kwargs dict cannot express.
    kwargs: dict[str, Any] = {"_env_file": None, **env}
    return Settings(**kwargs)


def _with(attr: str, value: str) -> Settings:
    return _settings(**{_alias(attr): value})


@pytest.mark.parametrize("attr", LIMIT_ATTRS)
def test_a_blank_value_falls_back_to_the_default(attr: str) -> None:
    """The admin UI writes `KEY=` for a cleared field, so blank is not exotic."""
    default = Settings.model_fields[attr].default
    assert getattr(_with(attr, ""), attr) == default


@pytest.mark.parametrize("attr", LIMIT_ATTRS)
def test_an_absent_value_falls_back_to_the_default(attr: str) -> None:
    default = Settings.model_fields[attr].default
    assert getattr(_settings(), attr) == default


@pytest.mark.parametrize("attr", LIMIT_ATTRS)
def test_a_value_below_the_range_is_clamped_not_fatal(attr: str) -> None:
    """A proxy that will not start is worse than one running a sane number."""
    limit = LIMIT_RANGES[attr]
    resolved = getattr(_with(attr, str(limit.minimum - 1000)), attr)
    assert resolved == type(resolved)(limit.minimum)


@pytest.mark.parametrize("attr", LIMIT_ATTRS)
def test_a_value_above_the_range_is_clamped_not_fatal(attr: str) -> None:
    limit = LIMIT_RANGES[attr]
    resolved = getattr(_with(attr, str(limit.maximum + 1000)), attr)
    assert resolved == type(resolved)(limit.maximum)


@pytest.mark.parametrize("attr", LIMIT_ATTRS)
def test_the_default_sits_inside_its_own_range(attr: str) -> None:
    """A default outside its range would be clamped on every single boot."""
    assert LIMIT_RANGES[attr].contains(Settings.model_fields[attr].default)


def test_the_compression_level_cannot_exceed_what_zstd_accepts() -> None:
    """Level 42 validated fine and then failed on every body write."""
    from compression import zstd

    limit = LIMIT_RANGES["request_log_compression_level"]
    assert limit.maximum == ZSTD_MAX_LEVEL
    zstd.compress(b"probe", level=int(limit.maximum))
    with pytest.raises(ValueError):
        zstd.compress(b"probe", level=int(limit.maximum) + 1)


def test_a_provider_is_always_allowed_one_attempt() -> None:
    """0 retries would mean never calling the provider at all."""
    assert LIMIT_RANGES["provider_retry_attempts"].minimum >= 1
    assert _settings(PROVIDER_RETRY_ATTEMPTS="0").provider_retry_attempts == 1


@pytest.mark.parametrize("attr", LIMIT_ATTRS)
def test_the_form_publishes_the_same_range_the_server_clamps_to(attr: str) -> None:
    """One table, so the form cannot accept what the server would change."""
    field = next(f for f in FIELDS if f.settings_attr == attr)
    limit = LIMIT_RANGES[attr]
    assert field.minimum == limit.minimum
    assert field.maximum == limit.maximum
    assert "Accepts" in field.description


def test_the_form_rejects_an_out_of_range_value_instead_of_clamping() -> None:
    assert range_errors({"REQUEST_LOG_COMPRESSION_LEVEL": "42"})
    assert range_errors({"PROVIDER_RETRY_ATTEMPTS": "0"})
    assert not range_errors({"REQUEST_LOG_COMPRESSION_LEVEL": "9"})
    assert not range_errors({"REQUEST_LOG_COMPRESSION_LEVEL": ""})


def test_a_field_without_a_range_is_left_alone() -> None:
    assert range_for("model") is None
    assert range_for(None) is None
