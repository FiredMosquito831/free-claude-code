"""Shared HTTP helper tests: client factory, JSON requests, error mapping."""

import httpx
import pytest

from my_claude_code.websearch.adapters.http import (
    build_async_client,
    extract_error_message,
    request_json,
)
from my_claude_code.websearch.errors import (
    WebSearchAuthError,
    WebSearchInvalidRequestError,
    WebSearchQuotaError,
    WebSearchRateLimitError,
    WebSearchUpstreamError,
)
from tests.websearch.support import json_response, text_response


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_build_async_client_without_proxy() -> None:
    client = build_async_client()
    try:
        assert isinstance(client, httpx.AsyncClient)
        assert client.timeout.read == 20.0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_request_json_returns_parsed_body() -> None:
    async with _client(lambda request: json_response({"ok": True})) as client:
        assert await request_json(client, "p", "GET", "https://x.test/") == {"ok": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, WebSearchAuthError),
        (403, WebSearchAuthError),
        (402, WebSearchQuotaError),
        (429, WebSearchRateLimitError),
        (400, WebSearchInvalidRequestError),
        (422, WebSearchInvalidRequestError),
        (500, WebSearchUpstreamError),
        (503, WebSearchUpstreamError),
    ],
)
async def test_status_mapping(status: int, error_type) -> None:
    async with _client(
        lambda request: json_response({"error": "nope"}, status=status)
    ) as client:
        with pytest.raises(error_type) as exc_info:
            await request_json(client, "prov", "GET", "https://x.test/")
    assert exc_info.value.provider == "prov"
    assert exc_info.value.status_code == status
    assert "nope" in str(exc_info.value)


@pytest.mark.asyncio
async def test_extra_status_errors_override_default_mapping() -> None:
    async with _client(
        lambda request: json_response({"detail": {"error": "plan cap"}}, status=432)
    ) as client:
        with pytest.raises(WebSearchQuotaError, match="plan cap"):
            await request_json(
                client,
                "tavily",
                "POST",
                "https://x.test/",
                extra_status_errors={432: WebSearchQuotaError},
            )


@pytest.mark.asyncio
async def test_timeout_maps_to_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with _client(handler) as client:
        with pytest.raises(WebSearchUpstreamError, match="timed out"):
            await request_json(client, "p", "GET", "https://x.test/")


@pytest.mark.asyncio
async def test_transport_error_maps_to_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with _client(handler) as client:
        with pytest.raises(WebSearchUpstreamError, match="transport error"):
            await request_json(client, "p", "GET", "https://x.test/")


@pytest.mark.asyncio
async def test_non_json_success_body_maps_to_upstream_error() -> None:
    async with _client(lambda request: text_response("<html>nope</html>")) as client:
        with pytest.raises(WebSearchUpstreamError, match="not valid JSON"):
            await request_json(client, "p", "GET", "https://x.test/")


@pytest.mark.asyncio
async def test_error_message_falls_back_to_body_text() -> None:
    async with _client(
        lambda request: text_response("rate limited", status=429)
    ) as client:
        with pytest.raises(WebSearchRateLimitError, match="rate limited"):
            await request_json(client, "p", "GET", "https://x.test/")


class TestExtractErrorMessage:
    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"error": "plain"}, "plain"),
            ({"error": {"message": "nested"}}, "nested"),
            ({"detail": {"error": "tavily shape"}}, "tavily shape"),
            ({"detail": "detail str"}, "detail str"),
            ({"message": "msg"}, "msg"),
            ({"unexpected": 1}, "fallback text"),
            ("string body", "string body"),
            (None, "fallback text"),
        ],
    )
    def test_message_shapes(self, payload, expected: str) -> None:
        assert extract_error_message(payload, fallback="fallback text") == expected

    def test_message_is_capped(self) -> None:
        long_message = extract_error_message({"error": "x" * 1000})
        assert len(long_message) <= 301
        assert long_message.endswith("…")


class TestRateLimitRetryAfter:
    """A 429 carries the provider's own reset time; don't discard it."""

    @pytest.mark.parametrize(
        ("header", "value", "expected"),
        [
            ("retry-after", "12", 12.0),
            ("retry-after-ms", "2500", 2.5),
            ("x-ratelimit-reset-requests", "6m0s", 360.0),
            ("ratelimit-reset", "45", 45.0),
        ],
    )
    @pytest.mark.asyncio
    async def test_retry_after_is_attached_to_rate_limit_error(
        self, header, value, expected
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429, json={"error": "slow down"}, headers={header: value}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(WebSearchRateLimitError) as excinfo:
            await request_json(client, "exa", "GET", "https://example.test/search")
        assert excinfo.value.retry_after_seconds == pytest.approx(expected)
        await client.aclose()

    @pytest.mark.asyncio
    async def test_missing_header_leaves_retry_after_unset(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": "slow down"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(WebSearchRateLimitError) as excinfo:
            await request_json(client, "exa", "GET", "https://example.test/search")
        assert excinfo.value.retry_after_seconds is None
        await client.aclose()
