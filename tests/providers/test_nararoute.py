"""Tests for the NaraRoute OpenAI-chat gateway provider profile."""

import pytest

from my_claude_code.config.provider_catalog import NARAROUTE_DEFAULT_BASE
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from tests.providers.support import (
    passthrough_rate_limiter,
    profiled_provider,
    reasoning_for,
)


@pytest.fixture
def nararoute_provider():
    return profiled_provider(
        "nararoute",
        ProviderConfig(
            api_key="test-nararoute-key",
            base_url=NARAROUTE_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def test_init_uses_documented_endpoint(nararoute_provider):
    assert isinstance(nararoute_provider, OpenAIChatProvider)
    assert nararoute_provider._api_key == "test-nararoute-key"
    assert nararoute_provider._base_url == NARAROUTE_DEFAULT_BASE
    assert nararoute_provider._provider_name == "NARAROUTE"


def test_build_request_body_openai_chat(nararoute_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "test-model",
            "max_tokens": 100,
            "messages": [Message(role="user", content="Hello")],
            "thinking": {"type": "enabled"},
        }
    )

    body = nararoute_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["model"] == "test-model"
    assert body["max_tokens"] == 100
    assert body["messages"] == [{"role": "user", "content": "Hello"}]


def test_default_base_url_constant():
    assert NARAROUTE_DEFAULT_BASE == "https://router.bynara.id/v1"
