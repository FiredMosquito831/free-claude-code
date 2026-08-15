"""Tests for the W&B Inference OpenAI-chat provider profile."""

from types import SimpleNamespace
from typing import Any

import pytest

from my_claude_code.config.constants import ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS
from my_claude_code.config.provider_catalog import WANDB_INFERENCE_DEFAULT_BASE
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
def wandb_provider():
    return profiled_provider(
        "wandb",
        ProviderConfig(
            api_key="test-wandb-key",
            base_url=WANDB_INFERENCE_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def _request(**overrides: Any) -> MessagesRequest:
    payload: dict[str, Any] = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    payload.update(overrides)
    return MessagesRequest.model_validate(payload)


def test_init_uses_documented_endpoint(wandb_provider):
    assert wandb_provider._api_key == "test-wandb-key"
    assert wandb_provider._base_url == WANDB_INFERENCE_DEFAULT_BASE
    assert wandb_provider._provider_name == "WANDB"


@pytest.mark.parametrize(
    ("reasoning", "enabled"),
    [(REASONING_OFF, False), (REASONING_ON, True)],
)
def test_build_request_body_encodes_documented_thinking_control(
    wandb_provider,
    reasoning: ReasoningPolicy,
    enabled: bool,
):
    body = wandb_provider._build_request_body(_request(), reasoning=reasoning)

    assert body["extra_body"] == {"chat_template_kwargs": {"enable_thinking": enabled}}


def test_build_request_body_omits_thinking_control_for_provider_default(wandb_provider):
    body = wandb_provider._build_request_body(
        _request(),
        reasoning=ReasoningPolicy.provider_default(),
    )

    assert "extra_body" not in body


def test_build_request_body_uses_max_completion_tokens(wandb_provider):
    body = wandb_provider._build_request_body(
        _request(),
        reasoning=reasoning_for(_request()),
    )

    assert body["max_completion_tokens"] == ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS
    assert "max_tokens" not in body


def test_build_request_body_replays_no_prior_reasoning(wandb_provider):
    request = _request(
        messages=[
            {"role": "user", "content": "Inspect the file."},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Read it first."},
                    {"type": "text", "text": "I will inspect it."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {"path": "example.py"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "print('hello')",
                    }
                ],
            },
        ]
    )

    body = wandb_provider._build_request_body(
        request,
        reasoning=reasoning_for(request),
    )

    assert body["messages"][1] == {
        "role": "assistant",
        "content": "I will inspect it.",
        "tool_calls": [
            {
                "id": "toolu_1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": '{"path": "example.py"}',
                },
            }
        ],
    }
    assert body["messages"][2] == {
        "role": "tool",
        "tool_call_id": "toolu_1",
        "content": "print('hello')",
    }
    assert (
        wandb_provider._profile.reasoning_delta(
            SimpleNamespace(reasoning="new thought")
        )
        == "new thought"
    )


def test_default_base_url_constant():
    assert WANDB_INFERENCE_DEFAULT_BASE == "https://api.inference.wandb.ai/v1"
