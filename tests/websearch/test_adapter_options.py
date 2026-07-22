"""Advanced option plumbing: payload/header/param changes per option.

Default behavior (all options empty) is frozen by test_adapters.py; these tests
cover only the opt-in paths.
"""

import pytest

from free_claude_code.websearch.adapters.brave import BraveWebSearchProvider
from free_claude_code.websearch.adapters.ddgs import DdgsWebSearchProvider
from free_claude_code.websearch.adapters.exa import ExaWebSearchProvider
from free_claude_code.websearch.adapters.firecrawl import FirecrawlWebSearchProvider
from free_claude_code.websearch.adapters.jina import JinaWebSearchProvider
from free_claude_code.websearch.adapters.linkup import LinkupWebSearchProvider
from free_claude_code.websearch.adapters.parallel import ParallelWebSearchProvider
from free_claude_code.websearch.adapters.perplexity import PerplexityWebSearchProvider
from free_claude_code.websearch.adapters.searchapi import SearchApiWebSearchProvider
from free_claude_code.websearch.adapters.searxng import SearxngWebSearchProvider
from free_claude_code.websearch.adapters.serpapi import SerpApiWebSearchProvider
from free_claude_code.websearch.adapters.serper import SerperWebSearchProvider
from free_claude_code.websearch.adapters.tavily import TavilyWebSearchProvider
from tests.websearch.support import (
    attach_mock_client,
    build_config,
    json_response,
    request_json_body,
)


class TestExaOptions:
    @pytest.mark.asyncio
    async def test_defaults_send_no_option_keys(self) -> None:
        provider = ExaWebSearchProvider(build_config())
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["contents"] == {"highlights": True}
        for key in (
            "type",
            "category",
            "startPublishedDate",
            "endPublishedDate",
            "userLocation",
        ):
            assert key not in body

    @pytest.mark.asyncio
    async def test_search_type_contents_dates_and_location(self) -> None:
        provider = ExaWebSearchProvider(
            build_config(
                options={
                    "EXA_SEARCH_TYPE": "deep",
                    "EXA_CONTENTS": "full",
                    "EXA_MAX_AGE_HOURS": "0",
                    "EXA_START_PUBLISHED_DATE": "2026-01-01T00:00:00Z",
                    "EXA_END_PUBLISHED_DATE": "2026-02-01T00:00:00Z",
                    "EXA_USER_LOCATION": "de",
                }
            )
        )
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["type"] == "deep"
        assert body["contents"] == {
            "highlights": True,
            "text": True,
            "summary": True,
            "maxAgeHours": 0,
        }
        assert body["startPublishedDate"] == "2026-01-01T00:00:00Z"
        assert body["endPublishedDate"] == "2026-02-01T00:00:00Z"
        assert body["userLocation"] == "de"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            ("highlights", {"highlights": True}),
            ("text", {"text": True}),
            ("highlights+text", {"highlights": True, "text": True}),
            ("highlights+summary", {"highlights": True, "summary": True}),
            ("bogus", {"highlights": True}),
        ],
    )
    async def test_contents_modes(self, mode: str, expected: dict) -> None:
        provider = ExaWebSearchProvider(build_config(options={"EXA_CONTENTS": mode}))
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        assert request_json_body(requests[0])["contents"] == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("category", ["company", "people"])
    async def test_company_people_category_skips_date_and_exclude(
        self, category: str
    ) -> None:
        provider = ExaWebSearchProvider(
            build_config(
                options={
                    "EXA_CATEGORY": category,
                    "EXA_START_PUBLISHED_DATE": "2026-01-01T00:00:00Z",
                    "EXA_END_PUBLISHED_DATE": "2026-02-01T00:00:00Z",
                }
            )
        )
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search(
                "q", allowed_domains=("a.com",), blocked_domains=("b.com",)
            )
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["category"] == category
        assert body["includeDomains"] == ["a.com"]
        assert "excludeDomains" not in body
        assert "startPublishedDate" not in body
        assert "endPublishedDate" not in body

    @pytest.mark.asyncio
    async def test_news_category_keeps_date_filters(self) -> None:
        provider = ExaWebSearchProvider(
            build_config(
                options={
                    "EXA_CATEGORY": "news",
                    "EXA_START_PUBLISHED_DATE": "2026-01-01T00:00:00Z",
                }
            )
        )
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search("q", blocked_domains=("b.com",))
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["startPublishedDate"] == "2026-01-01T00:00:00Z"
        assert body["excludeDomains"] == ["b.com"]

    @pytest.mark.asyncio
    async def test_summary_fills_snippet_and_content(self) -> None:
        provider = ExaWebSearchProvider(
            build_config(options={"EXA_CONTENTS": "highlights+summary"})
        )
        attach_mock_client(
            provider,
            lambda request: json_response(
                {"results": [{"title": "T", "url": "https://a.io", "summary": "SUM"}]}
            ),
        )
        try:
            response = await provider.search("q")
        finally:
            await provider.close()
        item = response.results[0]
        assert item.snippet == "SUM"
        assert item.content == "SUM"


class TestBraveOptions:
    @pytest.mark.asyncio
    async def test_web_mode_option_params(self) -> None:
        provider = BraveWebSearchProvider(
            build_config(
                options={
                    "BRAVE_SEARCH_MODE": "web",
                    "BRAVE_EXTRA_SNIPPETS": "true",
                    "BRAVE_FRESHNESS": "pw",
                    "BRAVE_COUNTRY": "de",
                    "BRAVE_SEARCH_LANG": "de",
                }
            )
        )
        requests = attach_mock_client(
            provider, lambda request: json_response({"web": {"results": []}})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        params = requests[0].url.params
        assert "/res/v1/web/search" in str(requests[0].url)
        assert params["extra_snippets"] == "true"
        assert params["freshness"] == "pw"
        assert params["country"] == "de"
        assert params["search_lang"] == "de"

    @pytest.mark.asyncio
    async def test_extra_snippets_become_content(self) -> None:
        provider = BraveWebSearchProvider(
            build_config(options={"BRAVE_EXTRA_SNIPPETS": "on"})
        )
        attach_mock_client(
            provider,
            lambda request: json_response(
                {
                    "web": {
                        "results": [
                            {
                                "title": "T",
                                "url": "https://a.io",
                                "description": "D",
                                "extra_snippets": ["e1", "e2"],
                            }
                        ]
                    }
                }
            ),
        )
        try:
            response = await provider.search("q")
        finally:
            await provider.close()
        item = response.results[0]
        assert item.snippet == "D"
        assert item.content == "e1\n\ne2"

    @pytest.mark.asyncio
    async def test_llm_context_mode_endpoint_and_mapping(self) -> None:
        provider = BraveWebSearchProvider(
            build_config(
                options={
                    "BRAVE_SEARCH_MODE": "llm-context",
                    "BRAVE_LLM_MAX_TOKENS": "4096",
                    "BRAVE_FRESHNESS": "pd",
                }
            )
        )
        requests = attach_mock_client(
            provider,
            lambda request: json_response(
                {
                    "grounding": {
                        "generic": [
                            {
                                "url": "https://a.io",
                                "title": "T",
                                "snippets": ["s1", "s2"],
                            }
                        ]
                    }
                }
            ),
        )
        try:
            response = await provider.search("q", max_results=7)
        finally:
            await provider.close()
        request = requests[0]
        assert request.method == "POST"
        assert "/res/v1/llm/context" in str(request.url)
        body = request_json_body(request)
        assert body["q"] == "q"
        assert body["maximum_number_of_urls"] == 7
        assert body["maximum_number_of_tokens"] == 4096
        assert body["freshness"] == "pd"
        item = response.results[0]
        assert (item.title, item.url) == ("T", "https://a.io")
        assert item.snippet == "s1\n\ns2"
        assert item.content == "s1\n\ns2"

    @pytest.mark.asyncio
    async def test_llm_context_defaults_omit_token_budget(self) -> None:
        provider = BraveWebSearchProvider(
            build_config(options={"BRAVE_SEARCH_MODE": "llm-context"})
        )
        requests = attach_mock_client(
            provider, lambda request: json_response({"grounding": {"generic": []}})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert "maximum_number_of_tokens" not in body
        assert "freshness" not in body


class TestTavilyOptions:
    @pytest.mark.asyncio
    async def test_defaults_send_no_option_keys(self) -> None:
        provider = TavilyWebSearchProvider(build_config())
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["search_depth"] == "basic"
        for key in ("topic", "time_range", "include_answer", "include_raw_content"):
            assert key not in body
        assert "auto_parameters" not in body

    @pytest.mark.asyncio
    async def test_depth_topic_time_range_answer_and_raw_content(self) -> None:
        provider = TavilyWebSearchProvider(
            build_config(
                options={
                    "TAVILY_SEARCH_DEPTH": "advanced",
                    "TAVILY_TOPIC": "news",
                    "TAVILY_TIME_RANGE": "week",
                    "TAVILY_INCLUDE_ANSWER": "basic",
                    "TAVILY_INCLUDE_RAW_CONTENT": "markdown",
                }
            )
        )
        requests = attach_mock_client(
            provider,
            lambda request: json_response(
                {
                    "answer": "The answer.",
                    "results": [
                        {
                            "title": "T",
                            "url": "https://a.io",
                            "content": "S",
                            "raw_content": "RAW",
                        }
                    ],
                }
            ),
        )
        try:
            response = await provider.search("q")
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["search_depth"] == "advanced"
        assert body["topic"] == "news"
        assert body["time_range"] == "week"
        assert body["include_answer"] == "basic"
        assert body["include_raw_content"] == "markdown"
        assert response.answer == "The answer."
        assert response.results[0].content == "RAW"

    @pytest.mark.asyncio
    async def test_no_answer_requested_leaves_answer_none(self) -> None:
        provider = TavilyWebSearchProvider(build_config())
        attach_mock_client(
            provider,
            lambda request: json_response({"answer": "", "results": []}),
        )
        try:
            response = await provider.search("q")
        finally:
            await provider.close()
        assert response.answer is None


class TestSerperOptions:
    @pytest.mark.asyncio
    async def test_gl_hl_tbs_params(self) -> None:
        provider = SerperWebSearchProvider(
            build_config(
                options={"SERPER_GL": "de", "SERPER_HL": "de", "SERPER_TBS": "qdr:w"}
            )
        )
        requests = attach_mock_client(
            provider, lambda request: json_response({"organic": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["gl"] == "de"
        assert body["hl"] == "de"
        assert body["tbs"] == "qdr:w"

    @pytest.mark.asyncio
    async def test_rich_blocks_on_by_default(self) -> None:
        provider = SerperWebSearchProvider(build_config())
        attach_mock_client(
            provider,
            lambda request: json_response(
                {
                    "answerBox": {"answer": "42"},
                    "knowledgeGraph": {"title": "Answer", "description": "The number."},
                    "peopleAlsoAsk": [{"question": "Why?", "snippet": "Because."}],
                    "organic": [{"title": "T", "link": "https://a.io", "snippet": "S"}],
                }
            ),
        )
        try:
            response = await provider.search("q")
        finally:
            await provider.close()
        assert response.answer == ("42\n\nAnswer: The number.\n\nQ: Why?\nA: Because.")

    @pytest.mark.asyncio
    async def test_rich_blocks_disabled(self) -> None:
        provider = SerperWebSearchProvider(
            build_config(options={"SERPER_RICH_BLOCKS": "false"})
        )
        attach_mock_client(
            provider,
            lambda request: json_response(
                {
                    "answerBox": {"answer": "42"},
                    "organic": [{"title": "T", "link": "https://a.io", "snippet": "S"}],
                }
            ),
        )
        try:
            response = await provider.search("q")
        finally:
            await provider.close()
        assert response.answer is None


class TestLinkupOptions:
    @pytest.mark.asyncio
    async def test_depth_and_sourced_answer(self) -> None:
        provider = LinkupWebSearchProvider(
            build_config(
                options={"LINKUP_DEPTH": "deep", "LINKUP_OUTPUT_TYPE": "sourcedAnswer"}
            )
        )
        requests = attach_mock_client(
            provider,
            lambda request: json_response(
                {
                    "answer": "Sourced answer text.",
                    "sources": [
                        {"name": "T", "url": "https://a.io", "snippet": "SNIP"}
                    ],
                }
            ),
        )
        try:
            response = await provider.search("q")
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["depth"] == "deep"
        assert body["outputType"] == "sourcedAnswer"
        assert response.answer == "Sourced answer text."
        item = response.results[0]
        assert (item.title, item.url, item.snippet) == ("T", "https://a.io", "SNIP")

    @pytest.mark.asyncio
    async def test_defaults_unchanged(self) -> None:
        provider = LinkupWebSearchProvider(build_config())
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["depth"] == "standard"
        assert body["outputType"] == "searchResults"


class TestPerplexityOptions:
    @pytest.mark.asyncio
    async def test_recency_and_context_size(self) -> None:
        provider = PerplexityWebSearchProvider(
            build_config(
                options={
                    "PERPLEXITY_SEARCH_RECENCY": "week",
                    "PERPLEXITY_CONTEXT_SIZE": "low",
                }
            )
        )
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["search_recency_filter"] == "week"
        assert body["search_context_size"] == "low"
        assert "max_tokens_per_page" not in body

    @pytest.mark.asyncio
    async def test_max_tokens_per_page_supersedes_context_size(self) -> None:
        provider = PerplexityWebSearchProvider(
            build_config(
                options={
                    "PERPLEXITY_CONTEXT_SIZE": "high",
                    "PERPLEXITY_MAX_TOKENS_PER_PAGE": "512",
                }
            )
        )
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["max_tokens_per_page"] == 512
        assert "search_context_size" not in body


class TestParallelOptions:
    @pytest.mark.asyncio
    async def test_mode_and_excerpt_budgets(self) -> None:
        provider = ParallelWebSearchProvider(
            build_config(
                options={
                    "PARALLEL_MODE": "turbo",
                    "PARALLEL_EXCERPT_CHARS": "1500",
                    "PARALLEL_TOTAL_CHARS": "6000",
                }
            )
        )
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["mode"] == "turbo"
        assert body["excerpts"] == {"max_chars_per_result": 1500}
        assert body["max_chars_total"] == 6000

    @pytest.mark.asyncio
    async def test_defaults_send_no_option_keys(self) -> None:
        provider = ParallelWebSearchProvider(build_config())
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        for key in ("mode", "excerpts", "max_chars_total"):
            assert key not in body


class TestFirecrawlOptions:
    @pytest.mark.asyncio
    async def test_sources_tbs_location(self) -> None:
        provider = FirecrawlWebSearchProvider(
            build_config(
                options={
                    "FIRECRAWL_SOURCES": "web,news",
                    "FIRECRAWL_TBS": "qdr:d",
                    "FIRECRAWL_LOCATION": "San Francisco,California,United States",
                }
            )
        )
        requests = attach_mock_client(
            provider,
            lambda request: json_response(
                {
                    "data": {
                        "web": [
                            {"title": "W", "url": "https://a.io", "description": "D"}
                        ],
                        "news": [
                            {
                                "title": "N",
                                "url": "https://b.io",
                                "snippet": "NS",
                                "date": "2026-01-02",
                            }
                        ],
                    }
                }
            ),
        )
        try:
            response = await provider.search("q")
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["sources"] == ["web", "news"]
        assert body["tbs"] == "qdr:d"
        assert body["location"] == "San Francisco,California,United States"
        web_item, news_item = response.results
        assert web_item.title == "W"
        assert (news_item.title, news_item.snippet, news_item.published) == (
            "N",
            "NS",
            "2026-01-02",
        )

    @pytest.mark.asyncio
    async def test_scrape_format_summary_upgrades_snippet(self) -> None:
        provider = FirecrawlWebSearchProvider(
            build_config(options={"FIRECRAWL_SCRAPE_FORMAT": "summary"})
        )
        requests = attach_mock_client(
            provider,
            lambda request: json_response(
                {
                    "data": {
                        "web": [
                            {
                                "title": "T",
                                "url": "https://a.io",
                                "description": "D",
                                "summary": "SUM",
                            }
                        ]
                    }
                }
            ),
        )
        try:
            response = await provider.search("q")
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["scrapeOptions"] == {"formats": [{"type": "summary"}]}
        assert response.results[0].snippet == "SUM"

    @pytest.mark.asyncio
    async def test_scrape_format_markdown_fills_content(self) -> None:
        provider = FirecrawlWebSearchProvider(
            build_config(options={"FIRECRAWL_SCRAPE_FORMAT": "markdown"})
        )
        requests = attach_mock_client(
            provider,
            lambda request: json_response(
                {
                    "data": {
                        "web": [
                            {
                                "title": "T",
                                "url": "https://a.io",
                                "description": "D",
                                "markdown": "MD",
                            }
                        ]
                    }
                }
            ),
        )
        try:
            response = await provider.search("q")
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["scrapeOptions"] == {"formats": [{"type": "markdown"}]}
        item = response.results[0]
        assert item.snippet == "D"
        assert item.content == "MD"

    @pytest.mark.asyncio
    async def test_defaults_unchanged(self) -> None:
        provider = FirecrawlWebSearchProvider(build_config())
        requests = attach_mock_client(
            provider, lambda request: json_response({"data": {"web": []}})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        body = request_json_body(requests[0])
        assert body["sources"] == ["web"]
        for key in ("scrapeOptions", "tbs", "location"):
            assert key not in body


class TestJinaOptions:
    @pytest.mark.asyncio
    async def test_max_tokens_header_and_site_gl_params(self) -> None:
        provider = JinaWebSearchProvider(
            build_config(
                options={
                    "JINA_MAX_TOKENS": "2000",
                    "JINA_SITE": "a.io",
                    "JINA_GL": "us",
                }
            )
        )
        requests = attach_mock_client(
            provider, lambda request: json_response({"data": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        request = requests[0]
        assert request.headers["X-Max-Tokens"] == "2000"
        assert request.url.params["site"] == "a.io"
        assert request.url.params["gl"] == "us"

    @pytest.mark.asyncio
    async def test_defaults_send_no_extras(self) -> None:
        provider = JinaWebSearchProvider(build_config())
        requests = attach_mock_client(
            provider, lambda request: json_response({"data": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        request = requests[0]
        assert "X-Max-Tokens" not in request.headers
        assert not request.url.params


class TestSearxngOptions:
    @pytest.mark.asyncio
    async def test_engines_categories_time_range_language(self) -> None:
        provider = SearxngWebSearchProvider(
            build_config(
                api_keys=(),
                rotation="single",
                base_url="https://sx.test",
                options={
                    "SEARXNG_ENGINES": "google,bing",
                    "SEARXNG_CATEGORIES": "general",
                    "SEARXNG_TIME_RANGE": "month",
                    "SEARXNG_LANGUAGE": "de",
                },
            )
        )
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        params = requests[0].url.params
        assert params["engines"] == "google,bing"
        assert params["categories"] == "general"
        assert params["time_range"] == "month"
        assert params["language"] == "de"
        assert params["format"] == "json"

    @pytest.mark.asyncio
    async def test_defaults_send_only_q_and_format(self) -> None:
        provider = SearxngWebSearchProvider(
            build_config(api_keys=(), rotation="single", base_url="https://sx.test")
        )
        requests = attach_mock_client(
            provider, lambda request: json_response({"results": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        assert dict(requests[0].url.params) == {"q": "q", "format": "json"}


class TestDdgsOptions:
    @pytest.mark.asyncio
    async def test_backend_region_timelimit_safesearch(self, monkeypatch) -> None:
        captured: dict = {}

        class FakeDDGS:
            def __init__(self, proxy=None, timeout=None) -> None:
                pass

            def text(self, query, **kwargs):
                captured.update(kwargs)
                return []

        monkeypatch.setattr("free_claude_code.websearch.adapters.ddgs.DDGS", FakeDDGS)
        provider = DdgsWebSearchProvider(
            build_config(
                api_keys=(),
                rotation="single",
                options={
                    "DDGS_BACKEND": "bing",
                    "DDGS_REGION": "uk-en",
                    "DDGS_TIMELIMIT": "w",
                    "DDGS_SAFESEARCH": "off",
                },
            )
        )
        await provider.search("q", max_results=3)
        assert captured == {
            "max_results": 3,
            "backend": "bing",
            "region": "uk-en",
            "timelimit": "w",
            "safesearch": "off",
        }

    @pytest.mark.asyncio
    async def test_defaults_send_no_option_kwargs(self, monkeypatch) -> None:
        captured: dict = {}

        class FakeDDGS:
            def __init__(self, proxy=None, timeout=None) -> None:
                pass

            def text(self, query, **kwargs):
                captured.update(kwargs)
                return []

        monkeypatch.setattr("free_claude_code.websearch.adapters.ddgs.DDGS", FakeDDGS)
        provider = DdgsWebSearchProvider(build_config(api_keys=(), rotation="single"))
        await provider.search("q", max_results=3)
        assert captured == {"max_results": 3}


class TestSearchApiOptions:
    @pytest.mark.asyncio
    async def test_engine_time_gl_hl_params(self) -> None:
        provider = SearchApiWebSearchProvider(
            build_config(
                options={
                    "SEARCHAPI_ENGINE": "google_scholar",
                    "SEARCHAPI_TIME_PERIOD": "last_week",
                    "SEARCHAPI_GL": "de",
                    "SEARCHAPI_HL": "de",
                }
            )
        )
        requests = attach_mock_client(
            provider, lambda request: json_response({"organic_results": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        params = requests[0].url.params
        assert params["engine"] == "google_scholar"
        assert params["time_period"] == "last_week"
        assert params["gl"] == "de"
        assert params["hl"] == "de"

    @pytest.mark.asyncio
    async def test_answer_box_and_knowledge_graph_become_answer(self) -> None:
        provider = SearchApiWebSearchProvider(build_config())
        attach_mock_client(
            provider,
            lambda request: json_response(
                {
                    "answer_box": {"answer": "AB"},
                    "knowledge_graph": {"title": "KG", "description": "Desc."},
                    "organic_results": [
                        {"title": "T", "link": "https://a.io", "snippet": "S"}
                    ],
                }
            ),
        )
        try:
            response = await provider.search("q")
        finally:
            await provider.close()
        assert response.answer == "AB\n\nKG: Desc."

    @pytest.mark.asyncio
    async def test_default_engine_is_google(self) -> None:
        provider = SearchApiWebSearchProvider(build_config())
        requests = attach_mock_client(
            provider, lambda request: json_response({"organic_results": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        assert requests[0].url.params["engine"] == "google"
        assert "time_period" not in requests[0].url.params


class TestSerpApiOptions:
    @pytest.mark.asyncio
    async def test_engine_tbs_gl_hl_params(self) -> None:
        provider = SerpApiWebSearchProvider(
            build_config(
                options={
                    "SERPAPI_ENGINE": "google_light",
                    "SERPAPI_TBS": "qdr:w",
                    "SERPAPI_GL": "de",
                    "SERPAPI_HL": "de",
                }
            )
        )
        requests = attach_mock_client(
            provider, lambda request: json_response({"organic_results": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        params = requests[0].url.params
        assert params["engine"] == "google_light"
        assert params["tbs"] == "qdr:w"
        assert params["gl"] == "de"
        assert params["hl"] == "de"

    @pytest.mark.asyncio
    async def test_answer_box_becomes_answer(self) -> None:
        provider = SerpApiWebSearchProvider(build_config())
        attach_mock_client(
            provider,
            lambda request: json_response(
                {
                    "answer_box": {"snippet": "AB"},
                    "organic_results": [
                        {"title": "T", "link": "https://a.io", "snippet": "S"}
                    ],
                }
            ),
        )
        try:
            response = await provider.search("q")
        finally:
            await provider.close()
        assert response.answer == "AB"

    @pytest.mark.asyncio
    async def test_default_engine_is_google(self) -> None:
        provider = SerpApiWebSearchProvider(build_config())
        requests = attach_mock_client(
            provider, lambda request: json_response({"organic_results": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        assert requests[0].url.params["engine"] == "google"
        assert "tbs" not in requests[0].url.params
