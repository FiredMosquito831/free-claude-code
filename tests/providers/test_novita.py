"""Tests for Novita AI (OpenAI-compatible) provider."""

from unittest.mock import patch

import pytest

from free_claude_code.config.provider_catalog import NOVITA_DEFAULT_BASE
from free_claude_code.providers.base import ProviderConfig
from tests.providers.request_factory import make_messages_request
from tests.providers.support import passthrough_rate_limiter, profiled_provider


def make_request(**overrides):
    return make_messages_request("deepseek/deepseek-v3.2", **overrides)


@pytest.fixture
def novita_config():
    return ProviderConfig(
        api_key="test_novita_key",
        base_url=NOVITA_DEFAULT_BASE,
        rate_limit=10,
        rate_window=60,
    )


@pytest.fixture
def novita_provider(novita_config):
    return profiled_provider(
        "novita", novita_config, rate_limiter=passthrough_rate_limiter()
    )


def test_init(novita_config):
    """Test provider initialization."""
    with patch(
        "free_claude_code.providers.openai_chat.provider.AsyncOpenAI"
    ) as mock_openai:
        provider = profiled_provider(
            "novita", novita_config, rate_limiter=passthrough_rate_limiter()
        )
        assert provider._api_key == "test_novita_key"
        assert provider._base_url == NOVITA_DEFAULT_BASE
        mock_openai.assert_called_once()


def test_default_base_url_constant():
    assert NOVITA_DEFAULT_BASE == "https://api.novita.ai/openai"


def test_novita_catalog_descriptor():
    from free_claude_code.config.provider_catalog import PROVIDER_CATALOG

    descriptor = PROVIDER_CATALOG["novita"]

    assert descriptor.credential_env == "NOVITA_API_KEY"
    assert descriptor.credential_attr == "novita_api_key"
    assert descriptor.proxy_attr == "novita_proxy"
    assert descriptor.dynamic is False
    assert descriptor.local is False


def test_build_request_body_basic(novita_provider):
    """Basic request body conversion attaches system message from Claude request."""
    req = make_request()
    body = novita_provider._build_request_body(req)

    assert body["model"] == "deepseek/deepseek-v3.2"
    assert body["messages"][0]["role"] == "system"
    assert "max_tokens" in body


def test_reasoning_replay_uses_think_tags(novita_provider):
    from free_claude_code.core.anthropic import ReasoningReplayMode

    assert (
        novita_provider._profile.request_policy.reasoning_replay
        is ReasoningReplayMode.THINK_TAGS
    )
