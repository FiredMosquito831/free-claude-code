# AGENT SPEC — Proxy-Level Web Search Providers

> Temporary coordination spec for the `proxy-level-websearch` branch. Remove before final release.

## Goal

Add first-class web search providers to free-claude-code so the proxy can fulfill Claude Code's
official `web_search_20250305` server tool with a user-configured search backend:

- 14 search provider adapters (see catalog below).
- Multiple API keys per provider (comma-separated in the same env var) + key rotation
  (policies: `single | round_robin | least_used | failover`; tiered cooldown, circuit breaker).
- Per-provider / per-key usage analytics (SQLite, weekly + monthly rollups).
- A dedicated admin UI page mirroring the model-providers page (configure keys, manage multiple
  keys, test provider) plus a web-search analytics view (per provider, per key, weekly/monthly).

Non-goals: web_fetch providers, MCP search, changing model-provider rotation (fork branches).

## Repo facts

- Python 3.14, uv-managed. Run everything via `uv run`. CI: `scripts/ci.sh` (bash) /
  `scripts\ci.ps1`: suppressions grep → `ruff format` → `ruff check --fix` → `ty check` → pytest.
- No `# type: ignore`, no `# ty: ignore`, no `from __future__ import annotations`.
- Branch: `proxy-level-websearch` (tracks `fork/proxy-level-websearch`). Base: d98a6b0 (v4.12.0).
- Persistence convention: config in dotenv (`~/.fcc/.env` managed), logs/analytics in SQLite
  under `~/.fcc/logs/`.
- Import boundary contract: `src/free_claude_code/core/` must NOT import `config/` (or other
  top-level packages). Tests in `tests/contracts/` enforce boundaries, catalog order, and the
  admin manifest — update/extend them when adding modules.
- Existing server-tool path: `src/free_claude_code/api/web_tools/` (`request.py` detection,
  `streaming.py` SSE synthesis, `outbound.py` DuckDuckGo scrape in `_run_web_search`).
  `MessagesHandler` intercept chain in `api/handlers/messages.py`.
- Admin UI: FastAPI `api/admin_routes.py` (loopback-only) + `api/admin_static/{index.html,
  admin.js, admin.css}`; manifest-driven fields in `config/admin/manifest.py`,
  auto-generated provider fields in `config/admin/provider_manifest.py`; apply flow persists to
  `~/.fcc/.env` atomically and hot-swaps providers.
- Research notes with per-provider API details: `research/web-search-providers.md` (READ THIS FIRST).

## Architecture (decided)

```
src/free_claude_code/
  core/websearch/            # neutral, no imports outside stdlib+core
    __init__.py
    models.py                # contracts below
  config/websearch_catalog.py   # WebSearchDescriptor + WEBSEARCH_CATALOG (single source of truth)
  websearch/                 # adapters + runtime (may import core + config)
    __init__.py
    errors.py                # WebSearchError hierarchy (auth/rate_limit/quota/invalid/upstream)
    base.py                  # WebSearchProviderConfig dataclass + BaseWebSearchProvider ABC
    rotation.py              # KeyPool: acquire/report_success/report_failure/report_rate_limit, health snapshot
    registry.py              # build_providers(settings) -> dict[str, BaseWebSearchProvider]; active_provider(settings)
    analytics.py             # (Worker B) SQLite store + stats
    adapters/
      __init__.py            # ADAPTER_CLASSES: dict[provider_id, class]
      http.py                # shared httpx async helper (timeouts, proxy, error mapping)
      exa.py ollama.py ddgs.py brave.py firecrawl.py tavily.py jina.py searxng.py
      serper.py linkup.py parallel.py perplexity.py searchapi.py serpapi.py
```

## Fixed contracts (do not change without main-agent approval)

### `core/websearch/models.py`

```python
@dataclass(frozen=True, slots=True)
class WebSearchResultItem:
    title: str
    url: str
    snippet: str          # "" when absent
    content: str | None   # fuller text when the provider returns it
    published: str | None # ISO date when known

@dataclass(frozen=True, slots=True)
class WebSearchResponse:
    provider: str         # provider_id
    query: str
    results: tuple[WebSearchResultItem, ...]
    key_index: int        # which key served it (0-based)
    cost_usd: float | None
```

### `websearch/base.py`

```python
@dataclass(frozen=True, slots=True)
class WebSearchProviderConfig:
    api_keys: tuple[str, ...]      # may be empty for keyless providers (ddgs, searxng)
    credential_rotation: str       # single|round_robin|least_used|failover
    base_url: str | None           # override (searxng self-host, testing)
    proxy: str | None
    http_timeout: float            # seconds, default 20

class BaseWebSearchProvider(ABC):
    def __init__(self, config: WebSearchProviderConfig) -> None: ...
    @abstractmethod
    async def search(self, query: str, *, max_results: int = 10,
                     allowed_domains: tuple[str, ...] = (),
                     blocked_domains: tuple[str, ...] = ()) -> WebSearchResponse: ...
    async def close(self) -> None: ...
```

Concrete adapters implement `_search_with_key(query, key, key_index, *, max_results, ...)`.
`BaseWebSearchProvider.search` owns the KeyPool rotation loop (acquire → try → report).

### `config/websearch_catalog.py`

```python
@dataclass(frozen=True, slots=True)
class WebSearchDescriptor:
    provider_id: str
    display_name: str
    credential_env: str | None        # None for keyless
    credential_url: str | None        # where to obtain a key
    settings_attr: str | None         # Settings attribute holding the key(s)
    default_base_url: str | None
    base_url_attr: str | None         # for self-hosted (searxng)
    requires_key: bool
    supports_domains: bool            # allowed/blocked domains passthrough
    free_tier: str                    # short human note for UI
    notes: str

WEBSEARCH_CATALOG: dict[str, WebSearchDescriptor]  # insertion order = display order
SUPPORTED_WEBSEARCH_PROVIDER_IDS: tuple[str, ...]
```

Catalog entries, in this exact order (contract test asserts order):

| id | display | env var | keyless | notes |
|---|---|---|---|---|
| ddgs | DuckDuckGo (ddgs) | — | yes | uses `ddgs` package, `DDGS().text()` |
| ollama | Ollama Web Search | `OLLAMA_SEARCH_API_KEY` | no | POST ollama.com/api/web_search, Bearer |
| exa | Exa | `EXA_API_KEY` | no | POST api.exa.ai/search, `x-api-key` |
| tavily | Tavily | `TAVILY_API_KEY` | no | POST api.tavily.com/search, Bearer |
| brave | Brave Search | `BRAVE_SEARCH_API_KEY` | no | GET, `X-Subscription-Token` |
| searxng | SearXNG (self-hosted) | — | yes | needs `SEARXNG_BASE_URL`, `?format=json` |
| jina | Jina Search | `JINA_API_KEY` | no | GET s.jina.ai/{query}, Accept: application/json |
| serper | Serper (Google) | `SERPER_API_KEY` | no | POST google.serper.dev/search, `X-API-KEY` |
| firecrawl | Firecrawl | `FIRECRAWL_API_KEY` | no | POST api.firecrawl.dev/v2/search, Bearer |
| linkup | Linkup | `LINKUP_API_KEY` | no | POST api.linkup.so/v1/search, Bearer; title field is `name` |
| perplexity | Perplexity Search | `PERPLEXITY_SEARCH_API_KEY` | no | POST api.perplexity.ai/search, Bearer |
| parallel | Parallel | `PARALLEL_API_KEY` | no | POST api.parallel.ai/v1beta/search, `x-api-key` |
| searchapi | SearchAPI.io | `SEARCHAPI_API_KEY` | no | GET, `api_key` query param |
| serpapi | SerpAPI | `SERPAPI_API_KEY` | no | GET serpapi.com/search, `api_key` query param |

(Crawl4AI from the user's list is a crawler, not a search API — excluded from the search catalog;
the existing web_fetch path covers fetching.)

### Settings additions (`config/settings.py`)

- `web_search_provider: str = "auto"` — `auto` = first catalog provider with a configured key,
  else `ddgs`. Validated against catalog ids + `auto` + `off` (off = keep today's scrape behavior).
- One optional str field per `settings_attr` above (env via validation_alias, matching existing
  style), plus `searxng_base_url: str | None`.
- Rotation per provider via dotenv-only `{CREDENTIAL_ENV}_ROTATION` read from env file/process
  (same pattern as the model-provider rotation branch), default `failover` when >1 key else
  `single`. No new Settings fields for rotation.
- `WEBSEARCH_LOG_ENABLED: bool = True`, `WEBSEARCH_LOG_MAX_ROWS: int = 50000` (analytics).

### Key storage & rotation semantics

- Multiple keys: comma-separated in the same env var (`EXA_API_KEY=k1,k2`). Parsing helper
  `parse_websearch_keys(raw) -> tuple[str, ...]` (strip, drop empties).
- KeyPool health states: `HEALTHY → COOLDOWN` (10s/30s/60s/120s tiers on consecutive failures),
  `CIRCUIT_OPEN` after 4 consecutive failures (60s), 401/403 → lockout (5min escalating),
  429 → report_rate_limit (60s cooldown). In-memory only. `snapshot()` for admin UI.

### Analytics contract (Worker B)

- `websearch/analytics.py`: `WebSearchLogStore` (SQLite `~/.fcc/logs/websearch.db`, WAL,
  background writer thread like `core/request_log.py`).
- Record fields: `ts_epoch, ts_iso, provider, key_index, key_label (masked: first4…last4),
  query (cap 256 chars), results_count, duration_ms, status (success|error), error_kind,
  error_message (cap 500), cost_usd`.
- API: `store.record(...)`, `store.stats(period: str) -> dict` with
  `totals`, `by_provider` (requests, errors, avg_duration_ms, results), `by_key`
  (provider+key_label breakdown), `series` bucketed by ISO week (`weekly`) or month (`monthly`)
  per provider, `top_errors`. Retention prune to max_rows every 100 inserts.
- Admin endpoints (new router file `api/admin_websearch_routes.py`, loopback-guarded like
  admin_routes): `GET /admin/api/websearch/stats?period=weekly|monthly`,
  `GET /admin/api/websearch/requests` (paged, filters provider/status/q/since/until),
  `DELETE /admin/api/websearch/requests`.
- Recording hook: `websearch/registry.py` exposes `search_with_logging(provider, query, ...)`
  that wraps `search()` and records (Worker B implements; Worker A leaves a TODO seam:
  registry calls `record_search(...)` no-op function defined in `analytics.py` — B fills it in;
  A should define registry to accept an optional `recorder` callable, default None).

### Admin UI contract (Worker C)

- `config/admin/websearch_manifest.py`: `websearch_field_specs()` auto-generating secret fields
  per catalog credential + `searxng_base_url` text + `web_search_provider` select
  (options from catalog + auto/off). New `ConfigSectionSpec` id `websearch`
  (label "Web Search", after `web_tools` section).
- `api/admin_static/index.html` + `admin.js`: new `VIEW_GROUPS` entry `web_search`
  (label "Web Search") with sections: `websearch` (config cards per provider: status, key
  field(s), rotation select, Manage-keys panel, Test button) and `websearch_analytics`
  (new view: period toggle weekly/monthly, per-provider table, per-key table, recent requests
  table; vanilla JS + existing admin.css styles, no new dependencies).
- Key-management endpoints in `api/admin_routes.py` (websearch-scoped):
  `GET /admin/api/websearch/credentials/{env_key}/keys` (masked list + live health),
  `POST .../keys` (append), `DELETE .../keys/{index}`. Reuse the persistence helpers from
  `config/admin/persistence.py`.
- Test endpoint: `POST /admin/api/websearch/providers/{provider_id}/test` runs a real
  `web search` query (max_results=3) and reports latency/result count/error.

### web_tools integration (Worker C)

- `api/web_tools/outbound.py::_run_web_search`: when `settings.web_search_provider != "off"`
  and the registry yields a configured provider, route through
  `registry.search_with_logging(...)`; honor `WEB_SEARCH_MAX_RESULTS` existing cap constants.
  Fallback chain: configured provider error → ddgs (if not already) → legacy HTML scrape.
  Do NOT change SSE synthesis (`streaming.py`).
- Keep current interception semantics (forced tool_choice only). Do not relax request.py.

### .env.example

New `# Web Search Providers` block listing every env var above with one-line comments
(incl. `_ROTATION` examples and `WEB_SEARCH_PROVIDER`).

## Task slices

### Worker A — core websearch package (worktree: none, main workspace)
Owns: `core/websearch/`, `config/websearch_catalog.py`, `websearch/` (EXCEPT `analytics.py`),
`config/settings.py` (additive), `pyproject.toml` (add `ddgs` dep via `uv add ddgs` — do NOT
bump version), `.env.example`, `tests/websearch/`, `tests/contracts/test_websearch_catalog.py`.
Forbidden: `api/`, `config/admin/`, `websearch/analytics.py`, version bump.
Tests: per-adapter unit tests (httpx MockTransport / monkeypatch; cover auth headers, payload,
result mapping, error mapping 401/429/quota), KeyPool rotation tests, catalog contract test,
settings parse tests (comma-separated keys, rotation env).

### Worker B — analytics (worktree: agent/websearch-analytics)
Owns: `websearch/analytics.py`, `api/admin_websearch_routes.py`, router registration in
`api/app.py`, `tests/websearch/test_analytics.py`, `tests/api/test_admin_websearch.py`.
Forbidden: everything else (esp. `api/admin_routes.py`, `api/admin_static/`, `config/admin/`).

### Worker C — integration + admin UI (worktree: agent/websearch-ui)
Owns: `api/web_tools/outbound.py` (+ its tests), `config/admin/websearch_manifest.py`,
`config/admin/manifest.py` (additive section), `api/admin_routes.py` (additive key-mgmt +
test endpoints), `api/admin_static/{index.html, admin.js, admin.css}`, related tests
(`tests/api/test_web_server_tools.py` updates, `tests/config/test_admin_websearch_manifest.py`,
contract test updates for admin manifest).
Forbidden: `websearch/analytics.py`, `api/admin_websearch_routes.py`, `core/`, adapters.

## Merge order & verification

A → validate → B and C in parallel (worktrees off merged state) → merge B → merge C →
integration fixups (registry recorder seam, app.py router registration) → semver MINOR bump
(4.12.0 → 4.13.0) + `uv lock` → `scripts/ci.sh` green → final commit.
