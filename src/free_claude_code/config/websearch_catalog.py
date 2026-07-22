"""Neutral web search provider catalog: IDs, credentials, defaults, capabilities.

Adapter classes live in :mod:`free_claude_code.websearch.adapters`; this module stays
free of adapter implementation imports (see contract tests). Insertion order of
``WEBSEARCH_CATALOG`` is the canonical display/auto-resolution order.
"""

from dataclasses import dataclass

OLLAMA_SEARCH_DEFAULT_BASE = "https://ollama.com"
EXA_DEFAULT_BASE = "https://api.exa.ai"
TAVILY_DEFAULT_BASE = "https://api.tavily.com"
BRAVE_SEARCH_DEFAULT_BASE = "https://api.search.brave.com"
JINA_SEARCH_DEFAULT_BASE = "https://s.jina.ai"
SERPER_DEFAULT_BASE = "https://google.serper.dev"
FIRECRAWL_DEFAULT_BASE = "https://api.firecrawl.dev"
LINKUP_DEFAULT_BASE = "https://api.linkup.so"
PERPLEXITY_SEARCH_DEFAULT_BASE = "https://api.perplexity.ai"
PARALLEL_DEFAULT_BASE = "https://api.parallel.ai"
SEARCHAPI_DEFAULT_BASE = "https://www.searchapi.io"
SERPAPI_DEFAULT_BASE = "https://serpapi.com"


@dataclass(frozen=True, slots=True)
class WebSearchDescriptor:
    """Metadata for building web search providers and admin/manifest wiring."""

    provider_id: str
    display_name: str
    credential_env: str | None  # None for keyless
    credential_url: str | None  # where to obtain a key
    settings_attr: str | None  # Settings attribute holding the key(s)
    default_base_url: str | None
    base_url_attr: str | None  # for self-hosted (searxng)
    requires_key: bool
    supports_domains: bool  # allowed/blocked domains passthrough
    free_tier: str  # short human note for UI
    notes: str


WEBSEARCH_CATALOG: dict[str, WebSearchDescriptor] = {
    "ddgs": WebSearchDescriptor(
        provider_id="ddgs",
        display_name="DuckDuckGo (ddgs)",
        credential_env=None,
        credential_url=None,
        settings_attr=None,
        default_base_url=None,
        base_url_attr=None,
        requires_key=False,
        supports_domains=False,
        free_tier="Free, keyless (unofficial metasearch)",
        notes="Uses the ddgs package (DDGS().text()); engines may IP-rate-limit.",
    ),
    "ollama": WebSearchDescriptor(
        provider_id="ollama",
        display_name="Ollama Web Search",
        credential_env="OLLAMA_SEARCH_API_KEY",
        credential_url="https://ollama.com/settings/keys",
        settings_attr="ollama_search_api_key",
        default_base_url=OLLAMA_SEARCH_DEFAULT_BASE,
        base_url_attr=None,
        requires_key=True,
        supports_domains=False,
        free_tier="Free hosted tier with a free Ollama account",
        notes="POST /api/web_search with Bearer auth; max 10 results per request.",
    ),
    "exa": WebSearchDescriptor(
        provider_id="exa",
        display_name="Exa",
        credential_env="EXA_API_KEY",
        credential_url="https://dashboard.exa.ai/api-keys",
        settings_attr="exa_api_key",
        default_base_url=EXA_DEFAULT_BASE,
        base_url_attr=None,
        requires_key=True,
        supports_domains=True,
        free_tier="$20 signup credit + $10/month free ongoing",
        notes="POST /search with x-api-key; snippets via contents.highlights opt-in.",
    ),
    "tavily": WebSearchDescriptor(
        provider_id="tavily",
        display_name="Tavily",
        credential_env="TAVILY_API_KEY",
        credential_url="https://app.tavily.com/home",
        settings_attr="tavily_api_key",
        default_base_url=TAVILY_DEFAULT_BASE,
        base_url_attr=None,
        requires_key=True,
        supports_domains=True,
        free_tier="1,000 credits/month free, no card",
        notes="POST /search with Bearer auth; HTTP 432 = plan usage limit.",
    ),
    "brave": WebSearchDescriptor(
        provider_id="brave",
        display_name="Brave Search",
        credential_env="BRAVE_SEARCH_API_KEY",
        credential_url="https://api-dashboard.search.brave.com/",
        settings_attr="brave_search_api_key",
        default_base_url=BRAVE_SEARCH_DEFAULT_BASE,
        base_url_attr=None,
        requires_key=True,
        supports_domains=False,
        free_tier="$5 in free credits every month",
        notes="GET /res/v1/web/search with X-Subscription-Token header.",
    ),
    "searxng": WebSearchDescriptor(
        provider_id="searxng",
        display_name="SearXNG (self-hosted)",
        credential_env=None,
        credential_url=None,
        settings_attr=None,
        default_base_url=None,
        base_url_attr="searxng_base_url",
        requires_key=False,
        supports_domains=False,
        free_tier="Free, self-hosted (AGPL)",
        notes="Needs SEARXNG_BASE_URL; instance must enable format=json in settings.yml.",
    ),
    "jina": WebSearchDescriptor(
        provider_id="jina",
        display_name="Jina Search",
        credential_env="JINA_API_KEY",
        credential_url="https://jina.ai/api-dashboard/",
        settings_attr="jina_api_key",
        default_base_url=JINA_SEARCH_DEFAULT_BASE,
        base_url_attr=None,
        requires_key=True,
        supports_domains=False,
        free_tier="10M free tokens for new keys",
        notes="GET s.jina.ai/{query} with Accept: application/json; key required.",
    ),
    "serper": WebSearchDescriptor(
        provider_id="serper",
        display_name="Serper (Google)",
        credential_env="SERPER_API_KEY",
        credential_url="https://serper.dev/api-key",
        settings_attr="serper_api_key",
        default_base_url=SERPER_DEFAULT_BASE,
        base_url_attr=None,
        requires_key=True,
        supports_domains=False,
        free_tier="2,500 free queries, one-time on signup",
        notes="POST /search with X-API-KEY header; Google SERP proxy.",
    ),
    "firecrawl": WebSearchDescriptor(
        provider_id="firecrawl",
        display_name="Firecrawl",
        credential_env="FIRECRAWL_API_KEY",
        credential_url="https://www.firecrawl.dev/app/api-keys",
        settings_attr="firecrawl_api_key",
        default_base_url=FIRECRAWL_DEFAULT_BASE,
        base_url_attr=None,
        requires_key=True,
        supports_domains=True,
        free_tier="One-time free credit grant on signup",
        notes="POST /v2/search with Bearer auth; web source only.",
    ),
    "linkup": WebSearchDescriptor(
        provider_id="linkup",
        display_name="Linkup",
        credential_env="LINKUP_API_KEY",
        credential_url="https://app.linkup.so/",
        settings_attr="linkup_api_key",
        default_base_url=LINKUP_DEFAULT_BASE,
        base_url_attr=None,
        requires_key=True,
        supports_domains=True,
        free_tier="$20 free credit, topped back up monthly",
        notes="POST /v1/search with Bearer auth; result title field is `name`.",
    ),
    "perplexity": WebSearchDescriptor(
        provider_id="perplexity",
        display_name="Perplexity Search",
        credential_env="PERPLEXITY_SEARCH_API_KEY",
        credential_url="https://www.perplexity.ai/settings/api",
        settings_attr="perplexity_search_api_key",
        default_base_url=PERPLEXITY_SEARCH_DEFAULT_BASE,
        base_url_attr=None,
        requires_key=True,
        supports_domains=True,
        free_tier="No meaningful free tier (prepaid credit)",
        notes="POST /search with Bearer auth; stale keys fail with HTTP 451.",
    ),
    "parallel": WebSearchDescriptor(
        provider_id="parallel",
        display_name="Parallel",
        credential_env="PARALLEL_API_KEY",
        credential_url="https://platform.parallel.ai/",
        settings_attr="parallel_api_key",
        default_base_url=PARALLEL_DEFAULT_BASE,
        base_url_attr=None,
        requires_key=True,
        supports_domains=False,
        free_tier="Pay-per-use from $0.005 per 10 results",
        notes="POST /v1beta/search with x-api-key + parallel-beta header (Search API beta).",
    ),
    "searchapi": WebSearchDescriptor(
        provider_id="searchapi",
        display_name="SearchAPI.io",
        credential_env="SEARCHAPI_API_KEY",
        credential_url="https://www.searchapi.io/",
        settings_attr="searchapi_api_key",
        default_base_url=SEARCHAPI_DEFAULT_BASE,
        base_url_attr=None,
        requires_key=True,
        supports_domains=False,
        free_tier="100 free requests, one-time",
        notes="GET /api/v1/search; api_key as query param.",
    ),
    "serpapi": WebSearchDescriptor(
        provider_id="serpapi",
        display_name="SerpAPI",
        credential_env="SERPAPI_API_KEY",
        credential_url="https://serpapi.com/manage-api-key",
        settings_attr="serpapi_api_key",
        default_base_url=SERPAPI_DEFAULT_BASE,
        base_url_attr=None,
        requires_key=True,
        supports_domains=False,
        free_tier="250 free searches/month",
        notes="GET /search; api_key as query param.",
    ),
}

# Insertion order is the canonical display order and the `auto` resolution order;
# ``SUPPORTED_WEBSEARCH_PROVIDER_IDS`` inherits it for UI and validation.
SUPPORTED_WEBSEARCH_PROVIDER_IDS: tuple[str, ...] = tuple(WEBSEARCH_CATALOG.keys())

if len(set(SUPPORTED_WEBSEARCH_PROVIDER_IDS)) != len(SUPPORTED_WEBSEARCH_PROVIDER_IDS):
    raise AssertionError("Duplicate provider ids in WEBSEARCH_CATALOG key order")
