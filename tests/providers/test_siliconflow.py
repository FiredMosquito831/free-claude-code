"""Tests for the SiliconFlow OpenAI-chat provider profile."""

import pytest

from my_claude_code.config.provider_catalog import SILICONFLOW_DEFAULT_BASE
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from tests.providers.support import (
    passthrough_rate_limiter,
    profiled_provider,
    reasoning_for,
)


@pytest.fixture
def siliconflow_provider():
    return profiled_provider(
        "siliconflow",
        ProviderConfig(
            api_key="test-siliconflow-key",
            base_url=SILICONFLOW_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def test_init_uses_documented_endpoint(siliconflow_provider):
    assert isinstance(siliconflow_provider, OpenAIChatProvider)
    assert siliconflow_provider._api_key == "test-siliconflow-key"
    assert siliconflow_provider._base_url == SILICONFLOW_DEFAULT_BASE
    assert siliconflow_provider._provider_name == "SILICONFLOW"


def test_build_request_body_openai_chat(siliconflow_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "Qwen/Qwen3-32B",
            "max_tokens": 100,
            "messages": [Message(role="user", content="Hello")],
            "thinking": {"type": "enabled"},
        }
    )

    body = siliconflow_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["model"] == "Qwen/Qwen3-32B"
    assert body["max_tokens"] == 100
    assert body["messages"] == [{"role": "user", "content": "Hello"}]


def test_default_base_url_constant():
    assert SILICONFLOW_DEFAULT_BASE == "https://api.siliconflow.com/v1"
