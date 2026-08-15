"""Tests for the QwenCloud Coding Plan OpenAI-chat provider profile."""

import pytest

from my_claude_code.config.provider_catalog import QWENCLOUD_CODING_DEFAULT_BASE
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from tests.providers.support import (
    passthrough_rate_limiter,
    profiled_provider,
    reasoning_for,
)


@pytest.fixture
def qwencloud_coding_provider():
    return profiled_provider(
        "qwencloud_coding",
        ProviderConfig(
            api_key="test-qwencloud-coding-key",
            base_url=QWENCLOUD_CODING_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def test_init_uses_documented_endpoint(qwencloud_coding_provider):
    assert isinstance(qwencloud_coding_provider, OpenAIChatProvider)
    assert qwencloud_coding_provider._api_key == "test-qwencloud-coding-key"
    assert qwencloud_coding_provider._base_url == QWENCLOUD_CODING_DEFAULT_BASE
    assert qwencloud_coding_provider._provider_name == "QWENCLOUD_CODING"


def test_build_request_body_openai_chat(qwencloud_coding_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "qwen3-coder",
            "max_tokens": 100,
            "messages": [Message(role="user", content="Hello")],
            "thinking": {"type": "enabled"},
        }
    )

    body = qwencloud_coding_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["model"] == "qwen3-coder"
    assert body["max_tokens"] == 100
    assert body["messages"] == [{"role": "user", "content": "Hello"}]


def test_default_base_url_constant():
    assert QWENCLOUD_CODING_DEFAULT_BASE == (
        "https://coding-intl.dashscope.aliyuncs.com/v1"
    )
