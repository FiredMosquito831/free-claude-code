"""Admin configuration manifest."""

from collections.abc import Iterable
from dataclasses import replace

from my_claude_code.config.limits import describe_range, range_for
from my_claude_code.config.reasoning import (
    ROOT_REASONING_PREFERENCES,
    ROUTE_REASONING_PREFERENCES,
    ReasoningPreference,
)
from my_claude_code.config.settings import Settings

# Spec types live in the neutral .spec module so catalog-derived generators
# (provider_manifest, websearch_manifest) can use them without import cycles;
# they remain importable from here for existing consumers.
from .provider_manifest import provider_field_specs
from .spec import ConfigFieldSpec, ConfigOptionSpec, ConfigSectionSpec, FieldType
from .websearch_manifest import websearch_field_specs

__all__ = [
    "ConfigFieldSpec",
    "ConfigOptionSpec",
    "ConfigSectionSpec",
    "FieldType",
]


def _reasoning_options(
    preferences: tuple[ReasoningPreference, ...],
) -> tuple[ConfigOptionSpec, ...]:
    labels = {
        ReasoningPreference.INHERIT: "Inherit",
        ReasoningPreference.OFF: "Off",
        ReasoningPreference.CLIENT: "From client",
        ReasoningPreference.LOW: "Low",
        ReasoningPreference.MEDIUM: "Medium",
        ReasoningPreference.HIGH: "High",
        ReasoningPreference.XHIGH: "X-High",
        ReasoningPreference.MAX: "Max",
    }
    return tuple(
        ConfigOptionSpec(preference.value, labels[preference])
        for preference in preferences
    )


SECTIONS: tuple[ConfigSectionSpec, ...] = (
    ConfigSectionSpec(
        "providers",
        "Providers",
        "Provider keys, local endpoints, and proxy settings.",
    ),
    ConfigSectionSpec(
        "models",
        "Model Routing",
        "Where each Claude tier sends its requests, and what covers it when "
        "that model cannot.",
    ),
    ConfigSectionSpec(
        "reasoning",
        "Reasoning",
        "Client reasoning policy and route-specific overrides.",
    ),
    ConfigSectionSpec(
        "runtime",
        "Runtime",
        "Server API token, rate limits, timeouts, and process settings.",
    ),
    ConfigSectionSpec(
        "messaging",
        "Messaging",
        "Discord, Telegram, CLI workspace, and session settings.",
    ),
    ConfigSectionSpec(
        "voice",
        "Voice",
        "Voice note transcription settings.",
    ),
    ConfigSectionSpec(
        "web_tools",
        "Web Tools",
        "Local Anthropic web_search and web_fetch behavior.",
    ),
    ConfigSectionSpec(
        "websearch",
        "Web Search",
        "Web search provider selection, API keys, and key rotation.",
    ),
    ConfigSectionSpec(
        "limits",
        "Limits",
        "What MCC waits for, keeps, and records. Every value here is a "
        "trade-off between how long a failing model may hold a request and "
        "how much history survives on disk.",
    ),
    ConfigSectionSpec(
        "diagnostics",
        "Diagnostics",
        "Logging and debugging flags.",
        advanced=True,
    ),
    ConfigSectionSpec(
        "smoke",
        "Smoke Tests",
        "Optional live smoke-test model overrides.",
        advanced=True,
    ),
)


_NON_PROVIDER_FIELDS: tuple[ConfigFieldSpec, ...] = (
    ConfigFieldSpec(
        "MODEL",
        "Default Model",
        "models",
        "model",
        settings_attr="model",
        default="nvidia_nim/nvidia/nemotron-3-super-120b-a12b",
    ),
    ConfigFieldSpec(
        "MODEL_FALLBACKS",
        "Default Fallback Chain",
        "models",
        "model_chain",
        settings_attr="model_fallbacks",
    ),
    ConfigFieldSpec(
        "MODEL_FABLE",
        "Fable Override",
        "models",
        "optional_model",
        settings_attr="model_fable",
    ),
    ConfigFieldSpec(
        "MODEL_FABLE_FALLBACKS",
        "Fable Fallback Chain",
        "models",
        "model_chain",
        settings_attr="model_fable_fallbacks",
    ),
    ConfigFieldSpec(
        "MODEL_OPUS",
        "Opus Override",
        "models",
        "optional_model",
        settings_attr="model_opus",
    ),
    ConfigFieldSpec(
        "MODEL_OPUS_FALLBACKS",
        "Opus Fallback Chain",
        "models",
        "model_chain",
        settings_attr="model_opus_fallbacks",
    ),
    ConfigFieldSpec(
        "MODEL_SONNET",
        "Sonnet Override",
        "models",
        "optional_model",
        settings_attr="model_sonnet",
    ),
    ConfigFieldSpec(
        "MODEL_SONNET_FALLBACKS",
        "Sonnet Fallback Chain",
        "models",
        "model_chain",
        settings_attr="model_sonnet_fallbacks",
    ),
    ConfigFieldSpec(
        "MODEL_HAIKU",
        "Haiku Override",
        "models",
        "optional_model",
        settings_attr="model_haiku",
    ),
    ConfigFieldSpec(
        "MODEL_HAIKU_FALLBACKS",
        "Haiku Fallback Chain",
        "models",
        "model_chain",
        settings_attr="model_haiku_fallbacks",
    ),
    ConfigFieldSpec(
        "MODEL_VISION",
        "Vision Adapter",
        "models",
        "optional_model",
        settings_attr="model_vision",
    ),
    ConfigFieldSpec(
        "MODEL_VISION_FALLBACKS",
        "Vision Fallback Chain",
        "models",
        "model_chain",
        settings_attr="model_vision_fallbacks",
    ),
    ConfigFieldSpec(
        "REASONING_POLICY",
        "Reasoning Policy",
        "reasoning",
        "select",
        settings_attr="reasoning_policy",
        default="client",
        options=_reasoning_options(ROOT_REASONING_PREFERENCES),
        description=(
            "From client preserves CLI effort. Providers translate only the controls "
            "their API supports."
        ),
    ),
    ConfigFieldSpec(
        "REASONING_FABLE",
        "Fable Reasoning",
        "reasoning",
        "select",
        settings_attr="reasoning_fable",
        default="inherit",
        options=_reasoning_options(ROUTE_REASONING_PREFERENCES),
    ),
    ConfigFieldSpec(
        "REASONING_OPUS",
        "Opus Reasoning",
        "reasoning",
        "select",
        settings_attr="reasoning_opus",
        default="inherit",
        options=_reasoning_options(ROUTE_REASONING_PREFERENCES),
    ),
    ConfigFieldSpec(
        "REASONING_SONNET",
        "Sonnet Reasoning",
        "reasoning",
        "select",
        settings_attr="reasoning_sonnet",
        default="inherit",
        options=_reasoning_options(ROUTE_REASONING_PREFERENCES),
    ),
    ConfigFieldSpec(
        "REASONING_HAIKU",
        "Haiku Reasoning",
        "reasoning",
        "select",
        settings_attr="reasoning_haiku",
        default="inherit",
        options=_reasoning_options(ROUTE_REASONING_PREFERENCES),
    ),
    ConfigFieldSpec(
        "ANTHROPIC_AUTH_TOKEN",
        "API/CLI Auth Token",
        "runtime",
        "secret",
        settings_attr="anthropic_auth_token",
        default="freecc",
        secret=True,
        restart_required=True,
        description="Bearer token protecting Claude/API access. It is not admin-page login.",
    ),
    ConfigFieldSpec(
        "PROVIDER_RATE_LIMIT",
        "Provider Rate Limit",
        "runtime",
        "number",
        settings_attr="provider_rate_limit",
        default="1",
    ),
    ConfigFieldSpec(
        "PROVIDER_RATE_WINDOW",
        "Provider Rate Window",
        "runtime",
        "number",
        settings_attr="provider_rate_window",
        default="3",
    ),
    ConfigFieldSpec(
        "PROVIDER_MAX_CONCURRENCY",
        "Provider Max Concurrency",
        "runtime",
        "number",
        settings_attr="provider_max_concurrency",
        default="5",
    ),
    ConfigFieldSpec(
        "HTTP_READ_TIMEOUT",
        "HTTP Read Timeout",
        "runtime",
        "number",
        settings_attr="http_read_timeout",
        default="300",
    ),
    ConfigFieldSpec(
        "HTTP_WRITE_TIMEOUT",
        "HTTP Write Timeout",
        "runtime",
        "number",
        settings_attr="http_write_timeout",
        default="60",
    ),
    ConfigFieldSpec(
        "HTTP_CONNECT_TIMEOUT",
        "HTTP Connect Timeout",
        "runtime",
        "number",
        settings_attr="http_connect_timeout",
        default="60",
    ),
    ConfigFieldSpec(
        "HOST",
        "Server Host",
        "runtime",
        settings_attr="host",
        default="0.0.0.0",
        restart_required=True,
    ),
    ConfigFieldSpec(
        "PORT",
        "Server Port",
        "runtime",
        "number",
        settings_attr="port",
        default="8082",
        restart_required=True,
    ),
    ConfigFieldSpec(
        "FCC_OPEN_BROWSER",
        "Open Admin on Startup",
        "runtime",
        "boolean",
        settings_attr="open_admin_browser",
        default="true",
        description="Open the Admin UI after the next fcc-server launch becomes healthy.",
    ),
    ConfigFieldSpec(
        "MESSAGING_PLATFORM",
        "Messaging Platform",
        "messaging",
        "select",
        settings_attr="messaging_platform",
        default="discord",
        options=("telegram", "discord", "none"),
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "MESSAGING_RATE_LIMIT",
        "Messaging Rate Limit",
        "messaging",
        "number",
        settings_attr="messaging_rate_limit",
        default="1",
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "MESSAGING_RATE_WINDOW",
        "Messaging Rate Window",
        "messaging",
        "number",
        settings_attr="messaging_rate_window",
        default="1",
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "TELEGRAM_BOT_TOKEN",
        "Telegram Bot Token",
        "messaging",
        "secret",
        settings_attr="telegram_bot_token",
        secret=True,
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "ALLOWED_TELEGRAM_USER_ID",
        "Allowed Telegram User ID",
        "messaging",
        settings_attr="allowed_telegram_user_id",
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "TELEGRAM_PROXY_URL",
        "Telegram Proxy URL",
        "messaging",
        "secret",
        settings_attr="telegram_proxy_url",
        secret=True,
        session_sensitive=True,
        description="Optional Telegram-only proxy, e.g. socks5://127.0.0.1:1080.",
    ),
    ConfigFieldSpec(
        "DISCORD_BOT_TOKEN",
        "Discord Bot Token",
        "messaging",
        "secret",
        settings_attr="discord_bot_token",
        secret=True,
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "ALLOWED_DISCORD_CHANNELS",
        "Allowed Discord Channels",
        "messaging",
        settings_attr="allowed_discord_channels",
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "ALLOWED_DIR",
        "Allowed Directory",
        "messaging",
        settings_attr="allowed_dir",
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "MAX_MESSAGE_LOG_ENTRIES_PER_CHAT",
        "Max Tracked Messages Per Chat",
        "messaging",
        "number",
        settings_attr="max_message_log_entries_per_chat",
        advanced=True,
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "VOICE_NOTE_ENABLED",
        "Voice Notes",
        "voice",
        "boolean",
        settings_attr="voice_note_enabled",
        default="false",
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "WHISPER_DEVICE",
        "Whisper Device",
        "voice",
        "select",
        settings_attr="whisper_device",
        default="nvidia_nim",
        options=("cpu", "cuda", "nvidia_nim"),
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "WHISPER_MODEL",
        "Whisper Model",
        "voice",
        settings_attr="whisper_model",
        default="openai/whisper-large-v3",
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "FAST_PREFIX_DETECTION",
        "Fast Prefix Detection",
        "runtime",
        "boolean",
        settings_attr="fast_prefix_detection",
        default="true",
        advanced=True,
    ),
    ConfigFieldSpec(
        "ENABLE_NETWORK_PROBE_MOCK",
        "Network Probe Mock",
        "runtime",
        "boolean",
        settings_attr="enable_network_probe_mock",
        default="true",
        advanced=True,
    ),
    ConfigFieldSpec(
        "ENABLE_TITLE_GENERATION_SKIP",
        "Title Generation Skip",
        "runtime",
        "boolean",
        settings_attr="enable_title_generation_skip",
        default="true",
        advanced=True,
    ),
    ConfigFieldSpec(
        "ENABLE_SUGGESTION_MODE_SKIP",
        "Suggestion Mode Skip",
        "runtime",
        "boolean",
        settings_attr="enable_suggestion_mode_skip",
        default="true",
        advanced=True,
    ),
    ConfigFieldSpec(
        "ENABLE_FILEPATH_EXTRACTION_MOCK",
        "Filepath Extraction Mock",
        "runtime",
        "boolean",
        settings_attr="enable_filepath_extraction_mock",
        default="true",
        advanced=True,
    ),
    ConfigFieldSpec(
        "ENABLE_WEB_SERVER_TOOLS",
        "Web Server Tools",
        "web_tools",
        "boolean",
        settings_attr="enable_web_server_tools",
        default="true",
    ),
    ConfigFieldSpec(
        "WEB_FETCH_ALLOWED_SCHEMES",
        "Allowed Web Fetch Schemes",
        "web_tools",
        settings_attr="web_fetch_allowed_schemes",
        default="http,https",
    ),
    ConfigFieldSpec(
        "WEB_FETCH_ALLOW_PRIVATE_NETWORKS",
        "Allow Private Networks",
        "web_tools",
        "boolean",
        settings_attr="web_fetch_allow_private_networks",
        default="false",
    ),
    ConfigFieldSpec(
        "DEBUG_PLATFORM_EDITS",
        "Debug Platform Edits",
        "diagnostics",
        "boolean",
        settings_attr="debug_platform_edits",
        default="false",
        advanced=True,
        restart_required=True,
    ),
    ConfigFieldSpec(
        "DEBUG_SUBAGENT_STACK",
        "Debug Subagent Stack",
        "diagnostics",
        "boolean",
        settings_attr="debug_subagent_stack",
        default="false",
        advanced=True,
        restart_required=True,
    ),
    ConfigFieldSpec(
        "LOG_RAW_API_PAYLOADS",
        "Log Raw API Payloads",
        "diagnostics",
        "boolean",
        settings_attr="log_raw_api_payloads",
        default="false",
        advanced=True,
        restart_required=True,
    ),
    ConfigFieldSpec(
        "LOG_RAW_SSE_EVENTS",
        "Log Raw SSE Events",
        "diagnostics",
        "boolean",
        settings_attr="log_raw_sse_events",
        default="false",
        advanced=True,
    ),
    ConfigFieldSpec(
        "LOG_API_ERROR_TRACEBACKS",
        "Log API Error Tracebacks",
        "diagnostics",
        "boolean",
        settings_attr="log_api_error_tracebacks",
        default="false",
        advanced=True,
        restart_required=True,
    ),
    ConfigFieldSpec(
        "LOG_RAW_MESSAGING_CONTENT",
        "Log Raw Messaging Content",
        "diagnostics",
        "boolean",
        settings_attr="log_raw_messaging_content",
        default="false",
        advanced=True,
        restart_required=True,
    ),
    ConfigFieldSpec(
        "LOG_RAW_CLI_DIAGNOSTICS",
        "Log Raw CLI Diagnostics",
        "diagnostics",
        "boolean",
        settings_attr="log_raw_cli_diagnostics",
        default="false",
        advanced=True,
        restart_required=True,
    ),
    ConfigFieldSpec(
        "LOG_MESSAGING_ERROR_DETAILS",
        "Log Messaging Error Details",
        "diagnostics",
        "boolean",
        settings_attr="log_messaging_error_details",
        default="false",
        advanced=True,
        restart_required=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_NVIDIA_NIM",
        "Smoke NVIDIA NIM Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_OPEN_ROUTER",
        "Smoke OpenRouter Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_MISTRAL",
        "Smoke Mistral Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_MISTRAL_CODESTRAL",
        "Smoke Mistral Codestral Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_DEEPSEEK",
        "Smoke DeepSeek Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_LMSTUDIO",
        "Smoke LM Studio Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_LLAMACPP",
        "Smoke llama.cpp Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_OLLAMA",
        "Smoke Ollama Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_OLLAMA_CLOUD",
        "Smoke Ollama Cloud Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_KIMI",
        "Smoke Kimi Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_MINIMAX",
        "Smoke MiniMax Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_WAFER",
        "Smoke Wafer Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_OPENCODE",
        "Smoke OpenCode Zen Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_OPENCODE_GO",
        "Smoke OpenCode Go Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_VERCEL",
        "Smoke Vercel AI Gateway Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_HUGGINGFACE",
        "Smoke Hugging Face Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_COHERE",
        "Smoke Cohere Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_GITHUB_MODELS",
        "Smoke GitHub Models Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_ZAI",
        "Smoke Z.ai Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_ALIBABA_CODING",
        "Smoke Alibaba Coding Plan (International) Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_ALIBABA_CODING_CN",
        "Smoke Alibaba Coding Plan (China) Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_ALIBABA",
        "Smoke Alibaba Token Plan (International) Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_ALIBABA_CN",
        "Smoke Alibaba Token Plan (China) Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_FIREWORKS",
        "Smoke Fireworks Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_NOVITA",
        "Smoke Novita Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_NOUS_PORTAL",
        "Smoke Nous Portal Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_KILO",
        "Smoke Kilo Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_CLINE",
        "Smoke Cline Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_CLOUDFLARE",
        "Smoke Cloudflare Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_GEMINI",
        "Smoke Gemini Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_GROQ",
        "Smoke Groq Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_SAMBANOVA",
        "Smoke SambaNova Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_CEREBRAS",
        "Smoke Cerebras Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_QWENCLOUD",
        "Smoke QwenCloud Token Plan Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_QWENCLOUD_CODING",
        "Smoke QwenCloud Coding Plan Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_AGNES",
        "Smoke Agnes AI Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_WANDB",
        "Smoke W&B Inference Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_ZENMUX",
        "Smoke ZenMux Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_BEDROCK",
        "Smoke Amazon Bedrock Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_TOKENROUTER",
        "Smoke TokenRouter Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_NARAROUTE",
        "Smoke NaraRoute Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_XAI",
        "Smoke xAI Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_TOGETHER",
        "Smoke Together AI Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_DEEPINFRA",
        "Smoke DeepInfra Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_SILICONFLOW",
        "Smoke SiliconFlow Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_NEBIUS",
        "Smoke Nebius Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_CHUTES",
        "Smoke Chutes Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_MODEL_FEATHERLESS",
        "Smoke Featherless AI Model",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_NIM_MODELS",
        "Smoke NIM Models",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_NIM_EXTRA_MODELS",
        "Smoke NIM Extra Models",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_OPENROUTER_FREE_MODELS",
        "Smoke OpenRouter Free Models",
        "smoke",
        advanced=True,
    ),
    ConfigFieldSpec(
        "FCC_SMOKE_OPENROUTER_FREE_EXTRA_MODELS",
        "Smoke OpenRouter Free Extra Models",
        "smoke",
        advanced=True,
    ),
    # ---- Limits: when to stop waiting ------------------------------------
    ConfigFieldSpec(
        "FALLBACK_FIRST_TOKEN_TIMEOUT",
        "First-token deadline",
        "limits",
        "number",
        settings_attr="fallback_first_token_timeout",
        default="120",
        description=(
            "Seconds a model may stay silent before the next model on the "
            "chain takes over. Nothing has reached the client yet, so the "
            "handover is invisible. 0 waits indefinitely."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_TOTAL_TIMEOUT",
        "Total request budget",
        "limits",
        "number",
        settings_attr="fallback_total_timeout",
        default="600",
        description=(
            "Seconds one request may run across every attempt, retry and "
            "recovery. Once output has started no fallback can replace it, "
            "but it can still stop. 0 disables the budget."
        ),
    ),
    ConfigFieldSpec(
        "SERVER_GRACEFUL_SHUTDOWN_SECONDS",
        "Graceful shutdown budget",
        "limits",
        "number",
        settings_attr="server_graceful_shutdown_seconds",
        default="300",
        restart_required=True,
        description=(
            "Seconds a closing process gives in-flight requests to finish "
            "before the supervisor force-drops them during a reload or process "
            "replace. Sits just over the measured p99.9 whole-request budget so "
            "a healthy long request usually drains; longer ones (up to the 600s total "
            "budget) may still be cut. 1s is the floor; "
            "0 would be an immediate, no-drain shutdown rather than waiting."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_EJECT_AFTER_FAILURES",
        "Bench a model after",
        "limits",
        "number",
        settings_attr="fallback_eject_after_failures",
        default="3",
        description=(
            "Consecutive failures before routing skips a model, so a request "
            "stops re-paying a dead model's timeout on its way to a healthy "
            "one. A chain is never emptied: if every model is benched they "
            "are tried in order anyway. 0 disables benching."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_EJECT_SECONDS",
        "Keep it benched for",
        "limits",
        "number",
        settings_attr="fallback_eject_seconds",
        default="30",
        description="Seconds a benched model stays out of routing.",
    ),
    ConfigFieldSpec(
        "PROVIDER_RETRY_ATTEMPTS",
        "Retries before the chain",
        "limits",
        "number",
        settings_attr="provider_retry_attempts",
        default="5",
        restart_required=True,
        description=(
            "How many times one model is retried on a 429 or 5xx before the "
            "next model is tried. Each retry waits longer than the last, so "
            "5 attempts spend about 30s before a healthy fallback is used."
        ),
    ),
    ConfigFieldSpec(
        "STREAM_EARLY_RETRY_ATTEMPTS",
        "Retries inside one model",
        "limits",
        "number",
        settings_attr="stream_early_retry_attempts",
        default="5",
        restart_required=True,
        advanced=True,
        description=(
            "Attempts a provider makes on its own, before the failure reaches "
            "routing at all."
        ),
    ),
    ConfigFieldSpec(
        "STREAM_MIDSTREAM_RECOVERY_ATTEMPTS",
        "Mid-stream recovery attempts",
        "limits",
        "number",
        settings_attr="stream_midstream_recovery_attempts",
        default="5",
        restart_required=True,
        description=(
            "After output has started and the connection drops, how many "
            "times the same model is asked to finish. No chain can help here, "
            "so this bounds how long a dying stream may hold a request."
        ),
    ),
    ConfigFieldSpec(
        "STREAM_COMMIT_HOLDBACK_SECONDS",
        "Commit holdback",
        "limits",
        "number",
        settings_attr="stream_commit_holdback_seconds",
        default="0.75",
        restart_required=True,
        description=(
            "Seconds the first output is held before it goes to the client. "
            "While it is held a failure can still fall back silently, so this "
            "is the width of the invisible-recovery window. 0 commits at once "
            "and disables invisible recovery."
        ),
    ),
    ConfigFieldSpec(
        "RATE_LIMIT_COOLDOWN_SECONDS",
        "Rate-limit cooldown",
        "limits",
        "number",
        settings_attr="rate_limit_cooldown_seconds",
        default="60",
        restart_required=True,
        advanced=True,
        description=(
            "How long a rate-limited provider is paused when it sends no "
            "Retry-After header of its own to obey."
        ),
    ),
    ConfigFieldSpec(
        "CREDENTIAL_CIRCUIT_THRESHOLD",
        "Bench a key after",
        "limits",
        "number",
        settings_attr="credential_circuit_threshold",
        default="3",
        restart_required=True,
        advanced=True,
        description="Consecutive failures before one API key is benched by rotation.",
    ),
    # ---- Limits: what to keep --------------------------------------------
    ConfigFieldSpec(
        "REQUEST_LOG_ENABLED",
        "Record requests",
        "limits",
        "boolean",
        settings_attr="request_log_enabled",
        default="true",
        restart_required=True,
        description="Turn the request log and the Analytics tab on or off.",
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_MAX_ROWS",
        "Requests to keep",
        "limits",
        "number",
        settings_attr="request_log_max_rows",
        default="50000",
        restart_required=True,
        description=(
            "The newest N requests are kept and older ones are deleted as new "
            "ones arrive. All-time counters keep counting either way; only "
            "the rows themselves are pruned."
        ),
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_CAPTURE_BODIES",
        "Store prompts and replies",
        "limits",
        "boolean",
        settings_attr="request_log_capture_bodies",
        default="true",
        restart_required=True,
        description=(
            "Keeps the full text of each request so content search can find "
            "it. Bodies are about 99% of the stored bytes."
        ),
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_COMPRESS_BODIES",
        "Compress stored text",
        "limits",
        "boolean",
        settings_attr="request_log_compress_bodies",
        default="true",
        restart_required=True,
        description=(
            "Compresses bodies against a dictionary trained on your own "
            "traffic and stores a repeated prompt once. Applies to new rows; "
            "run fcc-compact-log to convert existing history."
        ),
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_CAPTURE_IMAGES",
        "Store image thumbnails",
        "limits",
        "boolean",
        settings_attr="request_log_capture_images",
        default="true",
        restart_required=True,
        description=(
            "Keeps a downscaled copy of every image or document a request "
            "carried, so the request detail can show what the model was "
            "looking at. The count is recorded either way."
        ),
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_IMAGE_MAX_PIXELS",
        "Thumbnail size",
        "limits",
        "number",
        settings_attr="request_log_image_max_pixels",
        default="512",
        restart_required=True,
        description=(
            "Longest edge of a stored thumbnail. The same image re-sent on "
            "later turns of a conversation is stored once."
        ),
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_TEXT_MAX_CHARS",
        "Longest text stored",
        "limits",
        "number",
        settings_attr="request_log_text_max_chars",
        default="50000",
        restart_required=True,
        description=(
            "Text longer than this is truncated before it is stored, which "
            "also bounds what content search can ever find."
        ),
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_COMPRESSION_LEVEL",
        "Compression level",
        "limits",
        "number",
        settings_attr="request_log_compression_level",
        default="9",
        restart_required=True,
        advanced=True,
        description=(
            "zstd level for stored bodies. Measured on a real log, level 19 "
            "was 4.9% smaller than 9 at a ninth of the speed."
        ),
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_QUEUE_MAX_SIZE",
        "Pending writes held",
        "limits",
        "number",
        settings_attr="request_log_queue_max_size",
        default="10000",
        restart_required=True,
        advanced=True,
        description=(
            "Records waiting to be written. When this fills under a burst, "
            "further records are dropped rather than slowing the request."
        ),
    ),
    # ---- Limits: what to record ------------------------------------------
    ConfigFieldSpec(
        "LOG_LEVEL",
        "Log level",
        "limits",
        "select",
        settings_attr="log_level",
        default="INFO",
        options=("DEBUG", "INFO", "WARNING", "ERROR"),
        restart_required=True,
        description=(
            "How much the server writes to its log file. DEBUG includes every "
            "routing decision, which is what to use when a fallback behaves "
            "unexpectedly."
        ),
    ),
)


def _with_range(field: ConfigFieldSpec) -> ConfigFieldSpec:
    """Attach the usable range to a numeric field, and say so in its help.

    Written here rather than in each spec so the bounds the form enforces are
    the same object the server clamps to; two hand-maintained copies would
    eventually disagree, and the form would accept a value the server changes.
    """
    limit = range_for(field.settings_attr)
    if limit is None:
        return field
    text = f"Accepts {describe_range(limit)}."
    description = f"{field.description} {text}".strip()
    return replace(
        field,
        minimum=limit.minimum,
        maximum=limit.maximum,
        description=description,
    )


FIELDS: tuple[ConfigFieldSpec, ...] = tuple(
    _with_range(field)
    for field in (
        *(ConfigFieldSpec(**spec) for spec in provider_field_specs()),
        *_NON_PROVIDER_FIELDS,
        *(ConfigFieldSpec(**spec) for spec in websearch_field_specs()),
    )
)
FIELD_BY_KEY = {field.key: field for field in FIELDS}


def field_input_key(field: ConfigFieldSpec) -> str | None:
    """Return the Settings input key used for a manifest field."""

    if field.settings_attr is None:
        return None
    model_field = Settings.model_fields[field.settings_attr]
    alias = model_field.validation_alias
    if alias is None:
        return field.settings_attr
    return str(alias)


def env_keys() -> frozenset[str]:
    """Return env keys owned by the admin manifest."""

    return frozenset(field.key for field in FIELDS)


def fields_with_attrs() -> Iterable[ConfigFieldSpec]:
    """Yield fields that validate through Settings."""

    return (field for field in FIELDS if field.settings_attr is not None)
