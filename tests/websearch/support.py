"""Shared helpers for websearch tests."""

import json
from collections.abc import Callable

import httpx

from free_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)
from free_claude_code.websearch.base import (
    BaseWebSearchProvider,
    WebSearchProviderConfig,
)

TEST_KEY = "test-key-0001abcd"


def build_config(
    api_keys: tuple[str, ...] = (TEST_KEY,),
    *,
    rotation: str = "failover",
    base_url: str | None = None,
    proxy: str | None = None,
    http_timeout: float = 20.0,
) -> WebSearchProviderConfig:
    return WebSearchProviderConfig(
        api_keys=api_keys,
        credential_rotation=rotation,
        base_url=base_url,
        proxy=proxy,
        http_timeout=http_timeout,
    )


def json_response(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def text_response(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=body)


def attach_mock_client(
    provider: BaseWebSearchProvider,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Swap the provider's HTTP client for a MockTransport one; returns requests."""

    requests: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(_capture))
    return requests


def request_json_body(request: httpx.Request) -> dict:
    return json.loads(request.content.decode("utf-8"))


class StubWebSearchProvider(BaseWebSearchProvider):
    """Scriptable provider for rotation-loop tests (no HTTP)."""

    PROVIDER_ID = "stub"
    SUPPORTS_DOMAINS = False

    def __init__(
        self,
        config: WebSearchProviderConfig,
        behavior: dict[int, str | Exception] | None = None,
    ) -> None:
        super().__init__(config)
        self._behavior = behavior or {}
        self.calls: list[dict] = []

    async def _search_with_key(
        self,
        query: str,
        key: str,
        key_index: int,
        *,
        max_results: int,
        allowed_domains: tuple[str, ...],
        blocked_domains: tuple[str, ...],
    ) -> WebSearchResponse:
        self.calls.append(
            {
                "key_index": key_index,
                "key": key,
                "query": query,
                "max_results": max_results,
                "allowed_domains": allowed_domains,
                "blocked_domains": blocked_domains,
            }
        )
        outcome = self._behavior.get(key_index, "ok")
        if isinstance(outcome, Exception):
            raise outcome
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=(
                WebSearchResultItem(
                    title="t",
                    url="https://example.com",
                    snippet="s",
                    content=None,
                    published=None,
                ),
            ),
            key_index=key_index,
            cost_usd=None,
        )


class DomainStubWebSearchProvider(StubWebSearchProvider):
    """Stub variant that passes allowed/blocked domains through."""

    PROVIDER_ID = "stub_domains"
    SUPPORTS_DOMAINS = True


class FakeClock:
    """Deterministic monotonic clock for KeyPool tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
