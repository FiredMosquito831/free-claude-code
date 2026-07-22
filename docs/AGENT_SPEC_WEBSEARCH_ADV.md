# AGENT SPEC — Websearch Advanced Options + Rich Digest

> Temporary coordination spec on branch `feat/websearch-providers`. Base commit: 05b9c45.
> Research source of truth: `research/web-search-advanced.md` (READ IT).

## Goal

1. **Advanced options layer**: per-provider optional settings (dotenv-only, catalog-driven) that
   unlock each provider's high-value capabilities (quality modes, content modes, freshness/geo
   filters, rich answers).
2. **Rich digest pipeline**: provider richness (snippets, full content, published dates, LLM
   answers/summaries) flows through outbound → streaming so Claude Code's text block becomes a
   proper per-result digest with an optional answer lead; `page_age` emitted on result items.

## Mechanism (decided)

- Catalog-driven: `WebSearchDescriptor` gains `advanced_options: tuple[WebSearchOptionSpec, ...]`.
  `WebSearchOptionSpec` (frozen dataclass in `config/websearch_catalog.py`):
  `env: str` (full var name), `label: str`, `field_type: str` ("select"|"text"|"number"|"boolean"),
  `default: str`, `options: tuple[tuple[str, str], ...]` (value,label pairs for select; empty
  otherwise), `cost_note: str` ("" or warning like "2 credits/query").
- Values are **dotenv-only** (like `{ENV}_ROTATION`): helper
  `websearch/rotation.py`-style reader `read_websearch_options(provider_id, descriptor) ->
  dict[str, str]` reading process env then dotenv files (mirror how `credential_rotation_policy`
  reads env on this branch — find it and copy the pattern). NOT pydantic Settings fields.
- `WebSearchProviderConfig` gains `options: Mapping[str, str]` (frozen dataclass — use
  `field(default_factory=dict)`; registry fills it via the reader).
- Adapters consume `self._config.options.get("EXA_SEARCH_TYPE")` etc. Unknown/empty = provider
  default behavior (stay backwards compatible: all defaults reproduce v4.9.0 behavior).

## Core model changes (additive, backwards compatible)

- `core/websearch/models.py`: `WebSearchResponse` gains `answer: str | None = None` (provider
  LLM answer / rich block lead). `WebSearchResultItem` UNCHANGED (snippet = best short digest
  text; content = fuller text; published = ISO date).

## Per-provider option sets (EXACT env names; implement exactly these)

| Provider | Env vars (field_type, default, notes) |
|---|---|
| exa | EXA_SEARCH_TYPE (select: ""=auto, instant, fast, auto, deep-lite, deep, deep-reasoning; cost_note "deep* = $0.015/query vs $0.005") · EXA_CONTENTS (select: ""="highlights", highlights, text, highlights+text, highlights+summary, full; "full"=text+highlights+summary; cost "+$0.001/page per content type") · EXA_CATEGORY (select: "", company, people, research paper, news, personal site, financial report; cost_note "company/people disable date+exclude filters") · EXA_MAX_AGE_HOURS (number, ""; 0=always fresh) · EXA_START_PUBLISHED_DATE (text ISO) · EXA_END_PUBLISHED_DATE (text ISO) · EXA_USER_LOCATION (text, 2-letter) |
| ollama | — none (document in catalog notes) |
| ddgs | DDGS_BACKEND (select: "", bing, brave, duckduckgo, google, mojeek, startpage, yandex, yahoo, wikipedia) · DDGS_REGION (text, e.g. us-en) · DDGS_TIMELIMIT (select: "", d, w, m, y) · DDGS_SAFESEARCH (select: ""=moderate, on, moderate, off) |
| brave | BRAVE_SEARCH_MODE (select: ""=web, web, llm-context; llm-context cost_note "$5/1k, returns pre-extracted page text") · BRAVE_EXTRA_SNIPPETS (boolean; "plan-gated") · BRAVE_FRESHNESS (select: "", pd, pw, pm, py) · BRAVE_COUNTRY (text) · BRAVE_SEARCH_LANG (text) · BRAVE_LLM_MAX_TOKENS (number, ""; 1024–32768, llm-context mode only) |
| searxng | SEARXNG_ENGINES (text, comma list) · SEARXNG_CATEGORIES (text) · SEARXNG_TIME_RANGE (select: "", day, month, year) · SEARXNG_LANGUAGE (text) |
| jina | JINA_MAX_TOKENS (number, ""; X-Max-Tokens cost guardrail) · JINA_SITE (text, site filter) · JINA_GL (text, geo) |
| serper | SERPER_GL (text) · SERPER_HL (text) · SERPER_TBS (text, e.g. qdr:w) · SERPER_RICH_BLOCKS (boolean, default on; capture answerBox/knowledgeGraph/peopleAlsoAsk → response.answer) |
| firecrawl | FIRECRAWL_SOURCES (select: ""=web, web, "web,news", "web,news,images") · FIRECRAWL_SCRAPE_FORMAT (select: "", summary, markdown; cost "multiplies credits per result") · FIRECRAWL_TBS (text, e.g. qdr:d) · FIRECRAWL_LOCATION (text) |
| tavily | TAVILY_SEARCH_DEPTH (select: ""=basic, basic, fast, ultra-fast, advanced; advanced cost "2 credits/query") · TAVILY_TOPIC (select: ""=general, general, news, finance) · TAVILY_TIME_RANGE (select: "", day, week, month, year) · TAVILY_INCLUDE_ANSWER (select: "", basic, advanced) · TAVILY_INCLUDE_RAW_CONTENT (select: "", markdown, text; free) |
| linkup | LINKUP_DEPTH (select: ""=standard, standard, deep; deep cost "10× ($0.05/query)") · LINKUP_OUTPUT_TYPE (select: ""=searchResults, searchResults, sourcedAnswer; sourcedAnswer cost "+$0.001, returns answer+sources") |
| perplexity | PERPLEXITY_SEARCH_RECENCY (select: "", hour, day, week, month, year) · PERPLEXITY_CONTEXT_SIZE (select: "", low, medium, high) · PERPLEXITY_MAX_TOKENS_PER_PAGE (number, "") |
| parallel | PARALLEL_MODE (select: ""=basic, turbo, basic, advanced) · PARALLEL_EXCERPT_CHARS (number, "") · PARALLEL_TOTAL_CHARS (number, "") |
| searchapi | SEARCHAPI_ENGINE (select: ""=google, google, google_news, google_scholar, bing) · SEARCHAPI_TIME_PERIOD (select: "", last_hour, last_day, last_week, last_month, last_year) · SEARCHAPI_GL (text) · SEARCHAPI_HL (text) |
| serpapi | SERPAPI_ENGINE (select: ""=google, google, google_light, bing; google_light cost "cheaper, num=100 works") · SERPAPI_TBS (text, e.g. qdr:w) · SERPAPI_GL (text) · SERPAPI_HL (text) |

Adapter behavior notes (from research, all in web-search-advanced.md):
- exa: contents modes map to `contents: {highlights: true, text: {maxCharacters...}, summary: true}`;
  deep* types accept ≤25 results; category company/people must SKIP date/exclude params.
- brave llm-context mode: POST /res/v1/llm/context, map `grounding.generic[]{url,title,snippets[]}`;
  maximum_number_of_tokens from BRAVE_LLM_MAX_TOKENS.
- tavily answer → response.answer; raw_content → item.content; NEVER enable auto_parameters.
- serper rich blocks: answerBox → response.answer; peopleAlsoAsk/knowledgeGraph appended to answer text.
- linkup sourcedAnswer → response.answer (+sources as results).
- ddgs: news backend not in scope (timelimit covers freshness); keep text() only.
- searxng: engines/categories/time_range/language as query params; keep format=json.
- perplexity: search_recency_filter etc.; country not included (keep bounded).
- searchapi/serpapi: engine + time/tbs + gl/hl; capture answer_box/knowledge_graph → response.answer.
- jina: X-Max-Tokens header; site/gl query params.
- firecrawl: sources array, tbs, location; scrapeOptions.formats per FIRECRAWL_SCRAPE_FORMAT
  (summary → item.snippet upgrade, markdown → item.content).
- parallel: mode, excerpts.max_chars_per_result/max_chars_total.

## Rich digest pipeline

- New pydantic Settings (these ARE settings): `websearch_digest_chars: int = 600`
  (WEBSEARCH_DIGEST_CHARS), `websearch_digest_answer: bool = True` (WEBSEARCH_DIGEST_ANSWER).
- `api/web_tools/outbound.py::_web_search_response_items` — stop flattening to title/url:
  pass through {title, url, snippet, published, answer(response-level), provider}. Keep
  _MAX_SEARCH_RESULTS cap. Thread settings for digest budget.
- `api/web_tools/streaming.py::_search_summary` → rich digest: optional lead `response.answer`
  (when WEBSEARCH_DIGEST_ANSWER), then per result: `N. title (published date when known)\nurl\n
  snippet-or-content-excerpt capped at WEBSEARCH_DIGEST_CHARS`. Legacy scrape path: unchanged
  (title/url only).
- Result items in the SSE `web_search_tool_result` block gain `page_age` when published is
  known (spec-official field; format the ISO date as e.g. "July 22, 2026").

## Admin UI (Worker UI)

- `config/admin/websearch_manifest.py`: emit each catalog `advanced_options` entry as a field
  spec (key=env, field_type mapped select/text/number/boolean, advanced=True, section
  "websearch", dotenv-only like rotation fields, description=cost_note).
- `admin.js`: in each websearch provider card render a collapsed `<details class="ws-advanced">`
  "Advanced options" group containing that provider's advanced fields (save through the
  existing apply flow); show cost_note as small muted text. Keep everything else unchanged.

## Slices

- Worker BACKEND (worktree ../.worktrees/ws-adv-backend, branch agent/ws-adv-backend):
  catalog specs, options reader, config plumbing, 14 adapter upgrades, models.answer,
  digest pipeline (outbound/streaming/settings), .env.example block, tests. Forbidden:
  admin_static/, websearch_manifest.py.
- Worker UI (worktree ../.worktrees/ws-adv-ui, branch agent/ws-adv-ui): websearch_manifest.py
  advanced field generation + admin.js advanced groups + tests. Codes against the catalog
  contract above; the catalog file itself arrives via merge (UI worker must NOT edit
  config/websearch_catalog.py; for its tests, construct WebSearchOptionSpec instances directly
  or defensively handle descriptors lacking advanced_options).

## Validation & merge

Each worker: ruff format/check --fix, ty check, targeted pytest; commit in worktree.
Merge order: backend → UI. Then main agent: integration, bump 4.9.0 → 4.10.0, uv lock, full CI.
