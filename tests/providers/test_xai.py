"""Tests for the xAI OpenAI-chat provider profile."""

import pytest

from my_claude_code.config.provider_catalog import XAI_DEFAULT_BASE
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from tests.providers.support import (
    passthrough_rate_limiter,
    profiled_provider,
    reasoning_for,
)


@pytest.fixture
def xai_provider():
    return profiled_provider(
        "xai",
        ProviderConfig(
            api_key="test-xai-key",
            base_url=XAI_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def test_init_uses_documented_endpoint(xai_provider):
    assert isinstance(xai_provider, OpenAIChatProvider)
    assert xai_provider._api_key == "test-xai-key"
    assert xai_provider._base_url == XAI_DEFAULT_BASE
    assert xai_provider._provider_name == "XAI"


def test_build_request_body_openai_chat(xai_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "grok-4.5",
            "max_tokens": 100,
            "messages": [Message(role="user", content="Hello")],
            "thinking": {"type": "enabled"},
        }
    )

    body = xai_provider._build_request_body(request, reasoning=reasoning_for(request))

    assert body["model"] == "grok-4.5"
    assert body["max_tokens"] == 100
    assert body["messages"] == [{"role": "user", "content": "Hello"}]


def test_default_base_url_constant():
    assert XAI_DEFAULT_BASE == "https://api.x.ai/v1"
