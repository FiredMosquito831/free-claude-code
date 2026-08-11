"""Passive ejection of models that have just failed repeatedly."""

from my_claude_code.application.route_health import RouteHealthRegistry


def _registry(
    clock: list[float],
    *,
    eject_after_failures: int,
    eject_seconds: float,
) -> RouteHealthRegistry:
    return RouteHealthRegistry(
        eject_after_failures=eject_after_failures,
        eject_seconds=eject_seconds,
        now=lambda: clock[0],
    )


def test_a_model_is_ejected_only_after_the_configured_streak() -> None:
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=3, eject_seconds=30.0)

    registry.record_failure("a/one")
    registry.record_failure("a/one")
    assert not registry.is_ejected("a/one")

    registry.record_failure("a/one")
    assert registry.is_ejected("a/one")


def test_one_success_clears_the_streak() -> None:
    """A model that answered is serving, whatever it did before."""
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=2, eject_seconds=30.0)

    registry.record_failure("a/one")
    registry.record_success("a/one")
    registry.record_failure("a/one")

    assert not registry.is_ejected("a/one")


def test_ejection_expires_and_does_not_immediately_recur() -> None:
    """The streak resets with the bench, so recovery is not one failure from re-ejection."""
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=2, eject_seconds=30.0)
    registry.record_failure("a/one")
    registry.record_failure("a/one")
    assert registry.is_ejected("a/one")

    clock[0] = 31.0
    assert not registry.is_ejected("a/one")

    registry.record_failure("a/one")
    assert not registry.is_ejected("a/one")


def test_usable_indexes_skips_an_ejected_model() -> None:
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=1, eject_seconds=30.0)
    registry.record_failure("a/one")

    assert registry.usable_indexes(("a/one", "b/two", "c/three")) == (1, 2)


def test_a_fully_ejected_chain_is_returned_intact() -> None:
    """Skipping a bad model is an optimisation; refusing to try anything is an outage."""
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=1, eject_seconds=30.0)
    registry.record_failure("a/one")
    registry.record_failure("b/two")

    assert registry.usable_indexes(("a/one", "b/two")) == (0, 1)


def test_ejection_can_be_switched_off() -> None:
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=0, eject_seconds=30.0)
    for _ in range(10):
        registry.record_failure("a/one")

    assert not registry.is_ejected("a/one")
    assert registry.usable_indexes(("a/one", "b/two")) == (0, 1)


def test_failures_are_tracked_per_model_not_per_provider() -> None:
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=2, eject_seconds=30.0)
    registry.record_failure("a/one")
    registry.record_failure("a/two")

    assert registry.usable_indexes(("a/one", "a/two")) == (0, 1)
