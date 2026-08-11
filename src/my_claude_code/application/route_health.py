"""Passive health tracking that keeps a known-bad model out of a chain.

A fallback chain without this pays the primary's full failure cost on *every*
request while it is down -- the timeout, the retries, then the hop. Ejecting a
model that has just failed repeatedly makes that hop free until it has had time
to recover, which is what turns a chain from "eventually correct" into "fast".

This is passive outlier detection, not a circuit breaker: nothing probes the
model, it is simply skipped while benched and tried again once the bench
expires. Providers already bench individual *credentials*; this benches the
provider/model pair a route points at, which is the thing a chain can route
around.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from loguru import logger

from my_claude_code.config.constants import (
    FALLBACK_EJECT_AFTER_FAILURES_DEFAULT,
    FALLBACK_EJECT_SECONDS_DEFAULT,
)


@dataclass(slots=True)
class _ModelHealth:
    consecutive_failures: int = 0
    ejected_until: float = 0.0


@dataclass(slots=True)
class RouteHealthRegistry:
    """Consecutive-failure ejection for provider/model refs used by routing."""

    eject_after_failures: int = FALLBACK_EJECT_AFTER_FAILURES_DEFAULT
    eject_seconds: float = FALLBACK_EJECT_SECONDS_DEFAULT
    now: Callable[[], float] = time.monotonic
    _models: dict[str, _ModelHealth] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.eject_after_failures > 0 and self.eject_seconds > 0

    def record_success(self, model_ref: str) -> None:
        """Clear the failure streak; one good answer means it is serving again."""
        health = self._models.get(model_ref)
        if health is None:
            return
        health.consecutive_failures = 0
        health.ejected_until = 0.0

    def record_failure(self, model_ref: str) -> None:
        if not self.enabled:
            return
        health = self._models.setdefault(model_ref, _ModelHealth())
        health.consecutive_failures += 1
        if health.consecutive_failures < self.eject_after_failures:
            return
        health.ejected_until = self.now() + self.eject_seconds
        logger.warning(
            "MODEL EJECTED: '{}' failed {} times in a row; skipping it for {:g}s",
            model_ref,
            health.consecutive_failures,
            self.eject_seconds,
        )

    def is_ejected(self, model_ref: str) -> bool:
        health = self._models.get(model_ref)
        if health is None or health.ejected_until <= 0.0:
            return False
        if self.now() >= health.ejected_until:
            # The bench expired. Clear the streak too, so one more failure does
            # not immediately re-eject a model that may well have recovered.
            health.ejected_until = 0.0
            health.consecutive_failures = 0
            return False
        return True

    def usable_indexes(self, model_refs: tuple[str, ...]) -> tuple[int, ...]:
        """Indexes worth attempting, in order, given what is currently benched.

        Ejecting *every* candidate would turn a degraded route into a dead one,
        so when nothing survives the filter the chain is returned untouched and
        the request takes its chances. Skipping a bad model is an optimisation;
        refusing to try anything is an outage.
        """
        if not self.enabled:
            return tuple(range(len(model_refs)))
        usable = tuple(
            index
            for index, model_ref in enumerate(model_refs)
            if not self.is_ejected(model_ref)
        )
        if usable:
            return usable
        logger.warning(
            "MODEL EJECTION BYPASSED: every model on this route is benched;"
            " trying the chain in order anyway"
        )
        return tuple(range(len(model_refs)))
