# Free Claude Code — Complete Usage Guide

Everything from a fresh install to tuning providers, web search, and analytics.

For the short version, see the [README](../README.md). This page is the long one.

---

## Contents

1. [What this actually does](#1-what-this-actually-does)
2. [Install](#2-install)
3. [First run](#3-first-run)
4. [Adding API keys](#4-adding-api-keys)
5. [Choosing models](#5-choosing-models)
6. [Connecting Claude Code (CLI)](#6-connecting-claude-code-cli)
7. [Connecting Claude Desktop](#7-connecting-claude-desktop)
8. [Connecting Codex and Pi](#8-connecting-codex-and-pi)
9. [Web search](#9-web-search)
10. [Analytics](#10-analytics)
11. [Multi-key rotation](#11-multi-key-rotation)
12. [Updating](#12-updating)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. What this actually does

Free Claude Code is a **local proxy that speaks Anthropic's API**. Your coding agent thinks it's talking to Anthropic; the proxy forwards the request to whichever provider you configured — NVIDIA NIM, OpenRouter, a local Ollama, 27 of them — and translates the response back into Anthropic's format.

That means streaming, tool use, reasoning and image input keep working, while the model behind them is whatever you chose.

Two things follow from this design and are worth internalising early:

- **The proxy must be running** for your agent to work. It's a server, not a library.
- **Your agent's model picker lists FCC's catalog**, not Anthropic's. Picking "Opus" routes to whatever you mapped Opus to.

---

## 2. Install

Pick **one** environment and stay in it. On Windows you can install under PowerShell *or* WSL — both work, but they keep separate configs (`C:\Users\<you>\.fcc` versus `~/.fcc` inside WSL), and installing in both is the most common way to end up editing one config while the server reads the other.

> Already develop inside WSL? Install in WSL. Otherwise use PowerShell.

**Windows (PowerShell)** — no admin rights needed:

```powershell
& ([scriptblock]::Create((irm "https://raw.githubusercontent.com/FiredMosquito831/free-claude-code/main/scripts/install.ps1")))
```

If PowerShell blocks it, allow it for this session only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

**WSL, Linux, macOS:**

```bash
curl -fsSL "https://raw.githubusercontent.com/FiredMosquito831/free-claude-code/main/scripts/install.sh" | sh
```

**Then close and reopen your terminal.** The installer adds `~/.local/bin` to your `PATH`, and an already-open shell won't see it. This is the single most common reason `fcc-server` looks "not found" immediately after a successful install.

Verify:

```bash
fcc-server --version
```

### What the installer does, and doesn't

It installs `uv`, downloads the **latest** release wheel, verifies the SHA-256 that GitHub publishes for that asset, and puts `fcc-server`, `fcc-claude`, `fcc-codex` and `fcc-pi` on your `PATH`.

**It does not install Claude Code, Codex, or Pi.** Those are separate third-party tools and the proxy doesn't need any of them. Install whichever you actually use, yourself — the `fcc-*` launchers just point an agent you already have at the proxy.

To pin a version instead of taking the newest:

```bash
sh install.sh --version 4.16.0      # PowerShell: -Version 4.16.0
```

Add `--dry-run` (`-DryRun`) to see what it would do without changing anything.

---

## 3. First run

```bash
fcc-server
```

Keep this running. The Admin UI opens in your browser once the server is healthy, and its address is always printed in the startup log — by default <http://127.0.0.1:8082/admin>.

<div align="center">
  <img src="../assets/admin-page.png" alt="Admin dashboard overview" width="820">
</div>

The Admin UI is **loopback-only**: it binds to `127.0.0.1` and rejects non-local callers. Nothing here is exposed to your network.

Everything on this page can also be set by editing `~/.fcc/.env` directly — see [.env.example](../.env.example) for the full annotated list. The UI writes to that same file.

---

## 4. Adding API keys

Open **Providers**. Each provider card has a field for its key, a **Validate** button that makes a real call, and **Apply** to save.

<div align="center">
  <img src="../assets/admin-requests.png" alt="Provider configuration" width="820">
</div>

The practical workflow:

1. Paste the key into the provider you want.
2. Press **Validate** — this actually calls the provider. A green result means the key works *and* the model you have selected is reachable with it.
3. Press **Apply**.
4. Set that provider as active.

Validation failing is informative: a 401 means the key is wrong, a 404 usually means the key is fine but the *model id* isn't available on your account.

Prefer the file? Set the matching variable in `~/.fcc/.env`:

```bash
NVIDIA_NIM_API_KEY="nvapi-..."
OPEN_ROUTER_API_KEY="sk-or-..."
```

Restart `fcc-server` after editing the file by hand — it reads config at startup.

---

## 5. Choosing models

Open **Model Config**. FCC routes by *tier* rather than by a single model: Fable, Opus, Sonnet, Haiku and a fallback each map to a real model on your provider.

<div align="center">
  <img src="../assets/admin-model-config.png" alt="Model tier configuration" width="820">
</div>

So when Claude Code asks for "Sonnet", it gets whatever you mapped Sonnet to. This is why your agent's own `/model` picker shows FCC's catalog:

<div align="center">
  <img src="../assets/cc-model-picker.png" alt="Claude Code model picker showing FCC gateway models" width="700">
</div>

Practical advice: map **Haiku to something cheap and fast**. Agents use the small tier constantly for internal bookkeeping, and a slow model there makes the whole session feel sluggish even if your main model is quick.

---

## 6. Connecting Claude Code (CLI)

Easiest path — the launcher sets the environment for you:

```bash
fcc-claude
```

That starts Claude Code pointed at the proxy. Its `/model` picker will show the FCC catalog.

Doing it manually is just two environment variables:

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8082"
export ANTHROPIC_AUTH_TOKEN="any-value"     # or your FCC token if you set one
claude
```

The auth token only matters if you enabled `ANTHROPIC_AUTH_TOKEN` on the proxy; otherwise any non-empty value works, because the real credentials live server-side.

---

## 7. Connecting Claude Desktop

Claude Desktop can point at the proxy too. Enable developer settings first:

<div align="center">
  <img src="../assets/claude-desktop-developer-menu.png" alt="Claude Desktop developer menu" width="700">
</div>

Then set the gateway to your local proxy address:

<div align="center">
  <img src="../assets/claude-desktop-gateway-config.png" alt="Claude Desktop gateway configuration" width="700">
</div>

Use the same address the server printed at startup (`http://127.0.0.1:8082` by default). If Desktop is running on a different machine from the proxy, `127.0.0.1` won't reach it — the proxy binds locally on purpose.

---

## 8. Connecting Codex and Pi

```bash
fcc-codex      # Codex CLI against the local FCC Responses provider
fcc-pi         # Pi
```

Codex's own model picker reads a catalog FCC generates:

<div align="center">
  <img src="../assets/codex-model-picker.png" alt="Codex model picker with the generated FCC catalog" width="700">
</div>

Editor integrations work the same way: Claude Code and Codex in VS Code, or Claude Code through JetBrains ACP — point them at the proxy address and they behave normally.

---

## 9. Web search

Claude Code's `web_search` is an Anthropic **server tool** — normally Anthropic runs the search and bills you for it. FCC fulfils it locally instead, against a provider you pick, so no Anthropic search credits are used and it works with any model provider.

<div align="center">
  <img src="../assets/admin-websearch.png" alt="Web search configuration and analytics" width="820">
</div>

### Picking a provider

Set `WEB_SEARCH_PROVIDER`, or use the Web Search tab. It accepts `auto` (default), `off`, `disabled`, or one of 14 provider ids.

**`auto` works with zero configuration** — with no keys set it falls back to keyless DuckDuckGo, so search works out of the box. Set a key for anything else and `auto` prefers it.

```bash
WEB_SEARCH_PROVIDER=auto
WEB_SEARCH_FALLBACK_POLICY=auto     # auto | none | ddgs | legacy
TAVILY_API_KEY="tvly-..."
```

A missing API key always fails **visibly** rather than silently falling back — an unconfigured provider is an operator error, not an outage.

### Getting full page text instead of snippets

This is the highest-value setting and it's off by default. Most providers return a one-or-two sentence snippet; several can return the **extracted text of the page**, which is the difference between the model guessing from a summary and actually reading the source.

```bash
# Turn it on for whichever provider you use:
EXA_CONTENTS=text                    # or highlights+text, full
TAVILY_INCLUDE_RAW_CONTENT=markdown  # or text
FIRECRAWL_SCRAPE_FORMAT=markdown     # or summary
BRAVE_EXTRA_SNIPPETS=true            # plan-gated

# Then give it room to reach the model:
WEBSEARCH_DIGEST_CONTENT_CHARS=4000
```

Jina, Parallel and Linkup return extracted text by default and need no switch.

Extracted text has its **own cap**, separate from the snippet cap, so opting in isn't silently trimmed back to snippet length. Set it to `0` to keep snippets only.

> **Cost:** content options bill more on most providers and increase input tokens on every search. Each option's drawer in the Admin UI states its cost.

### Restricting to specific sites

Claude Code declares `allowed_domains`, `blocked_domains` and `max_uses` on its `web_search` tool; FCC forwards them:

```json
{ "type": "web_search_20250305", "name": "web_search",
  "allowed_domains": ["docs.python.org"] }
```

That filters **server-side** on Exa, Tavily, Firecrawl, Linkup, Perplexity and Parallel — you pay for relevant results instead of filtering afterwards. Providers without native support search normally; every recorded attempt shows `supports_domain_filters` so you can tell which happened.

### Safe search, locale, freshness

```bash
BRAVE_SAFESEARCH=strict       # off | moderate | strict
SEARXNG_SAFESEARCH=2          # 0 | 1 | 2
SERPAPI_SAFE=active
FIRECRAWL_COUNTRY=DE          # Firecrawl defaults to US results otherwise
TAVILY_START_DATE=2026-01-01  # precise window, not just "past week"
```

Two worth knowing for coding work: `TAVILY_CHUNKS_PER_SOURCE=3` is the cheapest way to get more text out of Tavily, and `FIRECRAWL_CATEGORIES=github,research` restricts to GitHub or papers.

All 66 advanced options are editable from the Web Search tab's **Advanced options** drawers, and every one states what leaving it blank does.

---

## 10. Analytics

Two separate stores, both local SQLite under `~/.fcc/logs/`, both non-blocking.

### Model requests

<div align="center">
  <img src="../assets/admin-analytics.png" alt="Model request analytics" width="820">
</div>

Cards cover request volume, success and error rate, latency percentiles, TTFT, and token usage. Below that: requests over time, tokens by model, and per-provider and per-key tables.

<div align="center">
  <img src="../assets/admin-key-performance.png" alt="Per-key performance breakdown" width="820">
</div>

**Reading the token columns.** Input is reported in two parts:

| Column | Meaning |
| --- | --- |
| Input (uncached) | prompt tokens the provider actually processed |
| Cached input | prompt tokens served from the provider's cache |
| Cache hit rate | cached ÷ total input |
| Cache writes | tokens written into the cache |

A cache hit rate of **—** means that provider never reported caching at all, which is different from a measured **0.0%**. Prompt caching is provider-dependent: OpenAI (prefixes ≥1,024 tokens) and DeepSeek report it; NVIDIA NIM's hosted endpoint does not do meaningful prefix caching, so a near-zero rate there is accurate rather than a fault.

Every row has a **View** dialog with the full request and response, the resolved configuration, and timing.

```bash
REQUEST_LOG_ENABLED=true
REQUEST_LOG_MAX_ROWS=50000       # oldest rows pruned beyond this
```

### Web search

The Web Search tab has its own analytics with the same shape, plus two levels made explicit: **logical searches** (one per `web_search` call) and **provider attempts** (one per try, so a fallback produces more than one).

```bash
WEBSEARCH_LOG_ENABLED=true
WEBSEARCH_LOG_MAX_ROWS=50000
WEBSEARCH_LOG_CAPTURE_CONTENT=true    # false = lengths and hashes only
```

**Privacy:** search content routinely includes private queries, result URLs and page text. `WEBSEARCH_LOG_CAPTURE_CONTENT=false` withholds the captured payloads **and the query text itself**, keeping only lengths and SHA-256 hashes. API keys are never written to either store — only masked `first4…last4` labels.

---

## 11. Multi-key rotation

Both model providers and web search providers accept multiple keys in the same variable:

```bash
EXA_API_KEY="key-a,key-b,key-c"
EXA_API_KEY_ROTATION=failover     # single | round_robin | least_used | failover
```

`failover` is the default with multiple keys, `single` with one.

Health is tracked per key: repeated failures cool a key down on a rising ladder, sustained failures open a circuit, and auth failures lock the key out for longer each time. A rate-limited key is benched for **exactly as long as the provider says** via its `Retry-After` or reset headers, rather than an invented fixed delay.

Per-key state is visible in the Admin UI, including which keys served which requests.

---

## 12. Updating

The dashboard shows your running version, announces new releases with the release notes inline, and installs them for you.

<div align="center">
  <img src="../assets/admin-version.png" alt="Version panel" width="820">
</div>

<div align="center">
  <img src="../assets/admin-update-banner.png" alt="Update available banner" width="820">
</div>

**Update now** downloads the wheel, verifies its SHA-256 against the digest GitHub publishes, and installs it with `uv`. A checksum mismatch aborts. Extras you originally installed (voice support, for instance) are detected and preserved.

**Upgrading never restarts the server** — a running process keeps serving the code it already loaded, so an upgrade can't drop an in-flight stream. You get a *restart required* banner and restart when it suits you.

**On Windows the install is deferred.** Windows holds the running interpreter and its DLLs open, so the environment can't be replaced underneath a live process. FCC stages the verified wheel and a background helper installs it once you stop the server: you'll see *"Update staged — stop the server to finish installing"*. Stop `fcc-server`, the update applies itself, start it again. Your working install is untouched until that moment, so a failed update can't strand you.

Re-running the install command does the same thing from the command line.

---

## 13. Troubleshooting

**`fcc-server: command not found` right after installing.** Close and reopen your terminal. The installer extends `PATH`; an existing shell won't see it.

**Two configs on Windows.** If you installed in both PowerShell and WSL you have `C:\Users\<you>\.fcc` and `~/.fcc` inside WSL. Check which one your running server uses — `fcc-server` prints its config directory at startup.

**Provider validation fails with 404.** Usually the key is fine but the model id isn't available on your account. Check the exact id against the provider's model list.

**Agent can't reach the proxy.** Confirm `fcc-server` is running and the address matches what it printed. The proxy binds to loopback, so another machine can't reach it.

**Web search returns nothing useful.** Check the Web Search analytics — the attempt detail shows exactly what was sent and what came back, including whether your domain filters were applied or dropped.

**Update did nothing on Windows.** Versions below 4.21.5 had a bug where the deferred installer could stall. A self-updater can't fix its own updater, so update once from the install script; after that the button works.

**Something else.** The request analytics **View** dialog shows the full exchange for any request, which is usually the fastest way to see what actually happened.
