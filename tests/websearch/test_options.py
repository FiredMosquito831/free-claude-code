"""Advanced options reader: dotenv/process precedence and registry plumbing."""

from my_claude_code.config.settings import Settings
from my_claude_code.config.websearch_catalog import (
    WEBSEARCH_CATALOG,
    WebSearchDescriptor,
    WebSearchOptionSpec,
)
from my_claude_code.websearch.base import WebSearchProviderConfig
from my_claude_code.websearch.options import (
    option_enabled,
    option_int,
    read_websearch_options,
)
from my_claude_code.websearch.registry import build_provider

_DESCRIPTOR = WebSearchDescriptor(
    provider_id="stub",
    display_name="Stub",
    credential_env="STUB_API_KEY",
    credential_url=None,
    settings_attr=None,
    default_base_url=None,
    base_url_attr=None,
    requires_key=False,
    supports_domains=False,
    free_tier="",
    notes="",
    advanced_options=(
        WebSearchOptionSpec(
            env="STUB_MODE", label="Mode", field_type="select", default=""
        ),
        WebSearchOptionSpec(
            env="STUB_LIMIT", label="Limit", field_type="number", default=""
        ),
    ),
)


class TestReadWebsearchOptions:
    def test_process_env_wins_over_dotenv(self, monkeypatch, tmp_path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("STUB_MODE=dotenv\nSTUB_LIMIT=5\n", encoding="utf-8")
        monkeypatch.setitem(Settings.model_config, "env_file", (env_file,))
        monkeypatch.setenv("STUB_MODE", "process")
        options = read_websearch_options("stub", _DESCRIPTOR)
        assert options == {"STUB_MODE": "process", "STUB_LIMIT": "5"}

    def test_dotenv_used_when_process_unset(self, monkeypatch, tmp_path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("STUB_MODE=dotenv\n", encoding="utf-8")
        monkeypatch.setitem(Settings.model_config, "env_file", (env_file,))
        monkeypatch.delenv("STUB_MODE", raising=False)
        monkeypatch.delenv("STUB_LIMIT", raising=False)
        options = read_websearch_options("stub", _DESCRIPTOR)
        assert options == {"STUB_MODE": "dotenv"}

    def test_last_dotenv_file_wins(self, monkeypatch, tmp_path) -> None:
        low = tmp_path / "low.env"
        high = tmp_path / "high.env"
        low.write_text("STUB_MODE=low\n", encoding="utf-8")
        high.write_text("STUB_MODE=high\n", encoding="utf-8")
        monkeypatch.setitem(Settings.model_config, "env_file", (low, high))
        monkeypatch.delenv("STUB_MODE", raising=False)
        options = read_websearch_options("stub", _DESCRIPTOR)
        assert options == {"STUB_MODE": "high"}

    def test_unset_and_blank_values_are_skipped(self, monkeypatch, tmp_path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("STUB_MODE=   \n", encoding="utf-8")
        monkeypatch.setitem(Settings.model_config, "env_file", (env_file,))
        monkeypatch.delenv("STUB_MODE", raising=False)
        monkeypatch.delenv("STUB_LIMIT", raising=False)
        assert read_websearch_options("stub", _DESCRIPTOR) == {}

    def test_only_catalog_declared_envs_are_read(self, monkeypatch) -> None:
        monkeypatch.setitem(Settings.model_config, "env_file", ())
        monkeypatch.setenv("STUB_MODE", "x")
        monkeypatch.setenv("STUB_UNDECLARED", "y")
        monkeypatch.delenv("STUB_LIMIT", raising=False)
        assert read_websearch_options("stub", _DESCRIPTOR) == {"STUB_MODE": "x"}

    def test_values_are_stripped(self, monkeypatch) -> None:
        monkeypatch.setitem(Settings.model_config, "env_file", ())
        monkeypatch.setenv("STUB_MODE", "  deep  ")
        monkeypatch.delenv("STUB_LIMIT", raising=False)
        assert read_websearch_options("stub", _DESCRIPTOR) == {"STUB_MODE": "deep"}


class TestOptionValueHelpers:
    def test_option_enabled_defaults_and_parsing(self) -> None:
        assert option_enabled(None) is False
        assert option_enabled(None, default=True) is True
        assert option_enabled("") is False
        assert option_enabled("", default=True) is True
        for raw in ("1", "true", "TRUE", "yes", "on"):
            assert option_enabled(raw) is True
        for raw in ("0", "false", "FALSE", "no", "off"):
            assert option_enabled(raw, default=True) is False
        assert option_enabled("bogus", default=True) is True
        assert option_enabled("bogus") is False

    def test_option_int_parsing(self) -> None:
        assert option_int(None) is None
        assert option_int("") is None
        assert option_int("0") == 0
        assert option_int(" 1024 ") == 1024
        assert option_int("abc") is None


class TestRegistryPlumbing:
    def test_config_options_default_to_empty_mapping(self) -> None:
        config = WebSearchProviderConfig(
            api_keys=("k",),
            credential_rotation="single",
            base_url=None,
            proxy=None,
            http_timeout=20.0,
        )
        assert config.options == {}

    def test_build_provider_fills_options_from_process_env(self, monkeypatch) -> None:
        monkeypatch.setitem(Settings.model_config, "env_file", ())
        monkeypatch.setenv("EXA_API_KEY", "k1-aaaa1111bbbb")
        monkeypatch.setenv("EXA_SEARCH_TYPE", "deep")
        monkeypatch.setenv("EXA_CONTENTS", "text")
        provider = build_provider(Settings(), "exa")
        assert provider.config.options == {
            "EXA_SEARCH_TYPE": "deep",
            "EXA_CONTENTS": "text",
        }

    def test_build_provider_without_options_has_empty_mapping(
        self, monkeypatch
    ) -> None:
        monkeypatch.setitem(Settings.model_config, "env_file", ())
        monkeypatch.setenv("EXA_API_KEY", "k1-aaaa1111bbbb")
        for spec in WEBSEARCH_CATALOG["exa"].advanced_options:
            monkeypatch.delenv(spec.env, raising=False)
        provider = build_provider(Settings(), "exa")
        assert provider.config.options == {}
