"""Protocol-faithful tests for the Command Code Provider API."""

import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from free_claude_code.config.provider_catalog import (
    COMMANDCODE_DEFAULT_BASE,
    PROVIDER_CATALOG,
)
from free_claude_code.core.anthropic.models import Message, MessagesRequest
from free_claude_code.core.reasoning import ReasoningPolicy
from free_claude_code.providers.base import ProviderConfig
from free_claude_code.providers.commandcode import (
    CommandCodeProvider,
    extract_commandcode_model_infos,
    is_anthropic_messages_model,
)
from free_claude_code.providers.model_listing import ModelListResponseError
from free_claude_code.providers.rate_limit import ProviderRateLimiter
from free_claude_code.providers.runtime.factory import create_provider
from free_claude_code.providers.runtime.rotating import RotatingProvider


def _provider() -> CommandCodeProvider:
    config = ProviderConfig(
        api_key="user_secret_commandcode_key",
        base_url=COMMANDCODE_DEFAULT_BASE,
        rate_limit=100,
        rate_window=60,
        max_concurrency=5,
        retry_attempts=1,
        early_retry_attempts=1,
        commit_holdback_seconds=0,
    )
    provider = CommandCodeProvider(
        config,
        rate_limiter=ProviderRateLimiter(
            rate_limit=100,
            rate_window=60,
            max_concurrency=5,
            max_retries=0,
        ),
    )
    return provider


def _request(model: str) -> MessagesRequest:
    return MessagesRequest(
        model=model,
        max_tokens=32,
        messages=[Message(role="user", content="ping")],
        stream=True,
    )


def test_catalog_descriptor_and_base_url() -> None:
    descriptor = PROVIDER_CATALOG["commandcode"]

    assert COMMANDCODE_DEFAULT_BASE == "https://api.commandcode.ai/provider/v1"
    assert descriptor.credential_env == "COMMANDCODE_API_KEY"
    assert descriptor.credential_attr == "commandcode_api_key"
    assert descriptor.proxy_attr == "commandcode_proxy"
    assert descriptor.group == "gateway"


def test_factory_preserves_key_pool_rotation_and_proxy(monkeypatch) -> None:
    from free_claude_code.config.settings import Settings

    monkeypatch.setenv("COMMANDCODE_API_KEY_ROTATION", "round_robin")
    settings = Settings.model_validate(
        {
            "COMMANDCODE_API_KEY": "key-one,key-two",
            "COMMANDCODE_PROXY": "http://proxy.test:8080",
            "MESSAGING_PLATFORM": "none",
        }
    )

    provider = create_provider("commandcode", settings)

    assert isinstance(provider, RotatingProvider)
    assert len(provider._providers) == 2
    assert all(isinstance(item, CommandCodeProvider) for item in provider._providers)
    assert [item._config.api_key for item in provider._providers] == [
        "key-one",
        "key-two",
    ]
    assert all(
        item._config.proxy == "http://proxy.test:8080" for item in provider._providers
    )
    assert provider._state.policy == "round_robin"


def test_protocol_classifier_is_narrow_and_case_insensitive() -> None:
    assert is_anthropic_messages_model("claude-sonnet-5")
    assert is_anthropic_messages_model(" CLAUDE-OPUS-5 ")
    assert not is_anthropic_messages_model("anthropic/claude-sonnet-5")
    assert not is_anthropic_messages_model("deepseek/deepseek-v4-flash")


def test_model_catalog_preserves_context_metadata() -> None:
    infos = extract_commandcode_model_infos(
        {
            "object": "list",
            "data": [
                {"id": "claude-sonnet-5", "context_length": 1_000_000},
                {
                    "id": "deepseek/deepseek-v4-flash",
                    "context_length": 1_000_000,
                },
            ],
        },
        provider_name="COMMANDCODE",
    )

    assert {(item.model_id, item.context_length) for item in infos} == {
        ("claude-sonnet-5", 1_000_000),
        ("deepseek/deepseek-v4-flash", 1_000_000),
    }
    assert all(item.supports_thinking is None for item in infos)


@pytest.mark.parametrize(
    "payload",
    [
        {"data": "wrong"},
        {"data": []},
        {"data": [{"id": "", "context_length": 100}]},
        {"data": [{"id": "model", "context_length": 0}]},
    ],
)
def test_model_catalog_rejects_malformed_payload(payload: object) -> None:
    with pytest.raises(ModelListResponseError, match="COMMANDCODE model-list"):
        extract_commandcode_model_infos(payload, provider_name="COMMANDCODE")


@pytest.mark.asyncio
async def test_list_model_infos_uses_live_openai_models_endpoint() -> None:
    provider = _provider()
    provider._openai._client.models.list = AsyncMock(
        return_value={
            "data": [
                {"id": "claude-sonnet-5", "context_length": 1_000_000},
                {"id": "gpt-5.6-sol", "context_length": 1_050_000},
            ]
        }
    )
    try:
        infos = await provider.list_model_infos()
    finally:
        await provider.cleanup()

    assert {item.model_id for item in infos} == {"claude-sonnet-5", "gpt-5.6-sol"}


@pytest.mark.asyncio
async def test_claude_models_use_native_messages_with_bearer_auth() -> None:
    captured_path = ""
    captured_authorization: str | None = None
    captured_body: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_path, captured_authorization, captured_body
        captured_path = request.url.path
        captured_authorization = request.headers.get("authorization")
        parsed_body = json.loads(request.content)
        assert isinstance(parsed_body, dict)
        captured_body = parsed_body
        frames = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "claude-sonnet-5",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 3, "output_tokens": 1},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "pong"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 2},
            },
            {"type": "message_stop"},
        ]
        content = "".join(
            f"event: {item['type']}\ndata: {json.dumps(item)}\n\n" for item in frames
        )
        return httpx.Response(200, text=content)

    provider = _provider()
    original_client = provider._anthropic._client
    provider._anthropic._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=30,
    )
    try:
        events = [
            event
            async for event in provider.stream_response(
                _request("claude-sonnet-5"),
                reasoning=ReasoningPolicy.provider_default(),
            )
        ]
    finally:
        await provider.cleanup()
        await original_client.aclose()

    assert captured_path == "/provider/v1/messages"
    assert captured_authorization == "Bearer user_secret_commandcode_key"
    assert captured_body["stream"] is True
    assert captured_body["model"] == "claude-sonnet-5"
    assert any('"text":"pong"' in event for event in events)


@pytest.mark.asyncio
async def test_non_claude_models_delegate_to_chat_completions() -> None:
    provider = _provider()
    provider._openai.stream_response = MagicMock()
    sentinel = _events("openai")
    provider._openai.stream_response.return_value = sentinel
    request = _request("deepseek/deepseek-v4-flash")

    stream = provider.stream_response(request)

    assert stream is sentinel
    provider._openai.stream_response.assert_called_once()
    await provider.cleanup()


async def _events(value: str) -> AsyncIterator[str]:
    yield value
