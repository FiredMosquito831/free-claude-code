"""Tests for the Chutes OpenAI-chat provider profile."""

import pytest

from my_claude_code.config.provider_catalog import CHUTES_DEFAULT_BASE
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from tests.providers.support import (
    passthrough_rate_limiter,
    profiled_provider,
    reasoning_for,
)


@pytest.fixture
def chutes_provider():
    return profiled_provider(
        "chutes",
        ProviderConfig(
            api_key="test-chutes-key",
            base_url=CHUTES_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def test_init_uses_documented_endpoint(chutes_provider):
    assert isinstance(chutes_provider, OpenAIChatProvider)
    assert chutes_provider._api_key == "test-chutes-key"
    assert chutes_provider._base_url == CHUTES_DEFAULT_BASE
    assert chutes_provider._provider_name == "CHUTES"


def test_build_request_body_openai_chat(chutes_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "Qwen/Qwen3-32B-TEE",
            "max_tokens": 100,
            "messages": [Message(role="user", content="Hello")],
            "thinking": {"type": "enabled"},
        }
    )

    body = chutes_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["model"] == "Qwen/Qwen3-32B-TEE"
    assert body["max_tokens"] == 100
    assert body["messages"] == [{"role": "user", "content": "Hello"}]


def test_default_base_url_constant():
    assert CHUTES_DEFAULT_BASE == "https://llm.chutes.ai/v1"
