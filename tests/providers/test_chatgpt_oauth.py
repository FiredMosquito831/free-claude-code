"""Tests for the direct ChatGPT OAuth Responses API provider."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from free_claude_code.application.errors import (
    InvalidRequestError,
)
from free_claude_code.config.provider_catalog import CHATGPT_OAUTH_DEFAULT_BASE
from free_claude_code.core.anthropic.models import Message, MessagesRequest
from free_claude_code.providers.base import ProviderConfig
from free_claude_code.providers.chatgpt_oauth import ChatGPTOAuthProvider
from free_claude_code.providers.chatgpt_oauth.conversion import (
    build_chatgpt_oauth_request_body,
    chatgpt_tool_call_to_anthropic,
)
from free_claude_code.providers.chatgpt_oauth.credentials import (
    load_chatgpt_oauth_credentials,
)
from free_claude_code.providers.chatgpt_oauth.streaming import (
    ChatGPTOAuthStreamConverter,
)
from tests.providers.support import passthrough_rate_limiter


def _provider_config(**overrides) -> ProviderConfig:
    return ProviderConfig(
        api_key=overrides.get("api_key", "test_token"),
        base_url=overrides.get("base_url", CHATGPT_OAUTH_DEFAULT_BASE),
        rate_limit=overrides.get("rate_limit", 10),
        rate_window=overrides.get("rate_window", 60),
        max_concurrency=overrides.get("max_concurrency", 5),
        http_read_timeout=overrides.get("http_read_timeout", 300.0),
        http_write_timeout=overrides.get("http_write_timeout", 10.0),
        http_connect_timeout=overrides.get("http_connect_timeout", 60.0),
        proxy=overrides.get("proxy", ""),
    )


@pytest.fixture
def chatgpt_oauth_provider():
    return ChatGPTOAuthProvider(
        _provider_config(),
        rate_limiter=passthrough_rate_limiter(),
        account_id="test_account_id",
    )


def test_provider_uses_default_base_url():
    provider = ChatGPTOAuthProvider(
        _provider_config(base_url=""),
        rate_limiter=passthrough_rate_limiter(),
    )
    assert provider._base_url == CHATGPT_OAUTH_DEFAULT_BASE


def test_provider_uses_configured_base_url():
    provider = ChatGPTOAuthProvider(
        _provider_config(base_url="https://example.com/backend-api"),
        rate_limiter=passthrough_rate_limiter(),
    )
    assert provider._base_url == "https://example.com/backend-api"


def test_build_request_body_converts_messages():
    request = MessagesRequest(
        model="gpt-5",
        max_tokens=50,
        messages=[Message(role="user", content="hi")],
    )

    body = build_chatgpt_oauth_request_body(request)

    assert body["model"] == "gpt-5"
    assert body["store"] is False
    assert body["stream"] is True
    assert body["parallel_tool_calls"] is False
    assert "max_output_tokens" not in body
    assert body["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hi"}],
        }
    ]


def test_build_request_body_converts_tool_calls_to_function_items():
    """Assistant tool calls become function_call items; results become outputs."""
    request = MessagesRequest.model_validate(
        {
            "model": "gpt-5",
            "messages": [
                {"role": "user", "content": "list files"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me check."},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "bash",
                            "input": {"command": "ls"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "file.txt",
                        }
                    ],
                },
            ],
        }
    )

    body = build_chatgpt_oauth_request_body(request)

    items = body["input"]
    types = [item["type"] for item in items]
    assert "tool_calls" not in str(items)
    function_calls = [item for item in items if item["type"] == "function_call"]
    function_outputs = [
        item for item in items if item["type"] == "function_call_output"
    ]
    assert function_calls == [
        {
            "type": "function_call",
            "call_id": "toolu_1",
            "name": "bash",
            "arguments": '{"command": "ls"}',
        }
    ]
    assert function_outputs == [
        {
            "type": "function_call_output",
            "call_id": "toolu_1",
            "output": "file.txt",
        }
    ]
    assistant_messages = [
        item
        for item in items
        if item["type"] == "message" and item["role"] == "assistant"
    ]
    assert assistant_messages[0]["content"] == [
        {"type": "output_text", "text": "Let me check."}
    ]
    assert types[0] == "message"


def test_build_request_body_adds_reasoning_for_gpt5():
    request = MessagesRequest(
        model="gpt-5",
        messages=[Message(role="user", content="hi")],
    )

    body = build_chatgpt_oauth_request_body(request)

    assert body["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert "reasoning.encrypted_content" in body["include"]


def test_build_request_body_skips_reasoning_for_non_reasoning_model():
    request = MessagesRequest(
        model="unknown-model",
        messages=[Message(role="user", content="hi")],
    )

    body = build_chatgpt_oauth_request_body(request)

    assert "reasoning" not in body


def test_build_request_body_extracts_system_as_instructions():
    request = MessagesRequest(
        model="gpt-5",
        system="You are a helpful assistant.",
        messages=[Message(role="user", content="hi")],
    )

    body = build_chatgpt_oauth_request_body(request)

    assert body["instructions"] == "You are a helpful assistant."


def test_build_request_body_rejects_extra_body():
    request = MessagesRequest.model_validate(
        {
            "model": "gpt-5",
            "messages": [{"role": "user", "content": "x"}],
            "extra_body": {"x": 1},
        }
    )

    with pytest.raises(InvalidRequestError):
        build_chatgpt_oauth_request_body(request)


def test_chatgpt_tool_call_to_anthropic():
    item = {
        "id": "call_1",
        "name": "bash",
        "arguments": json.dumps({"command": "ls"}),
    }

    block = chatgpt_tool_call_to_anthropic(item)

    assert block["type"] == "tool_use"
    assert block["id"] == "call_1"
    assert block["name"] == "bash"
    assert block["input"] == {"command": "ls"}


def test_stream_converter_emits_text_delta():
    from free_claude_code.core.anthropic.streaming import AnthropicStreamLedger

    ledger = AnthropicStreamLedger("msg_1", "gpt-5", input_tokens=0)
    converter = ChatGPTOAuthStreamConverter(ledger)

    events = list(
        converter.feed({"type": "response.output_text.delta", "delta": "hello"})
    )

    assert any("content_block_start" in e and "text" in e for e in events)
    assert any("text_delta" in e and "hello" in e for e in events)


def test_stream_converter_emits_tool_call():
    from free_claude_code.core.anthropic.streaming import AnthropicStreamLedger

    ledger = AnthropicStreamLedger("msg_1", "gpt-5", input_tokens=0)
    converter = ChatGPTOAuthStreamConverter(ledger)

    events = list(
        converter.feed(
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "id": "call_1", "name": "bash"},
            }
        )
    )
    events += list(
        converter.feed(
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "call_1",
                "delta": '{"command":',
            }
        )
    )
    events += list(
        converter.feed(
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "call_1",
                "delta": '"ls"}',
            }
        )
    )
    events += list(
        converter.feed(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "call_1",
                    "arguments": '{"command":"ls"}',
                },
            }
        )
    )

    assert any("tool_use" in e for e in events)
    assert any('"bash"' in e for e in events)
    # partial_json contains the raw argument string; look for the key without
    # requiring exact JSON quote escaping in the serialized SSE line.
    assert any("command" in e for e in events)


@pytest.mark.asyncio
async def test_list_model_ids_returns_known_models(chatgpt_oauth_provider):
    models = await chatgpt_oauth_provider.list_model_ids()
    # OpenCode-aligned allowlist: explicit allows, disallowed pro, and a
    # version heuristic that keeps GPT-5.x models newer than 5.4.
    assert "gpt-5.5" in models
    assert "gpt-5.4" in models
    assert "gpt-5.4-mini" in models
    assert "gpt-5.3-codex-spark" in models
    assert "gpt-5.5-pro" not in models
    assert "gpt-5.2-codex" not in models
    # "gpt-5.6" is a family name on this plan, not a servable id: only the
    # -luna, -sol and -terra variants exist, and the bare id 404s.
    assert "gpt-5.6" not in models
    assert {"gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"} <= models


@pytest.mark.asyncio
async def test_cleanup_closes_http_client(chatgpt_oauth_provider):
    chatgpt_oauth_provider._client.aclose = AsyncMock()

    await chatgpt_oauth_provider.cleanup()

    chatgpt_oauth_provider._client.aclose.assert_awaited_once()


def test_load_credentials_prefers_explicit_token():
    creds = load_chatgpt_oauth_credentials(
        access_token="explicit_token",
        account_id="explicit_account",
    )

    assert creds.access_token == "explicit_token"
    assert creds.account_id == "explicit_account"


def test_runtime_does_not_implicitly_read_codex_auth_file(tmp_path, monkeypatch):
    from free_claude_code.providers.chatgpt_oauth import credentials as creds_module
    from free_claude_code.providers.chatgpt_oauth.credentials import ChatGPTOAuthError

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        '{"tokens": {"access_token": "file_token"}}', encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        creds_module,
        "chatgpt_oauth_auth_path",
        lambda: tmp_path / ".fcc" / "auth" / "chatgpt-oauth.json",
    )

    with pytest.raises(ChatGPTOAuthError, match="Sign in or import"):
        load_chatgpt_oauth_credentials()


def _jwt(payload_dict: dict) -> str:
    import base64

    header = base64.urlsafe_b64encode(b"{}").decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).decode().rstrip("=")
    )
    return f"{header}.{payload}."


def _store_managed_credentials(tmp_path, monkeypatch, tokens):
    from free_claude_code.providers.chatgpt_oauth import credentials as creds_module

    auth_path = tmp_path / ".fcc" / "auth" / "chatgpt-oauth.json"
    monkeypatch.setattr(
        creds_module,
        "chatgpt_oauth_auth_path",
        lambda: auth_path,
    )
    creds_module.store_managed_chatgpt_oauth_tokens(tokens, auth_path=auth_path)
    return auth_path


def test_load_credentials_prefers_id_token_for_account_id(tmp_path, monkeypatch):
    access_token = _jwt({"exp": 9999999999})
    id_token = _jwt({"chatgpt_account_id": "acct_from_id_token"})
    _store_managed_credentials(
        tmp_path,
        monkeypatch,
        {
            "access_token": access_token,
            "refresh_token": "refresh_1",
            "id_token": id_token,
        },
    )

    creds = load_chatgpt_oauth_credentials()

    assert creds.account_id == "acct_from_id_token"


def test_load_credentials_falls_back_to_organization_id(tmp_path, monkeypatch):
    access_token = _jwt({"organizations": [{"id": "org_123"}], "exp": 9999999999})
    _store_managed_credentials(
        tmp_path,
        monkeypatch,
        {
            "access_token": access_token,
            "refresh_token": "refresh_1",
            "id_token": "id_1",
        },
    )

    creds = load_chatgpt_oauth_credentials()

    assert creds.account_id == "org_123"


def test_refresh_persists_rotated_tokens_to_managed_auth_file(tmp_path, monkeypatch):
    import time

    from free_claude_code.providers.chatgpt_oauth import credentials as creds_module

    expired_access = _jwt({"exp": int(time.time()) - 100})
    auth_path = _store_managed_credentials(
        tmp_path,
        monkeypatch,
        {
            "access_token": expired_access,
            "refresh_token": "refresh_old",
            "id_token": "id_old",
            "account_id": "acct_old",
        },
    )

    new_access = _jwt({"exp": 9999999999, "chatgpt_account_id": "acct_new"})
    monkeypatch.setattr(
        creds_module,
        "_refresh_access_token",
        lambda refresh: (new_access, "refresh_rotated", 9999999999, None),
    )

    creds = load_chatgpt_oauth_credentials()

    assert creds.access_token == new_access
    assert creds.account_id == "acct_new"
    saved = json.loads(auth_path.read_text(encoding="utf-8"))
    assert saved["tokens"]["access_token"] == new_access
    assert saved["tokens"]["refresh_token"] == "refresh_rotated"
    assert saved["tokens"]["expires_at"] == 9999999999


def test_refresh_uses_current_origin_request_shape(monkeypatch):
    from free_claude_code.providers.chatgpt_oauth import credentials as creds_module

    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return httpx.Response(
            200,
            json={
                "access_token": "access_new",
                "refresh_token": "refresh_new",
                "id_token": "id_new",
                "expires_in": 3600,
            },
        )

    monkeypatch.setattr(creds_module.httpx, "post", fake_post)

    access, refresh, _, id_token = creds_module._refresh_access_token("refresh_old")

    assert access == "access_new"
    assert refresh == "refresh_new"
    assert id_token == "id_new"
    assert captured["json"] == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh_old",
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
    }
    assert captured["headers"]["originator"] == "codex_cli_rs"
    assert "data" not in captured


def test_import_codex_copies_renewable_bundle_without_modifying_source(
    tmp_path, monkeypatch
):
    from free_claude_code.providers.chatgpt_oauth import credentials as creds_module

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    codex_path = codex_home / "auth.json"
    source_payload = {
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": _jwt(
                {"exp": 9999999999, "chatgpt_account_id": "acct_codex"}
            ),
            "refresh_token": "refresh_codex",
            "id_token": "id_codex",
            "account_id": "acct_codex",
        },
        "last_refresh": "preserve-me",
    }
    codex_path.write_text(json.dumps(source_payload, indent=2), encoding="utf-8")
    source_bytes = codex_path.read_bytes()
    managed_path = tmp_path / ".fcc" / "auth" / "chatgpt-oauth.json"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        creds_module,
        "chatgpt_oauth_auth_path",
        lambda: managed_path,
    )

    credentials = creds_module.import_codex_cli_tokens()

    assert credentials.source_name == "fcc-managed"
    assert credentials.account_id == "acct_codex"
    assert codex_path.read_bytes() == source_bytes
    managed = json.loads(managed_path.read_text(encoding="utf-8"))
    assert managed["tokens"]["refresh_token"] == "refresh_codex"
    assert managed["tokens"]["id_token"] == "id_codex"


def test_settings_references_managed_credentials_without_copying_secret(
    tmp_path, monkeypatch
):
    from free_claude_code.config import settings as settings_module
    from free_claude_code.config.constants import (
        CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
    )
    from free_claude_code.config.settings import Settings

    managed_path = tmp_path / ".fcc" / "auth" / "chatgpt-oauth.json"
    managed_path.parent.mkdir(parents=True)
    managed_path.write_text('{"secret": "must-not-enter-settings"}', encoding="utf-8")
    monkeypatch.delenv("CHATGPT_OAUTH_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(
        settings_module,
        "chatgpt_oauth_auth_path",
        lambda: managed_path,
    )

    settings = Settings()

    assert (
        settings.chatgpt_oauth_access_token
        == CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE
    )
    assert "must-not-enter-settings" not in settings.chatgpt_oauth_access_token


def test_rejected_refresh_removes_only_fcc_managed_credentials(tmp_path, monkeypatch):
    from free_claude_code.providers.chatgpt_oauth import credentials as creds_module

    managed_path = _store_managed_credentials(
        tmp_path,
        monkeypatch,
        {
            "access_token": _jwt({"exp": 9999999999}),
            "refresh_token": "refresh_invalid",
            "id_token": "id_1",
            "account_id": "acct_1",
        },
    )
    codex_path = tmp_path / ".codex" / "auth.json"
    codex_path.parent.mkdir()
    codex_path.write_text('{"preserve": true}', encoding="utf-8")
    monkeypatch.setattr(
        creds_module,
        "_refresh_access_token",
        lambda refresh: (_ for _ in ()).throw(
            creds_module.ChatGPTOAuthRefreshError(401)
        ),
    )

    with pytest.raises(creds_module.ChatGPTOAuthError, match="Reconnect"):
        creds_module.force_refresh_managed_chatgpt_oauth_credentials()

    assert not managed_path.exists()
    assert codex_path.read_text(encoding="utf-8") == '{"preserve": true}'


def test_write_managed_auth_file_stores_complete_bundle(tmp_path):
    from free_claude_code.providers.chatgpt_oauth import oauth_login

    auth_path = tmp_path / ".fcc" / "auth" / "chatgpt-oauth.json"
    oauth_login._write_managed_auth_file(
        {
            "access_token": "access_1",
            "refresh_token": "refresh_1",
            "id_token": "id_1",
            "account_id": "acct_1",
            "expires_in": 3600,
        },
        auth_path=auth_path,
    )

    saved = json.loads(auth_path.read_text(encoding="utf-8"))
    assert saved["version"] == 1
    assert saved["tokens"]["id_token"] == "id_1"


def test_load_credentials_extracts_account_id_from_jwt(tmp_path, monkeypatch):
    import base64

    header = base64.urlsafe_b64encode(b"{}").decode().rstrip("=")
    payload_dict = {"https://api.openai.com/auth": {"chatgpt_account_id": "acct_123"}}
    payload = (
        base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).decode().rstrip("=")
    )
    token = f"{header}.{payload}."

    _store_managed_credentials(
        tmp_path,
        monkeypatch,
        {
            "access_token": token,
            "refresh_token": "refresh_1",
            "id_token": "id_1",
        },
    )

    creds = load_chatgpt_oauth_credentials()

    assert creds.account_id == "acct_123"


def test_build_headers_matches_opencode_shape(chatgpt_oauth_provider):
    from free_claude_code.providers.chatgpt_oauth.credentials import (
        ChatGPTOAuthCredentials,
    )
    from free_claude_code.providers.chatgpt_oauth.provider import _build_headers

    credentials = ChatGPTOAuthCredentials(
        access_token="test_token",
        account_id="acct_123",
    )
    headers = _build_headers(credentials, chatgpt_oauth_provider._session_id)

    assert headers["Authorization"] == "Bearer test_token"
    assert headers["originator"] == "codex_cli_rs"
    assert headers["session-id"] == chatgpt_oauth_provider._session_id
    assert "User-Agent" in headers
    assert headers["User-Agent"].startswith("codex_cli_rs/")
    assert headers["ChatGPT-Account-ID"] == "acct_123"


def test_session_id_is_stable_per_provider():
    from free_claude_code.providers.base import ProviderConfig

    config = ProviderConfig(
        api_key="x",
        base_url="",
        rate_limit=10,
        rate_window=60,
        max_concurrency=5,
        http_read_timeout=300.0,
        http_write_timeout=10.0,
        http_connect_timeout=60.0,
        proxy="",
    )
    provider1 = ChatGPTOAuthProvider(
        config,
        rate_limiter=passthrough_rate_limiter(),
    )
    provider2 = ChatGPTOAuthProvider(
        config,
        rate_limiter=passthrough_rate_limiter(),
    )
    assert provider1._session_id
    assert provider1._session_id == provider1._session_id
    assert provider1._session_id != provider2._session_id


def test_model_filter_logic():
    from free_claude_code.providers.chatgpt_oauth.provider import (
        _is_chatgpt_oauth_model,
    )

    assert _is_chatgpt_oauth_model("gpt-5.5") is True
    assert _is_chatgpt_oauth_model("gpt-5.4") is True
    assert _is_chatgpt_oauth_model("gpt-5.4-mini") is True
    assert _is_chatgpt_oauth_model("gpt-5.7") is True
    assert _is_chatgpt_oauth_model("gpt-5.5-pro") is False
    assert _is_chatgpt_oauth_model("gpt-5.6") is False
    assert _is_chatgpt_oauth_model("gpt-5.6-luna") is True
    assert _is_chatgpt_oauth_model("gpt-5.6-sol") is True
    assert _is_chatgpt_oauth_model("gpt-5.6-terra") is True
    assert _is_chatgpt_oauth_model("gpt-5.2-codex") is False
    assert _is_chatgpt_oauth_model("codex-mini-latest") is False


def test_perform_chatgpt_oauth_login_writes_managed_auth_file(tmp_path, monkeypatch):
    import base64

    from free_claude_code.providers.chatgpt_oauth import oauth_login

    monkeypatch.setattr(oauth_login, "CHATGPT_OAUTH_POLL_SAFETY_MS", 1)
    auth_file = tmp_path / ".fcc" / "auth" / "chatgpt-oauth.json"

    header = base64.urlsafe_b64encode(b"{}").decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"https://api.openai.com/auth": {"chatgpt_account_id": "acct_xyz"}}
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    access_token = f"{header}.{payload}."

    responses = {
        ("POST", oauth_login.CHATGPT_OAUTH_DEVICE_URL): {
            "json": {
                "device_auth_id": "device_1",
                "user_code": "ABCD-EFGH",
                "interval": "1",
            }
        },
        ("POST", oauth_login.CHATGPT_OAUTH_DEVICE_TOKEN_URL): {
            "json": {
                "authorization_code": "auth_code_1",
                "code_verifier": "verifier_1",
            }
        },
        ("POST", oauth_login.CODEX_OAUTH_TOKEN_URL): {
            "json": {
                "access_token": access_token,
                "refresh_token": "refresh_1",
                "id_token": "id_1",
                "expires_in": 3600,
            }
        },
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        payload = responses[("POST", str(request.url))]
        return httpx.Response(200, json=payload["json"])

    fake_client = httpx.Client(transport=httpx.MockTransport(_handler))
    tokens = oauth_login.perform_chatgpt_oauth_login(
        timeout_seconds=2,
        auth_path=auth_file,
        http_client=fake_client,
    )

    assert tokens["access_token"] == access_token
    assert tokens["account_id"] == "acct_xyz"
    assert auth_file.exists()
    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert saved["tokens"]["access_token"] == access_token
    assert saved["tokens"]["refresh_token"] == "refresh_1"
    assert "expires_at" in saved["tokens"]


@pytest.mark.asyncio
async def test_stream_response_uses_send_not_stream_context_manager(
    chatgpt_oauth_provider,
):
    """Regression: awaiting httpx.AsyncClient.stream() raises TypeError."""
    from unittest.mock import MagicMock

    request = MessagesRequest(
        model="gpt-5",
        messages=[Message(role="user", content="hi")],
    )

    async def _raw_stream():
        yield b'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
        yield b'data: {"type":"response.completed","response":{}}\n\n'

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.aiter_raw = _raw_stream
    fake_response.aclose = AsyncMock()

    client = chatgpt_oauth_provider._client
    client.build_request = MagicMock(return_value=MagicMock())
    client.send = AsyncMock(return_value=fake_response)

    chunks = [
        chunk
        async for chunk in chatgpt_oauth_provider.stream_response(
            request, request_id="req_1"
        )
    ]

    assert any("content_block_start" in chunk and "text" in chunk for chunk in chunks)
    assert any("text_delta" in chunk and "hello" in chunk for chunk in chunks)
    client.send.assert_awaited_once()
    send_call = client.send.await_args
    assert send_call is not None
    assert send_call.kwargs.get("stream") is True
    fake_response.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_response_refreshes_managed_credentials_once_after_401(
    monkeypatch,
):
    from unittest.mock import MagicMock

    from free_claude_code.config.constants import (
        CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
    )
    from free_claude_code.providers.chatgpt_oauth import provider as provider_module
    from free_claude_code.providers.chatgpt_oauth.credentials import (
        ChatGPTOAuthCredentials,
    )

    provider = ChatGPTOAuthProvider(
        _provider_config(api_key=CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE),
        rate_limiter=passthrough_rate_limiter(),
    )
    rejected = ChatGPTOAuthCredentials(
        access_token="access_old",
        account_id="acct_1",
        refresh_token="refresh_old",
        source_name="fcc-managed",
    )
    refreshed = ChatGPTOAuthCredentials(
        access_token="access_new",
        account_id="acct_1",
        refresh_token="refresh_new",
        source_name="fcc-managed",
    )
    refresh_calls: list[bool] = []
    monkeypatch.setattr(
        provider_module,
        "load_chatgpt_oauth_credentials",
        lambda **kwargs: rejected,
    )

    def _refresh():
        refresh_calls.append(True)
        return refreshed

    monkeypatch.setattr(
        provider_module,
        "force_refresh_managed_chatgpt_oauth_credentials",
        _refresh,
    )

    unauthorized = MagicMock(status_code=401)
    unauthorized.aclose = AsyncMock()

    async def _raw_stream():
        yield b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
        yield b'data: {"type":"response.completed","response":{}}\n\n'

    success = MagicMock(status_code=200)
    success.aiter_raw = _raw_stream
    success.aclose = AsyncMock()
    provider._send_stream_request = AsyncMock(side_effect=[unauthorized, success])
    request = MessagesRequest(
        model="gpt-5",
        messages=[Message(role="user", content="hi")],
    )

    chunks = [chunk async for chunk in provider.stream_response(request)]

    assert any("text_delta" in chunk and "ok" in chunk for chunk in chunks)
    assert refresh_calls == [True]
    assert provider._send_stream_request.await_count == 2
    second_call = provider._send_stream_request.await_args_list[1]
    assert second_call.kwargs["headers"]["Authorization"] == "Bearer access_new"
    unauthorized.aclose.assert_awaited_once()
    success.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_model_ids_discovers_new_models_from_models_dev(
    chatgpt_oauth_provider, monkeypatch
):
    """The catalog comes from models.dev, so a new GPT-5.x needs no code change.

    The backend's own models endpoint answers 401 for an OAuth session, which
    is why discovery reads the models.dev index FCC already caches.
    """
    from free_claude_code.providers.chatgpt_oauth import provider as provider_module

    monkeypatch.setattr(
        provider_module,
        "models_dev_provider_model_ids",
        lambda _provider: frozenset({"gpt-5.9-nova", "gpt-4o", "gpt-5.5-pro"}),
    )

    models = await chatgpt_oauth_provider.list_model_ids()

    assert "gpt-5.9-nova" in models
    assert "gpt-4o" not in models
    assert "gpt-5.5-pro" not in models
    # The static ids remain, so an empty or stale cache never empties the picker.
    assert "gpt-5.5" in models


@pytest.mark.asyncio
async def test_list_model_ids_falls_back_when_models_dev_is_unavailable(
    chatgpt_oauth_provider, monkeypatch
):
    from free_claude_code.providers.chatgpt_oauth import provider as provider_module

    monkeypatch.setattr(
        provider_module,
        "models_dev_provider_model_ids",
        lambda _provider: frozenset(),
    )

    models = await chatgpt_oauth_provider.list_model_ids()

    assert {"gpt-5.4", "gpt-5.4-mini", "gpt-5.5", "gpt-5.6-luna"} <= models
