"""Dotenv-only advanced option reader for web search providers.

Mirrors how ``{CREDENTIAL_ENV}_ROTATION`` is read in ``registry``: the process
environment wins, otherwise the last configured dotenv value applies. Values
are raw strings consumed verbatim by adapters; empty/unset values always
reproduce the provider's default behavior.
"""

import os

from my_claude_code.config.env_files import env_file_override
from my_claude_code.config.settings import Settings
from my_claude_code.config.websearch_catalog import WebSearchDescriptor


def read_websearch_options(
    provider_id: str, descriptor: WebSearchDescriptor
) -> dict[str, str]:
    """Return explicitly-set advanced options for one provider.

    Only env vars declared in the descriptor's ``advanced_options`` catalog
    entry are read (catalog-driven); blank values are treated as unset.
    """

    options: dict[str, str] = {}
    for spec in descriptor.advanced_options:
        value = _env_or_dotenv(spec.env)
        if value is None:
            continue
        value = value.strip()
        if value:
            options[spec.env] = value
    return options


def _env_or_dotenv(key: str) -> str | None:
    """Process env wins; otherwise the last configured dotenv value."""

    if key in os.environ:
        return os.environ[key]
    return env_file_override(Settings.model_config, key)


_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def option_enabled(raw: str | None, *, default: bool = False) -> bool:
    """Parse a boolean option value; empty/unknown falls back to ``default``."""

    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return default


def option_int(raw: str | None) -> int | None:
    """Parse a number option value; empty/invalid yields None (option omitted)."""

    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None
