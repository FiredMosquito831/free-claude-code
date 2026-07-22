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
class WebSearchOptionSpec:
    """One dotenv-only advanced option for a web search provider.

    Values are read from process env / dotenv (never pydantic Settings) by
    ``websearch.options.read_websearch_options`` and consumed verbatim by the
    provider adapter. Empty/unset values always reproduce default behavior.
    """

    env: str  # full env var name
    label: str  # short human label for UI
    field_type: str  # "select" | "text" | "number" | "boolean"
    default: str  # default/empty selection value ("" unless documented)
    options: tuple[tuple[str, str], ...] = ()  # (value, label) pairs for select
    cost_note: str = ""  # "" or a short cost/gating warning


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
    advanced_options: tuple[WebSearchOptionSpec, ...] = ()


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
        advanced_options=(
            WebSearchOptionSpec(
                env="DDGS_BACKEND",
                label="Backend engine",
                field_type="select",
                default="",
                options=(
                    ("", "auto (default)"),
                    ("bing", "bing"),
                    ("brave", "brave"),
                    ("duckduckgo", "duckduckgo"),
                    ("google", "google"),
                    ("mojeek", "mojeek"),
                    ("startpage", "startpage"),
                    ("yandex", "yandex"),
                    ("yahoo", "yahoo"),
                    ("wikipedia", "wikipedia"),
                ),
                cost_note="free; pin one engine to dodge per-engine rate limits",
            ),
            WebSearchOptionSpec(
                env="DDGS_REGION",
                label="Region (e.g. us-en)",
                field_type="text",
                default="",
            ),
            WebSearchOptionSpec(
                env="DDGS_TIMELIMIT",
                label="Time limit",
                field_type="select",
                default="",
                options=(
                    ("", "any time (default)"),
                    ("d", "past day"),
                    ("w", "past week"),
                    ("m", "past month"),
                    ("y", "past year"),
                ),
            ),
            WebSearchOptionSpec(
                env="DDGS_SAFESEARCH",
                label="SafeSearch",
                field_type="select",
                default="",
                options=(
                    ("", "moderate (default)"),
                    ("on", "on"),
                    ("moderate", "moderate"),
                    ("off", "off"),
                ),
            ),
        ),
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
        notes=(
            "POST /api/web_search with Bearer auth; max 10 results per request. "
            "No advanced options: the API exposes only query + max_results."
        ),
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
        advanced_options=(
            WebSearchOptionSpec(
                env="EXA_SEARCH_TYPE",
                label="Search type",
                field_type="select",
                default="",
                options=(
                    ("", "auto (default)"),
                    ("instant", "instant"),
                    ("fast", "fast"),
                    ("auto", "auto"),
                    ("deep-lite", "deep-lite"),
                    ("deep", "deep"),
                    ("deep-reasoning", "deep-reasoning"),
                ),
                cost_note="deep* = $0.015/query vs $0.005",
            ),
            WebSearchOptionSpec(
                env="EXA_CONTENTS",
                label="Contents mode",
                field_type="select",
                default="",
                options=(
                    ("", "highlights (default)"),
                    ("highlights", "highlights"),
                    ("text", "text"),
                    ("highlights+text", "highlights+text"),
                    ("highlights+summary", "highlights+summary"),
                    ("full", "full (text+highlights+summary)"),
                ),
                cost_note="+$0.001/page per content type",
            ),
            WebSearchOptionSpec(
                env="EXA_CATEGORY",
                label="Category vertical",
                field_type="select",
                default="",
                options=(
                    ("", "none (default)"),
                    ("company", "company"),
                    ("people", "people"),
                    ("research paper", "research paper"),
                    ("news", "news"),
                    ("personal site", "personal site"),
                    ("financial report", "financial report"),
                ),
                cost_note="company/people disable date+exclude filters",
            ),
            WebSearchOptionSpec(
                env="EXA_MAX_AGE_HOURS",
                label="Max content age in hours (0 = always fresh)",
                field_type="number",
                default="",
            ),
            WebSearchOptionSpec(
                env="EXA_START_PUBLISHED_DATE",
                label="Start published date (ISO 8601)",
                field_type="text",
                default="",
            ),
            WebSearchOptionSpec(
                env="EXA_END_PUBLISHED_DATE",
                label="End published date (ISO 8601)",
                field_type="text",
                default="",
            ),
            WebSearchOptionSpec(
                env="EXA_USER_LOCATION",
                label="User location (2-letter country)",
                field_type="text",
                default="",
            ),
        ),
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
        advanced_options=(
            WebSearchOptionSpec(
                env="TAVILY_SEARCH_DEPTH",
                label="Search depth",
                field_type="select",
                default="",
                options=(
                    ("", "basic (default)"),
                    ("basic", "basic"),
                    ("fast", "fast"),
                    ("ultra-fast", "ultra-fast"),
                    ("advanced", "advanced"),
                ),
                cost_note="advanced = 2 credits/query",
            ),
            WebSearchOptionSpec(
                env="TAVILY_TOPIC",
                label="Topic",
                field_type="select",
                default="",
                options=(
                    ("", "general (default)"),
                    ("general", "general"),
                    ("news", "news"),
                    ("finance", "finance"),
                ),
            ),
            WebSearchOptionSpec(
                env="TAVILY_TIME_RANGE",
                label="Time range",
                field_type="select",
                default="",
                options=(
                    ("", "any time (default)"),
                    ("day", "day"),
                    ("week", "week"),
                    ("month", "month"),
                    ("year", "year"),
                ),
            ),
            WebSearchOptionSpec(
                env="TAVILY_INCLUDE_ANSWER",
                label="Include LLM answer",
                field_type="select",
                default="",
                options=(
                    ("", "off (default)"),
                    ("basic", "basic"),
                    ("advanced", "advanced"),
                ),
            ),
            WebSearchOptionSpec(
                env="TAVILY_INCLUDE_RAW_CONTENT",
                label="Include raw content",
                field_type="select",
                default="",
                options=(
                    ("", "off (default)"),
                    ("markdown", "markdown"),
                    ("text", "text"),
                ),
                cost_note="free (may add latency)",
            ),
        ),
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
        advanced_options=(
            WebSearchOptionSpec(
                env="BRAVE_SEARCH_MODE",
                label="Search mode",
                field_type="select",
                default="",
                options=(
                    ("", "web (default)"),
                    ("web", "web"),
                    ("llm-context", "llm-context"),
                ),
                cost_note="llm-context: $5/1k, returns pre-extracted page text",
            ),
            WebSearchOptionSpec(
                env="BRAVE_EXTRA_SNIPPETS",
                label="Extra snippets (web mode)",
                field_type="boolean",
                default="",
                cost_note="plan-gated",
            ),
            WebSearchOptionSpec(
                env="BRAVE_FRESHNESS",
                label="Freshness",
                field_type="select",
                default="",
                options=(
                    ("", "any time (default)"),
                    ("pd", "past day"),
                    ("pw", "past week"),
                    ("pm", "past month"),
                    ("py", "past year"),
                ),
            ),
            WebSearchOptionSpec(
                env="BRAVE_COUNTRY",
                label="Country (2-letter code, web mode)",
                field_type="text",
                default="",
            ),
            WebSearchOptionSpec(
                env="BRAVE_SEARCH_LANG",
                label="Search language (ISO 639-1, web mode)",
                field_type="text",
                default="",
            ),
            WebSearchOptionSpec(
                env="BRAVE_LLM_MAX_TOKENS",
                label="LLM context max tokens (1024-32768)",
                field_type="number",
                default="",
                cost_note="llm-context mode only",
            ),
        ),
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
        advanced_options=(
            WebSearchOptionSpec(
                env="SEARXNG_ENGINES",
                label="Engines (comma list)",
                field_type="text",
                default="",
            ),
            WebSearchOptionSpec(
                env="SEARXNG_CATEGORIES",
                label="Categories (comma list)",
                field_type="text",
                default="",
            ),
            WebSearchOptionSpec(
                env="SEARXNG_TIME_RANGE",
                label="Time range",
                field_type="select",
                default="",
                options=(
                    ("", "any time (default)"),
                    ("day", "day"),
                    ("month", "month"),
                    ("year", "year"),
                ),
            ),
            WebSearchOptionSpec(
                env="SEARXNG_LANGUAGE",
                label="Language code (or all)",
                field_type="text",
                default="",
            ),
        ),
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
        advanced_options=(
            WebSearchOptionSpec(
                env="JINA_MAX_TOKENS",
                label="Max tokens per response (X-Max-Tokens)",
                field_type="number",
                default="",
                cost_note="token-billed; best cost guardrail",
            ),
            WebSearchOptionSpec(
                env="JINA_SITE",
                label="Site filter (domain)",
                field_type="text",
                default="",
            ),
            WebSearchOptionSpec(
                env="JINA_GL",
                label="Geo country code",
                field_type="text",
                default="",
            ),
        ),
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
        advanced_options=(
            WebSearchOptionSpec(
                env="SERPER_GL",
                label="Country (2-letter code)",
                field_type="text",
                default="",
            ),
            WebSearchOptionSpec(
                env="SERPER_HL",
                label="Language code",
                field_type="text",
                default="",
            ),
            WebSearchOptionSpec(
                env="SERPER_TBS",
                label="Date filter (e.g. qdr:w)",
                field_type="text",
                default="",
            ),
            WebSearchOptionSpec(
                env="SERPER_RICH_BLOCKS",
                label="Rich blocks -> answer (answerBox/knowledgeGraph/peopleAlsoAsk)",
                field_type="boolean",
                default="true",
            ),
        ),
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
        advanced_options=(
            WebSearchOptionSpec(
                env="FIRECRAWL_SOURCES",
                label="Sources",
                field_type="select",
                default="",
                options=(
                    ("", "web (default)"),
                    ("web", "web"),
                    ("web,news", "web,news"),
                    ("web,news,images", "web,news,images"),
                ),
            ),
            WebSearchOptionSpec(
                env="FIRECRAWL_SCRAPE_FORMAT",
                label="Scrape format per result",
                field_type="select",
                default="",
                options=(
                    ("", "off (default)"),
                    ("summary", "summary"),
                    ("markdown", "markdown"),
                ),
                cost_note="multiplies credits per result",
            ),
            WebSearchOptionSpec(
                env="FIRECRAWL_TBS",
                label="Date filter (e.g. qdr:d)",
                field_type="text",
                default="",
            ),
            WebSearchOptionSpec(
                env="FIRECRAWL_LOCATION",
                label="Location (free text)",
                field_type="text",
                default="",
            ),
        ),
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
        advanced_options=(
            WebSearchOptionSpec(
                env="LINKUP_DEPTH",
                label="Depth",
                field_type="select",
                default="",
                options=(
                    ("", "standard (default)"),
                    ("standard", "standard"),
                    ("deep", "deep"),
                ),
                cost_note="deep = 10x ($0.05/query)",
            ),
            WebSearchOptionSpec(
                env="LINKUP_OUTPUT_TYPE",
                label="Output type",
                field_type="select",
                default="",
                options=(
                    ("", "searchResults (default)"),
                    ("searchResults", "searchResults"),
                    ("sourcedAnswer", "sourcedAnswer"),
                ),
                cost_note="sourcedAnswer = +$0.001, returns answer+sources",
            ),
        ),
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
        advanced_options=(
            WebSearchOptionSpec(
                env="PERPLEXITY_SEARCH_RECENCY",
                label="Recency filter",
                field_type="select",
                default="",
                options=(
                    ("", "any time (default)"),
                    ("hour", "hour"),
                    ("day", "day"),
                    ("week", "week"),
                    ("month", "month"),
                    ("year", "year"),
                ),
            ),
            WebSearchOptionSpec(
                env="PERPLEXITY_CONTEXT_SIZE",
                label="Search context size",
                field_type="select",
                default="",
                options=(
                    ("", "provider default"),
                    ("low", "low"),
                    ("medium", "medium"),
                    ("high", "high"),
                ),
                cost_note="omitted when max tokens per page is set",
            ),
            WebSearchOptionSpec(
                env="PERPLEXITY_MAX_TOKENS_PER_PAGE",
                label="Max tokens per page",
                field_type="number",
                default="",
            ),
        ),
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
        advanced_options=(
            WebSearchOptionSpec(
                env="PARALLEL_MODE",
                label="Mode",
                field_type="select",
                default="",
                options=(
                    ("", "provider default (advanced)"),
                    ("turbo", "turbo"),
                    ("basic", "basic"),
                    ("advanced", "advanced"),
                ),
                cost_note="turbo cheapest, advanced highest quality",
            ),
            WebSearchOptionSpec(
                env="PARALLEL_EXCERPT_CHARS",
                label="Max excerpt chars per result",
                field_type="number",
                default="",
            ),
            WebSearchOptionSpec(
                env="PARALLEL_TOTAL_CHARS",
                label="Max total excerpt chars",
                field_type="number",
                default="",
            ),
        ),
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
        advanced_options=(
            WebSearchOptionSpec(
                env="SEARCHAPI_ENGINE",
                label="Engine",
                field_type="select",
                default="",
                options=(
                    ("", "google (default)"),
                    ("google", "google"),
                    ("google_news", "google_news"),
                    ("google_scholar", "google_scholar"),
                    ("bing", "bing"),
                ),
            ),
            WebSearchOptionSpec(
                env="SEARCHAPI_TIME_PERIOD",
                label="Time period",
                field_type="select",
                default="",
                options=(
                    ("", "any time (default)"),
                    ("last_hour", "last_hour"),
                    ("last_day", "last_day"),
                    ("last_week", "last_week"),
                    ("last_month", "last_month"),
                    ("last_year", "last_year"),
                ),
            ),
            WebSearchOptionSpec(
                env="SEARCHAPI_GL",
                label="Country code",
                field_type="text",
                default="",
            ),
            WebSearchOptionSpec(
                env="SEARCHAPI_HL",
                label="Language code",
                field_type="text",
                default="",
            ),
        ),
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
        advanced_options=(
            WebSearchOptionSpec(
                env="SERPAPI_ENGINE",
                label="Engine",
                field_type="select",
                default="",
                options=(
                    ("", "google (default)"),
                    ("google", "google"),
                    ("google_light", "google_light"),
                    ("bing", "bing"),
                ),
                cost_note="google_light: cheaper, num=100 works",
            ),
            WebSearchOptionSpec(
                env="SERPAPI_TBS",
                label="Date filter (e.g. qdr:w)",
                field_type="text",
                default="",
            ),
            WebSearchOptionSpec(
                env="SERPAPI_GL",
                label="Country code",
                field_type="text",
                default="",
            ),
            WebSearchOptionSpec(
                env="SERPAPI_HL",
                label="Language code",
                field_type="text",
                default="",
            ),
        ),
    ),
}

# Insertion order is the canonical display order and the `auto` resolution order;
# ``SUPPORTED_WEBSEARCH_PROVIDER_IDS`` inherits it for UI and validation.
SUPPORTED_WEBSEARCH_PROVIDER_IDS: tuple[str, ...] = tuple(WEBSEARCH_CATALOG.keys())

if len(set(SUPPORTED_WEBSEARCH_PROVIDER_IDS)) != len(SUPPORTED_WEBSEARCH_PROVIDER_IDS):
    raise AssertionError("Duplicate provider ids in WEBSEARCH_CATALOG key order")
