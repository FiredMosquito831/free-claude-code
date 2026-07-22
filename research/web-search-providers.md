# Web Search Provider APIs — Adapter Implementation Reference

Research date: 2026-07. Unified extraction target: `results: [{title, url, snippet|content}]`.

## Unified field mapping (quick reference)

| Provider | Method/Endpoint | Auth | Results path | title | url | snippet/content |
|---|---|---|---|---|---|---|
| Exa | POST api.exa.ai/search | `x-api-key` | `results[]` | `title` | `url` | `text`/`highlights[]` (opt-in via `contents`) |
| Ollama | POST ollama.com/api/web_search | `Authorization: Bearer` | `results[]` | `title` | `url` | `content` |
| DDGS | (library, no HTTP) | none | list of dicts | `title` | `href` | `body` |
| Brave | GET api.search.brave.com/res/v1/web/search | `X-Subscription-Token` | `web.results[]` | `title` | `url` | `description` |
| Firecrawl | POST api.firecrawl.dev/v2/search | `Authorization: Bearer` | `data.web[]` | `title` | `url` | `description` (+`markdown` opt-in) |
| Tavily | POST api.tavily.com/search | `Authorization: Bearer tvly-…` | `results[]` | `title` | `url` | `content` |
| Jina | GET s.jina.ai/{query} | `Authorization: Bearer jina_…` | `data[]` (JSON mode) | `title` | `url` | `content` |
| SearXNG | GET {instance}/search?format=json | none | `results[]` | `title` | `url` | `content` |
| Serper | POST google.serper.dev/search | `X-API-KEY` | `organic[]` | `title` | `link` | `snippet` |
| Linkup | POST api.linkup.so/v1/search | `Authorization: Bearer` | `results[]` | `name` | `url` | `content` |
| Parallel | POST api.parallel.ai/v1beta/search | `x-api-key` + beta header | `results[]` | `title` | `url` | `excerpts[]` |
| Perplexity | POST api.perplexity.ai/search | `Authorization: Bearer pplx-…` | `results[]` | `title` | `url` | `snippet` |
| SearchAPI.io | GET www.searchapi.io/api/v1/search | `api_key` query param | `organic_results[]` | `title` | `link` | `snippet` |
| SerpAPI | GET serpapi.com/search | `api_key` query param | `organic_results[]` | `title` | `link` | `snippet` |

---

## 1. Exa (exa.ai) — PRIORITY

- **(a) Official HTTP API:** Yes — first-class product.
- **(b) Endpoint/auth:** `POST https://api.exa.ai/search`; header `x-api-key: $EXA_API_KEY` (keys at dashboard.exa.ai/api-keys). Also `POST https://api.exa.ai/contents` for URL→content.
- **(c) Minimal payload:** `{"query": "latest developments in LLMs", "contents": {"highlights": true}}`
  - `type`: `auto` (default), `instant` (~250ms), `fast`, `deep-lite`, `deep`, `deep-reasoning` (legacy `neural`/`keyword` still seen). Also `numResults`, `includeDomains`/`excludeDomains`, `category` (`company`, `people`, `research paper`, etc.).
- **(d) Response shape:** top-level `requestId`, `searchType`, `results[]`, `costDollars.total`. Each result: `title`, `url`, `id` (= URL), `publishedDate`, `author`, `image`, `favicon`. Content fields only if requested under `contents`: `text` (full page; limit via `contents.text.maxCharacters`), `highlights: string[]` (+`highlightScores`), `summary`. NOTE: on `/search`, content params MUST be nested in `contents`; on `/contents` they are top-level. `useAutoprompt`/`livecrawl`/`numSentences` are deprecated — never emit.
- **(e) Free tier/pricing:** $20 credits on sign-up + $10/month free ongoing. Search $7/1k requests (up to 10 results), +$1/1k per extra result above 10; AI summaries $1/1k pages; Deep search $12–15/1k; Contents $1/1k pages. Startup/edu grants: $1000 credits.
- **(f) Python SDK:** official `exa-py` (`from exa_py import Exa; Exa().search(q, contents={"highlights": True})`). Reads `EXA_API_KEY` env.
- **(g) Rate limits/gotchas:** 429 on rate limit (error body `{"error": "…"}`); `category: company/people` does NOT support `excludeDomains`/date filters; default `numResults` 10.
- **(h) Docs:** https://exa.ai/docs/reference/search-api-guide-for-coding-agents · https://exa.ai/docs/reference/contents-api-guide-for-coding-agents · https://exa.ai/pricing

## 2. Ollama Web Search — PRIORITY

- **(a) Official HTTP API:** Yes — hosted API launched Sept 2025.
- **(b) Endpoint/auth:** `POST https://ollama.com/api/web_search` and `POST https://ollama.com/api/web_fetch`. Auth: `Authorization: Bearer $OLLAMA_API_KEY` (create at ollama.com/settings/keys; free account required). Same paths on a local server (`http://localhost:11434/api/web_search`, no auth) proxy to the hosted API when signed in.
- **(c) Minimal payload:** `{"query": "what is ollama?"}` — optional `max_results` (int, default 5, **max 10**). web_fetch: `{"url": "…"}`.
- **(d) Response shape:** `{"results": [{"title": str, "url": str, "content": str}]}` — `content` is a relevant snippet. web_fetch returns `{title, content, links[]}`.
- **(e) Free tier/pricing:** Included with a free Ollama account ("generous free tier for individuals"); higher limits via paid cloud plans (Pro $20/mo, Max $100/mo). Exact free-tier numbers unpublished.
- **(f) Python SDK:** Yes — official `ollama` **>= 0.6.0** exposes `ollama.web_search(query, max_results=…)` → `WebSearchResponse(results=[WebSearchResult(title,url,content)])` and `ollama.web_fetch(url)` → `WebFetchResponse(title,content,links)`. Both can be passed directly as `tools=[web_search, web_fetch]` to `ollama.chat`. Reads `OLLAMA_API_KEY` env. JS SDK: `client.webSearch`/`webFetch`.
- **(g) Rate limits/gotchas:** max 10 results/request; results can be thousands of tokens (docs recommend ≥32k context); limits unpublished — back off on 429; official MCP server wrapper exists.
- **(h) Docs:** https://docs.ollama.com/capabilities/web-search · https://ollama.com/blog/web-search · https://ollama.com/pricing

## 3. DuckDuckGo — PRIORITY

- **(a) Official HTTP search API:** **No.** DDG has no official web-results API. The official **Instant Answer API** `GET https://api.duckduckgo.com/?q=X&format=json&no_html=1` returns only instant answers (`AbstractText`, `AbstractURL`, `Answer`, `RelatedTopics[]`, `Results[]` — mostly official-site/disambiguation entries), NOT a general SERP. Verified live: query "ollama" returned just the Wikipedia abstract + one "Official site" entry. Useless as a general search backend.
- **(b–d) Practical route — `ddgs` package** (formerly `duckduckgo_search`, renamed 2025; old name emits a RuntimeWarning: "renamed to `ddgs`"):
  - Unofficial scraper/metasearch — no API key, free. Now multi-engine: text backends include `bing`, `brave`, `duckduckgo`, `google`, `mojeek`, `startpage`, `yandex`, `yahoo`, `wikipedia`.
  - Usage: `from ddgs import DDGS; DDGS().text("python programming", max_results=5)` → `list[dict]` keyed **`title`, `href`, `body`**. Also `images()`, `videos()`, `news()`, `books()`, `extract(url)` (URL→markdown/text).
  - Params: `region` ("us-en"), `safesearch` ("on|moderate|off"), `timelimit` ("d|w|m|y"), `page`, `backend` ("auto" or comma list).
  - Extras: CLI, FastAPI server (`pip install ddgs[api]`, `ddgs api`, `/search/text` etc.), MCP server (`ddgs[mcp]`).
- **(e) Free tier:** Entirely free/keyless; README disclaimer "educational purposes only" — ToS risk since it scrapes engines.
- **(f) Python SDK:** `ddgs` (unofficial). Legacy alias `duckduckgo_search`.
- **(g) Rate limits/gotchas:** Engines rate-limit by IP — expect ratelimit exceptions/403s under load; mitigate with backoff, low concurrency, `DDGS(proxy=…)`. Field names vary per method (news: `url`/`body`/`date`/`source`; text: `href`). Backend availability drifts as engines change markup — pin version, add retries/fallback.
- **(h) Docs:** https://github.com/deedy5/ddgs · https://api.duckduckgo.com/ (live-verified)

## 4. Brave Search API

- **(a) Official HTTP API:** Yes.
- **(b) Endpoint/auth:** `GET https://api.search.brave.com/res/v1/web/search?q=…`; header **`X-Subscription-Token: <key>`** (key from api-dashboard.search.brave.com). Separate AI-oriented LLM Context endpoint (same key); Answers API `POST /res/v1/chat/completions` (OpenAI-compatible).
- **(c) Minimal request:** query params only — `q` (required), `count` (max 20, default 20), `offset` (max 9), `country`, `search_lang`, `freshness` (`pd|pw|pm|py` or `2022-04-01to2022-07-30`), `safesearch`, `extra_snippets=true`.
- **(d) Response shape:** `web.results[]` → each has `title`, `url`, `description` (snippet), plus `age`, `page_age`, `profile`, `meta_url`; `extra_snippets: string[]` when enabled (≤5). Pagination hint: `query.more_results_available` (bool). Top level also has `type`, `discussions`, `locations`, `videos`, `news`, `rich` blocks.
- **(e) Free tier/pricing:** **$5 in free credits every month**, auto-applied to all plans. Paid web search billed per 1k requests against credits (verify current per-1k table; historically ~$3–5/1k). Answers API: $4/1k requests + $5/1M tokens.
- **(f) Python SDK:** No official Python SDK (plain GET).
- **(g) Rate limits/gotchas:** Search plan 50 QPS (Answers 2 QPS); `offset` caps at 9 → ~200 results max via pagination; one query per request (no batching); local POI `id`s expire ~8h.
- **(h) Docs:** https://api-dashboard.search.brave.com/app/documentation/web-search/get-started · https://brave.com/search/api/

## 5. Firecrawl

- **(a) Official HTTP API:** Yes.
- **(b) Endpoint/auth:** `POST https://api.firecrawl.dev/v2/search` (v1 deprecated); header `Authorization: Bearer fc-…`.
- **(c) Minimal payload:** `{"query": "firecrawl", "limit": 10}` — optional `sources: ["web"|"images"|"news"]`, `categories` (`github`,`research`,`pdf`), `includeDomains`/`excludeDomains` (mutually exclusive), `tbs` (time filter e.g. `qdr:w`), `location`, `country`, `timeout`, `scrapeOptions` (e.g. `{"formats":[{"type":"markdown"}]}` for full page content per result).
- **(d) Response shape:** `{success, data: {web: [{title, description, url, markdown?, html?, links?, metadata{statusCode,error,…}}], images?, news?}, warning, id, creditsUsed}`. Default (no scrapeOptions) returns only `url/title/description`. News results use `snippet` instead of `description`.
- **(e) Free tier/pricing:** Credit model; free plan one-time credit grant (~500 credits — verify current at firecrawl.dev/pricing); standard web search ~2 credits / 10 results; scraping each result costs extra credits; ZDR/anon options priced per 10 results.
- **(f) Python SDK:** official `firecrawl-py` (`from firecrawl import Firecrawl; Firecrawl(api_key=…).search(query, limit=5)`).
- **(g) Rate limits/gotchas:** `limit` 1–100 per source; `query` ≤500 chars; `timeout` default 60s; using `scrapeOptions` multiplies credit cost; enterprise `threatProtection` overrides can 403.
- **(h) Docs:** https://docs.firecrawl.dev/api-reference/endpoint/search

## 6. Tavily

- **(a) Official HTTP API:** Yes.
- **(b) Endpoint/auth:** `POST https://api.tavily.com/search`; header `Authorization: Bearer tvly-YOUR_API_KEY`.
- **(c) Minimal payload:** `{"query": "who is Leo Messi?"}` — key options: `search_depth` (`basic`/`advanced`/`fast`/`ultra-fast`), `max_results` (0–20, default 5), `topic` (`general`/`news`/`finance`), `time_range` (`day|week|month|year`), `include_answer`, `include_raw_content` (`true|markdown|text`), `include_domains`/`exclude_domains`, `auto_parameters`.
- **(d) Response shape:** `{query, answer?, images[], results: [{title, url, content, score, raw_content, favicon}], response_time, usage.credits, request_id}`. `results[].content` = NLP summary (basic) or ≤500-char chunks joined with `[...]` (advanced, `chunks_per_source` 1–3).
- **(e) Free tier/pricing:** Free plan **1,000 credits/month**, no card. Cost: basic/fast/ultra-fast = 1 credit; advanced = 2 credits. `auto_parameters` may silently upgrade to advanced — pin `search_depth` to control cost.
- **(f) Python SDK:** official `tavily-python` (`from tavily import TavilyClient; TavilyClient(api_key=…).search(q)`).
- **(g) Rate limits/gotchas:** Errors shaped `{"detail": {"error": "…"}}`; 401 missing/invalid key; 429 excessive requests; **432** = plan usage limit. `country` only with `topic=general`.
- **(h) Docs:** https://docs.tavily.com/documentation/api-reference/endpoint/search

## 7. Jina (Reader / Search Foundation)

- **(a) Official HTTP API:** Yes — URL-prefix endpoints: Reader `https://r.jina.ai/{url}`, Search **`https://s.jina.ai/{query}`**.
- **(b) Endpoint/auth:** `GET https://s.jina.ai/<url-encoded query>`; header `Authorization: Bearer jina_…`. **s.jina.ai requires an API key**; r.jina.ai works keyless at low limits. Structured output: `Accept: application/json`. Other control headers: `X-Timeout`, `X-Engine: browser`, `X-Return-Format`, `X-Token-Budget`.
- **(c) Minimal request:** `curl "https://s.jina.ai/what%20is%20ollama" -H "Authorization: Bearer $JINA_API_KEY" -H "Accept: application/json"`.
- **(d) Response shape (JSON mode):** `{"code": 200, "status": …, "data": [{"title", "url", "content", "description"?}]}` — Search mode returns ~5 entries, each the Reader-extraction of a SERP hit; `content` is full page text (LONG — budget/truncate downstream). Without `Accept: application/json`, response is plain markdown text, not JSON.
- **(e) Free tier/pricing:** New keys get **10M free tokens** pooled across Reader/Embeddings/Reranker/Classifier (no card); then token top-ups. Keyless r.jina.ai at reduced rate.
- **(f) Python SDK:** No dedicated official SDK (plain HTTP; official MCP server exists).
- **(g) Rate limits/gotchas:** r.jina.ai ≈20 RPM keyless, ~500 RPM free key, higher premium; s.jina.ai blocked without key. Token-billed per tokens processed (cost varies with page size), not per request. Anonymous shared-IP pool hits abuse blocks (451) on some domains; a key fixes most.
- **(h) Docs:** https://jina.ai/reader · https://github.com/open-webui/open-webui/discussions/6854 (s.jina.ai usage) · https://aicredits.dev/submissions/156 (free-token detail)

## 8. SearXNG

- **(a) Official HTTP API:** Yes (self-hosted metasearch over 70+ engines).
- **(b) Endpoint/auth:** `GET/POST {instance}/search` (also `/`); typically **no auth** (per-deployment basic auth possible).
- **(c) Minimal request:** `GET {instance}/search?q=ollama&format=json` — params: `categories`, `language`, `pageno`, `time_range` (`day|month|year`), `safesearch` (0–2).
- **(d) Response shape (format=json):** `{query, number_of_results, results: [{url, title, content, engine, engines[], score, category, publishedDate?, img_src?}], answers[], corrections[], infoboxes[], suggestions[], unresponsive_engines[]}`.
- **(e) Free tier/pricing:** Free/open source (AGPL); public instances free but unreliable.
- **(f) Python SDK:** None needed (plain GET).
- **(g) Rate limits/gotchas:** **#1 gotcha: `format=json` must be enabled in the instance's `settings.yml` (`search: formats:`) — most public instances disable it and return 403 Forbidden.** Self-host (Docker) for production. Public instances bot-check/rate-limit aggressively; upstream engine blocks produce partial results (`unresponsive_engines`).
- **(h) Docs:** https://docs.searxng.org/dev/search_api.html

## 9. Serper (serper.dev)

- **(a) Official HTTP API:** Yes — Google SERP proxy.
- **(b) Endpoint/auth:** `POST https://google.serper.dev/search` (also `/news`, `/images`, `/places`, `/scholar`); header **`X-API-KEY: <key>`**.
- **(c) Minimal payload:** `{"q": "coffee", "gl": "us", "hl": "en"}` — optional `num` (default 10), `page`, `tbs`, `autocorrect`.
- **(d) Response shape:** `{searchParameters, organic: [{title, link, snippet, position, date?, sitelinks?}], knowledgeGraph?, peopleAlsoAsk[], relatedSearches[], answerBox?, credits}`.
- **(e) Free tier/pricing:** **2,500 free queries, one-time on signup**, no card. Paid ~$0.30–1.00/1k by volume; credits expire after 6 months.
- **(f) Python SDK:** No official SDK (plain requests).
- **(g) Rate limits/gotchas:** Fast (1–2s); Google-only; free credits are one-time, not monthly.
- **(h) Docs:** https://serper.dev · corroborated: https://metacpan.org/pod/Net::Async::WebSearch::Provider::Serper

## 10. Linkup

- **(a) Official HTTP API:** Yes.
- **(b) Endpoint/auth:** `POST https://api.linkup.so/v1/search` (GET with query params also works); header `Authorization: Bearer <key>`.
- **(c) Minimal payload:** `{"q": "What is Microsoft's 2024 revenue?", "depth": "standard", "outputType": "searchResults"}` — `depth`: `fast`(beta)/`standard`/`deep`; `outputType`: `searchResults`/`sourcedAnswer`/`structured` (+`structuredOutputSchema`); optional `fromDate`/`toDate`, `includeDomains` (≤50)/`excludeDomains`, `includeImages`.
- **(d) Response shape:** `searchResults` → `{"results": [{"type": "text", "name", "url", "content"}]}`; `sourcedAnswer` → `{"answer": str, "sources": [{"name", "url", "snippet"}]}`. NOTE: title field is **`name`**, not `title`.
- **(e) Free tier/pricing:** **$20 free credit on signup, topped back up to $20 monthly.** fast/standard `searchResults` $0.005; with answer/structured $0.006; deep $0.05–0.055.
- **(f) Python SDK:** official `linkup-python` (`from linkup import LinkupClient`).
- **(g) Gotchas:** deep is 10× the price; `<guidance>` XML in query for domain prioritization.
- **(h) Docs:** https://docs.linkup.so/pages/documentation/endpoints/search/overview · https://docs.linkup.so/pages/documentation/development/pricing

## 11. Parallel

- **(a) Official HTTP API:** Yes (Search API beta, June 2025).
- **(b) Endpoint/auth:** `POST https://api.parallel.ai/v1beta/search`; headers `x-api-key: <key>` plus beta opt-in (`parallel-beta: search-excerpt-2025-10-10` per docs). Verify exact header names against docs.parallel.ai before shipping.
- **(c) Minimal payload:** `{"objective": "natural-language goal", "search_queries": ["q1","q2"], "max_results": 10, "excerpts": {"max_chars_per_result": …}}` — `objective` is the differentiator: declarative semantic intent instead of keywords.
- **(d) Response shape (verbatim from docs):** `{search_id, results: [{url, title, publish_date, excerpts: [str,…]}], warnings, usage: [{name: "sku_search", count}], session_id}`. Snippet equivalent = `excerpts[]` (dense token-compressed passages, often multi-paragraph).
- **(e) Free tier/pricing:** Pay-per-use "starts at $0.005 for 10 results" (processor tiers affect price). Trial-credit details unverified — check docs.parallel.ai.
- **(f) Python SDK:** official SDK + MCP server (docs.parallel.ai).
- **(g) Gotchas:** beta headers required; excerpts are long.
- **(h) Docs:** https://docs.parallel.ai/search/search-quickstart · https://parallel.ai/products/search

## 12. Perplexity Search API

- **(a) Official HTTP API:** Yes (launched 2025-09-25).
- **(b) Endpoint/auth:** `POST https://api.perplexity.ai/search` (EU: `https://eu.api.perplexity.ai/search`); header `Authorization: Bearer pplx-…`. Distinct from Sonar `/chat/completions` (synthesized answer + `search_results`).
- **(c) Minimal payload:** `{"query": "prompt engineering", "max_results": 10}` — `query` may be a list of ≤5 queries (batch); options: `max_results` 1–20 (default 10), `max_tokens_per_page` (default 1024), `country` (ISO alpha-2), `search_recency_filter` (`hour|day|week|month|year`), `search_after/before_date_filter`, `search_domain_filter` (≤20 domains; allow OR `-deny`, never mixed), academic mode.
- **(d) Response shape:** `{id, results: [{title, url, snippet, date, last_updated}], server_time}`. `snippet` = pre-extracted sub-document chunk (can be long markdown).
- **(e) Free tier/pricing:** **$5/1,000 requests, no token charges** on raw search. No meaningful free tier (prepaid API credit).
- **(f) Python SDK:** official `perplexityai` (`from perplexity import Perplexity; Perplexity().search.create(query=…, max_results=5)`).
- **(g) Rate limits/gotchas:** **Old API keys fail with 451 `api_key_created_before_search_api_cutoff` — mint a fresh key.** Rate limits scale with cumulative-spend tiers. Don't conflate Sonar (answer) vs Search (results) shapes.
- **(h) Docs:** https://docs.perplexity.ai · corroborated: https://aiengineerguide.com/blog/domain-filtering-in-perplexity-search-api/ · https://www.testingcatalog.com/perplexity-unveils-search-api-for-startups-enterprises-and-agent-builders/

## 13. SearchAPI.io

- **(a) Official HTTP API:** Yes — Google/Bing/40+ engine SERP proxy.
- **(b) Endpoint/auth:** `GET https://www.searchapi.io/api/v1/search?engine=google&q=…&api_key=…` — key as **`api_key` query param** or `Authorization: Bearer`. Usage endpoint: `GET /api/v1/me`.
- **(c) Minimal request:** `engine=google&q=<query>` (+`gl`, `hl`, `location`, `num`, `time_period`, `safe`).
- **(d) Response shape:** SerpAPI-compatible: `organic_results[]` → `{position, title, link, snippet, displayed_link, date?}`; plus `knowledge_graph`, `answer_box`, `related_questions`, `search_metadata`, `search_information`.
- **(e) Free tier/pricing:** **100 free requests (one-time, no card)**; paid from ~$40/mo ($4–10/1k by tier); hourly rate limits per plan (via `/api/v1/me`).
- **(f) Python SDK:** No first-party SDK (LiteLLM/LangChain integrations exist).
- **(g) Gotchas:** pay-per-success; engine-specific params vary.
- **(h) Docs:** https://www.searchapi.io/ · https://www.searchapi.io/docs/account-api

## 14. SerpAPI

- **(a) Official HTTP API:** Yes — incumbent Google SERP API (many engines).
- **(b) Endpoint/auth:** `GET https://serpapi.com/search?engine=google&q=…&api_key=…` (`api_key` query param; `/search.json` alias).
- **(c) Minimal request:** `engine=google&q=coffee&api_key=…` (+`google_domain`, `hl`, `gl`, `num`, `location`). Cheaper engines: `google_light`, `google_light_fast`.
- **(d) Response shape:** `organic_results[]` → `{position, title, link, displayed_link, snippet, favicon, sitelinks?}`; plus `knowledge_graph`, `local_results`, `shopping_results`, `search_metadata{id,status}`.
- **(e) Free tier/pricing:** **250 free searches/month** (raised from 100 in late 2025); paid from ~$75/mo — pricey vs peers.
- **(f) Python SDK:** official **`google-search-results`** (`from serpapi import GoogleSearch; GoogleSearch({…}).get_dict()`).
- **(g) Gotchas:** num>10 unreliable on plain `google` engine (Google dropped deep paging) — use `google_light_fast` for num=100; monthly credits don't roll over.
- **(h) Docs:** https://serpapi.com/search-api · https://serpapi.com/blog/get-google-organic-search-results-instantly-with-google-light-fast-api/

## 15. Crawl4AI

- **(a) Official HTTP API:** Not a search API — open-source **crawler/scraper library** (LLM-ready extraction). No hosted search endpoint, no key.
- **(b) Access:** `pip install crawl4ai` (library) or self-hosted Docker server with REST (`POST /crawl`, optional token when configured).
- **(c/d) Usage:** `AsyncWebCrawler().arun(url)` → `markdown`, `extracted_content`, `links`, `media`. In an adapter stack it's the **fetch/read leg** (like Jina Reader / Firecrawl scrape), not the SERP leg — pair with DDGS/SearXNG/Serper for discovery.
- **(e) Pricing:** Free, open source (Apache-2.0).
- **(g) Gotchas:** heavyweight (Playwright); run `crawl4ai-setup` post-install; no built-in web-search ranking.
- **(h) Docs:** https://docs.crawl4ai.com/

---

## Adapter recommendations for the
## Adapter recommendations for the FCC proxy

1. **Zero-config fallback chain:** `ddgs` (keyless) → self-hosted SearXNG → user-keyed providers.
2. **Best free tiers for end users:** Ollama (free account), Tavily (1k/mo), Brave ($5/mo credit), Linkup ($20/mo top-up), Serper (2.5k one-time), Jina (10M tokens), Exa ($10/mo).
3. **Unified result model:** `{title, url, snippet, published_date?, raw_content?, provider}`. Outlier fields to map: DDGS `href`/`body`; Serper/SerpAPI/SearchAPI `link`; Linkup `name`; Brave `description`; Parallel `excerpts[]`; Jina full-page `content` (truncate); Perplexity `snippet` can be long markdown.
4. **Auth styles to support:** `x-api-key` header (Exa); `Authorization: Bearer` (Ollama/Tavily/Firecrawl/Jina/Linkup/Perplexity); custom headers (Brave `X-Subscription-Token`, Serper `X-API-KEY`); query param (SerpAPI/SearchAPI `api_key`); none (ddgs/SearXNG/Crawl4AI).
5. **Common failure modes:** 429 backoff (all); SearXNG 403 (format=json disabled); Perplexity 451 (stale key); Tavily 432 (quota); Exa 422 (params must nest under `contents`); DDGS IP ratelimits; Brave offset cap 9.
