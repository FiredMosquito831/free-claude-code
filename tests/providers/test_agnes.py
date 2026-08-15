"""Tests for the Agnes AI OpenAI-chat provider profile."""

from typing import Any

import pytest

from my_claude_code.config.constants import ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS
from my_claude_code.config.provider_catalog import AGNES_DEFAULT_BASE
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from tests.providers.support import (
    REASONING_OFF,
    REASONING_ON,
    passthrough_rate_limiter,
    profiled_provider,
    reasoning_for,
)


@pytest.fixture
def agnes_provider():
    return profiled_provider(
        "agnes",
        ProviderConfig(
            api_key="test-agnes-key",
            base_url=AGNES_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def _request(**overrides: Any) -> MessagesRequest:
    payload: dict[str, Any] = {
        "model": "agnes-2.0-flash",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    payload.update(overrides)
    return MessagesRequest.model_validate(payload)


def test_init_uses_documented_endpoint(agnes_provider):
    assert agnes_provider._api_key == "test-agnes-key"
    assert agnes_provider._base_url == AGNES_DEFAULT_BASE
    assert agnes_provider._provider_name == "AGNES"


@pytest.mark.parametrize(
    ("reasoning", "enabled"),
    [(REASONING_OFF, False), (REASONING_ON, True)],
)
def test_build_request_body_encodes_documented_thinking_control(
    agnes_provider,
    reasoning: ReasoningPolicy,
    enabled: bool,
):
    body = agnes_provider._build_request_body(_request(), reasoning=reasoning)

    assert body["extra_body"] == {"chat_template_kwargs": {"enable_thinking": enabled}}


def test_build_request_body_omits_thinking_control_for_provider_default(agnes_provider):
    body = agnes_provider._build_request_body(
        _request(),
        reasoning=ReasoningPolicy.provider_default(),
    )

    assert "extra_body" not in body


def test_build_request_body_applies_default_max_tokens(agnes_provider):
    body = agnes_provider._build_request_body(
        _request(),
        reasoning=reasoning_for(_request()),
    )

    assert body["max_tokens"] == ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS


def test_build_request_body_replays_reasoning_in_content(agnes_provider):
    request = _request(
        messages=[
            {"role": "user", "content": "Solve it."},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Work through it."},
                    {"type": "text", "text": "The answer is 42."},
                ],
            },
            {"role": "user", "content": "Continue."},
        ]
    )

    body = agnes_provider._build_request_body(
        request,
        reasoning=reasoning_for(request),
    )

    assert body["messages"][1] == {
        "role": "assistant",
        "content": "<think>\nWork through it.\n</think>\n\nThe answer is 42.",
    }


def test_default_base_url_constant():
    assert AGNES_DEFAULT_BASE == "https://apihub.agnes-ai.com/v1"
