"""Generate the Claude Code configuration reference from the official docs.

Claude Code ships several times a week, so this reference is generated rather
than hand-maintained. It reads the published markdown source of the official
documentation pages and emits two artifacts:

* ``docs/CLAUDE-CODE-CONFIG.md``           -- the human reference
* ``docs/claude-code-config-catalog.json`` -- the same data, machine readable,
  for the Configure Claude Code page to build its controls from

Usage::

    uv run python scripts/gen_claude_config_reference.py --fetch
    uv run python scripts/gen_claude_config_reference.py            # cached

``--fetch`` downloads the source pages into the cache directory; without it the
script reads whatever is already cached, so a regeneration is reproducible and
CI never needs the network.

Classification is explicit wherever the documentation states a closed value
set. The boolean/number/string split is inferred from the documentation's own
consistent phrasing ("Set to `1` to ...", "(default: 600000)", "in
milliseconds"), and every inferred decision records its reason in
``classification_evidence`` so a wrong guess is visible rather than silent.
"""

import argparse
import json
import re
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = REPO_ROOT / ".claude-docs-cache"
DOC_BASE_URL = "https://code.claude.com/docs/en"
DOCS_BASE = "https://code.claude.com"

SOURCE_PAGES = ("env-vars", "settings", "permissions", "hooks", "tools-reference")

# --------------------------------------------------------------------------
# Values the docs state explicitly as a closed set. Nothing here is guessed.
# --------------------------------------------------------------------------
ENV_ENUMS: dict[str, list[str]] = {
    "ANTHROPIC_BEDROCK_REGION_PREFIX": ["us", "eu", "apac", "jp", "au", "global"],
    "ANTHROPIC_BEDROCK_SERVICE_TIER": ["default", "flex", "priority"],
    "CLAUDE_CODE_CERT_STORE": ["bundled", "system", "bundled,system"],
    "CLAUDE_CODE_DEBUG_LOG_LEVEL": ["verbose", "debug", "info", "warn", "error"],
    "CLAUDE_CODE_EFFORT_LEVEL": ["low", "medium", "high", "xhigh", "max", "auto"],
    "CLAUDE_EFFORT": ["low", "medium", "high", "xhigh", "max"],
    "ENABLE_TOOL_SEARCH": ["true", "false", "auto", "auto:N"],
    "MCP_SDK_GENERATION": ["v1", "v2"],
    "OTEL_LOG_RAW_API_BODIES": ["0", "1", "file:<dir>"],
}

SETTING_ENUMS: dict[str, list[str]] = {
    "advisorModel": ["fable", "opus", "sonnet", "<model-id>"],
    "askUserQuestionTimeout": ["60s", "5m", "10m", "never"],
    "autoUpdatesChannel": ["latest", "stable"],
    "browserExternalPageTools": ["disabled"],
    "crossSessionInbound": ["accept", "hold", "refuse"],
    "defaultShell": ["bash", "powershell"],
    "dialogExpiry": ["60s", "5m", "10m", "never"],
    "disableAutoMode": ["disable"],
    "disableDeepLinkRegistration": ["disable"],
    "editorMode": ["normal", "vim"],
    "effortLevel": ["low", "medium", "high", "xhigh"],
    "outputStyle": ["Default", "Explanatory", "Learning", "<custom>"],
    "parentSettingsBehavior": ["first-wins", "merge"],
    "preferredNotifChannel": [
        "auto",
        "terminal_bell",
        "iterm2",
        "iterm2_with_bell",
        "kitty",
        "ghostty",
        "notifications_disabled",
    ],
    "teammateMode": ["in-process", "auto", "tmux", "iterm2"],
    "theme": [
        "auto",
        "dark",
        "light",
        "dark-daltonized",
        "light-daltonized",
        "dark-ansi",
        "light-ansi",
        "custom:<slug>",
    ],
    "tui": ["default", "fullscreen"],
    "viewMode": ["default", "verbose", "focus"],
    "workflowSizeGuideline": ["unrestricted", "small", "medium", "large"],
    "defaultMode": [
        "default",
        "manual",
        "acceptEdits",
        "plan",
        "auto",
        "dontAsk",
        "bypassPermissions",
    ],
    "disableBypassPermissionsMode": ["disable"],
    "diffTool": ["auto", "terminal"],
    "worktree.baseRef": ["fresh", "head"],
    "worktree.bgIsolation": ["worktree", "none"],
}

# Docs, "Variables" note: these read only whether they are SET AT ALL, so `0`
# and `false` still turn the behaviour on. A UI must render them as a
# set/unset switch, never as a true/false toggle.
SET_OR_UNSET = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DISABLE_TELEMETRY",
    "DISABLE_ERROR_REPORTING",
    "CLAUDE_CODE_TMUX_TRUECOLOR",
    "FALLBACK_FOR_ALL_PRIMARY_MODELS",
    "IS_DEMO",
}

# Reads a number, so only `0` turns it off (docs call this out by name).
NUMERIC_BOOLEAN = {"FORCE_HYPERLINK"}

SECRET_LIKE = re.compile(
    r"(API_KEY|AUTH_TOKEN|_TOKEN$|CLIENT_SECRET|PASSPHRASE|BEARER)", re.I
)

# Names the credential pattern catches that do not actually carry a credential:
# a refresh interval whose name embeds API_KEY, and a token Claude Code exports
# to its own subprocesses rather than one you ever set.
NOT_SECRET = {"CLAUDE_CODE_API_KEY_HELPER_TTL_MS", "CLAUDE_CODE_MESSAGING_TOKEN"}

CATEGORIES: list[tuple[str, re.Pattern[str]]] = [
    (
        "auth",
        re.compile(
            r"^(ANTHROPIC_(API_KEY|AUTH_TOKEN|PROFILE|ORGANIZATION_ID|FEDERATION_RULE_ID|WORKSPACE_ID))|^CLAUDE_CODE_OAUTH|^MCP_CLIENT_SECRET|^MCP_OAUTH"
        ),
    ),
    (
        "provider",
        re.compile(
            r"^(ANTHROPIC_(AWS|BEDROCK|FOUNDRY|VERTEX)|AWS_|VERTEX_REGION|CLAUDE_CODE_(USE_|SKIP_).*(BEDROCK|VERTEX|FOUNDRY|MANTLE|AWS))"
        ),
    ),
    (
        "endpoint",
        re.compile(
            r"BASE_URL|^HTTP_PROXY|^HTTPS_PROXY|^NO_PROXY|^CLAUDE_CODE_(CLIENT_CERT|CLIENT_KEY|CERT_STORE|PROXY_RESOLVES_HOSTS)|CUSTOM_HEADERS|ANTHROPIC_BETAS|EXTRA_BODY"
        ),
    ),
    (
        "model",
        re.compile(
            r"^ANTHROPIC_(MODEL|DEFAULT_|SMALL_FAST|CUSTOM_MODEL)|^CLAUDE_CODE_(SUBAGENT_MODEL|MAX_OUTPUT_TOKENS|MAX_CONTEXT_TOKENS|EFFORT_LEVEL|DISABLE_1M|DISABLE_ADAPTIVE|DISABLE_LEGACY_MODEL|ALWAYS_ENABLE_EFFORT|DISABLE_THINKING|DISABLE_FAST_MODE|SKIP_FAST_MODE)|^MAX_THINKING_TOKENS|^CLAUDE_EFFORT|^FALLBACK_FOR_ALL"
        ),
    ),
    (
        "context",
        re.compile(
            r"AUTOCOMPACT|AUTO_COMPACT|DISABLE_COMPACT|PROMPT_CACHING|^SLASH_COMMAND_TOOL_CHAR_BUDGET|MAX_CONTEXT"
        ),
    ),
    (
        "tools",
        re.compile(
            r"^BASH_|^CLAUDE_CODE_(GLOB|SHELL|BASH|POWERSHELL|USE_POWERSHELL|FILE_READ|MAX_TOOL_USE|MAX_WEB_SEARCHES|PERFORCE|SCRIPT_CAPS)|^USE_BUILTIN_RIPGREP|^TASK_MAX_OUTPUT|^CLAUDE_ENV_FILE"
        ),
    ),
    (
        "subagents",
        re.compile(
            r"SUBAGENT|AGENT_SDK|BACKGROUND_TASKS|AGENT_TEAMS|ASYNC_AGENT|MAX_TURNS|AGENT_VIEW|WORKFLOWS|EXPLORE_PLAN|FORK_SUBAGENT|TASK_LIST_ID|ENABLE_TASKS|STOP_HOOK_BLOCK"
        ),
    ),
    (
        "mcp",
        re.compile(
            r"^MCP_|^MAX_MCP_|^CLAUDE_CODE_MCP_|ENABLE_TOOL_SEARCH|CLAUDEAI_MCP"
        ),
    ),
    (
        "telemetry",
        re.compile(
            r"^OTEL_|TELEMETRY|DO_NOT_TRACK|GROWTHBOOK|ERROR_REPORTING|FEEDBACK_SURVEY|PROPAGATE_TRACEPARENT"
        ),
    ),
    (
        "network",
        re.compile(
            r"WATCHDOG|STREAM_IDLE|API_TIMEOUT|API_FORCE_IDLE|MAX_RETRIES|RETRY_WATCHDOG|NONSTREAMING_FALLBACK"
        ),
    ),
    (
        "ui",
        re.compile(
            r"FLICKER|MOUSE|SCROLL|CURSOR|HYPERLINK|STRIKETHROUGH|SYNC_OUTPUT|TMUX|TERMINAL_TITLE|SYNTAX_HIGHLIGHT|ALT_SCREEN|VIRTUAL_SCROLL|ACCESSIBILITY|AX_|HIDE_CWD|IS_DEMO|SPINNER|NATIVE_CURSOR"
        ),
    ),
    (
        "plugins",
        re.compile(r"PLUGIN|MARKETPLACE|SYNC_SKILLS|BUNDLED_SKILLS|POLICY_SKILLS"),
    ),
    (
        "session",
        re.compile(
            r"SESSION|RESUME|PROMPT_HISTORY|EXIT_AFTER_STOP|CONFIG_DIR|TMPDIR|DEBUG|SAFE_MODE|CLAUDE_PID|CHILD_SESSION|CLAUDECODE|MESSAGING|CRON|AFK|USER_DIALOG|CLIENT_PRESENCE|REMOTE"
        ),
    ),
    (
        "updates",
        re.compile(
            r"AUTOUPDATER|DISABLE_UPDATES|INSTALLATION_CHECKS|PACKAGE_MANAGER_AUTO_UPDATE"
        ),
    ),
    (
        "commands",
        re.compile(
            r"^DISABLE_(DOCTOR|FEEDBACK|BUG|LOGIN|LOGOUT|UPGRADE|EXTRA_USAGE|INSTALL_GITHUB|COST_WARNINGS)"
        ),
    ),
]


FEATURE_TOGGLE = re.compile(r"DISABLE|ENABLE|FORCE_|SKIP_")


def categorise(name: str) -> str:
    for label, pattern in CATEGORIES:
        if pattern.search(name):
            return label
    if FEATURE_TOGGLE.search(name):
        return "features"
    return "other"


DEFAULT_PATTERNS = [
    re.compile(r"\(default:\s*`?([^`,;)]+?)`?\s*[,;)]"),
    re.compile(r"\(default:\s*`?([^`)]+?)`?\)"),
    re.compile(r"[Dd]efaults? to `([^`]+)`"),
    re.compile(r"\*\*Default\*\*:\s*`([^`]+)`"),
    re.compile(r"Default(?:s)? `([^`]+)`"),
    re.compile(r"Default:\s*`?([A-Za-z0-9_.\-]+)`?"),
]


def extract_default(text: str) -> str | None:
    for pattern in DEFAULT_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return None


def classify_env(name: str, purpose: str) -> tuple[str, str]:
    """Return (control, evidence)."""
    if name in ENV_ENUMS:
        return "enum", "documented value set"
    if name in SET_OR_UNSET:
        return (
            "set_or_unset",
            "docs: any non-empty value enables; 0/false do NOT disable",
        )
    if name in NUMERIC_BOOLEAN:
        return "numeric_boolean", "docs: parsed as a number, only 0 turns it off"
    if SECRET_LIKE.search(name) and name not in NOT_SECRET:
        return "secret", "credential-shaped name"
    low = purpose.lower()
    if re.search(
        r"\bin milliseconds\b|\bmilliseconds\b|timeout in|\bin seconds\b", low
    ):
        return "number", "purpose states a time unit"
    if re.search(
        r"\(default:\s*\d|default `\d|\bnumber of\b|\bmaximum number\b|\bcap on\b|\bpercentage\b|\bport\b",
        low,
    ):
        return "number", "purpose states a numeric default or count"
    if re.search(r"set to `?(1|0|true|false)`?", low):
        return "boolean", "purpose uses the 1/0/true/false form"
    if re.search(r"\bpath to\b|\bdirectory\b|\boverride the .*path\b", low):
        return "path", "purpose names a path"
    if "json object" in low:
        return "json", "purpose states a JSON object"
    if re.search(r"comma-separated|newline-separated|space-separated|list of", low):
        return "list", "purpose states a delimited list"
    return "string", "no stronger signal"


def classify_setting(key: str, description: str, example: str) -> tuple[str, str]:
    if key in SETTING_ENUMS:
        return "enum", "documented value set"
    ex = example.strip().strip("`")
    if ex in {"true", "false"}:
        return "boolean", f"example is {ex}"
    if re.match(r"^-?\d+(\.\d+)?$", ex):
        return "number", "example is numeric"
    if ex.startswith("["):
        return "array", "example is an array"
    if ex.startswith("{"):
        return "object", "example is an object"
    if re.search(r"\*\*Default\*\*:\s*`(true|false)`", description):
        return "boolean", "documented boolean default"
    if ex.startswith('"') or ex:
        return "string", "example is a scalar string"
    return "string", "no example given"


ROW = re.compile(r"^\|\s*`?([^|`]+?)`?\s*\|(.*)\|\s*$")


def parse_table(lines: list[str], start: int) -> list[list[str]]:
    """Read a markdown table starting at the header line index `start`."""
    rows: list[list[str]] = []
    i = start + 2  # skip header + separator
    while i < len(lines) and lines[i].startswith("|"):
        cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
        rows.append(cells)
        i += 1
    return rows


def find_table_after(lines: list[str], heading: str) -> list[list[str]]:
    for i, line in enumerate(lines):
        if line.strip() == heading:
            for j in range(i, min(i + 40, len(lines))):
                if lines[j].startswith("| ") and lines[j + 1].lstrip(
                    "|"
                ).lstrip().startswith(":-"):
                    return parse_table(lines, j)
    raise SystemExit(f"table not found for heading: {heading}")


def unbacktick(value: str) -> str:
    return value.strip().strip("`").strip()


def build_catalog(docs: Path) -> dict:
    env_lines = (docs / "env-vars.md").read_text(encoding="utf-8").splitlines()
    # The Variables table is the only one in env-vars.md with a `Purpose` column.
    env_rows: list[list[str]] = []
    for i, line in enumerate(env_lines):
        if line.startswith("| Variable") and "Purpose" in line:
            env_rows = parse_table(env_lines, i)
            break
    if not env_rows:
        raise SystemExit("env-vars Variables table not found")

    env: list[dict] = []
    for name_cell, purpose in (r[:2] for r in env_rows):
        name = unbacktick(name_cell)
        control, evidence = classify_env(name, purpose)
        entry = {
            "name": name,
            "kind": "env",
            "category": categorise(name),
            "control": control,
            "classification_evidence": evidence,
            "default": extract_default(purpose),
            "purpose": purpose.strip(),
        }
        if control == "enum":
            entry["values"] = ENV_ENUMS[name]
        if (
            "Removed in v" in purpose
            or "no-op" in purpose
            or "DEPRECATED" in purpose
            or purpose.strip().startswith("Deprecated")
            or "Accepted for compatibility with older releases" in purpose
        ):
            entry["deprecated"] = True
        if (
            "Set by Claude Code, not by you" in purpose
            or "Set automatically" in purpose
        ):
            entry["read_only"] = True
        env.append(entry)

    set_lines = (docs / "settings.md").read_text(encoding="utf-8").splitlines()

    def collect(heading: str, kind: str, prefix: str = "") -> list[dict]:
        out = []
        for row in find_table_after(set_lines, heading):
            if len(row) < 2:
                continue
            key = prefix + unbacktick(row[0])
            description = row[1]
            example = row[2] if len(row) > 2 else ""
            control, evidence = classify_setting(key, description, example)
            entry = {
                "name": key,
                "kind": kind,
                "control": control,
                "classification_evidence": evidence,
                "default": extract_default(description),
                "example": example.strip(),
                "purpose": description.strip(),
                "managed_only": "(Managed settings only)" in description
                or "Managed settings only." in description,
            }
            if control == "enum":
                entry["values"] = SETTING_ENUMS[key]
            out.append(entry)
        return out

    settings = collect("### Available settings", "setting")
    global_config = collect("### Global config settings", "global_config")
    worktree = collect("### Worktree settings", "setting")
    permissions = collect(
        "### Permission settings", "permission_setting", "permissions."
    )
    sandbox = collect("### Sandbox settings", "sandbox_setting", "sandbox.")
    attribution = collect("### Attribution settings", "setting", "attribution.")

    return {
        "source": "https://code.claude.com/docs/en/ (env-vars, settings, permissions)",
        "env": env,
        "settings": settings + worktree,
        "global_config": global_config,
        "permission_settings": permissions,
        "sandbox_settings": sandbox,
        "attribution_settings": attribution,
    }


def fetch_sources(cache: Path) -> None:
    """Download the source documentation pages into the cache directory."""

    cache.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=60.0) as client:
        for page in SOURCE_PAGES:
            response = client.get(f"{DOC_BASE_URL}/{page}.md")
            response.raise_for_status()
            payload = response.text
            (cache / f"{page}.md").write_text(payload, encoding="utf-8")
            print(f"fetched {page}.md ({len(payload):,} bytes)")


CATEGORY_TITLES = {
    "auth": "Authentication and identity",
    "endpoint": "Endpoint, proxy, and TLS",
    "provider": "Third-party providers (Bedrock, Agent Platform, Foundry, Claude on AWS)",
    "model": "Model selection, thinking, and effort",
    "context": "Context window, compaction, and prompt caching",
    "tools": "Built-in tool behaviour",
    "subagents": "Subagents, background work, and workflows",
    "mcp": "MCP servers and tool search",
    "network": "Request timeouts, retries, and stream watchdogs",
    "telemetry": "Telemetry and OpenTelemetry",
    "session": "Session, storage, and process environment",
    "ui": "Terminal rendering and accessibility",
    "plugins": "Plugins, marketplaces, and skills",
    "updates": "Updates",
    "commands": "Hide built-in slash commands",
    "features": "Feature switches",
    "other": "Everything else",
}

CATEGORY_ORDER = [
    "auth",
    "endpoint",
    "provider",
    "model",
    "context",
    "network",
    "tools",
    "subagents",
    "mcp",
    "session",
    "telemetry",
    "ui",
    "plugins",
    "updates",
    "commands",
    "features",
    "other",
]

CONTROL_LABEL = {
    "boolean": "toggle",
    "set_or_unset": "set/unset ⚠",
    "numeric_boolean": "number-as-toggle ⚠",
    "enum": "select",
    "number": "number",
    "string": "text",
    "path": "path",
    "list": "list",
    "json": "JSON",
    "secret": "secret",
    "array": "array",
    "object": "object",
}


def esc(text: str) -> str:
    text = text.replace("](/docs/en/", f"]({DOCS_BASE}/docs/en/")
    text = text.replace("](/en/", f"]({DOCS_BASE}/docs/en/")
    return text.replace("|", "\\|").replace("\n", " ").strip()


def flags(entry: dict) -> str:
    marks = []
    if entry.get("deprecated"):
        marks.append("**deprecated**")
    if entry.get("read_only"):
        marks.append("**read-only**")
    if entry.get("managed_only"):
        marks.append("**managed-only**")
    return " ".join(marks)


def env_table(entries: list[dict]) -> list[str]:
    lines = [
        "| Variable | Control | Values / default | What it does |",
        "| --- | --- | --- | --- |",
    ]
    for entry in sorted(entries, key=lambda e: e["name"]):
        values = ""
        if entry.get("values"):
            values = ", ".join(f"`{v}`" for v in entry["values"])
        if entry.get("default"):
            default = f"default `{entry['default']}`"
            values = f"{values}<br>{default}" if values else default
        note = flags(entry)
        purpose = esc(entry["purpose"])
        if note:
            purpose = f"{note} — {purpose}"
        lines.append(
            f"| `{entry['name']}` | {CONTROL_LABEL[entry['control']]} | {values or '—'} | {purpose} |"
        )
    return lines


def setting_table(entries: list[dict]) -> list[str]:
    lines = [
        "| Key | Control | Values / default | Example | What it does |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in sorted(entries, key=lambda e: e["name"]):
        values = ""
        if entry.get("values"):
            values = ", ".join(f"`{v}`" for v in entry["values"])
        if entry.get("default"):
            default = f"default `{entry['default']}`"
            values = f"{values}<br>{default}" if values else default
        note = flags(entry)
        purpose = esc(entry["purpose"])
        if note:
            purpose = f"{note} — {purpose}"
        lines.append(
            f"| `{entry['name']}` | {CONTROL_LABEL[entry['control']]} "
            f"| {values or '—'} | {esc(entry['example']) or '—'} | {purpose} |"
        )
    return lines


PREAMBLE = """<!--
  Generated from the official Claude Code documentation.
  Regenerate with scripts/gen_claude_config_reference.py after refreshing
  the source pages from https://code.claude.com/docs/en/<page>.md
-->

# Claude Code configuration reference

Complete reference for every knob the Claude Code CLI reads: environment
variables, `settings.json` keys, `~/.claude.json` global config, permission
rules, sandbox policy, and hook events.

This exists so My Claude Code's **Configure Claude Code** page can present each
setting with the right control, the right option list, and an explanation —
rather than a free-text JSON box. Every row carries a **Control** column naming
the widget the value wants.

Source: the official Claude Code docs (`code.claude.com/docs/en/`), pages
`env-vars`, `settings`, `permissions`, `hooks`, `tools-reference`, plus the
Claude Code changelog. Captured at Claude Code **v2.1.228**. Anthropic ships
Claude Code several times a week, so re-run the generator before trusting a
version-gated note.

---

## 1. Where configuration lives

Claude Code merges four scopes. Each has its own file, and the same key can
appear in all of them.

| Scope | Location | Applies to | Shared |
| --- | --- | --- | --- |
| **Managed** | Server-managed (claude.ai admin console or a Claude apps gateway); macOS `com.anthropic.claudecode` managed preferences; Windows `HKLM\\SOFTWARE\\Policies\\ClaudeCode` then `HKCU\\...`; files at `/Library/Application Support/ClaudeCode/`, `/etc/claude-code/`, `C:\\Program Files\\ClaudeCode\\` (plus a `managed-settings.d/` drop-in directory) | Everyone in the org or on the machine | Yes, deployed by IT |
| **User** | `~/.claude/settings.json` | You, in every project | No |
| **Project** | `.claude/settings.json` in the repo | Everyone on the repo | Yes, committed |
| **Local** | `.claude/settings.local.json` at the repository root | You, in this repo only | No, gitignored |

Two more files sit outside that hierarchy:

- `~/.claude.json` — OAuth session, user/local MCP servers, per-project trust
  state, caches, **and the six global-config keys in §7**. Keys from that
  section are silently ignored if you put them in `settings.json`.
- `.mcp.json` — project-scoped MCP servers.

`CLAUDE_CONFIG_DIR` relocates the whole `~/.claude` tree, which is how you run
two accounts side by side.

### Precedence, highest first

1. **Managed settings** — cannot be overridden, not even by CLI arguments.
2. **Command line** — `--settings <file-or-json>` merges like any other layer.
3. **Local project settings** (`.claude/settings.local.json`).
4. **Shared project settings** (`.claude/settings.json`).
5. **User settings** (`~/.claude/settings.json`).

Within the managed tier only one source wins (they are not merged):
`policyHelper` output > remote/server-managed > MDM/OS policy > file-based >
HKCU registry. The exceptions are `env` (merged per key across admin sources
since v2.1.223), the sandbox lock keys, `allowAllClaudeAiMcps`,
`sandbox.bwrapPath`/`socatPath`, and `forceRemoteSettingsRefresh`.

**Arrays merge across scopes** — `permissions.allow`, `sandbox.filesystem.*`
and friends are concatenated and de-duplicated, so a lower scope can add
entries without displacing a higher one. Two arrays do **not** merge:
`fallbackModel` (ordered chain, highest-precedence file supplies all of it) and
a managed `availableModels` (applies as-is; lower scopes cannot extend it).

A few security-sensitive keys accept a *restrictive* value from a scope that
otherwise could not override: a `true` for `disableClaudeAiConnectors` or
`isolatePeerMachines` from any scope, a `false` for `remoteControlAtStartup`
from project/local, and a stricter `crossSessionInbound` on the
`accept < hold < refuse` ladder.

### When an edit takes effect

Claude Code watches the settings files and reloads on change, so `permissions`,
`hooks`, `apiKeyHelper` and most keys apply to the running session. Two are
read once at startup: **`model`** (use `/model` instead) and **`outputStyle`**
(rebuilt on `/clear` or restart). Variables in an `env` block are re-applied to
a running session when the file changes — but **removing** one does not unset
it until the next launch, and features that read their variables once at
startup (OpenTelemetry) keep the startup values.

---

## 2. Environment variable vs settings key

Many behaviours have both. The rules:

- **The environment variable wins** over its settings-key twin. `ANTHROPIC_MODEL`
  beats `model`; `CLAUDE_CODE_AUTO_CONNECT_IDE` beats `autoConnectIde`.
- **A settings `env` block beats the shell.** Claude Code writes each `env`
  entry into the process environment at startup and again on file change,
  replacing whatever the shell exported.
- **You cannot unset a variable from a settings file** — only set it. To
  neutralise a stale shell export, set it to `""`; Claude Code treats the empty
  value as unset for provider selection (subprocesses still inherit the empty
  value).
- **CLI flags and slash commands vary.** `--model` and `/model` beat
  `ANTHROPIC_MODEL`, but `CLAUDE_CODE_EFFORT_LEVEL` beats `/effort`.

---

## 3. Value formats a config editor must get right

These are the traps. An editor that renders every switch as a true/false toggle
will silently do the opposite of what the user asked.

### Normal on/off variables

`1` or `true` turns it on, `0` or `false` turns it off, any casing.

### "Set or unset" variables — `0` does NOT turn them off

These read only *whether the variable exists*. Any non-empty value, including
the string `0`, enables the behaviour. The only way to turn them off is to
remove the variable entirely. **Render these as a present/absent switch that
deletes the key, never as a `false` write.**

- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`
- `DISABLE_TELEMETRY`
- `DISABLE_ERROR_REPORTING`
- `CLAUDE_CODE_TMUX_TRUECOLOR`
- `FALLBACK_FOR_ALL_PRIMARY_MODELS`
- `IS_DEMO`

### One number-as-boolean

`FORCE_HYPERLINK` is parsed as a **number**, so `false`, `no` and `off` all
*enable* hyperlinks. Only `0` disables them.

### Numbers

Since v2.1.211 numeric variables accept scientific notation and digit
separators — `2e3` is 2000, `64_000` is 64000 — except where a row says plain
digits only. Notable plain-digits-only cases:
`CLAUDE_CODE_AUTO_COMPACT_WINDOW` (`500k` reads as `500` and clamps to
100000), `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`,
`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`, and
`CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION`. Out-of-range values are usually
ignored rather than rejected, so an editor should validate against the stated
bounds and say so, not rely on the CLI to complain.

### Read-only variables

Claude Code sets these itself in the session and in the subprocesses it spawns.
An editor should display them, never offer to write them; a settings `env`
block that sets `CLAUDE_CODE_MESSAGING_SOCKET` or `CLAUDE_CODE_MESSAGING_TOKEN`
is ignored outright.

`CLAUDECODE`, `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_SESSION_ID`,
`CLAUDE_CODE_BRIDGE_SESSION_ID`, `CLAUDE_CODE_REMOTE`,
`CLAUDE_CODE_REMOTE_SESSION_ID`, `CLAUDE_CODE_MESSAGING_SOCKET`,
`CLAUDE_CODE_MESSAGING_TOKEN`, `CLAUDE_EFFORT`, `CLAUDE_PID`.

Claude Code also ignores identity variables its hosting environments own, such
as `CLAUDE_CODE_ACCOUNT_UUID`, when they appear in a settings `env` block.

### Variables injected into hooks, skills, and subprocesses

Not settable and not in the reference table — read them from a hook or script:
`CLAUDE_PROJECT_DIR`, `CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA`,
`CLAUDE_SKILL_DIR`, `CLAUDE_JOB_DIR`, `CLAUDE_SESSION_ID`, `TRACEPARENT`, and
`CLAUDE_CODE_MCP_SERVER_NAME` / `CLAUDE_CODE_MCP_SERVER_URL` (MCP
`headersHelper` scripts only).

### Secrets

Ten variables carry credentials. An editor must mask them on display, never
log them, and never echo them back in an API response:
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_AWS_API_KEY`,
`ANTHROPIC_FOUNDRY_API_KEY`, `ANTHROPIC_FOUNDRY_AUTH_TOKEN`,
`AWS_BEARER_TOKEN_BEDROCK`, `CLAUDE_CODE_OAUTH_TOKEN`,
`CLAUDE_CODE_OAUTH_REFRESH_TOKEN`, `CLAUDE_CODE_CLIENT_KEY_PASSPHRASE`,
`MCP_CLIENT_SECRET`, plus `apiKeyHelper` / `awsCredentialExport` output.

### Turning off feature-flag fetching has side effects

`DISABLE_GROWTHBOOK`, `DISABLE_TELEMETRY`, `DO_NOT_TRACK` and
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` all stop Claude Code fetching
feature flags. That makes Remote Control, cross-session messaging,
`claude import` / `/import`, `/schedule`, the advisor tool, `/loop`
self-pacing, and the built-in `/loop` maintenance prompt unavailable. An editor
should warn on these four, not just flip them.

---
"""

PERMISSION_SYNTAX = """
## 9. Permission rule syntax

Rules are `Tool` or `Tool(specifier)`. Evaluation order is **deny → ask →
allow**; the *first* match decides, and specificity never changes the order. A
broad `deny` therefore cannot carry an allowlist exception.

A bare tool name in `deny` (`Bash`) removes the tool from Claude's context
entirely. A scoped rule (`Bash(rm *)`) leaves the tool available and blocks
matching calls. `EndConversation` is the exception: it cannot be removed while
any other tool remains.

### Per-tool specifier syntax

| Tool | Syntax | Notes |
| --- | --- | --- |
| `Bash` / `PowerShell` | `Bash(npm run *)` | `*` matches at any position and spans spaces. A trailing ` *` enforces a word boundary (`ls *` matches `ls -la`, not `lsof`); `ls*` does not. `:*` is an equivalent trailing wildcard. Compound commands are split on `&&`, `\\|\\|`, `;`, `\\|`, `\\|&`, `&` and newlines, and every subcommand must match. PowerShell aliases are canonicalised (`gci` matches `Get-ChildItem`) and matching is case-insensitive. |
| `Read` / `Edit` | `Read(./.env)`, `Edit(/src/**)` | gitignore pattern syntax. `//path` = filesystem root, `~/path` = home, `/path` = relative to the settings source, `path` / `./path` = relative to cwd. A `Read` deny also blocks Edit and Write on that path (not NotebookEdit). Rules written for `Write`, `NotebookEdit`, `Glob` or `MultiEdit` are accepted but never consulted — use `Edit(...)` and `Read(...)`. |
| `WebFetch` | `WebFetch(domain:example.com)` | `*.example.com` matches subdomains at any depth but not the apex. Elsewhere a `*` matches only between two dots. |
| MCP | `mcp__server`, `mcp__server__*`, `mcp__server__tool` | Allow-rule globs are permitted only after a literal `mcp__<server>__`; the server segment must be glob-free. Deny/ask accept `mcp__*` and `*`. |
| `Agent` | `Agent(Explore)` | Names a subagent. |
| `Cd` | `Cd(~/code/**)` | Not model-invocable; governs `/cd`. Any allow rule switches `/cd` to allowlist mode. `*` is one segment, `**` spans segments. |
| any tool | `Tool(param:value)` | **deny and ask only.** Matches a top-level scalar input, e.g. `Agent(model:opus)`, `Bash(run_in_background:true)`. Cannot target a tool's primary content field (`command`, `file_path`, `path`, `url`, `notebook_path`) — such a rule is ignored with a startup warning. |

### Anchoring of `/path` by settings source

| Rule defined in | `/path` resolves to |
| --- | --- |
| `.claude/settings.json` | `<project root>/path` |
| `.claude/settings.local.json` | `<original cwd>/path` |
| `~/.claude/settings.json` | `~/.claude/path` |
| `--settings <file>` | `<directory of file>/path` |
| CLI flags, `/permissions`, session rules | `<original cwd>/path` |

A single-segment relative directory pattern also matches at different depths by
rule type: `Edit(src/**)` as an **allow** rule matches only `<cwd>/src`, but as
a **deny or ask** rule matches a `src` directory at any depth.

### Permission modes

| Mode | Behaviour |
| --- | --- |
| `default` (alias `manual`) | Prompts on first use of each tool. |
| `acceptEdits` | Auto-accepts file edits and common filesystem commands (`mkdir`, `touch`, `mv`, `cp`) inside the working directory and `additionalDirectories`. |
| `plan` | Reads and runs read-only shell commands only; does not edit source. |
| `auto` | Auto-approves with a background classifier. Ignored when set in project or local settings, so a repo cannot grant itself auto mode. |
| `dontAsk` | Auto-denies anything not pre-approved. |
| `bypassPermissions` | Skips prompts. Explicit `ask` rules, org-set connector `ask` tools, `requiresUserInteraction` MCP tools, and root/home `rm -rf` still prompt. |

Block modes with `permissions.disableBypassPermissionsMode: "disable"` and
`permissions.disableAutoMode: "disable"` — most useful in managed settings.

### Built-in read-only Bash commands

`ls`, `cat`, `echo`, `pwd`, `head`, `tail`, `grep`, `find`, `wc`, `which`,
`diff`, `stat`, `du`, `cd`, and read-only `git` forms run without a prompt in
every mode. The set is **not configurable** — add an `ask` or `deny` rule to
force a prompt.

Claude Code strips these wrappers before matching: `timeout`, `time`, `nice`,
`nohup`, `stdbuf`, `command`, `builtin`, zsh `noglob`, and bare `xargs`. It
does **not** strip environment runners (`npx`, `docker exec`, `devbox run`,
`mise exec`, `direnv exec`) — a rule like `Bash(devbox run *)` therefore
approves anything after `run`.

---
"""

HOOKS = """
## 10. Hook events

Configured under the `hooks` key. Each event takes a list of matcher groups,
each with a list of hook handlers (`command`, `http`, or `prompt` types).

`SessionStart`, `Setup`, `InstructionsLoaded`, `UserPromptSubmit`,
`UserPromptExpansion`, `MessageDisplay`, `PreToolUse`, `PermissionRequest`,
`PostToolUse`, `PostToolUseFailure`, `PostToolBatch`, `PermissionDenied`,
`Notification`, `SubagentStart`, `SubagentStop`, `TaskCreated`,
`TaskCompleted`, `Stop`, `StopFailure`, `TeammateIdle`, `ConfigChange`,
`CwdChanged`, `DirectoryAdded`, `FileChanged`, `WorktreeCreate`,
`WorktreeRemove`, `PreCompact`, `PostCompact`, `SessionEnd`.

Related keys: `disableAllHooks`, `allowManagedHooksOnly` (managed only),
`allowedHttpHookUrls`, `httpHookAllowedEnvVars`,
`CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS`, `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`.

## 11. Canonical tool names

Permission rules and hook matchers match the **canonical** name, not the label
shown in the transcript (`Stop Task` in the UI is `TaskStop` in a rule).

`Agent`, `Artifact`, `AskUserQuestion`, `Bash`, `CronCreate`, `CronDelete`,
`CronList`, `Edit`, `EndConversation`, `EnterPlanMode`, `EnterWorktree`,
`ExitPlanMode`, `ExitWorktree`, `Glob`, `Grep`, `ListAgents`,
`ListMcpResourcesTool`, `LSP`, `Monitor`, `NotebookEdit`, `PowerShell`,
`PushNotification`, `Read`, `ReadMcpResourceTool`, `RemoteTrigger`,
`ReportFindings`, `ScheduleWakeup`, `SendMessage`, `SendUserFile`,
`ShareOnboardingGuide`, `Skill`, `TaskCreate`, `TaskGet`, `TaskList`,
`TaskOutput`, `TaskStop`, `TaskUpdate`, `TodoWrite`, `ToolSearch`,
`WaitForMcpServers`, `WebFetch`, `WebSearch`, `Write`.

---

## 12. Notes for the config editor

- **Do not write what you cannot read back.** `apiKeyHelper` and the credential
  helpers run a shell command; render the command, never its output.
- **Warn before writing to a scope that cannot win.** If managed settings set a
  key, editing the user file changes nothing. Surface the effective source.
- **`env` is a flat string map.** Every value is a JSON string, including
  numbers and booleans — `"1"`, not `1`.
- **Deleting a key is a distinct operation from setting it false**, and for the
  six set/unset variables in §3 it is the *only* way to turn the behaviour off.
- **Back up before writing.** Claude Code keeps five timestamped backups of its
  own; a third-party editor should take its own (My Claude Code already writes
  a `.fcc-backup` sibling before its first change).
- **A malformed user/project/local settings file is rejected as a whole** and
  shows a Settings Error dialog at startup. Managed settings parse tolerantly
  and strip only the invalid entry. Validate before writing.
- **`$schema`**: `https://json.schemastore.org/claude-code-settings.json` gives
  editors autocomplete, but lags the newest CLI releases.
"""


def render_reference(CATALOG: dict) -> str:
    parts: list[str] = [PREAMBLE]

    by_category: dict[str, list[dict]] = {}
    for entry in CATALOG["env"]:
        by_category.setdefault(entry["category"], []).append(entry)

    parts.append(
        f"## 4. Environment variables ({len(CATALOG['env'])})\n\n"
        "Grouped the way a settings page would group them. The **Control**\n"
        "column names the widget: `toggle` is a normal 1/0 boolean, `set/unset ⚠`\n"
        "and `number-as-toggle ⚠` are the traps from §3, `select` has a closed\n"
        "value list, and `secret` must be masked.\n"
    )
    for category in CATEGORY_ORDER:
        entries = by_category.get(category)
        if not entries:
            continue
        parts.append(
            f"### 4.{CATEGORY_ORDER.index(category) + 1} {CATEGORY_TITLES[category]}\n"
        )
        parts.append("\n".join(env_table(entries)) + "\n")

    parts.append(
        "\n---\n\n"
        f"## 5. `settings.json` keys ({len(CATALOG['settings'])})\n\n"
        "Top-level keys, including the `worktree.*` group. `permissions.*`,\n"
        "`sandbox.*` and `attribution.*` have their own sections below.\n"
    )
    parts.append("\n".join(setting_table(CATALOG["settings"])) + "\n")

    parts.append(
        "\n---\n\n## 6. `permissions` keys\n\n"
        "See §9 for the rule syntax that goes inside `allow`, `ask` and `deny`.\n"
    )
    parts.append("\n".join(setting_table(CATALOG["permission_settings"])) + "\n")

    parts.append(
        "\n---\n\n## 7. Global config (`~/.claude.json`, not `settings.json`)\n\n"
        "Putting these in `settings.json` does nothing — Claude Code ignores them\n"
        "silently at startup.\n"
    )
    parts.append("\n".join(setting_table(CATALOG["global_config"])) + "\n")

    parts.append(
        "\n---\n\n## 8. `sandbox` keys\n\n"
        "Filesystem and network isolation for Bash. macOS, Linux and WSL2 only.\n"
        "Path entries accept `/` (absolute), `~/` (home) and `./` or bare\n"
        "(project root for project settings, `~/.claude` for user settings).\n"
    )
    parts.append("\n".join(setting_table(CATALOG["sandbox_settings"])) + "\n")

    parts.append("\n### `attribution` keys\n")
    parts.append("\n".join(setting_table(CATALOG["attribution_settings"])) + "\n")

    parts.append("\n---\n" + PERMISSION_SYNTAX)
    parts.append(HOOKS)

    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="download the source documentation pages before generating",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE,
        help="directory holding the cached documentation markdown",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=REPO_ROOT / "docs",
        help="directory to write the reference and catalog into",
    )
    args = parser.parse_args()

    if args.fetch:
        fetch_sources(args.cache)

    missing = [p for p in SOURCE_PAGES if not (args.cache / f"{p}.md").exists()]
    if missing:
        raise SystemExit(
            f"missing cached pages: {', '.join(missing)} -- run with --fetch first"
        )

    catalog = build_catalog(args.cache)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    catalog_path = args.out_dir / "claude-code-config-catalog.json"
    catalog_path.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")

    reference_path = args.out_dir / "CLAUDE-CODE-CONFIG.md"
    reference_path.write_text(render_reference(catalog), encoding="utf-8")

    for section, rows in catalog.items():
        if isinstance(rows, list):
            print(f"{len(rows):4d}  {section}")
    print(f"wrote {catalog_path.relative_to(REPO_ROOT)}")
    print(f"wrote {reference_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
