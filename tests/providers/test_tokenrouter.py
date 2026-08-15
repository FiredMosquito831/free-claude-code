"""Tests for the TokenRouter OpenAI-chat gateway provider profile."""

import pytest

from my_claude_code.config.provider_catalog import TOKENROUTER_DEFAULT_BASE
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from tests.providers.support import (
    passthrough_rate_limiter,
    profiled_provider,
    reasoning_for,
)


@pytest.fixture
def tokenrouter_provider():
    return profiled_provider(
        "tokenrouter",
        ProviderConfig(
            api_key="test-tokenrouter-key",
            base_url=TOKENROUTER_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def test_init_uses_documented_endpoint(tokenrouter_provider):
    assert isinstance(tokenrouter_provider, OpenAIChatProvider)
    assert tokenrouter_provider._api_key == "test-tokenrouter-key"
    assert tokenrouter_provider._base_url == TOKENROUTER_DEFAULT_BASE
    assert tokenrouter_provider._provider_name == "TOKENROUTER"


def test_build_request_body_openai_chat(tokenrouter_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "test-model",
            "max_tokens": 100,
            "messages": [Message(role="user", content="Hello")],
            "thinking": {"type": "enabled"},
        }
    )

    body = tokenrouter_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["model"] == "test-model"
    assert body["max_tokens"] == 100
    assert body["messages"] == [{"role": "user", "content": "Hello"}]


def test_default_base_url_constant():
    assert TOKENROUTER_DEFAULT_BASE == "https://api.tokenrouter.com/v1"
