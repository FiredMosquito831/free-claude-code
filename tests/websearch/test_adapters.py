"""Per-adapter tests: auth/payload construction, result mapping, error mapping."""

from dataclasses import dataclass

import httpx
import pytest

from free_claude_code.websearch.adapters.brave import BraveWebSearchProvider
from free_claude_code.websearch.adapters.ddgs import DdgsWebSearchProvider
from free_claude_code.websearch.adapters.exa import ExaWebSearchProvider
from free_claude_code.websearch.adapters.firecrawl import FirecrawlWebSearchProvider
from free_claude_code.websearch.adapters.jina import JinaWebSearchProvider
from free_claude_code.websearch.adapters.linkup import LinkupWebSearchProvider
from free_claude_code.websearch.adapters.ollama import OllamaWebSearchProvider
from free_claude_code.websearch.adapters.parallel import ParallelWebSearchProvider
from free_claude_code.websearch.adapters.perplexity import PerplexityWebSearchProvider
from free_claude_code.websearch.adapters.searchapi import SearchApiWebSearchProvider
from free_claude_code.websearch.adapters.searxng import SearxngWebSearchProvider
from free_claude_code.websearch.adapters.serpapi import SerpApiWebSearchProvider
from free_claude_code.websearch.adapters.serper import SerperWebSearchProvider
from free_claude_code.websearch.adapters.tavily import TavilyWebSearchProvider
from free_claude_code.websearch.base import BaseWebSearchProvider
from free_claude_code.websearch.errors import (
    WebSearchAuthError,
    WebSearchConfigError,
    WebSearchQuotaError,
    WebSearchRateLimitError,
    WebSearchUpstreamError,
)
from tests.websearch.support import (
    TEST_KEY,
    attach_mock_client,
    build_config,
    json_response,
    request_json_body,
)


@dataclass(frozen=True, slots=True)
class AdapterCase:
    provider_id: str
    adapter_cls: type[BaseWebSearchProvider]
    payload: dict
    method: str = "POST"
    path: str = "/search"
    auth_header: tuple[str, str] | None = None
    auth_query_key: str | None = None
    snippet: str = "S"
    published: str | None = None
    content: str | None = None
    cost: float | None = None
    keyless: bool = False


_BEARER = ("Authorization", f"Bearer {TEST_KEY}")

CASES: tuple[AdapterCase, ...] = (
    AdapterCase(
        "ollama",
        OllamaWebSearchProvider,
        {"results": [{"title": "T", "url": "https://a.io/x", "content": "S"}]},
        path="/api/web_search",
        auth_header=_BEARER,
        content="S",
    ),
    AdapterCase(
        "exa",
        ExaWebSearchProvider,
        {
            "results": [
                {
                    "title": "T",
                    "url": "https://a.io/x",
                    "highlights": ["h1", "h2"],
                    "publishedDate": "2026-01-02",
                }
            ],
            "costDollars": {"total": 0.007},
        },
        path="/search",
        auth_header=("x-api-key", TEST_KEY),
        snippet="h1 … h2",
        published="2026-01-02",
        cost=0.007,
    ),
    AdapterCase(
        "tavily",
        TavilyWebSearchProvider,
        {"results": [{"title": "T", "url": "https://a.io/x", "content": "S"}]},
        path="/search",
        auth_header=_BEARER,
    ),
    AdapterCase(
        "brave",
        BraveWebSearchProvider,
        {
            "web": {
                "results": [
                    {
                        "title": "T",
                        "url": "https://a.io/x",
                        "description": "S",
                        "page_age": "2026-01-02",
                    }
                ]
            }
        },
        method="GET",
        path="/res/v1/web/search",
        auth_header=("X-Subscription-Token", TEST_KEY),
        published="2026-01-02",
    ),
    AdapterCase(
        "searxng",
        SearxngWebSearchProvider,
        {
            "results": [
                {
                    "title": "T",
                    "url": "https://a.io/x",
                    "content": "S",
                    "publishedDate": "2026-01-02",
                }
            ]
        },
        method="GET",
        path="/search",
        published="2026-01-02",
        keyless=True,
    ),
    AdapterCase(
        "jina",
        JinaWebSearchProvider,
        {
            "code": 200,
            "data": [{"title": "T", "url": "https://a.io/x", "content": "S"}],
        },
        method="GET",
        path="/hello%20world",
        auth_header=_BEARER,
        content="S",
    ),
    AdapterCase(
        "serper",
        SerperWebSearchProvider,
        {
            "organic": [
                {
                    "title": "T",
                    "link": "https://a.io/x",
                    "snippet": "S",
                    "date": "Jan 2, 2026",
                }
            ]
        },
        path="/search",
        auth_header=("X-API-KEY", TEST_KEY),
        published="Jan 2, 2026",
    ),
    AdapterCase(
        "firecrawl",
        FirecrawlWebSearchProvider,
        {
            "success": True,
            "data": {
                "web": [{"title": "T", "url": "https://a.io/x", "description": "S"}]
            },
        },
        path="/v2/search",
        auth_header=_BEARER,
    ),
    AdapterCase(
        "linkup",
        LinkupWebSearchProvider,
        {
            "results": [
                {"type": "text", "name": "T", "url": "https://a.io/x", "content": "S"}
            ]
        },
        path="/v1/search",
        auth_header=_BEARER,
    ),
    AdapterCase(
        "perplexity",
        PerplexityWebSearchProvider,
        {
            "results": [
                {
                    "title": "T",
                    "url": "https://a.io/x",
                    "snippet": "S",
                    "date": "2026-01-02",
                }
            ]
        },
        path="/search",
        auth_header=_BEARER,
        published="2026-01-02",
    ),
    AdapterCase(
        "parallel",
        ParallelWebSearchProvider,
        {
            "results": [
                {
                    "title": "T",
                    "url": "https://a.io/x",
                    "publish_date": "2026-01-02",
                    "excerpts": ["e1", "e2"],
                }
            ]
        },
        path="/v1beta/search",
        auth_header=("x-api-key", TEST_KEY),
        snippet="e1\n\ne2",
        published="2026-01-02",
        content="e1\n\ne2",
    ),
    AdapterCase(
        "searchapi",
        SearchApiWebSearchProvider,
        {"organic_results": [{"title": "T", "link": "https://a.io/x", "snippet": "S"}]},
        method="GET",
        path="/api/v1/search",
        auth_query_key=TEST_KEY,
    ),
    AdapterCase(
        "serpapi",
        SerpApiWebSearchProvider,
        {"organic_results": [{"title": "T", "link": "https://a.io/x", "snippet": "S"}]},
        method="GET",
        path="/search",
        auth_query_key=TEST_KEY,
    ),
)

_CASE_IDS = [case.provider_id for case in CASES]


def _build(case: AdapterCase) -> BaseWebSearchProvider:
    config = (
        build_config(api_keys=(), rotation="single", base_url="https://sx.test")
        if case.keyless
        else build_config()
    )
    return case.adapter_cls(config)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
async def test_adapter_request_and_result_mapping(case: AdapterCase) -> None:
    provider = _build(case)
    requests = attach_mock_client(provider, lambda request: json_response(case.payload))
    try:
        response = await provider.search("hello world", max_results=5)
    finally:
        await provider.close()

    assert len(requests) == 1
    request = requests[0]
    assert request.method == case.method
    assert case.path in str(request.url)
    if case.auth_header is not None:
        name, value = case.auth_header
        assert request.headers[name] == value
    if case.auth_query_key is not None:
        assert request.url.params["api_key"] == case.auth_query_key

    assert response.provider == case.provider_id
    assert response.query == "hello world"
    assert response.key_index == 0
    assert len(response.results) == 1
    item = response.results[0]
    assert item.title == "T"
    assert item.url == "https://a.io/x"
    assert item.snippet == case.snippet
    assert item.published == case.published
    assert item.content == case.content
    assert response.cost_usd == case.cost


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
async def test_adapter_401_maps_to_auth_error(case: AdapterCase) -> None:
    provider = _build(case)
    attach_mock_client(
        provider, lambda request: json_response({"error": "bad key"}, status=401)
    )
    try:
        with pytest.raises(WebSearchAuthError):
            await provider.search("q")
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
async def test_adapter_429_maps_to_rate_limit_error(case: AdapterCase) -> None:
    provider = _build(case)
    attach_mock_client(
        provider, lambda request: json_response({"error": "slow"}, status=429)
    )
    try:
        with pytest.raises(WebSearchRateLimitError):
            await provider.search("q")
    finally:
        await provider.close()


class TestRequestBodyDetails:
    @pytest.mark.asyncio
    async def test_ollama_caps_max_results_at_10(self) -> None:
        provider = OllamaWebSearchProvider(build_config())
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search("q", max_results=50)
        finally:
            await provider.close()
        assert request_json_body(requests[0]) == {"query": "q", "max_results": 10}

    @pytest.mark.asyncio
    async def test_exa_requests_highlights_and_domain_filters(self) -> None:
        provider = ExaWebSearchProvider(build_config())
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search(
                "q",
                max_results=7,
                allowed_domains=("a.com",),
                blocked_domains=("b.com",),
            )
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["contents"] == {"highlights": True}
        assert body["numResults"] == 7
        assert body["includeDomains"] == ["a.com"]
        assert body["excludeDomains"] == ["b.com"]

    @pytest.mark.asyncio
    async def test_tavily_pins_search_depth_and_domains(self) -> None:
        provider = TavilyWebSearchProvider(build_config())
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search("q", allowed_domains=("a.com",))
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["search_depth"] == "basic"
        assert body["include_domains"] == ["a.com"]

    @pytest.mark.asyncio
    async def test_firecrawl_web_source_and_exclusive_domain_filters(self) -> None:
        provider = FirecrawlWebSearchProvider(build_config())
        requests = attach_mock_client(
            provider, lambda request: json_response({"data": {"web": []}})
        )
        try:
            await provider.search(
                "q", allowed_domains=("a.com",), blocked_domains=("b.com",)
            )
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["sources"] == ["web"]
        assert body["includeDomains"] == ["a.com"]
        assert "excludeDomains" not in body  # mutually exclusive upstream

    @pytest.mark.asyncio
    async def test_linkup_output_type(self) -> None:
        provider = LinkupWebSearchProvider(build_config())
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["outputType"] == "searchResults"
        assert body["depth"] == "standard"

    @pytest.mark.asyncio
    async def test_perplexity_blocked_domains_use_deny_prefix(self) -> None:
        provider = PerplexityWebSearchProvider(build_config())
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search("q", blocked_domains=("b.com", "c.com"))
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["search_domain_filter"] == ["-b.com", "-c.com"]

    @pytest.mark.asyncio
    async def test_parallel_beta_header_and_objective(self) -> None:
        provider = ParallelWebSearchProvider(build_config())
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search("find docs", max_results=4)
        finally:
            await provider.close()
        assert requests[0].headers["parallel-beta"] == "search-excerpt-2025-10-10"
        body = request_json_body(requests[0])
        assert body["objective"] == "find docs"
        assert body["search_queries"] == ["find docs"]
        assert body["max_results"] == 4

    @pytest.mark.asyncio
    async def test_jina_accept_header_and_query_in_path(self) -> None:
        provider = JinaWebSearchProvider(build_config())
        requests = attach_mock_client(
            provider, lambda request: json_response({"data": []})
        )
        try:
            await provider.search("hello world")
        finally:
            await provider.close()
        assert requests[0].headers["Accept"] == "application/json"
        assert "hello%20world" in str(requests[0].url)

    @pytest.mark.asyncio
    async def test_jina_truncates_snippet_keeps_full_content(self) -> None:
        long_text = "x" * 1500
        provider = JinaWebSearchProvider(build_config())
        attach_mock_client(
            provider,
            lambda request: json_response(
                {"data": [{"title": "T", "url": "https://a.io", "content": long_text}]}
            ),
        )
        try:
            response = await provider.search("q")
        finally:
            await provider.close()
        item = response.results[0]
        assert len(item.snippet) == 1000
        assert item.content == long_text

    @pytest.mark.asyncio
    async def test_brave_query_params(self) -> None:
        provider = BraveWebSearchProvider(build_config())
        requests = attach_mock_client(
            provider, lambda request: json_response({"web": {"results": []}})
        )
        try:
            await provider.search("q here", max_results=50)
        finally:
            await provider.close()
        assert requests[0].url.params["q"] == "q here"
        assert requests[0].url.params["count"] == "20"  # provider cap

    @pytest.mark.asyncio
    async def test_searxng_format_json_param(self) -> None:
        provider = SearxngWebSearchProvider(
            build_config(api_keys=(), rotation="single", base_url="https://sx.test/")
        )
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        assert requests[0].url.params["format"] == "json"
        assert requests[0].url.params["q"] == "q"
        assert str(requests[0].url).startswith("https://sx.test/search")


class TestProviderErrorQuirks:
    @pytest.mark.asyncio
    async def test_tavily_432_maps_to_quota(self) -> None:
        provider = TavilyWebSearchProvider(build_config())
        attach_mock_client(
            provider,
            lambda request: json_response(
                {"detail": {"error": "plan usage limit exceeded"}}, status=432
            ),
        )
        try:
            with pytest.raises(WebSearchQuotaError, match="plan usage limit"):
                await provider.search("q")
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_perplexity_451_maps_to_auth(self) -> None:
        provider = PerplexityWebSearchProvider(build_config())
        attach_mock_client(
            provider,
            lambda request: json_response(
                {"error": "api_key_created_before_search_api_cutoff"}, status=451
            ),
        )
        try:
            with pytest.raises(WebSearchAuthError, match="cutoff"):
                await provider.search("q")
        finally:
            await provider.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "adapter_cls", [SearchApiWebSearchProvider, SerpApiWebSearchProvider]
    )
    async def test_serp_200_with_error_body_raises_upstream(self, adapter_cls) -> None:
        provider = adapter_cls(build_config())
        attach_mock_client(
            provider, lambda request: json_response({"error": "Invalid API key"})
        )
        try:
            with pytest.raises(WebSearchUpstreamError, match="Invalid API key"):
                await provider.search("q")
        finally:
            await provider.close()

    def test_searxng_missing_base_url_is_config_error(self) -> None:
        with pytest.raises(WebSearchConfigError, match="SEARXNG_BASE_URL"):
            SearxngWebSearchProvider(build_config(api_keys=(), rotation="single"))

    @pytest.mark.asyncio
    async def test_searxng_403_carries_format_json_hint(self) -> None:
        provider = SearxngWebSearchProvider(
            build_config(api_keys=(), rotation="single", base_url="https://sx.test")
        )
        attach_mock_client(
            provider, lambda request: json_response({"error": "forbidden"}, status=403)
        )
        try:
            with pytest.raises(WebSearchAuthError, match="format=json"):
                await provider.search("q")
        finally:
            await provider.close()


class TestDdgsAdapter:
    @pytest.mark.asyncio
    async def test_ddgs_maps_href_and_body(self, monkeypatch) -> None:
        captured: dict = {}

        class FakeDDGS:
            def __init__(self, proxy=None, timeout=None) -> None:
                captured["proxy"] = proxy
                captured["timeout"] = timeout

            def text(self, query, **kwargs):
                captured["query"] = query
                captured["max_results"] = kwargs.get("max_results")
                return [{"title": "T", "href": "https://a.io/x", "body": "S"}]

        monkeypatch.setattr("free_claude_code.websearch.adapters.ddgs.DDGS", FakeDDGS)
        provider = DdgsWebSearchProvider(
            build_config(
                api_keys=(),
                rotation="single",
                proxy="http://proxy.test:8080",
                http_timeout=15.0,
            )
        )
        response = await provider.search("hello", max_results=3)
        assert captured == {
            "proxy": "http://proxy.test:8080",
            "timeout": 15,
            "query": "hello",
            "max_results": 3,
        }
        item = response.results[0]
        assert (item.title, item.url, item.snippet) == ("T", "https://a.io/x", "S")
        assert response.key_index == 0

    @pytest.mark.asyncio
    async def test_ddgs_ratelimit_maps_to_rate_limit_error(self, monkeypatch) -> None:
        from ddgs.exceptions import RatelimitException

        class FakeDDGS:
            def __init__(self, proxy=None, timeout=None) -> None:
                pass

            def text(self, query, **kwargs):
                raise RatelimitException("429 from engine")

        monkeypatch.setattr("free_claude_code.websearch.adapters.ddgs.DDGS", FakeDDGS)
        provider = DdgsWebSearchProvider(build_config(api_keys=(), rotation="single"))
        with pytest.raises(WebSearchRateLimitError):
            await provider.search("q")

    @pytest.mark.asyncio
    async def test_ddgs_generic_failure_maps_to_upstream(self, monkeypatch) -> None:
        from ddgs.exceptions import DDGSException

        class FakeDDGS:
            def __init__(self, proxy=None, timeout=None) -> None:
                pass

            def text(self, query, **kwargs):
                raise DDGSException("engine exploded")

        monkeypatch.setattr("free_claude_code.websearch.adapters.ddgs.DDGS", FakeDDGS)
        provider = DdgsWebSearchProvider(build_config(api_keys=(), rotation="single"))
        with pytest.raises(WebSearchUpstreamError, match="engine exploded"):
            await provider.search("q")


class TestRotationAcrossKeysEndToEnd:
    @pytest.mark.asyncio
    async def test_second_key_serves_after_first_key_auth_failure(self) -> None:
        seen_keys: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            key = request.headers.get("x-api-key")
            seen_keys.append(key)
            if key == "k1-aaaa1111bbbb":
                return json_response({"error": "bad"}, status=401)
            return json_response(
                {"results": [{"title": "T", "url": "https://a.io", "text": "S"}]}
            )

        provider = ExaWebSearchProvider(
            build_config(api_keys=("k1-aaaa1111bbbb", "k2-cccc2222dddd"))
        )
        attach_mock_client(provider, handler)
        try:
            response = await provider.search("q")
        finally:
            await provider.close()
        assert seen_keys == ["k1-aaaa1111bbbb", "k2-cccc2222dddd"]
        assert response.key_index == 1
