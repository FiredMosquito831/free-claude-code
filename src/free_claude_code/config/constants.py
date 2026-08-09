"""Shared defaults used by config models and provider adapters."""

# HTTP client connect timeout (seconds). Keep aligned with README.md and .env.example.
HTTP_CONNECT_TIMEOUT_DEFAULT = 10.0

# Anthropic Messages API default when the client omits max_tokens.
ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS = 81920

# Non-secret marker stored in Settings when FCC owns renewable ChatGPT credentials.
CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE = "fcc-managed-oauth"

# Fallback timing. These live here rather than beside the executor because
# Settings needs them and the application layer already reads Settings.
#
# Measured against a 51,000-request log: first-token latency is 4.5s at p50 and
# 181.7s at p99.9, so a 120s deadline re-rolls 0.21% of healthy requests onto
# the next model while ending stalls that otherwise ran for 9 minutes.
FALLBACK_FIRST_TOKEN_TIMEOUT_DEFAULT = 120.0
# Whole-request durations on that same log: 7.5s at p50, 255.7s at p99.9. A 600s
# budget cuts 0.03% of healthy requests short and caps the rest.
FALLBACK_TOTAL_TIMEOUT_DEFAULT = 600.0
# Consecutive failures before routing skips a provider/model, and for how long.
FALLBACK_EJECT_AFTER_FAILURES_DEFAULT = 3
FALLBACK_EJECT_SECONDS_DEFAULT = 30.0

# Resilience knobs that used to be module constants. Each one decides how long a
# failing model is allowed to hold a request, which is a deployment question,
# not a protocol fact.
PROVIDER_RETRY_ATTEMPTS_DEFAULT = 5
STREAM_EARLY_RETRY_ATTEMPTS_DEFAULT = 5
STREAM_MIDSTREAM_RECOVERY_ATTEMPTS_DEFAULT = 5
# Output is held this long before it commits. While held, a failure can still
# fall back invisibly, so this is the width of the fallback window itself.
STREAM_COMMIT_HOLDBACK_SECONDS_DEFAULT = 0.75
STREAM_COMMIT_HOLDBACK_MAX_BYTES_DEFAULT = 65_536
# Used only when a rate-limited provider sends no Retry-After to obey.
RATE_LIMIT_COOLDOWN_SECONDS_DEFAULT = 60.0
CREDENTIAL_CIRCUIT_THRESHOLD_DEFAULT = 3

# Request log storage.
REQUEST_LOG_MAX_ROWS_DEFAULT = 50_000
REQUEST_LOG_TEXT_MAX_CHARS_DEFAULT = 50_000
REQUEST_LOG_COMPRESSION_LEVEL_DEFAULT = 9
REQUEST_LOG_QUEUE_MAX_SIZE_DEFAULT = 10_000
