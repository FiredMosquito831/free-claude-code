"""Ranges for the numeric limits, and what each end of a range means.

One table, read by three places that must agree: Settings clamps to it so the
server always starts, the admin manifest publishes it so a number input can
refuse before saving, and the validator quotes it back when someone is outside.
Three separate copies of "sensible" would drift, and the disagreement would show
up as a form that accepts a value the server then quietly changes.

Bounds are deliberately wide. They exist to rule out values that cannot work --
a zstd level the compressor rejects, a retry count that never tries -- not to
express a preference about how anyone should run their proxy.
"""

from dataclasses import dataclass

# zstd rejects anything above this outright, so a higher setting would fail on
# every single body write rather than merely compressing badly.
ZSTD_MAX_LEVEL = 22


@dataclass(frozen=True, slots=True)
class LimitRange:
    """Bounds for one numeric setting, with the reason a bound exists."""

    minimum: float
    maximum: float
    # What the low end means when it is a real value rather than a floor,
    # e.g. "0 waits indefinitely". Empty when the minimum is just a floor.
    minimum_note: str = ""

    def clamp(self, value: float) -> float:
        return min(max(value, self.minimum), self.maximum)

    def contains(self, value: float) -> bool:
        return self.minimum <= value <= self.maximum


HOUR = 3600.0
DAY = 86400.0

LIMIT_RANGES: dict[str, LimitRange] = {
    # --- when to stop waiting ---------------------------------------------
    "fallback_first_token_timeout": LimitRange(
        0.0, HOUR, "0 waits indefinitely for the first token"
    ),
    "fallback_total_timeout": LimitRange(0.0, DAY, "0 disables the budget"),
    "fallback_eject_after_failures": LimitRange(0, 1_000, "0 never benches a model"),
    "fallback_eject_seconds": LimitRange(0.0, DAY),
    # A provider has to be allowed to try once, so the floor is 1 attempt.
    "provider_retry_attempts": LimitRange(1, 20),
    "stream_early_retry_attempts": LimitRange(1, 20),
    "stream_midstream_recovery_attempts": LimitRange(0, 20, "0 disables recovery"),
    # Above a few seconds the holdback is no longer a recovery window, it is
    # just latency the client cannot explain.
    "stream_commit_holdback_seconds": LimitRange(
        0.0, 30.0, "0 commits the first chunk immediately"
    ),
    "rate_limit_cooldown_seconds": LimitRange(0.0, DAY, "0 does not pause"),
    "credential_circuit_threshold": LimitRange(1, 1_000),
    # --- what to keep ------------------------------------------------------
    "request_log_max_rows": LimitRange(0, 100_000_000, "0 keeps every request"),
    "request_log_text_max_chars": LimitRange(0, 10_000_000, "0 stores no text"),
    "request_log_compression_level": LimitRange(1, ZSTD_MAX_LEVEL),
    # Below a few hundred a burst drops records; the queue is a buffer, not a
    # throttle.
    "request_log_queue_max_size": LimitRange(100, 10_000_000),
}


def range_for(settings_attr: str | None) -> LimitRange | None:
    """Return the range for a settings attribute, if it has one."""

    if settings_attr is None:
        return None
    return LIMIT_RANGES.get(settings_attr)


def describe_range(limit: LimitRange, *, unit: str = "") -> str:
    """Return a short human range, for a field description or an error."""

    def fmt(value: float) -> str:
        return str(int(value)) if float(value).is_integer() else str(value)

    suffix = f" {unit}" if unit else ""
    text = f"{fmt(limit.minimum)} to {fmt(limit.maximum)}{suffix}"
    if limit.minimum_note:
        text = f"{text} ({limit.minimum_note})"
    return text
