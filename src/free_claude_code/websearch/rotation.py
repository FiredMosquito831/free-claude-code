"""API key parsing and in-memory rotation health (KeyPool).

KeyPool health semantics (in-memory only):

- ``HEALTHY`` -> ``COOLDOWN`` on consecutive failures with tiered backoff
  (10s / 30s / 60s / 120s).
- ``CIRCUIT_OPEN`` when the 4th consecutive failure lands (60s); further
  failures beyond the threshold fall back to the 120s cooldown tier.
- 401/403 (and quota) failures -> ``LOCKED_OUT`` for 5 minutes, doubling on
  each repeated lockout (capped); a later success resets the escalation.
- 429 -> dedicated rate-limit cooldown (60s), tracked separately from the
  consecutive-failure ladder.

Expired states lazily return to ``HEALTHY`` on the next acquire.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from free_claude_code.config.credentials import mask_key_label
from free_claude_code.core.rate_limit import MAX_RATE_LIMIT_COOLDOWN_SECONDS

__all__ = [
    "KeyHealth",
    "KeyHealthState",
    "KeyPool",
    "default_rotation_policy",
    "mask_key_label",
    "parse_websearch_keys",
]

ROTATION_POLICIES: tuple[str, ...] = ("single", "round_robin", "least_used", "failover")
DEFAULT_SINGLE_KEY_POLICY = "single"
DEFAULT_MULTI_KEY_POLICY = "failover"

COOLDOWN_TIER_SECONDS: tuple[float, ...] = (10.0, 30.0, 60.0, 120.0)
CIRCUIT_OPEN_FAILURES = 4
CIRCUIT_OPEN_SECONDS = 60.0
RATE_LIMIT_COOLDOWN_SECONDS = 60.0
LOCKOUT_BASE_SECONDS = 300.0
LOCKOUT_MAX_SECONDS = 3600.0


def parse_websearch_keys(raw: str | None) -> tuple[str, ...]:
    """Split a comma-separated credential env value into stripped keys."""

    if not raw:
        return ()
    return tuple(part for part in (piece.strip() for piece in raw.split(",")) if part)


def default_rotation_policy(key_count: int) -> str:
    """Default policy: failover across multiple keys, single for one key."""

    return DEFAULT_MULTI_KEY_POLICY if key_count > 1 else DEFAULT_SINGLE_KEY_POLICY


class KeyHealthState(StrEnum):
    HEALTHY = "healthy"
    COOLDOWN = "cooldown"
    CIRCUIT_OPEN = "circuit_open"
    LOCKED_OUT = "locked_out"


@dataclass(slots=True)
class KeyHealth:
    """Mutable per-key runtime health tracked by :class:`KeyPool`."""

    key: str
    requests: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    rate_limits: int = 0
    lockouts: int = 0
    state: KeyHealthState = KeyHealthState.HEALTHY
    state_until: float = 0.0  # monotonic deadline; 0 when healthy
    last_error: str | None = None
    last_used_at: float | None = None


class KeyPool:
    """In-memory rotation pool over one provider's API keys."""

    def __init__(
        self,
        keys: tuple[str, ...],
        *,
        policy: str = DEFAULT_MULTI_KEY_POLICY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if policy not in ROTATION_POLICIES:
            raise ValueError(
                f"credential_rotation must be one of {ROTATION_POLICIES}, got {policy!r}"
            )
        if not keys:
            raise ValueError("KeyPool requires at least one key slot")
        self._policy = policy
        self._clock = clock
        self._health = [KeyHealth(key=key) for key in keys]
        self._round_robin_cursor = 0

    @property
    def policy(self) -> str:
        return self._policy

    @property
    def key_count(self) -> int:
        return len(self._health)

    def key_at(self, index: int) -> str:
        return self._health[index].key

    def health_at(self, index: int) -> KeyHealth:
        return self._health[index]

    def acquire(
        self, *, exclude: frozenset[int] = frozenset()
    ) -> tuple[int, str] | None:
        """Return the next usable ``(index, key)`` per policy, or None when exhausted."""

        now = self._clock()
        candidates = [
            index
            for index in range(len(self._health))
            if index not in exclude and self._is_usable(index, now)
        ]
        if self._policy == "single":
            # The single policy may only ever serve from key slot 0.
            candidates = [index for index in candidates if index == 0]
        if not candidates:
            return None
        index = self._select(candidates)
        health = self._health[index]
        health.requests += 1
        health.last_used_at = now
        return index, health.key

    def report_success(self, index: int) -> None:
        health = self._health[index]
        health.successes += 1
        health.consecutive_failures = 0
        health.lockouts = 0
        health.state = KeyHealthState.HEALTHY
        health.state_until = 0.0
        health.last_error = None

    def report_failure(
        self, index: int, *, kind: str, message: str | None = None
    ) -> None:
        """Record a non-429 failure; auth/quota lock out, others climb the ladder."""

        if kind == "rate_limit":
            self.report_rate_limit(index, message=message)
            return
        health = self._health[index]
        health.failures += 1
        health.last_error = message
        now = self._clock()
        if kind in ("auth", "quota"):
            health.lockouts += 1
            health.consecutive_failures = 0
            health.state = KeyHealthState.LOCKED_OUT
            backoff = LOCKOUT_BASE_SECONDS * (2 ** (health.lockouts - 1))
            health.state_until = now + min(backoff, LOCKOUT_MAX_SECONDS)
            return
        health.consecutive_failures += 1
        if health.consecutive_failures == CIRCUIT_OPEN_FAILURES:
            health.state = KeyHealthState.CIRCUIT_OPEN
            health.state_until = now + CIRCUIT_OPEN_SECONDS
            return
        health.state = KeyHealthState.COOLDOWN
        health.state_until = now + self._cooldown_seconds(health.consecutive_failures)

    def report_rate_limit(
        self,
        index: int,
        *,
        message: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        """Record a 429, honouring the provider's own reset when it sent one.

        A fixed cooldown either benches a key that resets in a second or keeps
        hammering one that needs an hour. ``retry_after_seconds`` is whatever
        the provider published; falling back to the default only when it said
        nothing.
        """

        health = self._health[index]
        health.failures += 1
        health.rate_limits += 1
        health.last_error = message
        health.state = KeyHealthState.COOLDOWN
        cooldown = (
            retry_after_seconds
            if retry_after_seconds is not None and retry_after_seconds >= 0
            else RATE_LIMIT_COOLDOWN_SECONDS
        )
        health.state_until = self._clock() + min(
            cooldown, MAX_RATE_LIMIT_COOLDOWN_SECONDS
        )

    def snapshot(self) -> dict[str, Any]:
        """Health snapshot for admin UI / diagnostics (keys masked)."""

        now = self._clock()
        keys: list[dict[str, Any]] = []
        for index, health in enumerate(self._health):
            self._refresh_state(health, now)
            keys.append(
                {
                    "index": index,
                    "key_label": mask_key_label(health.key),
                    "state": health.state.value,
                    "state_remaining_seconds": (
                        round(max(0.0, health.state_until - now), 3)
                        if health.state is not KeyHealthState.HEALTHY
                        else 0.0
                    ),
                    "requests": health.requests,
                    "successes": health.successes,
                    "failures": health.failures,
                    "consecutive_failures": health.consecutive_failures,
                    "rate_limits": health.rate_limits,
                    "lockouts": health.lockouts,
                    "last_error": health.last_error,
                }
            )
        return {"policy": self._policy, "keys": keys}

    def _select(self, candidates: list[int]) -> int:
        if self._policy == "single":
            return 0
        if self._policy == "failover":
            return min(candidates)
        if self._policy == "round_robin":
            ordered = sorted(candidates)
            index = next(
                (i for i in ordered if i >= self._round_robin_cursor), ordered[0]
            )
            self._round_robin_cursor = index + 1
            return index
        # least_used: fewest served requests, lowest index wins ties.
        return min(candidates, key=lambda i: (self._health[i].requests, i))

    def _is_usable(self, index: int, now: float) -> bool:
        health = self._health[index]
        self._refresh_state(health, now)
        return health.state is KeyHealthState.HEALTHY

    @staticmethod
    def _refresh_state(health: KeyHealth, now: float) -> None:
        if (
            health.state is not KeyHealthState.HEALTHY
            and health.state_until
            and now >= health.state_until
        ):
            health.state = KeyHealthState.HEALTHY
            health.state_until = 0.0

    @staticmethod
    def _cooldown_seconds(consecutive_failures: int) -> float:
        tier = min(consecutive_failures, len(COOLDOWN_TIER_SECONDS)) - 1
        return COOLDOWN_TIER_SECONDS[max(tier, 0)]
