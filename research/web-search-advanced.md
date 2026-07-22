# Web Search Providers — Advanced Capabilities Matrix (Implementation-Ready)

Research date: 2026-07. Companion to `web-search-providers.md`. Scope: advanced request options NOT yet used by the basic adapters (which only send endpoint+auth+query and extract title/url/snippet). For every option: exact parameter name, type/allowed values, default, cost implications, and the exact response fields it unlocks/affects.

Legend for "Cost" column: **free** = no extra charge vs base request; **$$** = extra credits/USD per use; **plan** = gated by subscription tier.

---

## 0. Anthropic `web_search_20250305` server-tool contract (target wire format)

Source: https://docs.claude.com/en/docs/agents-and-tools/tool-use/web-search-tool

Each result inside `web_search_tool_result.content[]` officially supports exactly **four** fields — nothing beyond these:

| Field | Type | Notes |
|---|---|---|
| `url` | string | Source page URL |
| `title` | string | Source page title |
| `page_age` | string | "When the site was last updated" (human string, e.g. `"April 30, 2025"`) |
| `encrypted_content` | string | Opaque encrypted blob; MUST be passed back verbatim in multi-turn conversations for citations. Cannot be synthesized by a third-party backend — when proxying, generate our own opaque token mapping to provider content, or omit and accept degraded citations |

Citations (`web_search_result_location`): `url`, `title`, `encrypted_index` (pass back multi-turn), `cited_text` (≤150 chars; `cited_text`/`title`/`url` do not count toward token usage).

Tool-call params: `max_uses` (int), `allowed_domains` XOR `blocked_domains` (subdomains auto-included, subpaths OK), `user_location: {type: "approximate", city, region, country, timezone}`.
Errors (HTTP 200 with `web_search_tool_result_error`): `too_many_requests`, `invalid_input`, `max_uses_exceeded`, `query_too_long`, `unavailable`.
Billing: $10/1k searches; failed searches unbilled. `usage.server_tool_use.web_search_requests`.

**Implication for the proxy:** the only first-class per-result extras we can legitimately emit are `page_age` (map from provider date fields: Exa `publishedDate`, Brave `page_age`, Perplexity `last_updated`, Serper/SerpAPI/SearchAPI `date`, Linkup n/a) and `encrypted_content` (our own opaque handle for citation continuity).

---

## 1. Exa — `POST https://api.exa.ai/search`

Docs: https://docs.exa.ai/reference/search-api-guide-for-coding-agents · https://docs.exa.ai/reference/contents-api-guide-for-coding-agents · pricing breakdown in `costDollars` response field.

### 1.1 Search modes — `type` (default `"auto"`)

| Value | Latency | Cost (per `costDollars.perRequestPrices`) | Notes |
|---|---|---|---|
| `instant` | ~250 ms | neural tier: $0.005/req (1–25 results), $0.025 (26–100) | real-time apps; lowest quality |
| `fast` | ~450 ms | same neural tier | optimized models, good balance |
| `auto` | ~1 s | same neural tier | router picks variant per query |
| `deep-lite` | ~4 s | deep tier: $0.015/req (1–25), $0.075 (26–100) | light synthesized output |
| `deep` | 4–15 s | deep tier | multi-step search + reasoning + structured outputs |
| `deep-reasoning` | 12–40 s | deep tier | max reasoning per step |

Stacking modifiers: `outputSchema` adds synthesis latency on ANY type; `contents.maxAgeHours: 0` (forced livecrawl) adds latency. Response fields affected: `searchType` (actual type used), `output` (deep/synthesis only).

### 1.2 Content retrieval — nested under `contents` (MANDATORY nesting on /search; top-level on /contents)

| Parameter | Type/values | Default | Cost (`perPagePrices`) | Unlocks in `results[]` |
|---|---|---|---|---|
| `contents.text` | bool or `{maxCharacters:int, includeHtmlTags:bool=false, verbosity:"compact"\|"standard"\|"full", includeSections:[header,navigation,banner,body,sidebar,footer,metadata], excludeSections:[...]}` | off | $0.001/page | `text` (full page markdown) |
| `contents.highlights` | bool or `{maxCharacters:int, query:string}` | off | $0.001/page | `highlights: string[]`, `highlightScores: float[]` (cosine similarity) |
| `contents.summary` | bool or `{query:string, schema:JSONSchema}` | off | $0.001/page | `summary` (LLM abstract; structured if schema given) |
| `contents.livecrawlTimeout` | int ms | 10000 | free | freshness of `text`/`highlights` (recommend 10000–15000) |
| `contents.maxAgeHours` | int; `0`=always livecrawl, `-1`=cache only, omit=livecrawl-as-fallback | omit | free (cache hits faster) | content freshness |
| `contents.subpages` | int | 0 | per extra page crawled | `subpages[]` (nested result objects, same shape) |
| `contents.subpageTarget` | string or string[] | — | free | focuses subpage selection (e.g. `["api","docs"]`) |
| `contents.extras.links` | int (# URLs) | 0 | free | `extras.links[]` |
| `contents.extras.imageLinks` | int (# image URLs) | 0 | free | `extras.imageLinks` |

DEPRECATED — never emit: `livecrawl` (→ `maxAgeHours`), `numSentences`, `highlightsPerUrl`, `useAutoprompt`, `tokensNum`, `includeUrls`/`excludeUrls` (→ `includeDomains`/`excludeDomains`).

### 1.3 Targeting/filters (top-level)

| Parameter | Type/values | Default | Cost | Notes / unlocked fields |
|---|---|---|---|---|
| `category` | `company`, `people`, `research paper`, `news`, `personal site`, `financial report` | — | free | vertical focus. `company`/`people`: NO date filters, NO `excludeDomains` (400 error); `people` includeDomains = LinkedIn only |
| `startPublishedDate` / `endPublishedDate` | ISO 8601 | — | free | filters on estimated pub date (`results[].publishedDate`) |
| `startCrawlDate` / `endCrawlDate` | ISO 8601 | — | free | filters on Exa crawl date |
| `userLocation` | 2-letter ISO country | — | free | geo-relevance bias |
| `moderation` | bool | false | free | filters unsafe content |
| `includeDomains` / `excludeDomains` | string[], ≤1200 | — | free | domain allow/deny |
| `numResults` | int 1–100 | 10 | >25 results jumps request price tier | result count |
| `additionalQueries` | string[] | — | free | extra query variations (deep variants only) |
| `systemPrompt` | string | — | free | guides synthesis/search planning (deep variants) |
| `outputSchema` | JSON Schema (depth≤2, ≤10 props) | — | adds synthesis cost/latency | unlocks top-level `output.content` + `output.grounding[]` (`{field, citations:[{url,title}], confidence: low\|medium\|high}`) |
| `stream` | bool | false | free | SSE `text/event-stream` with OpenAI-compatible chunks instead of JSON |

`includeText`/`excludeText` (legacy substring filters): NOT in current reference — treat as removed; do not use.

Always-present result metadata worth capturing: `publishedDate`, `author`, `image`, `favicon`, `id` (= URL; reuse with /contents). Cost telemetry: `costDollars.total` + `breakDown`.

### 1.4 `/contents` endpoint basics (`POST https://api.exa.ai/contents`)

- Body: `urls: string[]` (or `ids`), then `text`/`highlights`/`summary`/`subpages`/`subpageTarget`/`extras`/`maxAgeHours`/`livecrawlTimeout` **top-level** (no `contents` wrapper — inverse of /search). `highlights: true` = highest-quality default.
- Response: `results[]` (same shape as search results) + **`statuses[]`** per-URL (`status: success|error`, `error.tag`: `CRAWL_NOT_FOUND`/`CRAWL_TIMEOUT`/`CRAWL_LIVECRAWL_TIMEOUT`/`SOURCE_NOT_AVAILABLE`/`UNSUPPORTED_URL`/`CRAWL_UNKNOWN_ERROR`) — HTTP 200 even on per-URL failure; ALWAYS check statuses.
- Cost: contents pricing $0.001/page per content type (i.e. $1/1k pages). Enterprise: `compliance: "hipaa"` (cache-only, no livecrawl/summaries).
- No streaming on /contents.

### 1.5 Recommended digest payload (Exa)

Per result for an LLM consumer: `title` + `url` + `publishedDate` (→ `page_age`) + `author?` + **`highlights`** (request `contents.highlights: {maxCharacters: 1000–2000, query: <user query>}` — 10× fewer tokens than text, extractive not generated) + **`summary`** (only for overview-style tasks, `contents.summary: {query}`) — fallback `contents.text: {maxCharacters: 3000–5000}` when highlights insufficient. Skip `subpages`/`extras` by default (cost ×N).
---

---

## 2. Ollama — `POST https://ollama.com/api/web_search`

Docs: https://docs.ollama.com/capabilities/web-search

**Confirmed: nothing beyond the basics.** Request surface is exactly:
- `query` (string, required)
- `max_results` (int, default 5, max 10)

Response: `results[].{title, url, content}` — no date, no filters, no verticals, no geo. Companion endpoint `POST /api/web_fetch {url}` → `{title, content, links[]}` is the only enrichment path (fetch full page for a chosen result). Recommended digest: `content` as-is; optionally one `web_fetch` on the top hit, truncated. Docs recommend ≥32k model context since snippets can be long.

---

## 3. DDGS (`ddgs` package — keyless metasearch library)

Docs: https://github.com/deedy5/ddgs

### 3.1 `text()` params

| Parameter | Type/values | Default | Cost | Unlocks |
|---|---|---|---|---|
| `backend` | single or comma list: `bing`, `brave`, `duckduckgo`, `google`, `grokipedia`, `mojeek`, `startpage`, `yandex`, `yahoo`, `wikipedia`, or `auto` | `auto` | free | source diversity; pin to dodge per-engine IP ratelimits |
| `region` | e.g. `us-en`, `uk-en`, `ru-ru` | `us-en` | free | geo/language of results |
| `safesearch` | `on`/`moderate`/`off` | `moderate` | free | adult filtering |
| `timelimit` | `d`/`w`/`m`/`y` (text); `d`/`w`/`m` (news, videos) | None | free | freshness filter |
| `page` | int | 1 | free | pagination (with `max_results`) |
| `max_results` | int/None | 10 | free | cap |

### 3.2 Other methods worth exposing

| Method | Extra params | Result keys | Note |
|---|---|---|---|
| `images()` | `size` (Small/Medium/Large/Wallpaper), `color`, `type_image` (photo/clipart/gif/transparent/line), `layout` (Square/Tall/Wide), `license_image` | `title, image, thumbnail, url, height, width, source` | backends: bing, duckduckgo |
| `videos()` | `resolution`, `duration` (short/medium/long), `license_videos` | `content(url), description, duration, embed_url, images, published, publisher, statistics, title, uploader` | backend: duckduckgo |
| `news()` | (base params; timelimit d/w/m) | `date, title, body, url, image, source` | backends: bing, duckduckgo, yahoo — **`date` is a real ISO timestamp → best free `page_age` source** |
| `books()` | — | `title, author, publisher, info, url, thumbnail` | backend: annasarchive (piracy-adjacent — probably skip in product) |
| `extract(url, fmt)` | `fmt`: `text_markdown` (default), `text_plain`, `text_rich`, `text` (raw HTML), `content` (bytes) | `{url, content}` | free full-content leg — pairs with text() like Jina Reader |

Constructor knobs: `DDGS(proxy=..., timeout=5, verify=True)`. Also ships CLI, FastAPI server (`ddgs[api]`, port 4479, `/search/text|images|news|videos|books`, `/extract`) and MCP server (`ddgs[mcp]`).

### 3.3 Recommended digest payload (DDGS)

`title` + `href` + `body` (+ `date`/`source` for `news()` backend). For deep answers: top-N → `extract(fmt="text_markdown")` truncated to ~2–4k chars. Prefer `backend="auto"` with retry-fallback; expose `timelimit` since it's the only free freshness knob.

---

## 4. Brave — `GET https://api.search.brave.com/res/v1/web/search`

Docs: https://api-dashboard.search.brave.com/app/documentation/web-search/query · LLM Context: https://github.com/brave/brave-search-skills + Brave changelog (Feb 2026)

### 4.1 Web search params (all query-string)

| Parameter | Type/values | Default | Cost | Unlocks/affects |
|---|---|---|---|---|
| `extra_snippets` | bool | false | plan-gated (Free AI/Base AI/Pro AI/Data plans) | `web.results[].extra_snippets[]` — up to 5 alternative excerpts per result |
| `freshness` | `pd`/`pw`/`pm`/`py` or `YYYY-MM-DDtoYYYY-MM-DD` | — | free | filters by discovery date |
| `country` | 2-letter code | `US` | free | geo targeting |
| `search_lang` | ISO 639-1 | `en` | free | content language |
| `ui_lang` | e.g. `en-US` | `en-US` | free | response metadata language |
| `safesearch` | `off`/`moderate`/`strict` | `moderate` | free | adult filtering |
| `text_decorations` | bool | true | free | `<strong>` highlight markers in `description` (set false for clean LLM text) |
| `spellcheck` | bool | true | free | auto-corrects query; altered query surfaced in `query.altered` |
| `result_filter` | comma list: `discussions`, `faq`, `infobox`, `news`, `query`, `summarizer`, `videos`, `web`, `locations` | all available per plan | free | enables/disables whole vertical blocks in response |
| `goggles_id` / `goggles` | Goggle URL or inline definition; multiples combinable | — | free | custom re-ranking (boost/demote domains) |
| `units` | `metric`/`imperial` | from country | free | measurement units in rich/local results |
| `summary` | bool | false | requires Summarizer plan | generates summarizer `key` for AI summary endpoint |
| `count` | 1–20 | 20 | free | page size |
| `offset` | 0–9 | 0 | free | pagination; check `query.more_results_available` |
| `enable_rich_callback` | `1` | off | Pro plan + second call | `rich.hint.callback_key` → `GET /res/v1/web/rich?callback_key=…` (weather/stocks/sports structured data) |
| `x-loc-lat`, `x-loc-long`, `x-loc-city`, `x-loc-country`, `x-loc-timezone` (headers) | — | — | free | fine-grained geo hints |

Response fields beyond title/url/description worth capturing: `web.results[].age`, `page_age` (→ maps directly to Anthropic `page_age`), `meta_url.hostname`, `language`, `family_friendly`, `thumbnail.src`; blocks `news`, `videos`, `discussions`, `faq`, `infobox`, `locations.results[]` (POI `id`s, expire ~8h → `GET /res/v1/local/pois?ids=…` and `/local/descriptions`, ≤20 ids). Separate endpoints: `/res/v1/news/search`, `/res/v1/videos/search`, `/res/v1/images/search`.

### 4.2 LLM Context endpoint (agent-optimized) — `GET|POST /res/v1/llm/context`

Same auth (`X-Subscription-Token`), same $5/1k-request Search-plan pricing as web search. Launched Feb 2026.

| Parameter | Type/values | Default | Unlocks |
|---|---|---|---|
| `q` | string | required | — |
| `maximum_number_of_tokens` | 1024–32768 | 8192 | total extracted content budget |
| `maximum_number_of_urls` | 1–50 | 20 | source URL cap |
| `maximum_number_of_tokens_per_url` | 512–8192 | 4096 | per-page budget |
| `maximum_number_of_snippets_per_url` | 1–100 | 50 | passages per URL |
| `context_threshold_mode` | `strict`/`balanced`/`lenient`/`disabled` | `balanced` | relevance filtering of snippets |
| `freshness` | as web search | — | recency filter |
| `goggles` | as web search | — | re-ranking |

Response (DIFFERENT shape): `grounding.generic[]` → `{url, title, snippets: string[]}` — **snippets are plain strings** (real extracted page text: chunks, tables, code) — plus `sources` metadata (hostname, dates) and `poi`/`map` for local queries. ~600 ms p90. Eliminates the scrape-per-result step entirely.

Also exists: Answers API `POST /res/v1/chat/completions` (OpenAI-compatible, separate Answers plan: $4/1k requests + $5/1M tokens, `enable_research: true` for multi-search).

### 4.3 Recommended digest payload (Brave)

Best path: switch agent traffic to `/res/v1/llm/context` with `maximum_number_of_tokens` sized to the model window; digest = `title` + `url` + joined `snippets[]` + `page_age` from sources. If staying on web search: `description` + `extra_snippets[]` (≤5) + `page_age` + `age`; set `text_decorations=false`.

---

## 5. SearXNG — `GET {instance}/search`

Docs: https://docs.searxng.org/dev/search_api.html

| Parameter | Type/values | Default | Unlocks |
|---|---|---|---|
| `q` | string | required | supports upstream engine syntax (`site:`, etc.) |
| `categories` | comma list (e.g. `general`, `images`, `news`, `it`, `science`, `files`) | instance default | vertical selection (valid values per instance `/preferences`) |
| `engines` | comma list of engine names | instance default | per-engine selection (form field honored by /search; engine must be enabled instance-side) |
| `language` | lang code / `all` | from `search:` settings | language filter |
| `pageno` | int | 1 | pagination |
| `time_range` | `day`/`month`/`year` | — | freshness (only engines that support it) |
| `safesearch` | `0` (none) / `1` (moderate) / `2` (strict) | from settings | adult filter |
| `format` | `json`/`csv`/`rss` | html | machine output |

**Instance settings REQUIRED for API use (`settings.yml`):**
```yaml
search:
  formats: [html, json]   # without `json` here, format=json → 403 Forbidden (most public instances!)
server:
  limiter: false           # or allow our IPs; default limiter bot-checks break JSON clients
```
Response richness to capture: `results[].{url,title,content,engine,engines[],score,category,publishedDate?,img_src?}`; top-level `answers[]` (instant answers — high-value digest material), `corrections[]`, `infoboxes[]` (structured entity box), `suggestions[]`, `unresponsive_engines[]` (health telemetry). `publishedDate` present for news-capable engines → `page_age`.

### Recommended digest payload (SearXNG)

`title` + `url` + `content` + `publishedDate?` + `engines[]` (multi-engine hits = confidence signal) + append `answers[]`/`infoboxes[]` as a synthetic top result when present.

---

## 6. Jina — `GET https://s.jina.ai/{query}`

Docs: https://jina.ai/reader · https://github.com/jina-ai/reader · OpenAPI: https://s.jina.ai/openapi.json

### 6.1 Search-specific query params

| Param | Values | Default | Unlocks |
|---|---|---|---|
| `site` | domain | — | restrict to one site |
| `type` | `web`/`images`/`news` | `web` | vertical |
| `num` / `count` | 0–20 | ~5 | result count |
| `gl` | country code (e.g. `us`, `in`) | — | geo targeting |
| `filetype` | extension (`pdf`, …) | — | file-type filter |
| `intitle` | string | — | title must contain |

### 6.2 Reader headers that also work on s.jina.ai (billed per token processed)

| Header | Values | Default | Effect on output |
|---|---|---|---|
| `Accept: application/json` | — | off | **REQUIRED for structured output** → `{code, status, data[]:{title,url,content,description?}}`; without it, plain markdown text |
| `X-Timeout` | 1–180 s | — | page-load wait per result page |
| `X-Max-Tokens` | int ≥500 | — | **truncates** each response to budget (best cost guardrail) |
| `X-Token-Budget` | int | — | REJECTS request if content would exceed budget (all-or-nothing; ignored on search endpoint per README — use `X-Max-Tokens` instead) |
| `X-Respond-With` | `content`/`markdown`/`html`/`text`/`readerlm-v2`/`vlm`/`screenshot`/`pageshot` | `content` | output format; `readerlm-v2` = higher quality, **3× token cost** |
| `X-Retain-Images` | `none`/`all` | `all` | `none` strips images → token savings |
| `X-With-Generated-Alt` | bool | false | auto-captions every image (`Image [idx]: caption`) — extra tokens, helps multimodal reasoning |
| `X-With-Links-Summary` | bool | off | appends "Buttons & Links" section |
| `X-With-Images-Summary` | bool | off | appends "Images" section |
| `X-Engine` | `browser`/`direct`/`readerlm-v2` | — | fetch/parse engine; `direct` = faster/cheaper |
| `X-No-Cache` | bool | off | bypass cache (fresh content) |
| `X-Cache-Tolerance` | seconds | — | accept cache younger than N |
| `X-Proxy` | country code / `auto` / `none` | — | geo-specific egress proxy (localization) |
| `X-Locale` | e.g. `en-US` | — | browser locale (sites serve localized content) |
| `X-Target-Selector` / `X-Wait-For-Selector` / `X-Remove-Selector` | CSS selectors | — | extraction narrowing (e.g. remove `nav, footer`) |
| `X-Respond-Timing` | `html`/`visible-content`/`mutation-idle`/`resource-idle`/`media-idle`/`network-idle` | `resource-idle` | latency vs completeness tradeoff |
| `X-Set-Cookie` | cookie string | — | authenticated pages (uncached) |
| `X-User-Agent` / `X-Referer` | string | — | anti-bot / referer checks |
| `X-With-Iframe` / `X-With-Shadow-Dom` | bool | off | include iframe/shadow content |
| `X-Dnt` | bool | off | no cache/track (sensitive queries) |
| `X-Eu` | bool | off | EU-resident processing |

Cost model: token-billed across Reader/Search pool (10M free tokens on new keys); long full-page `content` per result is the cost driver — cap with `X-Max-Tokens`.

### 6.3 Recommended digest payload (Jina)

`title` + `url` + `description` (SERP snippet when present) + `content` truncated via `X-Max-Tokens: 2000` and/or `X-Retain-Images: none` + `X-Remove-Selector: nav,footer,.sidebar,.ads`. `gl`/`X-Proxy`/`X-Locale` for geo tasks.

---

## 7. Serper — `POST https://google.serper.dev/search`

Docs: https://serper.dev/playground (+ per-endpoint pages); corroborated by LiteLLM/MCP integrations.

### 7.1 Request params (JSON body)

| Param | Type/values | Default | Cost | Unlocks |
|---|---|---|---|---|
| `gl` | 2-letter country | `us` | free | geo |
| `hl` | lang code (`en`, `zh-cn`, `fr`…) | `en` | free | UI/content language |
| `location` | free text (`"New York"`) | — | free | city-level geo |
| `num` | int (up to 100) | 10 | free | results per page |
| `page` | int | 1 | free | pagination |
| `autocorrect` | bool | true | free | query spell-correction |
| `tbs` | Google tbs passthrough: `qdr:h`/`qdr:d`/`qdr:w`/`qdr:m`/`qdr:y` (+N), `sbd:1` (sort by date), `cdr:1,cd_min:MM/DD/YYYY,cd_max:MM/DD/YYYY` | — | free | freshness/date filters |
| batching | POST a JSON **array** of up to 100 query objects | — | 1 credit per query | mini-batch throughput |

### 7.2 Vertical endpoints (same params)

`/images`, `/videos`, `/news`, `/shopping`, `/scholar`, `/patents`, `/places`, `/maps`, `/autocomplete`, `/reviews`. Scholar/patents are the differentiators for research agents.

### 7.3 Response fields worth capturing (beyond `organic[]`)

| Field | Content |
|---|---|
| `answerBox` | direct answer (`answer`/`snippet`/`snippetHighlighted`) — top digest candidate |
| `knowledgeGraph` | entity card: `title`, `type`, `description`, `descriptionLink`, `attributes{}`, `website`, `rating` |
| `peopleAlsoAsk[]` | `{question, snippet, title, link}` — query-expansion + FAQ content |
| `relatedSearches[]` | `{query}` — follow-up query suggestions |
| `organic[]` extras | `date` (→ `page_age`), `position`, `sitelinks[]`, `rating`/`ratingCount`, `attributes` |
| `topStories[]` | news block when present |
| `credits` | per-request credit counter |

### 7.4 Recommended digest payload (Serper)

`answerBox` first if present, then per result: `title` + `link` + `snippet` + `date?`; append `knowledgeGraph.description` + top `peopleAlsoAsk[].question` as context tail. Use `tbs=qdr:*` for freshness-sensitive queries.

---

## 8. Firecrawl — `POST https://api.firecrawl.dev/v2/search`

Docs: https://docs.firecrawl.dev/api-reference/endpoint/search

### 8.1 Search params

| Param | Type/values | Default | Cost | Unlocks |
|---|---|---|---|---|
| `sources` | `[{type:"web"}\|{type:"images"}\|{type:"news"}]` | `["web"]` | per source | response arrays `data.web[]`/`data.images[]`/`data.news[]` (news items have `snippet`, `date`, `imageUrl`) |
| `categories` | `[{type:"github"}\|{type:"research"}\|{type:"pdf"}]` | `[]` | free | vertical filter (GitHub repos/issues; arXiv/Nature/IEEE/PubMed; PDFs); result `category` field |
| `limit` | 1–100 | 10 | ~2 credits / 10 results (web) | results **per source** |
| `tbs` | `qdr:h/d/w/m/y`, `cdr:1,cd_min:MM/DD/YYYY,cd_max:MM/DD/YYYY`, `sbd:1`; combinable (`sbd:1,qdr:w`) | — | free | freshness + sort-by-date |
| `location` | free text (`"San Francisco,California,United States"`) | — | free | geo (pair with `country`) |
| `country` | ISO code | `US` | free | geo |
| `includeDomains` / `excludeDomains` | hostnames only; mutually exclusive | — | free | domain filter |
| `timeout` | ms | 60000 | free | latency guard |
| `ignoreInvalidURLs` | bool | false | free | drops URLs unusable by other FC endpoints |
| `highlights` | bool | true | free | query-relevant highlighting in `description` |
| `enterprise` | `["anon"]` / `["zdr"]` | — | anon 2 credits/10 results; zdr 10 credits/10 results | zero-retention modes (team-gated) |
| `threatProtection` | object override | org policy | enterprise | per-request risk policy (403 without entitlement) |

Query operators also supported inside `query`: `""`, `-`, `site:`, `filetype:`, `inurl:`, `allinurl:`, `intitle:`, `allintitle:`, `related:`, `imagesize:`, `larger:`.

### 8.2 `scrapeOptions` (turns each result into full content — the quality multiplier)

| Sub-option | Values | Default | Cost |
|---|---|---|---|
| `formats` | `[{type:"markdown"}\|{type:"summary"}\|{type:"html"}\|{type:"rawHtml"}\|{type:"links"}\|{type:"screenshot"}\|{type:"json"}]` | — | each result page scraped = scrape credits on top of search credits; `summary` (LLM condensed) costs more than `markdown` |
| `onlyMainContent` | bool | true | strips nav/ads → smaller, cleaner `markdown` |
| `maxAge` | ms cache age | 2 days | cache hit = faster, no rescrape cost |
| `includeTags` / `excludeTags` | CSS-ish tags | — | extraction narrowing |
| `waitFor` | ms | — | JS settle time |
| `mobile` | bool | false | mobile rendering |
| `removeBase64Images` / `blockAds` / `skipTlsVerification` / `proxy` / `location` / `parsers` / `actions` | — | — | fetch controls |

Response unlocks per result: `markdown`, `summary`, `html`, `rawHtml`, `links[]`, `screenshot`, `metadata{statusCode,error,…}`; top-level `creditsUsed` (telemetry).
### 8.3 Recommended digest payload (Firecrawl)

Default: `title` + `url` + `description` (+`date` for news source, +`category` when categories used). Rich mode: `scrapeOptions: {formats: [{type: "summary"}]}` (condensed, LLM-ready, cheaper than full markdown) or `markdown` capped by budget, `onlyMainContent: true`, `maxAge` high for repeat queries.

---

## 9. Tavily — `POST https://api.tavily.com/search`

Docs: https://docs.tavily.com/documentation/api-reference/endpoint/search

| Param | Type/values | Default | Cost | Unlocks |
|---|---|---|---|---|
| `search_depth` | `basic`/`fast`/`ultra-fast` (1 credit) / `advanced` (2 credits) | `basic` | $$ | `advanced`+`fast` return multiple ≤500-char chunks per URL; `basic`/`ultra-fast` one NLP summary per URL |
| `chunks_per_source` | 1–3 | 3 | free | chunk count per URL in `results[].content` (`<c1> [...] <c2>`); advanced only |
| `max_results` | 0–20 | 5 | free | result count |
| `topic` | `general`/`news`/`finance` | `general` | free | vertical index (`finance` for market queries) |
| `time_range` | `day`/`week`/`month`/`year` or `d`/`w`/`m`/`y` | — | free | pub/updated-date filter |
| `start_date` / `end_date` | `YYYY-MM-DD` | — | free | explicit date window |
| `include_answer` | `false`/`true`/`basic`/`advanced` | `false` | free (included) | top-level `answer` (LLM; `advanced` = more detailed) |
| `include_raw_content` | `false`/`true`/`markdown`/`text` | `false` | free (may add latency) | `results[].raw_content` (cleaned full page; `text` slower) |
| `include_images` | bool | false | free | top-level `images[]` + per-result `images[]` |
| `include_image_descriptions` | bool | false | free | adds `description` per image |
| `include_favicon` | bool | false | free | `results[].favicon` |
| `include_domains` / `exclude_domains` | string[] (≤300 / ≤150) | — | free | domain filter |
| `country` | long enum (`"united states"`, `"germany"`, …) | — | free | boosts country sources; **only with `topic=general`** |
| `auto_parameters` | bool | false | ⚠ may silently upgrade to advanced = 2 credits | Tavily auto-configures params from query intent; explicit values win; `include_answer`/`include_raw_content`/`max_results` always manual; pin `search_depth` to control cost |
| `exact_match` | bool | false | free | quoted phrases must match literally |
| `include_usage` | bool | false | free | `usage.credits` in response |
| `safe_search` | bool | false | enterprise | adult filter; not for fast/ultra-fast |

Response extras: `results[].score` (relevance 0–1), `response_time`, `request_id`, `auto_parameters{}` (chosen params when enabled). Errors: 400 / 401 / 429 / **432** (plan usage limit) / PAYG limit.

### Recommended digest payload (Tavily)

`answer` (when `include_answer=true`) as headline; per result: `title` + `url` + `content` + `score` (expose for re-ranking) — use `search_depth=advanced` + `chunks_per_source=2–3` for high-precision queries (2 credits), `basic` otherwise. `raw_content: "markdown"` only when full text needed.

---

## 10. Linkup — `POST https://api.linkup.so/v1/search`

Docs: https://docs.linkup.so/pages/documentation/endpoints/search/overview

| Param | Type/values | Default | Cost | Unlocks |
|---|---|---|---|---|
| `depth` | `fast` (beta, <1 s, no LLM, query as-is) / `standard` (1–3 s, single-iteration agentic, can scrape 1 URL in query) / `deep` (5–30 s, multi-iteration search+scrape with evaluation) | required | fast/standard $0.005; deep $0.05 (**10×**) | result quality/agentic behavior |
| `outputType` | `searchResults` / `sourcedAnswer` / `structured` | required | +$0.001 for answer/structured (deep: $0.05→$0.055) | `searchResults` → `results[]{type,name,url,content}`; `sourcedAnswer` → `answer` (inline citations) + `sources[]{name,url,snippet}` (returned automatically — no separate includeSources param); `structured` → JSON matching schema |
| `structuredOutputSchema` | JSON Schema | — | requires `structured` | exact response shape |
| `maxResults` | int | — | free | source count cap |
| `includeImages` | bool | false | free | surfaces relevant images |
| `includeDomains` / `excludeDomains` | ≤100 domains | — | free | domain filter |
| `fromDate` / `toDate` | ISO 8601 | — | free | date window |

Notes: title field is `name`; GET with query params also works; `<guidance>` XML inside query steers domain prioritization; standard/deep follow literal multi-step instructions in the query.

### Recommended digest payload (Linkup)

Cheap grounding: `outputType=searchResults`, `depth=standard` → `name` + `url` + `content`. Answer-style: `sourcedAnswer` → `answer` + `sources[].snippet`. Reserve `deep` for hard multi-hop queries only (10× cost).

---

## 11. Perplexity — `POST https://api.perplexity.ai/search`

Docs: https://docs.perplexity.ai/api-reference/search-post (+ filter guides)

| Param | Type/values | Default | Cost | Unlocks |
|---|---|---|---|---|
| `query` | string **or string[] (≤5 — multi-query batching in one call)** | required | one request | batch retrieval |
| `max_results` | 1–20 | 10 | free | result count |
| `max_tokens` | 1–1,000,000 | — | free | TOTAL content-token cap across results |
| `max_tokens_per_page` | 1–1,000,000 | 1024 | free | per-page extraction cap (omit when using `search_context_size`) |
| `search_context_size` | `low`/`medium`/`high` | `high` | free | passage size preset (low=short relevant passages, high=detailed); omit when using max_tokens* |
| `country` | ISO alpha-2 (len 2) | — | free | geo |
| `search_language_filter` | ISO 639-1 codes, ≤20 | — | free | language allow-list |
| `search_domain_filter` | ≤20 domains; allow-list OR `-` deny-list, **never mixed** | — | free | domain control |
| `search_recency_filter` | `hour`/`day`/`week`/`month`/`year` | — | free | publication recency |
| `search_after_date_filter` / `search_before_date_filter` | `MM/DD/YYYY` | — | free | publication-date window |
| `last_updated_after_filter` / `last_updated_before_filter` | `MM/DD/YYYY` | — | free | last-updated window (distinct from publication!) |

Response: `results[]{title,url,snippet,date,last_updated}` + `id`, `server_time`. `snippet` = pre-extracted sub-document chunk (can be long markdown); `last_updated` → `page_age`. Cost: $5/1k requests flat, no token charges. Gotcha: pre-launch API keys 451 — mint fresh key.

### Recommended digest payload (Perplexity)

`title` + `url` + `snippet` (already query-relevant chunks; cap via `search_context_size=medium` or `max_tokens_per_page=512–1024` for tight contexts) + `date` + `last_updated`. Use `search_recency_filter` for news; batched `query[]` to cover multi-facet questions in one billable request.

---

## 12. Parallel — `POST https://api.parallel.ai/v1beta/search`

Docs: https://docs.parallel.ai/api-reference/search-api/search · https://docs.parallel.ai/search/best-practices · https://docs.parallel.ai/search/advanced-search-settings

| Param | Type/values | Default | Cost | Unlocks |
|---|---|---|---|---|
| `objective` | string ≤5000 chars — declarative natural-language research goal; may include source/freshness guidance | — (objective XOR search_queries required; GA /v1 requires search_queries) | free | semantic (non-keyword) retrieval — the differentiator |
| `search_queries` | 1–5 keyword queries, 3–6 words each, ≤200 chars | — | free | classic query guidance; 2–3 recommended |
| `mode` | `turbo` (lowest latency/cost) / `basic` / `advanced` (highest quality, more retrieval+compression); legacy `fast`/`one-shot`→basic, `agentic`→advanced | `advanced` | affects price | quality/latency tier |
| `processor` | `base` / `pro` (v1beta API ref) | `base` | pro tier higher price | may raise max_results ceiling |
| `max_results` | int (1–40, processor-limited) | 10 | priced per result count | result count |
| `excerpts` | `{max_chars_per_result: int}` | on by default | free | per-result excerpt size in `results[].excerpts[]` |
| `max_chars_total` | int | dynamic (based on queries/objective/client_model) | free | total excerpt budget across all results |
| `source_policy` | `{include_domains: string[], exclude_domains: string[], after_date: date}` | — | free | domain allow/deny + freshness floor (docs warn: over-filtering hurts quality) |
| `fetch_policy` | `{max_age_seconds: int (min 600), timeout_seconds: int}` | cached/indexed content | live fetch adds latency | cache-vs-live freshness control |
| `location` | ISO alpha-2 (subset only; invalid e.g. `uk` ignored with warning) | — | free | geo targeting |
| `client_model` | string (e.g. `"claude-opus-4-7"`) | — | free | model-specific result optimization |
| `session_id` | string | — | free | groups related search/extract calls per task |

Beta header: the v1beta path historically requires opt-in (`parallel-beta: search-excerpt-2025-10-10`; SDKs use `parallel.beta.search`). GA `/v1` exists per LangChain integration — verify current header requirement against docs before shipping.

Response: `{search_id, results:[{url,title,publish_date,excerpts:string[]}], warnings, usage:[{name:"sku_search",count}], session_id}`. Excerpts are dense token-compressed multi-paragraph passages (already "digested"). `publish_date` → `page_age`. Companion Extract API (`/v1beta/extract`) with `full_content` for full pages.

### Recommended digest payload (Parallel)

Send BOTH `objective` (task semantics) + 2–3 `search_queries`; digest = `title` + `url` + `publish_date` + joined `excerpts[]` capped with `excerpts.max_chars_per_result: 1500–3000` and `max_chars_total` for window safety. Nothing else needed — excerpts replace any scrape step.

---

## 13. SearchAPI.io — `GET https://www.searchapi.io/api/v1/search`

Docs: https://www.searchapi.io/docs/google (+ per-engine pages)

### 13.1 Engine selection (`engine` param — beyond `google`)

`google` · `google_news` · `google_scholar` · `google_videos` · `google_local` · `google_forums` (discussions) · `google_ai_mode` (AI Overview w/ citations) · `google_images`, `google_shopping`, `google_patents`, `google_finance`, `google_flights`, `google_maps`… plus **`bing`**, `bing_news`, `duckduckgo`, `yahoo`, `baidu`, `yandex`, `amazon`, `ebay` — 40+ total. Scholar + ai_mode + forums are the high-value ones for agents.

### 13.2 Google-engine params

| Param | Values | Default | Unlocks |
|---|---|---|---|
| `location` | canonical location / lat-lon | — | geo |
| `gl` | country code | `us` | country |
| `hl` | lang code | `en` | UI language |
| `lr` | `lang_xx` (e.g. `lang_en`) | — | restrict result language |
| `cr` | country restrict | — | restrict by host country |
| `device` | `desktop`/`mobile`/`tablet` | `desktop` | SERP variant |
| `time_period` | `last_hour`/`last_day`/`last_week`/`last_month`/`last_year`/`custom` | — | freshness |
| `time_period_min` / `time_period_max` | `MM/DD/YYYY` (with `custom`) | — | date window |
| `safe` | `active`/`off` | — | adult filter |
| `num` | int | 10 | results per page |
| `page` | int | 1 | pagination |
| auth | `api_key` query param or `Authorization: Bearer` | — | — |

### 13.3 Rich response fields to capture

`organic_results[]{position,title,link,displayed_link,snippet,snippet_highlighted_words,date,favicon,sitelinks,rich_snippet}` · `knowledge_graph` · `answer_box` · `related_questions[]` · `related_searches[]` · `local_results[]` · `search_information{total_results,time_taken_displayed,organic_results_state}` · `search_metadata{id,status,total_time_taken,json_url}` (replay/debug). Pay-per-success billing; hourly caps per plan (check `GET /api/v1/me`).

### Recommended digest payload (SearchAPI.io)

`answer_box` first; per result `title`+`link`+`snippet`+`date`; tail with `knowledge_graph` + `related_questions`. Use `engine=google_scholar` for academic, `google_forums` for opinions, `time_period` for freshness.

---

## 14. SerpAPI — `GET https://serpapi.com/search`

Docs: https://serpapi.com/search-api

### 14.1 Engine selection (`engine` param)

`google` (full SERP) · **`google_light` / `google_light_fast`** (stripped, faster response focused on organic results — use `google_light_fast` for `num=100`; Google killed deep paging/`num>10` on the plain `google` engine in Sep 2025) · `google_news` · `google_scholar` · `google_local` · `google_images` · `google_videos` · `google_shopping` · `google_patents` · `google_finance` · **`bing`** · `baidu` · `yahoo` · `yandex` · `duckduckgo` · `ebay` etc. Billing is per successful search regardless of engine tier; light engines return less payload, not less cost — the win is latency/size.

### 14.2 Google params

| Param | Values | Default | Unlocks |
|---|---|---|---|
| `location` / `uule` | canonical location / encoded | — | geo |
| `google_domain` | e.g. `google.co.uk` | `google.com` | regional Google |
| `gl` | country | `us` | country |
| `hl` | language | `en` | UI language |
| `lr` | `lang_xx` | — | language restrict |
| `cr` | country restrict | — | host-country restrict |
| `tbs` | `qdr:h/d/w/m/y` (+N, e.g. `qdr:d10`), `sbd:1` (sort by date), `cdr:1,cd_min:MM/DD/YYYY,cd_max:MM/DD/YYYY`, `li:1` (verbatim) | — | freshness/sort/verbatim |
| `safe` | `active`/`off` | — | adult filter |
| `nfpr` | `0`/`1` | 0 | 1 = exclude auto-corrected results |
| `filter` | `0`/`1` | 1 | 0 = include similar/omitted results |
| `start` | int | 0 | pagination offset |
| `num` | 10–100 (see engine caveat) | 10 | page size |
| `device` | `desktop`/`mobile`/`tablet` | `desktop` | SERP variant |
| `no_cache` | bool | false | bypass SerpAPI cache (fresh SERP) |
| `async` | bool | false | async retrieval via Search Archive |
| `json_restrictor` | JMESPath-ish | — | shrink payload server-side |

### 14.3 Rich response fields to capture

`answer_box` · `knowledge_graph{title,type,description,source,attributes,…}` · `related_questions[]{question,snippet,title,link}` · `related_searches[]` · `local_results.places[]` · `organic_results[]{position,title,link,displayed_link,snippet,date,sitelinks,rich_snippet,about_this_result.source.description,favicon,thumbnail,cached_page_link}` · `search_information{organic_results_state,total_results,query_displayed}` · `search_metadata{id,status,json_endpoint,total_time_taken}` (replay without new credit via json_endpoint).

### Recommended digest payload (SerpAPI)

Same pattern as Serper: `answer_box` → per-result `title`+`link`+`snippet`+`date` → `knowledge_graph.description` + `related_questions` tail. Prefer `engine=google_light_fast` when only organic data matters; `tbs=qdr:*` for freshness; `about_this_result.source.description` as a credibility hint per result.

---

## 15. Cross-provider cheat sheet

### 15.1 Which provider unlocks which agent-relevant capability

| Capability | Best providers |
|---|---|
| Free full page content | Jina (`X-Max-Tokens`), DDGS `extract()`, Firecrawl (credits), Exa `contents.text` ($0.001/pg), Tavily `include_raw_content` (free), Brave LLM Context |
| Query-focused excerpts (cheapest rich digest) | Exa highlights, Brave LLM Context, Parallel excerpts, Perplexity snippet |
| LLM summary per result | Exa `contents.summary`, Firecrawl `summary` format, Tavily `include_answer` (whole-query) |
| Structured output (schema) | Exa `outputSchema`, Linkup `structured`, Tavily answer (unstructured) |
| Date/recency filter | ALL except Ollama. Explicit windows: Exa, Linkup, Perplexity, Tavily (`start_date`), Serper/SerpAPI/Firecrawl (`tbs cdr:`), SearchAPI (`time_period_*`), Parallel (`after_date`) |
| Sort by date | Serper/SerpAPI/Firecrawl `tbs=sbd:1` |
| Geo targeting | Serper/SerpAPI/SearchAPI (`location` city-level), Firecrawl, Brave (`country`+x-loc headers), Jina (`gl`/`X-Proxy`/`X-Locale`), Exa (`userLocation`), Perplexity/Tavily/Parallel (country) |
| Verticals | Exa categories, Firecrawl categories, Tavily topics, Serper/SerpAPI/SearchAPI vertical endpoints, DDGS methods, Brave result_filter/endpoints, SearXNG categories |
| `page_age` source for Anthropic wire format | Brave (`page_age`), Perplexity (`last_updated`), Exa (`publishedDate`), Parallel (`publish_date`), Serper/SerpAPI/SearchAPI (`date`), Firecrawl news (`date`), SearXNG (`publishedDate`), DDGS news (`date`) |
| Answer engine included | Tavily, Linkup, Brave Answers, Perplexity (Sonar, separate), Jina DeepSearch |
| Multi-query batch | Perplexity (≤5), Parallel `search_queries` (≤5), Exa `additionalQueries`, Serper (≤100 array) |

### 15.2 Suggested implementation priority (quality gain per effort)

1. **Brave LLM Context mode** — same price, pre-extracted content, kills the scrape step.
2. **Exa `contents.highlights`** — one flag, 10× token efficiency, `$0.001/page`.
3. **Date filters unified** — map one proxy-level `freshness`/`date_range` option onto each provider's native knob (table 15.1 row 6) → populates Anthropic `page_age`.
4. **Tavily `search_depth` + Perplexity `search_context_size` cost guards** — pin to cheap tiers by default; `auto_parameters` off by default.
5. **Serper/SerpAPI rich blocks** (`answerBox`, `knowledgeGraph`, `peopleAlsoAsk`) — free extra context already in the response.
6. **Firecrawl `scrapeOptions.formats=[{type:"summary"}]` + `maxAge`** — cheap full-content mode.

## 16. Doc URLs used

- Anthropic: https://docs.claude.com/en/docs/agents-and-tools/tool-use/web-search-tool
- Exa: https://docs.exa.ai/reference/search-api-guide-for-coding-agents · https://docs.exa.ai/reference/contents-api-guide-for-coding-agents · https://docs.exa.ai/reference/search
- Ollama: https://docs.ollama.com/capabilities/web-search
- DDGS: https://github.com/deedy5/ddgs
- Brave: https://api-dashboard.search.brave.com/app/documentation/web-search/query · https://github.com/brave/brave-search-skills · https://github.com/brave/brave-search-mcp-server
- SearXNG: https://docs.searxng.org/dev/search_api.html
- Jina: https://jina.ai/reader · https://github.com/jina-ai/reader · https://s.jina.ai/openapi.json
- Serper: https://serper.dev/playground · https://docs.litellm.ai/docs/search/serper
- Firecrawl: https://docs.firecrawl.dev/api-reference/endpoint/search
- Tavily: https://docs.tavily.com/documentation/api-reference/endpoint/search
- Linkup: https://docs.linkup.so/pages/documentation/endpoints/search/overview
- Perplexity: https://docs.perplexity.ai/api-reference/search-post
- Parallel: https://docs.parallel.ai/api-reference/search-api/search · https://docs.parallel.ai/search/best-practices · https://docs.parallel.ai/search/advanced-search-settings · https://github.com/parallel-web/langchain-parallel
- SearchAPI.io: https://www.searchapi.io/docs/google · https://docs.litellm.ai/docs/search/searchapi
- SerpAPI: https://serpapi.com/search-api
