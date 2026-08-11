"""Tests for the neutral native Anthropic Messages upstream family."""

import json
from collections.abc import AsyncIterator

import pytest

from free_claude_code.application.errors import InvalidRequestError
from free_claude_code.core.anthropic.models import (
    ContentBlockThinking,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    MessagesRequest,
    ThinkingConfig,
    Tool,
)
from free_claude_code.core.reasoning import ReasoningPolicy
from free_claude_code.providers.anthropic_messages import (
    build_anthropic_messages_body,
    iter_anthropic_sse_frames,
)
from free_claude_code.providers.stream_recovery import TruncatedProviderStreamError


def test_native_request_preserves_tools_thinking_and_extensions() -> None:
    request = MessagesRequest(
        model="claude-sonnet-5",
        max_tokens=2048,
        system="Be exact.",
        messages=[
            Message(
                role="assistant",
                content=[
                    ContentBlockThinking.model_validate(
                        {
                            "type": "thinking",
                            "thinking": "check",
                            "signature": "signed",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ),
                    ContentBlockToolUse(
                        type="tool_use",
                        id="tool_1",
                        name="lookup",
                        input={"q": "x"},
                    ),
                ],
            ),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        type="tool_result",
                        tool_use_id="tool_1",
                        content="done",
                    )
                ],
            ),
        ],
        tools=[
            Tool.model_validate(
                {
                    "name": "lookup",
                    "description": "Look up a value",
                    "input_schema": {"type": "object"},
                    "cache_control": {"type": "ephemeral"},
                }
            )
        ],
        thinking=ThinkingConfig(type="enabled", budget_tokens=1024),
        betas=["fine-grained-tool-streaming-2025-05-14"],
        extra_body={"service_tier": "auto"},
    )

    body = build_anthropic_messages_body(
        request,
        reasoning=ReasoningPolicy.on(budget_tokens=1024),
    )

    assert body["model"] == "claude-sonnet-5"
    assert body["stream"] is True
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert body["messages"][0]["content"][0]["signature"] == "signed"
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["service_tier"] == "auto"
    assert "betas" not in body
    assert "extra_body" not in body


def test_reasoning_off_removes_thinking_control() -> None:
    request = MessagesRequest(
        model="claude-sonnet-5",
        max_tokens=16,
        messages=[Message(role="user", content="hello")],
        thinking=ThinkingConfig(type="enabled", budget_tokens=8),
    )

    body = build_anthropic_messages_body(request, reasoning=ReasoningPolicy.off())

    assert "thinking" not in body


def test_extra_body_cannot_override_canonical_fields() -> None:
    request = MessagesRequest(
        model="claude-sonnet-5",
        messages=[Message(role="user", content="hello")],
        extra_body={"model": "other"},
    )

    with pytest.raises(InvalidRequestError, match="canonical fields"):
        build_anthropic_messages_body(
            request,
            reasoning=ReasoningPolicy.provider_default(),
        )


async def _chunks(*frames: dict) -> AsyncIterator[bytes]:
    for frame in frames:
        yield (f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n").encode()


@pytest.mark.asyncio
async def test_native_sse_preserves_thinking_signature_tools_and_usage() -> None:
    frames = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 4}}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "check"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "signed"},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"q":'},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 9, "cache_read_input_tokens": 2},
        },
        {"type": "message_stop"},
    ]

    output = [event async for event in iter_anthropic_sse_frames(_chunks(*frames))]

    text = "".join(output)
    assert '"type":"thinking_delta"' in text
    assert '"type":"signature_delta"' in text
    assert '"type":"input_json_delta"' in text
    assert '"cache_read_input_tokens":2' in text
    assert output[-1].startswith("event: message_stop")


@pytest.mark.asyncio
async def test_native_sse_requires_terminal_event() -> None:
    with pytest.raises(TruncatedProviderStreamError, match="message_stop"):
        _ = [
            event
            async for event in iter_anthropic_sse_frames(
                _chunks({"type": "message_start", "message": {}})
            )
        ]
