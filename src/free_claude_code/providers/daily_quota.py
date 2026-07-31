"""Per-credential daily request budgets.

Providers commonly cap requests per *day* as well as per minute, and a daily
cap resets at a fixed wall-clock instant (Groq at UTC midnight, Gemini at
Pacific midnight) rather than N seconds after the last failure. The rotation
engine's cooldown timers cannot express that, so daily budgets are tracked
here and consulted when picking a credential.

Counts are in-memory. A restart forgets the day's usage, which can let a
credential exceed its real budget once; the alternative is a persistent
counter on the request path, which is a much larger cost for a guardrail whose
job is to avoid wasting requests rather than to enforce billing.
"""

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


def _day_key(now: float, reset_offset_hours: float) -> str:
    """Return the provider-local calendar day ``now`` falls in.

    ``reset_offset_hours`` shifts the day boundary away from UTC midnight, so a
    provider that resets at Pacific midnight uses ``-8``.
    """
    shifted = datetime.fromtimestamp(now, tz=UTC) + timedelta(hours=reset_offset_hours)
    return shifted.strftime("%Y-%m-%d")


def _seconds_until_reset(now: float, reset_offset_hours: float) -> float:
    shifted = datetime.fromtimestamp(now, tz=UTC) + timedelta(hours=reset_offset_hours)
    next_midnight = (shifted + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(0.0, (next_midnight - shifted).total_seconds())


@dataclass(slots=True)
class DailyUsage:
    """One credential's usage for one provider-local day."""

    day: str
    requests: int = 0


class DailyQuotaTracker:
    """Track and cap per-credential requests within a provider-local day."""

    def __init__(self, key_count: int, limit: int = 0, reset_offset_hours: float = 0.0):
        if key_count <= 0:
            raise ValueError("key_count must be > 0")
        self._limit = max(0, limit)
        self._reset_offset_hours = reset_offset_hours
        self._usage = [DailyUsage(day="") for _ in range(key_count)]
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """Whether a budget is configured at all."""
        return self._limit > 0

    @property
    def limit(self) -> int:
        return self._limit

    def _entry(self, index: int, now: float) -> DailyUsage:
        entry = self._usage[index]
        today = _day_key(now, self._reset_offset_hours)
        if entry.day != today:
            # Crossing the provider's reset boundary zeroes the count; no timer
            # or sweep is needed because the day is derived from the clock.
            entry.day = today
            entry.requests = 0
        return entry

    def record(self, index: int, *, now: float | None = None) -> None:
        """Count one request against a credential's daily budget."""
        if not (0 <= index < len(self._usage)):
            return
        moment = time.time() if now is None else now
        with self._lock:
            self._entry(index, moment).requests += 1

    def exhausted(self, index: int, *, now: float | None = None) -> bool:
        """Whether this credential has used up today's budget."""
        if not self.enabled or not (0 <= index < len(self._usage)):
            return False
        moment = time.time() if now is None else now
        with self._lock:
            return self._entry(index, moment).requests >= self._limit

    def exhausted_indices(self, *, now: float | None = None) -> frozenset[int]:
        """Every credential that has used up today's budget."""
        if not self.enabled:
            return frozenset()
        moment = time.time() if now is None else now
        with self._lock:
            return frozenset(
                index
                for index in range(len(self._usage))
                if self._entry(index, moment).requests >= self._limit
            )

    def seconds_until_reset(self, *, now: float | None = None) -> float:
        moment = time.time() if now is None else now
        return _seconds_until_reset(moment, self._reset_offset_hours)

    def snapshot(self, *, now: float | None = None) -> list[dict[str, object]]:
        """Per-credential usage for dashboards."""
        moment = time.time() if now is None else now
        with self._lock:
            entries = [self._entry(i, moment) for i in range(len(self._usage))]
            return [
                {
                    "day": entry.day,
                    "requests_today": entry.requests,
                    "daily_limit": self._limit or None,
                    "daily_remaining": (
                        max(0, self._limit - entry.requests) if self._limit else None
                    ),
                    "seconds_until_reset": _seconds_until_reset(
                        moment, self._reset_offset_hours
                    ),
                }
                for entry in entries
            ]
