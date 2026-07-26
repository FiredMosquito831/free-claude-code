# AGENT SPEC — Novita provider + dynamic custom providers

> Branch `feat/websearch-providers`. Temporary spec; delete before release.
> Exploration blueprint (read first): this spec distills it; key facts inline.

## Goals

1. **Novita AI** as a built-in provider (id `novita`): OpenAI-compatible, base
   `https://api.novita.ai/openai`, Bearer `NOVITA_API_KEY` (keys at
   https://novita.ai/settings or dashboard), models at GET /openai/v1/models.
2. **Custom providers**: user-defined OpenAI-compatible providers added at runtime via admin
   UI/API. Each becomes a full provider: independently configurable (display name, base URL,
   API keys + rotation, proxy), routable (`custom_x/model` refs, tier overrides), testable,
   discovered (models from GET {base}/models), with models.dev metadata fallback.

## Design (decided, from exploration blueprint)

### Static catalog stays static

`PROVIDER_CATALOG` / `SUPPORTED_PROVIDER_IDS` remain import-time frozen (contract test).
Add `novita` to the static catalog (choose placement: after `fireworks`, before `cloudflare` —
alphabetical-ish neighbors; update `tests/contracts/test_provider_catalog_order.py`
`_EXPECTED_PROVIDER_ORDER` accordingly) with an `OPENAI_CHAT_PROFILES` entry
(`OpenAIChatRequestPolicy(provider_name="novita", reasoning_replay=THINK_TAGS)` +
`NO_REASONING`, mirroring the simplest existing profiles like cerebras/groq — COPY an existing
simple profile), Settings field `novita_api_key`, `.env.example` line, admin auto-fields come
free from provider_manifest.

### Dynamic registry

New `src/free_claude_code/config/provider_registry.py`:

```python
@dataclass(frozen=True, slots=True)
class CustomProviderEntry:
    provider_id: str          # "custom_" + slug([a-z0-9_], from name)
    display_name: str
    base_url: str
    api_keys: tuple[str, ...] # multi-key, rotation applies
    credential_rotation: str  # single|round_robin|least_used|failover, default failover
    proxy: str | None
    enabled: bool
    added_at: str             # ISO

class ProviderRegistry:
    # loads/persists JSON at config_dir_path()/custom_providers.json (atomic tmp+replace)
    # thread-safe (lock); API: list_custom(), get(provider_id), add(entry) (validates id
    #   uniqueness vs static catalog + custom ids; id auto-slugged from display_name),
    #   update(provider_id, ...), remove(provider_id)
    # descriptor_for(entry) -> ProviderDescriptor (dynamic): carries direct credential/base_url
    # all_descriptors() -> Mapping[str, ProviderDescriptor]  (static + enabled custom)
    # supported_ids() -> tuple[str, ...]  (static order + custom insertion order)
    # on_change callback list (admin layer hooks provider_manager.replace)
```

Module singleton `get_provider_registry()` (lazy, loads once; `reset_provider_registry()` for
tests). `ProviderDescriptor` is frozen — dynamic descriptors are constructed with
`credential_env=None`-style bypass: add NO fields to ProviderDescriptor; instead the dynamic
descriptor gets `credential_attr=None, base_url_attr=None, static_credential=first key,
default_base_url=entry.base_url, dynamic=True` — ADD a `dynamic: bool = False` field to
ProviderDescriptor (default keeps static entries unchanged; contract tests assert static fields
so default-False keeps them green).

### Replace catalog reads with registry accessors (exact list from blueprint §7)

- `config/settings.py` validate_model_format — validate prefix against
  `get_provider_registry().supported_ids()` (call at validation time).
- `application/routing.py` — `_validate_provider_id` + `_direct_provider_model` use registry.
- `providers/runtime/factory.py` — create via registry descriptor; dynamic branch:
  skip Settings attrs; `ProviderConfig(api_keys=entry.api_keys, base_url=entry.base_url,
  proxy=entry.proxy, credential_rotation=entry.credential_rotation)`; construction via
  `create_openai_chat_provider` with a new `GENERIC_OPENAI_PROFILE` fallback (same shape as the
  simplest profile) when descriptor.dynamic. Multi-key rotation via RotatingProvider comes free.
- `providers/runtime/config.py` — `build_provider_config`/`provider_credential`: dynamic branch
  reads the registry entry instead of Settings attrs.
- `providers/runtime/discovery.py` — `model_cache_provider_ids_for_settings` includes enabled
  custom providers with ≥1 key.
- `providers/runtime/model_cache.py` — iterate registry ids.
- `config/admin/status.py` — merge custom provider status (configured if keys present).
- `api/admin_routes.py` — provider test endpoint must resolve custom ids (via registry-aware
  factory; should come free once factory is registry-aware).
- `cli/launchers/codex_model_catalog.py` — registry ids where it reads SUPPORTED_PROVIDER_IDS.
- Bootstrap (`runtime/bootstrap.py`): registry must be loadable before Settings rebuilds — the
  singleton is lazy and Settings validator calls it at validation time, so no ordering hazard as
  long as registry load never imports Settings (keep it config-local, no settings import!).

### models.dev fallback

- New `providers/runtime/models_dev.py`: fetches `https://models.dev/api.json` (httpx, 10s
  timeout), caches to `config_dir_path()/cache/models-dev.json` with `fetched_at`; uses cache if
  <24h old or fetch fails; fully silent when offline.
- Extend `ProviderModelInfo` (application/model_metadata.py) with optional
  `context_length: int | None = None`, `input_price: float | None = None`,
  `output_price: float | None = None` (per 1M tokens USD).
- Enrichment point: after `list_model_infos` in discovery — for CUSTOM providers (and novita),
  match model ids against models.dev (normalize: lowercase, strip `provider/` prefix on both
  sides, also try last `/`-segment) and fill metadata. Never blocks discovery on network when
  cache exists; first-ever run may fetch in background (fire-and-forget task, next refresh picks
  it up).

### Admin API (Worker 2 contract — DO NOT DEVIATE)

New router file `api/admin_custom_routes.py` (loopback-guarded like admin routes; register in
app.py):

- `GET /admin/api/custom-providers` → `{"providers": [{provider_id, display_name, base_url,
  key_count, masked_keys: [str], credential_rotation, proxy, enabled, model_count,
  status: "configured"|"missing_key"|"disabled", models: [model_id...] (from cache, may be
  empty), added_at}]}`
- `POST /admin/api/custom-providers` body `{display_name, base_url, api_key (first key),
  credential_rotation?, proxy?}` → validates + registers + hot reload; returns the created
  entry JSON as above + `models: [...]` detected via a live test call (reuse
  `ApplicationRuntime.test_provider` logic, non-fatal if it fails — return `test_error`).
- `PATCH /admin/api/custom-providers/{provider_id}` body any of `{display_name, base_url,
  proxy, enabled, credential_rotation}` → update + hot reload.
- `POST /admin/api/custom-providers/{provider_id}/keys` `{api_key}` append;
  `DELETE .../keys/{index}` remove (last key → status missing_key, provider stays).
- `DELETE /admin/api/custom-providers/{provider_id}` → remove + hot reload.
- Hot reload = mutate registry then `provider_manager.replace(current_settings, commit=True,
  reason="custom_provider_change")` via the existing admin apply plumbing (services.admin /
  ApplicationRuntime — follow how apply_admin_config triggers replace).

### Admin UI (Worker 2)

In the **Providers** view, after the static provider cards: a **"Custom providers"** section
header + each custom provider as a card styled like existing provider cards (status pill,
display name, base URL, masked keys + health, Test button, Edit (name/url/proxy/rotation),
Delete with confirm) + an **"Add custom provider"** card/button opening a small form (display
name, base URL, API key, rotation select, proxy optional). After add: show detected models
(count + first few ids) or the test error. Vanilla JS in admin.js matching existing patterns
(ws-* naming precedent), minimal admin.css additions. No new deps.

### Contracts & tests

- Keep `PROVIDER_CATALOG` static+ordered; update catalog-order test for `novita` only.
- Import boundaries: provider_registry lives in config/; api may import it (add to boundary
  declarations as needed). config must NOT import settings from registry module.
- Feature/capability manifest (smoke/features.py, smoke/capabilities.py): add custom-providers
  + novita entries if the contract requires inventory updates (check test_feature_manifest).
- Tests: registry CRUD/persistence/slug/uniqueness; settings validator accepts custom refs
  after registration; routing resolves custom provider/model; factory builds dynamic provider
  (generic profile, keys, proxy) + rotation wrap; discovery includes custom ids; models.dev
  cache/fallback/matching/offline; novita profile smoke (unit); admin CRUD endpoints (Worker 2);
  hot-reload triggers replace.

## Slices

- **Worker 1 BACKEND** (worktree ../.worktrees/custom-backend, branch agent/custom-backend):
  everything above except Admin API/UI files. Owns: config/provider_registry.py,
  provider_catalog.py (novita + dynamic field), settings.py validator, routing.py,
  providers/runtime/{factory,config,discovery,model_cache,models_dev}.py, openai_chat/profiles
  (novita + generic), application/model_metadata.py, config/admin/status.py,
  cli/launchers/codex_model_catalog.py, .env.example, tests (minus admin-route tests).
  Forbidden: api/admin*, api/admin_static/, version bump.
- **Worker 2 ADMIN** (worktree ../.worktrees/custom-admin, branch agent/custom-admin):
  api/admin_custom_routes.py + app.py registration + admin_static UI + admin/status merge if
  needed for display (coordinate: Worker 1 owns status.py — Worker 2 may NOT edit it; if gaps,
  report instead) + tests for the endpoints. Codes against the registry API + endpoint contract
  above; registry arrives via merge (for tests, construct CustomProviderEntry-like dicts or
  monkeypatch; be defensive via getattr where reasonable but primarily code to the contract).

## Merge & finish

Merge order: backend → admin. Main agent: integration fixes, bump 4.10.2 → 4.11.0, uv lock,
full CI, push fork (feat branch + main), install Windows + WSL.
