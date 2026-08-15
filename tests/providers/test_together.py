"""Tests for the Together AI OpenAI-chat provider profile."""

import pytest

from my_claude_code.config.provider_catalog import TOGETHER_DEFAULT_BASE
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from tests.providers.support import (
    passthrough_rate_limiter,
    profiled_provider,
    reasoning_for,
)


@pytest.fixture
def together_provider():
    return profiled_provider(
        "together",
        ProviderConfig(
            api_key="test-together-key",
            base_url=TOGETHER_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def test_init_uses_documented_endpoint(together_provider):
    assert isinstance(together_provider, OpenAIChatProvider)
    assert together_provider._api_key == "test-together-key"
    assert together_provider._base_url == TOGETHER_DEFAULT_BASE
    assert together_provider._provider_name == "TOGETHER"


def test_build_request_body_openai_chat(together_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "zai-org/GLM-5.2",
            "max_tokens": 100,
            "messages": [Message(role="user", content="Hello")],
            "thinking": {"type": "enabled"},
        }
    )

    body = together_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["model"] == "zai-org/GLM-5.2"
    assert body["max_tokens"] == 100
    assert body["messages"] == [{"role": "user", "content": "Hello"}]


def test_default_base_url_constant():
    assert TOGETHER_DEFAULT_BASE == "https://api.together.ai/v1"
