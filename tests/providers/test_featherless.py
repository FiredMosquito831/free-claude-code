"""Tests for the Featherless AI OpenAI-chat provider profile."""

import pytest

from my_claude_code.config.provider_catalog import FEATHERLESS_DEFAULT_BASE
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from tests.providers.support import (
    passthrough_rate_limiter,
    profiled_provider,
    reasoning_for,
)


@pytest.fixture
def featherless_provider():
    return profiled_provider(
        "featherless",
        ProviderConfig(
            api_key="test-featherless-key",
            base_url=FEATHERLESS_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def test_init_uses_documented_endpoint(featherless_provider):
    assert isinstance(featherless_provider, OpenAIChatProvider)
    assert featherless_provider._api_key == "test-featherless-key"
    assert featherless_provider._base_url == FEATHERLESS_DEFAULT_BASE
    assert featherless_provider._provider_name == "FEATHERLESS"


def test_build_request_body_openai_chat(featherless_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "Qwen/Qwen3-32B",
            "max_tokens": 100,
            "messages": [Message(role="user", content="Hello")],
            "thinking": {"type": "enabled"},
        }
    )

    body = featherless_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["model"] == "Qwen/Qwen3-32B"
    assert body["max_tokens"] == 100
    assert body["messages"] == [{"role": "user", "content": "Hello"}]


def test_default_base_url_constant():
    assert FEATHERLESS_DEFAULT_BASE == "https://api.featherless.ai/v1"
