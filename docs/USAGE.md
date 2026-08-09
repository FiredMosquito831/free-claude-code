# Free Claude Code — Complete Usage Guide

From a fresh machine to a tuned setup: installing, connecting Claude Code and Claude Desktop, adding providers, routing models, web search, and analytics.

The [README](../README.md) is the overview. This is the long-form manual.

---

## Contents

- [1. How it works](#1-how-it-works)
- [2. Install](#2-install)
- [3. First run](#3-first-run)
- [4. Tutorial: connect Claude Code (CLI)](#4-tutorial-connect-claude-code-cli)
- [5. Tutorial: connect Claude Desktop](#5-tutorial-connect-claude-desktop)
- [6. Tutorial: connect Codex and Pi](#6-tutorial-connect-codex-and-pi)
- [7. Providers and API keys](#7-providers-and-api-keys)
- [8. Model tiers and routing](#8-model-tiers-and-routing)
- [9. Web search](#9-web-search)
- [10. Analytics](#10-analytics)
- [11. Multi-key rotation](#11-multi-key-rotation)
- [12. Updating](#12-updating)
- [13. Security and networking](#13-security-and-networking)
- [14. Troubleshooting](#14-troubleshooting)

---

## 1. How it works

Free Claude Code is a **local server that speaks Anthropic's API**. Your coding agent believes it is talking to Anthropic. The proxy receives that request, forwards it to whichever provider you configured — NVIDIA NIM, OpenRouter, a local Ollama, 27 of them — and translates the response back into Anthropic's wire format.

<div align="center">
  <img src="../assets/how-it-works.svg" alt="Request flow from agent through the proxy to a provider" width="760">
</div>

Because the translation happens at the protocol level, streaming, tool use, reasoning blocks and image input keep working. Your agent doesn't know or care.

Three consequences worth internalising before you start:

1. **The server must be running.** It's a daemon, not a library. Close the terminal and your agent stops working.
2. **Your agent's model picker can list FCC's catalog**, not Anthropic's. Selecting "Sonnet" routes to whatever *you* mapped Sonnet to. Codex and Pi's pickers always do this; Claude Code's needs model discovery turned on (`fcc-claude --discover-models` or `fcc-claude-old`) — see [§4](#4-tutorial-connect-claude-code-cli).
3. **Credentials live server-side.** Your agent holds a token that only authenticates it to the proxy; the real provider keys never leave your machine.

<div align="center">
  <img src="../assets/pic.png" alt="Claude Code running through the Free Claude Code proxy" width="720">
  <p><em>Claude Code, running normally, backed by a provider of your choosing.</em></p>
</div>

---

## 2. Install

> **Pick one environment and stay in it.** On Windows you can install under PowerShell *or* WSL. Both work — but they keep **separate configs** (`C:\Users\<you>\.fcc` versus `~/.fcc` inside WSL). Installing in both is the most common way to end up editing one config while the server reads the other.
>
> Already develop inside WSL? Install in WSL. Otherwise use PowerShell.

### Windows (PowerShell)

No admin rights needed:

```powershell
& ([scriptblock]::Create((irm "https://raw.githubusercontent.com/FiredMosquito831/free-claude-code/main/scripts/install.ps1")))
```

If PowerShell blocks the script, allow it for this session only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### WSL, Linux, macOS

```bash
curl -fsSL "https://raw.githubusercontent.com/FiredMosquito831/free-claude-code/main/scripts/install.sh" | sh
```

### Then reopen your terminal

**This step catches almost everyone.** The installer appends `~/.local/bin` to your `PATH`, and an already-open shell will never see it. If `fcc-server` appears "not found" immediately after a successful install, this is why.

Verify:

```bash
fcc-server --version
```

### What the installer does — and doesn't

1. Installs `uv` (the Python tool runner) if missing or too old.
2. Looks up the **latest** release, downloads its wheel, and **verifies the SHA-256 that GitHub publishes for that asset**. A mismatch aborts rather than running unverified code.
3. Installs the package and puts `fcc-server`, `fcc-claude`, `fcc-claude-old`, `fcc-codex` and `fcc-pi` on your `PATH`.

**It does not install Claude Code, Codex, or Pi.** Those are separate third-party tools and the proxy doesn't need any of them to run. Install whichever you actually use, yourself — the `fcc-*` launchers simply point an agent you already have at the proxy.

Pin a specific version instead of the newest:

```bash
sh install.sh --version 4.16.0      # PowerShell: -Version 4.16.0
```

Add `--dry-run` (`-DryRun`) to print what it would do without changing anything. Both scripts are readable before you run them: [install.sh](../scripts/install.sh), [install.ps1](../scripts/install.ps1).

---

## 3. First run

```bash
fcc-server
```

Keep this process running. Once healthy, the Admin UI opens in your browser automatically (disable with `FCC_OPEN_BROWSER=0`). The address is always printed in the startup log — by default:

```text
http://127.0.0.1:8082/admin
```

<div align="center">
  <img src="../assets/admin-page.png" alt="Admin dashboard overview" width="860">
</div>

The dashboard is where everything is configured. Every setting maps to a variable in `~/.fcc/.env`, and the UI writes to that same file — see [.env.example](../.env.example) for the fully annotated list. If you edit the file by hand, restart the server, because configuration is read at startup.

There is also a **Guide** tab inside the dashboard with a condensed version of this document, available offline.

On first run, the dashboard opens straight to a **Get Started** checklist instead of the Providers tab. It walks through configuring a provider, mapping model tiers, connecting Claude Code, and then points at the optional web search and analytics pages. Dismiss it once you're set up — the Get Started tab stays in the nav if you want it back.

### The two addresses that matter

| What | Default | Who uses it |
| --- | --- | --- |
| **Proxy API** | `http://127.0.0.1:8082` | your coding agent |
| **Admin UI** | `http://127.0.0.1:8082/admin` | you, in a browser |

Same port. The Admin UI is additionally restricted to loopback callers — see [Security and networking](#13-security-and-networking).

---

## 4. Tutorial: connect Claude Code (CLI)

Claude Code is configured through its **settings file**, not shell variables. This matters: `~/.claude/settings.json` takes precedence over exported environment variables, so `export ANTHROPIC_BASE_URL=...` in your shell will appear to do nothing if the settings file says otherwise.

### Step 1 — open the settings file

| Platform | Path |
| --- | --- |
| macOS / Linux / WSL | `~/.claude/settings.json` |
| Windows | `%USERPROFILE%\.claude\settings.json` |

If the file doesn't exist yet, create it.

Prefer not to hand-edit it? The **Claude Code settings file** card on the dashboard's
Providers view lists every settings file it can see on this machine (including the
Windows-side file when this server runs under WSL) and warns when a higher-precedence
file — like an enterprise managed settings file — already sets these variables and
would override the one you configure here.

### Step 2 — add the `env` block

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "freecc",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8082"
  }
}
```

**Keep any other keys you already have** — merge these two entries into the existing `env` object rather than replacing the file.

- `ANTHROPIC_BASE_URL` points Claude Code at your local server.
- `ANTHROPIC_AUTH_TOKEN` is sent as a bearer token. It must match the proxy's own `ANTHROPIC_AUTH_TOKEN`, which ships as `freecc` in `.env.example`. If you changed it in the Admin UI, use your value here.

> **On the token:** it authenticates your agent *to the proxy*, nothing more. It is not a provider key. If you clear `ANTHROPIC_AUTH_TOKEN` on the server, the proxy stops requiring authentication altogether — convenient on a single-user machine, but read [Security and networking](#13-security-and-networking) first.

### Step 3 — restart Claude Code and verify

Restart the app, then run:

```text
/status
```

It should report:

```text
Anthropic base URL: http://127.0.0.1:8082
```

If it still shows Anthropic's own endpoint, the settings file wasn't picked up — check you edited the right path for your platform and that the JSON is valid.

### Step 4 — pick a model

No model overrides are needed — FCC exposes native **Fable / Opus / Sonnet / Haiku** tier models, so you can type a tier name at the `/model` prompt either way. Claude Code's built-in *picker*, though, only lists the FCC catalog once model discovery is on: add `"CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"` to the `env` block from Step 2, or use `fcc-claude --discover-models` from the Shortcut below.

<div align="center">
  <img src="../assets/cc-model-picker.png" alt="Claude Code model picker showing FCC gateway models" width="720">
  <p><em><code>/model</code> in Claude Code, listing the FCC catalog (model discovery on).</em></p>
</div>

### Shortcut

If you'd rather not edit the settings file, the bundled launcher sets the two
proxy variables for the session:

```bash
fcc-claude
```

`fcc-claude` only sets `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` — it
doesn't touch anything else, since `~/.claude/settings.json` (Step 2 above)
takes precedence over environment variables anyway. This also means its
native model picker stays empty by default; pass `--discover-models` to have
`fcc-claude` additionally set `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`
for the session (an extra request to the proxy on every launch, so it's
opt-in):

```bash
fcc-claude --discover-models
```

If you want the previous `fcc-claude` behavior — gateway model discovery
enabled, the auto-compact window set, telemetry/autoupdate disabled, and
inherited `ANTHROPIC_*` variables cleared — run `fcc-claude-old` instead.

Official references: [Claude Code LLM gateway docs](https://code.claude.com/docs/en/llm-gateway-connect) · [settings.json reference](https://code.claude.com/docs/en/settings)

---

## 5. Tutorial: connect Claude Desktop

The desktop app has a **native gateway setting** — no file editing required. Its *Code* tab also honours the `~/.claude/settings.json` above, but the gateway configuration below is the supported path for the app itself.

Menu labels shift slightly between app versions; this is the currently documented route.

### Step 1 — enable Developer Mode

**Help → Troubleshooting → Enable Developer Mode**

The app restarts and gains a **Developer** menu.

> On older builds the path is **Settings → enable Developer mode**, which exposes **Settings → Developer** instead.

### Step 2 — open the inference settings

**Developer → Configure Third-Party Inference…**

<div align="center">
  <img src="../assets/claude-desktop-developer-menu.png" alt="Claude Desktop Developer menu with Configure Third-Party Inference highlighted" width="780">
</div>

### Step 3 — fill in the Connection section

| Field | Value |
| --- | --- |
| **Connection** | `Gateway` |
| **Gateway base URL** | `http://127.0.0.1:8082` |
| **Gateway API key** | `freecc` |
| **Gateway auth scheme** | `bearer` |
| **Credential kind** | `Static API key` |
| **Model discovery** | on |

<div align="center">
  <img src="../assets/claude-desktop-gateway-config.png" alt="Claude Desktop third-party inference settings filled in for Free Claude Code" width="780">
</div>

Then click **Apply Changes**.

Use the port from your server's startup log if it isn't `8082`, and match the API key to your `ANTHROPIC_AUTH_TOKEN` if you changed it from `freecc`.

### Step 4 — test before restarting

The dialog has **Test connection** and **Test model discovery**. Both hit your running FCC server, so use them to confirm the setup *before* restarting — **the server must be running** or they will fail.

### Step 5 — restart the app

With **Model discovery** on, the app populates its picker from FCC's `/v1/models` at launch, so you can leave **Model list** empty.

**Two things to expect:**

- The **initial warning dialog can be safely ignored.** The picker fills in once discovery completes.
- With a gateway active, the desktop app runs **local sessions only** — no Anthropic-hosted cloud environments.

---

## 6. Tutorial: connect Codex and Pi

Both have launchers that configure the environment for you:

```bash
fcc-codex      # Codex CLI against the local FCC Responses provider
fcc-pi         # Pi
```

Codex reads a model catalog that FCC generates, so its own picker works normally:

<div align="center">
  <img src="../assets/codex-model-picker.png" alt="Codex model picker with the generated FCC catalog" width="720">
</div>

<div align="center">
  <img src="../assets/codex.png" alt="Codex CLI running through Free Claude Code" width="720">
</div>

**Editor integrations** work the same way — Claude Code and Codex in VS Code, or Claude Code through JetBrains ACP. Point them at the proxy address and they behave normally.

---

## 7. Providers and API keys

Open the **Providers** tab. Every provider is one card in a single searchable grid — there are 35 of them, so start by typing in **Search providers**. It matches the provider's name, its id and its environment variable, so `groq`, `GROQ_API_KEY` and `alibaba` all find what you would expect. **Only configured** hides everything you have not set up yet.

<div align="center">
  <img src="../assets/admin-requests.png" alt="Provider configuration in the Admin UI" width="860">
</div>

### The workflow

1. **Find the provider** — search by name or by variable name.
2. **Press Configure.** The card expands and opens that provider's key pool.
3. **Paste your key into "Add key"** and press it. Keys are saved immediately — you do not need **Apply** for them. To add several at once, paste them separated by commas; keys you already have are skipped rather than rejecting the whole paste.
4. **Press Refresh models.** This makes a real API call to that provider. A model count means the key works *and* FCC can read that provider's catalog.
5. **Choose the model** on the **Model Config** tab. There is no "active provider" to select — the model ref you set there decides which provider serves a request.

A provider holds a **pool** of keys, not a single value. Each key in the pool shows its own health (healthy, cooling down, locked out) and has its own **Remove**, which also takes effect immediately. If you added more than one key, pick a **Rotation** policy and press **Apply** — rotation is a restart-required setting, so the server restarts when you apply it.

Local backends (LM Studio, llama.cpp, Ollama) take a base URL instead of a key, and offer **Test connection** where remote providers offer Refresh models.

### Reading a failed Refresh models

| Result | Almost always means |
| --- | --- |
| **401 / 403** | The key is wrong, expired, or revoked. |
| **404** | The key is fine — the **model id** isn't available on your account. |
| **402** | Billing: no credit, or plan quota exhausted. |
| **Timeout** | Network, or a self-hosted endpoint that isn't running. |

That 404 case trips people up constantly. If Refresh models fails with 404, check the exact model id against the provider's own model list before assuming the key is bad.

### Doing it by file instead

Set the matching variable in `~/.fcc/.env`:

```bash
NVIDIA_NIM_API_KEY="nvapi-..."
OPEN_ROUTER_API_KEY="sk-or-..."
```

Restart `fcc-server` afterwards.

### Local providers

LM Studio, llama.cpp and Ollama need a base URL rather than a key:

```bash
LM_STUDIO_BASE_URL="http://127.0.0.1:1234/v1"
OLLAMA_BASE_URL="http://127.0.0.1:11434"
```

These take no credentials — the key field stays empty and validation just checks reachability.

---

## 8. Model tiers and routing

FCC routes by **tier**, not by a single model. Fable, Opus, Sonnet, Haiku and a fallback each map to a real model on your provider.

<div align="center">
  <img src="../assets/admin-model-config.png" alt="Model tier configuration" width="860">
</div>

So when Claude Code requests "Sonnet", it receives whatever you mapped Sonnet to. This is the mechanism that lets an unmodified agent run on any backend.

### Practical advice

**Map Haiku to something cheap and fast.** Agents use the small tier constantly for internal bookkeeping — summarising, classifying, deciding what to do next. A slow model there makes the entire session feel sluggish even when your main model is quick. This single choice affects perceived speed more than anything else in this document.

**Reserve the big tier for actual work.** Opus/Fable should be your strongest available model; you'll hit it far less often than you expect.

**Set the fallback deliberately.** It catches requests for models you haven't mapped. Pointing it at something cheap avoids nasty surprises.

### Fallback chains

Every tier can carry an ordered list of stand-ins. Press **Add fallback** under a tier's model, name a second model, and add a third if you want. When the model a request routes to cannot serve it, the next entry takes over — a free model that rate-limits at an awkward moment stops being the end of the request.

Each chain belongs to its own tier and they are never merged: a tier with its own model tries its own chain, and a tier left on **None** tries `MODEL` and `MODEL_FALLBACKS`.

**Failover stops once you have seen output.** This is the part people get wrong:

| The model fails… | What happens |
| --- | --- |
| while connecting, authenticating, or rate-limiting | the next model takes over, invisibly |
| before it emits anything | the next model takes over, invisibly |
| halfway through streaming its answer | the request fails |
| at any point, for a **non-streaming** request | the next model takes over — nothing reached you yet |

A chain rescues the failures that happen before the first word, not the ones that happen at word five hundred. Switching models mid-answer would splice two different replies together, so FCC refuses to.

Requests that name a provider and model directly (`open_router/…`) are never redirected. An explicit choice is honoured as given.

### Images and the vision adapter

Plenty of fast text-only models cannot read a screenshot. Set a **Vision adapter** on Model Config and any request carrying an image goes there instead — but only when the tier's own model is *known* not to accept images. A model whose provider publishes no capability data is left alone, because rerouting on silence would move traffic away from models that handle images perfectly well.

You do not have to work out which of your models are affected: a tier that needs the adapter says so on its own card, naming where its images actually go. If no adapter is set, the same line turns amber to say those images will fail there.

The adapter is a route like any other, so it gets its own **Add fallback** chain. One unreachable vision model would otherwise lose every image on the machine.

### Reasoning control

Providers expose reasoning differently. FCC resolves your intent once at the boundary and each provider adapter translates it, so you configure it in one place rather than per provider. See the Model Config tab.

---

## 9. Web search

Claude Code's `web_search` is an Anthropic **server tool**: normally Anthropic executes the search and bills you for it. FCC intercepts and fulfils it locally against a provider you choose, so **no Anthropic search credits are used**, and it works with any model provider.

<div align="center">
  <img src="../assets/admin-websearch.png" alt="Web search configuration and analytics" width="860">
</div>

### Choosing a provider

```bash
WEB_SEARCH_PROVIDER=auto            # auto | off | disabled | <provider id>
WEB_SEARCH_FALLBACK_POLICY=auto     # auto | none | ddgs | legacy
```

**`auto` works with zero configuration.** With no keys set it falls back to keyless DuckDuckGo, so search works out of the box. Set any provider key and `auto` prefers it.

A missing API key on an explicitly selected provider **fails visibly** rather than silently degrading — an unconfigured provider is an operator mistake, not an outage.

14 backends are supported. Free tiers worth knowing: Exa ($10/month ongoing), Tavily (1,000 credits/month), Brave ($5/month), Serper (2,500 one-time), Linkup ($20 topped up monthly), and DuckDuckGo (keyless, unlimited, lower quality).

### The setting most worth changing

By default most providers return a one-or-two sentence **snippet**. Several can return the **extracted text of the page** — the difference between the model guessing from a summary and actually reading the source.

Turn it on for your provider, then give it room to reach the model:

```bash
# Pick the one matching your provider:
EXA_CONTENTS=text                    # or highlights+text, full
TAVILY_INCLUDE_RAW_CONTENT=markdown  # or text
FIRECRAWL_SCRAPE_FORMAT=markdown     # or summary
BRAVE_EXTRA_SNIPPETS=true            # plan-gated

# How much of it actually reaches the model:
WEBSEARCH_DIGEST_CONTENT_CHARS=4000
```

Jina, Parallel and Linkup return extracted text by default and need no switch.

Extracted text has its **own cap**, separate from the snippet cap, so opting in isn't silently trimmed back to snippet length. Set it to `0` to keep snippets only.

> **Cost:** content options bill more on most providers — Firecrawl multiplies credits per result, Exa charges per content type — and they increase input tokens on **every** search. Each option's drawer in the Admin UI states its cost.

### Restricting searches to specific sites

Claude Code declares `allowed_domains`, `blocked_domains` and `max_uses` on its `web_search` tool. FCC reads them and forwards them:

```json
{
  "type": "web_search_20250305",
  "name": "web_search",
  "allowed_domains": ["docs.python.org", "peps.python.org"]
}
```

This filters **server-side** on Exa, Tavily, Firecrawl, Linkup, Perplexity and Parallel — you pay for relevant results instead of filtering after the fact. Providers without native support search normally and drop the filters; every recorded attempt shows `supports_domain_filters`, so the analytics detail tells you which happened.

Anthropic rejects requests carrying both lists, so if both arrive the allow list wins rather than being silently intersected.

### Safe search, locale, freshness

```bash
BRAVE_SAFESEARCH=strict       # off | moderate | strict
SEARXNG_SAFESEARCH=2          # 0 | 1 | 2
SERPAPI_SAFE=active
SEARCHAPI_SAFE=active
DDGS_SAFESEARCH=strict
```

Locale matters if you're not in the US — **Firecrawl returns US results unless told otherwise**:

```bash
FIRECRAWL_COUNTRY=DE
TAVILY_COUNTRY=germany
BRAVE_COUNTRY=DE
SERPER_GL=de                  # SERPAPI_GL / SEARCHAPI_GL / JINA_GL are equivalent
```

Freshness uses each provider's own vocabulary (`BRAVE_FRESHNESS=pw`, `TAVILY_TIME_RANGE=week`, `SERPER_TBS=qdr:w`). For a precise window rather than a relative one:

```bash
TAVILY_START_DATE=2026-01-01
TAVILY_END_DATE=2026-06-30
LINKUP_FROM_DATE=2026-01-01
EXA_START_PUBLISHED_DATE=2026-01-01
```

### Two options especially useful for coding

```bash
FIRECRAWL_CATEGORIES=github,research   # restrict to GitHub or papers
TAVILY_CHUNKS_PER_SOURCE=3             # more text per source, cheaply
```

All **66** advanced options are editable from the Web Search tab's **Advanced options** drawers, and every one states what leaving it blank does.

---

## 10. Analytics

Two separate local SQLite stores under `~/.fcc/logs/`, both written by a background thread so they never block a request.

### Model requests

<div align="center">
  <img src="../assets/admin-analytics.png" alt="Model request analytics" width="860">
</div>

Summary cards cover volume, success and error rate, latency percentiles, time-to-first-token and token usage. Below: requests over time, tokens by model, and per-provider and per-key tables.

<div align="center">
  <img src="../assets/admin-key-performance.png" alt="Per-key performance breakdown" width="860">
</div>

#### Reading the token columns

Input is reported in two parts, because cached and uncached prompt tokens bill differently:

| Column | Meaning |
| --- | --- |
| **Input (uncached)** | prompt tokens the provider actually processed |
| **Cached input** | prompt tokens served from the provider's cache |
| **Cache hit rate** | cached ÷ total input |
| **Cache writes** | tokens written into the cache |

> **A hit rate of `—` means that provider never reported caching at all** — which is different from a measured `0.0%`.
>
> Prompt caching is provider-dependent. OpenAI reports it for prefixes of 1,024+ tokens; DeepSeek reports it with its own fields. **NVIDIA NIM's hosted endpoint does not do real prefix caching** — it returns a small constant regardless of repetition — so a near-zero rate there is accurate rather than a fault. NVIDIA exposes prefix caching as a self-hosted deployment toggle (`NIM_ENABLE_KV_CACHE_REUSE`), not on the shared API.

#### Finding a request again

**Search text** matches across everything a request contains, not just the visible prompt and reply:

| Searched | |
| --- | --- |
| Prompt | what you sent |
| Reply | what the model answered |
| **Reasoning** | the model's thinking blocks |
| **Tool calls** | tool names and their arguments — commands, paths, patterns |

Reasoning and tool calls are the majority of a real log: on a typical machine 55% of requests carry thinking text and 78% carry tool calls. Before v4.46.0 neither was searched, so a term that appeared only in a command you ran returned nothing.

**Every word must appear, in any order and anywhere in the request.** Searching `proxy 8082` finds a request that says "restart the proxy" in the prompt and "port 8082 is busy" in the reasoning. A single word behaves exactly as before. Matching is case-insensitive and by substring, so `kube` finds `kubernetes`.

#### Which model actually answered

A request does not always go where the tier points. **View** on any row draws the whole path it took:

```
nous_portal/tencent/hy3:free                    failed
nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b    failed
opencode/deepseek-v4-flash-free                 answered
```

The chain is recorded even when your first choice answers, so you can confirm your fallbacks are configured without waiting for something to break. Rows carry a `fallback N` badge when a stand-in served them, and a `vision` badge when the vision adapter took the request instead — those say, in a sentence, which model could not read the image.

Two panels summarise it across the window: **Failover** pairs each failing primary with what covered for it, and **Vision adapter** does the same for image diversions. The **Served by fallback** card shows how often the safety net engaged; a `—` there means no request in the window recorded routing data at all, which is different from a measured zero.

Requests logged before v4.42.0 have no chain recorded, so the panel is hidden for them rather than inventing one.

Every row's dialog also shows the full request and response, the resolved configuration, and timing. It's usually the fastest way to see what actually happened.

#### Why the totals stop rising

`REQUEST_LOG_MAX_ROWS` caps **stored rows**. Once the table is full, one row is deleted for every row that arrives, so everything computed from those rows is a rolling window:

| Section | Covers | Affected by retention |
| --- | --- | --- |
| Summary cards, charts, tables | the filter row and time range | **yes** — frozen once the cap is reached |
| **All time** | every request ever completed | no — never pruned |

At the cap, Analytics says so above the cards rather than letting a frozen counter look like a bug. **All time** is a small permanent rollup kept per day, provider and model, so per-model request counts and token usage keep climbing after stored rows roll over. It ignores the filters and the time range on purpose.

Two things worth knowing about it:

- Upgrading seeds it from whatever history is still retained. Rows pruned before the upgrade are gone and cannot be recovered, so the two figures start out equal and diverge from then on.
- **Clear log** erases it too. It is an explicit "erase my history" action, and reporting millions of all-time requests over an empty table would read as a bug.

#### Sizing the cap

Bodies are **99% of the stored bytes** — about 30 KB of text per row against 332 bytes of metadata — so retention is really a disk decision.

They are therefore stored zstd-compressed in a side table, against a dictionary trained on your own traffic. That dictionary is what does the work: consecutive requests repeat a near-identical system prompt and conversation history, and per-row compression cannot see across rows. Replaying 4,000 real requests through both paths:

| | database | per row |
| --- | --- | --- |
| Inline text | 168.5 MB | 41.1 KB |
| Compressed | **28.2 MB** | **6.9 KB** |

Roughly **6× more retention for the same disk**. A body costs ~24 µs to read back, and search still matches inside compressed text.

```bash
REQUEST_LOG_ENABLED=true
REQUEST_LOG_MAX_ROWS=50000         # oldest rows pruned beyond this
REQUEST_LOG_COMPRESS_BODIES=true   # false stores text inline, as before
REQUEST_LOG_CAPTURE_BODIES=true    # false drops text entirely, ~77x more rows/GB
```

Two things not to worry about: the dictionary trains itself once the log has seen a few hundred requests, and each blob records which dictionary compressed it, so retraining never orphans an older row.

#### Compacting a log that predates compression

Compression only ever applies to **newly written** requests, so a database carried across the upgrade keeps paying the old price for its whole history. On a real 1.7 GB log that meant every one of its 50,000 rows.

`fcc-compact-log` rewrites them in place:

```bash
# stop the server first, or the final vacuum cannot reclaim the space
fcc-compact-log
```

Measured on a copy of that 1.7 GB database: **1.73 GB → 0.29 GB in 4.9 minutes**, and all 49,934 bodies verified byte-identical against the original afterwards. It is safe to interrupt — each batch commits on its own and a row is converted only after its body is stored, so a kill leaves a consistent database with the work merely unfinished. Running it again resumes.

It also **deduplicates prompts**. The prompt is 98% of the stored bytes and 35.3% of those bytes are exact repeats — a retry or a parallel subagent re-sends the same context — so it is stored in its own shared blob, apart from the reply, reasoning and tool calls that differ every time. Keying on the whole body instead deduplicated 1.4%; keying on the prompt alone removed **29.9%** of an already-compressed real log (299 MB → 209 MB, 35,461 distinct prompts across 50,460 requests).

#### No traffic, or no server?

A flat stretch in **Requests over time** means one of two very different things, and the chart alone cannot tell you which. The server records when it was actually running, so a line under the chart says whether a server covered the range or how much of it had none. Pick a time range to see it — over "all time" there is no bounded window to measure against.

Uptime is only recorded from v4.44.0 onwards, so earlier periods report nothing rather than claiming downtime that was never measured. Brief gaps from a restart are ignored; the threshold scales with the range you are looking at.

### Web search analytics

The Web Search tab has its own analytics with an important distinction made explicit:

- **Logical searches** — one per `web_search` call.
- **Provider attempts** — one per try. A fallback produces several attempts for one search.

The two are shown in separate tables so the numbers reconcile.

```bash
WEBSEARCH_LOG_ENABLED=true
WEBSEARCH_LOG_MAX_ROWS=50000
WEBSEARCH_LOG_CAPTURE_CONTENT=true      # false = lengths and hashes only
WEBSEARCH_LOG_CONTENT_MAX_CHARS=50000
```

> **Privacy.** Search content routinely includes private queries, result URLs and page text. `WEBSEARCH_LOG_CAPTURE_CONTENT=false` withholds the captured payloads **and the query text itself**, keeping only lengths and SHA-256 hashes.
>
> API keys are never written to either store — only masked `first4…last4` labels. Proxy credentials are stripped from recorded URLs.

---

## 11. Multi-key rotation

Both model and web search providers accept several keys in one variable:

```bash
EXA_API_KEY="key-a,key-b,key-c"
EXA_API_KEY_ROTATION=failover
```

| Policy | Behaviour |
| --- | --- |
| `single` | Only the first key. Default with one key. |
| `round_robin` | Even spread across healthy keys. |
| `least_used` | Prefers the key with fewest requests. |
| `failover` | First healthy key; move on when it fails. Default with several keys. |

### Health tracking

Each key carries its own state. Repeated failures cool a key down on a rising ladder; sustained failures open a circuit; auth failures lock the key out for progressively longer.

A **rate-limited key is benched for exactly as long as the provider says** — parsed from `Retry-After`, `retry-after-ms` or `x-ratelimit-reset-*` — rather than an invented fixed delay. A key that resets in one second isn't idled for a minute, and one that needs an hour isn't hammered.

Per-key state, usage and health are visible in the Admin UI, including which key served which request.

---

## 12. Updating

<div align="center">
  <img src="../assets/admin-version.png" alt="Version panel" width="860">
</div>

The dashboard shows your running version, checks the release feed (cached for six hours), and announces new releases with **the release notes inline** — expand *What changed* to decide whether an update matters to you.

<div align="center">
  <img src="../assets/admin-update-banner.png" alt="Update available banner" width="860">
</div>

**Update now** downloads the release wheel, verifies its SHA-256 against the digest GitHub publishes for that asset, and installs it with `uv`. A checksum mismatch aborts. Extras you originally installed — voice support, for instance — are detected and preserved.

**Upgrading never restarts the server.** A running process keeps serving the code it already loaded, so an upgrade can't drop an in-flight stream. You get a *restart required* banner and restart when convenient.

### Windows: the install is deferred

Windows holds a running executable and its loaded DLLs open, so the environment **cannot** be replaced underneath a live process — attempting it fails partway and leaves a broken install.

So on Windows, **Update now** downloads and verifies the wheel, then hands it to a background helper that waits for the server to exit and installs it then. You'll see:

> *Update staged — stop the server to finish installing*

Stop `fcc-server`, the update applies itself, start it again on the new version. **Your working install is untouched until that moment**, so a failed update can't strand you. If the deferred install does fail, the dashboard reports it on the next start.

WSL, Linux and macOS install in place, because they can replace files that are still open.

### From the command line

Re-running the install command does exactly the same thing and always fetches the newest release.

---

## 13. Security and networking

Worth understanding before you expose anything.

### What binds where

| Surface | Default bind | Access control |
| --- | --- | --- |
| **Proxy API** (`/v1/...`) | `0.0.0.0:8082` | Bearer token, if `ANTHROPIC_AUTH_TOKEN` is set |
| **Admin UI** (`/admin`) | same port | **Loopback callers only**, always |

The proxy binds to **all interfaces** by default, so another machine on your network can reach it. The Admin UI is separately restricted to loopback and cannot be reached remotely regardless of bind address.

### The auth token

`ANTHROPIC_AUTH_TOKEN` ships as `freecc` in `.env.example`. It is compared in constant time against the bearer token your agent sends.

**If you clear it, authentication is disabled entirely** — any caller that can reach the port can spend your provider credits. That is fine on a single-user laptop behind a firewall; it is not fine on a shared or exposed network. Change it from the default if anything other than you can route to the machine.

To bind loopback-only instead, set `HOST=127.0.0.1`.

### What never leaves the machine

Provider API keys are never sent to your agent, never written to the analytics stores, and never included in configuration snapshots — only masked `first4…last4` labels. Proxy credentials are stripped from any recorded URL.

---

## 14. Troubleshooting

**`fcc-server: command not found` right after installing.**
Close and reopen your terminal. The installer extends `PATH`; an existing shell won't see it. This is the single most common install issue.

**Two configs on Windows.**
If you installed under both PowerShell and WSL you have `C:\Users\<you>\.fcc` *and* `~/.fcc` inside WSL. The server prints which config directory it is using at startup — check that against the one you've been editing.

**Claude Code still talks to Anthropic.**
`~/.claude/settings.json` wins over shell exports. Confirm with `/status` — it should show `http://127.0.0.1:8082`. Check the JSON is valid and that you edited the path for your platform.

**401 from the proxy.**
Your agent's `ANTHROPIC_AUTH_TOKEN` doesn't match the server's. Compare the value in `~/.claude/settings.json` — or the Desktop gateway API key — against the server's setting.

**Provider validation fails with 404.**
Usually the model id, not the key. Check the exact id against the provider's model list.

**Claude Desktop's test buttons fail.**
The server must be running for **Test connection** and **Test model discovery** to succeed — they make real calls.

**Desktop shows a warning dialog on launch.**
Expected with model discovery on; the picker fills in once discovery completes.

**Agent can't reach the proxy from another machine.**
The proxy binds `0.0.0.0`, so it should be reachable — check your firewall. The *Admin UI* is loopback-only by design and will refuse remote callers no matter what.

**Web search returns nothing useful.**
Open the attempt detail in Web Search analytics. It shows exactly what was sent upstream and what came back, including whether your domain filters were applied or dropped.

**Cache hit rate shows `—`.**
That provider doesn't report prompt caching. Not a fault — see [Reading the token columns](#reading-the-token-columns).

**Update did nothing on Windows.**
Versions below 4.21.5 had a defect where the deferred installer could stall. A self-updater can't fix its own updater, so update once from the install script; after that the dashboard button works.

**Anything else.**
The request analytics **View** dialog shows the complete exchange for any request — request body, response, resolved provider and model, timing, and errors. Start there.
