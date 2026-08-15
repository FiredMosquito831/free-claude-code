"""Tests for the DeepInfra OpenAI-chat provider profile."""

import pytest

from my_claude_code.config.provider_catalog import DEEPINFRA_DEFAULT_BASE
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from tests.providers.support import (
    passthrough_rate_limiter,
    profiled_provider,
    reasoning_for,
)


@pytest.fixture
def deepinfra_provider():
    return profiled_provider(
        "deepinfra",
        ProviderConfig(
            api_key="test-deepinfra-key",
            base_url=DEEPINFRA_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def test_init_uses_documented_endpoint(deepinfra_provider):
    assert isinstance(deepinfra_provider, OpenAIChatProvider)
    assert deepinfra_provider._api_key == "test-deepinfra-key"
    assert deepinfra_provider._base_url == DEEPINFRA_DEFAULT_BASE
    assert deepinfra_provider._provider_name == "DEEPINFRA"


def test_build_request_body_openai_chat(deepinfra_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "deepseek-ai/DeepSeek-V4-Flash",
            "max_tokens": 100,
            "messages": [Message(role="user", content="Hello")],
            "thinking": {"type": "enabled"},
        }
    )

    body = deepinfra_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["model"] == "deepseek-ai/DeepSeek-V4-Flash"
    assert body["max_tokens"] == 100
    assert body["messages"] == [{"role": "user", "content": "Hello"}]


def test_default_base_url_constant():
    assert DEEPINFRA_DEFAULT_BASE == "https://api.deepinfra.com/v1/openai"
