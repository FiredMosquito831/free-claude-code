"""Protocol-faithful tests for the first-party Anthropic Messages provider."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.config.provider_catalog import (
    ANTHROPIC_DEFAULT_BASE,
    PROVIDER_CATALOG,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.providers.anthropic import (
    AnthropicProvider,
    extract_anthropic_model_infos,
)
from my_claude_code.providers.anthropic_messages import (
    ANTHROPIC_API_VERSION,
    ApiKeyAuth,
    BearerTokenAuth,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.model_listing import ModelListResponseError
from my_claude_code.providers.rate_limit import ProviderRateLimiter

_KEY = "sk-ant-user-secret-key"


def _config(base_url: str = ANTHROPIC_DEFAULT_BASE) -> ProviderConfig:
    return ProviderConfig(
        api_key=_KEY,
        base_url=base_url,
        rate_limit=100,
        rate_window=60,
        max_concurrency=5,
        retry_attempts=1,
        early_retry_attempts=1,
        commit_holdback_seconds=0,
    )


def _provider(**kwargs: Any) -> AnthropicProvider:
    return AnthropicProvider(
        _config(),
        rate_limiter=ProviderRateLimiter(
            rate_limit=100,
            rate_window=60,
            max_concurrency=5,
            max_retries=0,
        ),
        **kwargs,
    )


def _request() -> MessagesRequest:
    return MessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=32,
        messages=[Message(role="user", content="ping")],
        stream=True,
    )


# --------------------------------------------------------------------------
# Catalog wiring
# --------------------------------------------------------------------------


def test_catalog_descriptor_and_base_url() -> None:
    descriptor = PROVIDER_CATALOG["anthropic"]
    assert descriptor.credential_env == "ANTHROPIC_API_KEY"
    assert descriptor.credential_attr == "anthropic_api_key"
    assert descriptor.default_base_url == ANTHROPIC_DEFAULT_BASE
    assert ANTHROPIC_DEFAULT_BASE == "https://api.anthropic.com/v1"


def test_upstream_base_url_does_not_read_anthropic_base_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``ANTHROPIC_BASE_URL`` points Claude Code AT MCC.

    Reading it as the provider's upstream would make the proxy dial its own
    listener and loop forever, so the field is bound to a distinct variable.
    This asserts the loop-inducing variable is ignored and the real one wins.
    """
    # Isolate from this machine's real ~/.fcc/.env so the assertion is about
    # the binding, not about whatever the developer happens to have configured.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8082")
    monkeypatch.setenv("ANTHROPIC_UPSTREAM_BASE_URL", "https://example.invalid/v1")

    settings = Settings()

    assert settings.anthropic_base_url == "https://example.invalid/v1"
    assert "8082" not in settings.anthropic_base_url


# --------------------------------------------------------------------------
# Auth strategies
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_key_auth_uses_x_api_key_and_pins_version() -> None:
    headers = await ApiKeyAuth(_KEY).headers()

    assert headers["x-api-key"] == _KEY
    assert headers["anthropic-version"] == ANTHROPIC_API_VERSION
    # Anthropic rejects a Console key presented as a bearer token.
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_bearer_auth_preserves_the_existing_upstream_shape() -> None:
    """Regression guard: Command Code authenticates with a bearer token."""
    headers = await BearerTokenAuth("plan-key").headers()

    assert headers == {"Authorization": "Bearer plan-key"}


@pytest.mark.asyncio
async def test_stream_request_sends_api_key_headers() -> None:
    """The credential must reach the wire as ``x-api-key``, not a bearer."""
    provider = _provider()
    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        body = (
            b'event: message_start\ndata: {"type":"message_start"}\n\n'
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        )
        return httpx.Response(200, content=body)

    transport = httpx.MockTransport(handler)
    provider._messages._client = httpx.AsyncClient(transport=transport)

    frames = [frame async for frame in provider.stream_response(_request())]

    assert seen["x-api-key"] == _KEY
    assert seen["anthropic-version"] == ANTHROPIC_API_VERSION
    assert "authorization" not in seen
    assert any("message_stop" in frame for frame in frames)
    await provider.cleanup()


# --------------------------------------------------------------------------
# Model listing
# --------------------------------------------------------------------------


def test_extract_model_infos_reads_anthropic_page() -> None:
    payload = {
        "data": [
            {"type": "model", "id": "claude-opus-4-6", "display_name": "Opus"},
            {"type": "model", "id": "claude-sonnet-4-6", "display_name": "Sonnet"},
        ],
        "has_more": False,
    }

    infos = extract_anthropic_model_infos(payload, provider_name="ANTHROPIC")

    assert {info.model_id for info in infos} == {
        "claude-opus-4-6",
        "claude-sonnet-4-6",
    }


def test_extract_model_infos_reports_nothing_it_cannot_observe() -> None:
    """Anthropic publishes no context length, price, or capability flags.

    ``None`` means "not reported", which is what lets models.dev enrichment
    fill it and stops vision routing diverting on merely-unknown support.
    """
    payload = {"data": [{"id": "claude-sonnet-4-6"}]}

    info = next(iter(extract_anthropic_model_infos(payload, provider_name="A")))

    assert info.context_length is None
    assert info.supports_vision is None
    assert info.supports_thinking is None
    assert info.input_price is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": "not-an-array"},
        {"data": []},
        {"data": [{"display_name": "no id"}]},
        {"data": [{"id": "  "}]},
    ],
)
def test_extract_model_infos_rejects_malformed_payloads(payload: Any) -> None:
    with pytest.raises(ModelListResponseError):
        extract_anthropic_model_infos(payload, provider_name="ANTHROPIC")


@pytest.mark.asyncio
async def test_list_model_infos_calls_the_models_endpoint() -> None:
    provider = _provider()
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["x-api-key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={"data": [{"id": "claude-sonnet-4-6"}]})

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ids = await provider.list_model_ids()

    assert ids == frozenset({"claude-sonnet-4-6"})
    assert seen["url"].startswith("https://api.anthropic.com/v1/models")
    assert seen["x-api-key"] == _KEY
    await provider.cleanup()


@pytest.mark.asyncio
async def test_preflight_builds_a_native_messages_body() -> None:
    provider = _provider()

    # Must not raise: preflight validates serialization before any network I/O.
    provider.preflight_stream(_request())
    await provider.cleanup()


def test_credential_label_is_masked_not_raw() -> None:
    provider = _provider()

    label = provider.credential_label

    assert label is not None
    assert _KEY not in label
    assert json.dumps({"label": label}).count(_KEY) == 0
